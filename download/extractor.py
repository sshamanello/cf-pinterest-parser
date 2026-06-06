import argparse
import csv
import io
import json
import logging
import os
import subprocess
import shutil
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# Avoid numba cache locator failures in some system Python installs.
_NUMBA_CACHE_DIR = Path(__file__).resolve().parent / ".numba_cache"
os.environ.setdefault("NUMBA_CACHE_DIR", str(_NUMBA_CACHE_DIR))
_NUMBA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

from rembg import remove, new_session

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
STANDARD_OVERLAY_SIZE = 1500
STANDARD_CONTENT_MAX_PX = 1200
REMBG_MODEL_CANDIDATES = [
    m.strip()
    for m in os.environ.get(
        "EXTRACTOR_REMBG_MODELS",
        "",
    ).split(",")
    if m.strip()
]
REMBG_MODEL_TIMEOUT_SEC = int(os.environ.get("EXTRACTOR_REMBG_MODEL_TIMEOUT_SEC", "20"))
REMBG_ALPHA_MATTING = os.environ.get("EXTRACTOR_REMBG_ALPHA_MATTING", "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ExtractionResult:
    font_id: str
    source_path: str
    overlay_path: str
    mask_path: str
    report_path: str
    quality_score: float
    foreground_ratio: float
    transparency_ratio: float
    needs_manual_check: bool
    manual_reason: str
    simple_background: bool
    overlay_size: tuple[int, int]
    bbox_px: dict
    bbox_norm: dict
    font_colors: dict
    extraction_mode: str
    qc_metrics: dict
    qc_decision: str
    retry_count: int


def _estimate_simple_background(img_rgb: np.ndarray) -> bool:
    """Heuristic: if border pixels are mostly one color, background is likely simple."""
    h, w = img_rgb.shape[:2]
    border = max(4, min(h, w) // 40)

    top = img_rgb[:border, :, :]
    bottom = img_rgb[h - border :, :, :]
    left = img_rgb[:, :border, :]
    right = img_rgb[:, w - border :, :]

    border_pixels = np.concatenate(
        [top.reshape(-1, 3), bottom.reshape(-1, 3), left.reshape(-1, 3), right.reshape(-1, 3)],
        axis=0,
    )

    quantized = (border_pixels // 16).astype(np.uint8)
    _, counts = np.unique(quantized, axis=0, return_counts=True)
    dominant_ratio = float(counts.max() / counts.sum()) if counts.size else 0.0
    return dominant_ratio >= 0.80


def _analyze_source_profile(img_rgb: np.ndarray) -> dict:
    """
    Quick source diagnostics for mode selection.
    """
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    contrast = float(np.std(gray) / 255.0)
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    texture = float(min(1.0, np.var(lap) / 1800.0))

    h, w = gray.shape
    border = max(4, min(h, w) // 40)
    border_pixels = np.concatenate(
        [
            img_rgb[:border, :, :].reshape(-1, 3),
            img_rgb[h - border :, :, :].reshape(-1, 3),
            img_rgb[:, :border, :].reshape(-1, 3),
            img_rgb[:, w - border :, :].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float32)
    border_std = float(np.mean(np.std(border_pixels, axis=0)) / 255.0)
    simple_bg = _estimate_simple_background(img_rgb)
    return {
        "simple_bg": simple_bg,
        "contrast": round(contrast, 4),
        "texture": round(texture, 4),
        "border_texture": round(border_std, 4),
        "high_contrast": contrast >= 0.20,
    }


def _crop_working_zone_rgba(src_rgba: Image.Image) -> tuple[Image.Image, dict]:
    """
    Trim outer black borders and keep the inner working rectangle.
    Returns cropped image and crop metadata.
    """
    arr = np.array(src_rgba.convert("RGBA"))
    rgb = arr[:, :, :3]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    non_black = gray > 16

    h, w = gray.shape
    row_fill = np.mean(non_black, axis=1)
    col_fill = np.mean(non_black, axis=0)
    y_idx = np.where(row_fill > 0.06)[0]
    x_idx = np.where(col_fill > 0.06)[0]

    if y_idx.size == 0 or x_idx.size == 0:
        return src_rgba, {"applied": False, "x": 0, "y": 0, "w": w, "h": h}

    y0, y1 = int(y_idx.min()), int(y_idx.max()) + 1
    x0, x1 = int(x_idx.min()), int(x_idx.max()) + 1
    pad_y = max(2, int(0.01 * h))
    pad_x = max(2, int(0.01 * w))
    y0 = max(0, y0 - pad_y)
    y1 = min(h, y1 + pad_y)
    x0 = max(0, x0 - pad_x)
    x1 = min(w, x1 + pad_x)

    if (x1 - x0) < int(0.45 * w) or (y1 - y0) < int(0.45 * h):
        return src_rgba, {"applied": False, "x": 0, "y": 0, "w": w, "h": h}

    cropped = src_rgba.crop((x0, y0, x1, y1))
    return cropped, {"applied": True, "x": x0, "y": y0, "w": (x1 - x0), "h": (y1 - y0)}


def _enhance_gray(gray: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _remove_background_with_rembg(image: Image.Image, model_name: str = "") -> Image.Image:
    in_buf = io.BytesIO()
    image.save(in_buf, format="PNG")
    payload = in_buf.getvalue()
    try:
        session = new_session(model_name) if model_name else None
    except Exception:
        session = None
    try:
        if REMBG_ALPHA_MATTING:
            output_bytes = remove(
                payload,
                session=session,
                alpha_matting=True,
                alpha_matting_foreground_threshold=240,
                alpha_matting_background_threshold=10,
                alpha_matting_erode_size=8,
            )
        else:
            output_bytes = remove(payload, session=session)
    except Exception:
        output_bytes = remove(payload, session=session)
    return Image.open(io.BytesIO(output_bytes)).convert("RGBA")


def _remove_background_with_timeout(image: Image.Image, model_name: str, timeout_sec: int) -> Image.Image | None:
    # Isolate rembg call in subprocess to prevent hard hangs in main process.
    with tempfile.TemporaryDirectory(prefix="rembg_tmp_") as td:
        in_path = Path(td) / "in.png"
        out_path = Path(td) / "out.png"
        image.save(in_path, format="PNG")
        py = (
            "import io\n"
            "from pathlib import Path\n"
            "from PIL import Image\n"
            "from rembg import remove,new_session\n"
            f"inp=Path(r'''{str(in_path)}''')\n"
            f"out=Path(r'''{str(out_path)}''')\n"
            f"model=r'''{model_name}'''\n"
            "session=new_session(model) if model else None\n"
            "payload=inp.read_bytes()\n"
            f"alpha_matting={str(REMBG_ALPHA_MATTING)}\n"
            "try:\n"
            "  if alpha_matting:\n"
            "    res=remove(payload,session=session,alpha_matting=True,alpha_matting_foreground_threshold=240,alpha_matting_background_threshold=10,alpha_matting_erode_size=8)\n"
            "  else:\n"
            "    res=remove(payload,session=session)\n"
            "except Exception:\n"
            "  res=remove(payload,session=session)\n"
            "out.write_bytes(res)\n"
        )
        try:
            proc = subprocess.run(
                ["python3", "-c", py],
                check=True,
                timeout=max(1, int(timeout_sec)),
                capture_output=True,
                text=True,
            )
            if not out_path.exists():
                if proc.stderr:
                    logger.warning("rembg subprocess no output (%s): %s", model_name or "default", proc.stderr[-300:])
                return None
            return Image.open(out_path).convert("RGBA")
        except Exception as exc:
            logger.warning("rembg subprocess failed (%s): %s", model_name or "default", exc)
            return None


def _collect_rembg_masks(source_img: Image.Image, simple_bg: bool) -> list[tuple[str, np.ndarray]]:
    """
    Try multiple rembg models and return processed masks.
    This gives a free local quality boost similar to commercial ensembles.
    """
    out: list[tuple[str, np.ndarray]] = []
    tried = set()
    if not REMBG_MODEL_CANDIDATES:
        return out

    models = list(REMBG_MODEL_CANDIDATES)
    for model in models:
        model_key = model or "default"
        if model_key in tried:
            continue
        tried.add(model_key)
        try:
            rgba = _remove_background_with_timeout(
                source_img,
                model_name=model,
                timeout_sec=REMBG_MODEL_TIMEOUT_SEC,
            )
            if rgba is None:
                logger.debug("rembg model timeout/empty: %s", model_key)
                continue
            alpha = np.array(rgba)[:, :, 3]
            mask = _postprocess_alpha(alpha, simple_bg=simple_bg)
            out.append((f"rembg_{model_key}", mask))
        except Exception as exc:
            logger.debug("rembg model failed: %s (%s)", model_key, exc)
    return out


def _postprocess_alpha(alpha: np.ndarray, simple_bg: bool) -> np.ndarray:
    """
    Clean alpha channel while preserving glyph shape.
    We avoid aggressive operations to keep the original font contour.
    """
    if simple_bg:
        _, binary = cv2.threshold(alpha, 12, 255, cv2.THRESH_BINARY)
        kernel = np.ones((2, 2), np.uint8)
        cleaned = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel, iterations=1)
    else:
        # For complex backgrounds we do softer cleanup to avoid damaging thin glyph details.
        _, binary = cv2.threshold(alpha, 8, 255, cv2.THRESH_BINARY)
        kernel = np.ones((2, 2), np.uint8)
        cleaned = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)

    # Slight blur then threshold to reduce staircase artifacts on edges.
    blurred = cv2.GaussianBlur(cleaned, (3, 3), 0)
    _, final_mask = cv2.threshold(blurred, 16, 255, cv2.THRESH_BINARY)
    return final_mask


def _foreground_ratio(mask: np.ndarray) -> float:
    return float(np.count_nonzero(mask > 0) / float(mask.size))


def _looks_like_full_rectangle(mask: np.ndarray) -> bool:
    """
    Detect failure mode when mask is basically a filled rectangular card.
    Global foreground ratio can be moderate (center card only), so we also
    check dense fill inside the largest component bounding box.
    """
    fg = _foreground_ratio(mask)
    if fg >= 0.82:
        return True

    bbox, fill, area = _largest_component_bbox_and_fill(mask)
    if bbox is None:
        return False

    area_ratio = float(area / mask.size)
    return area_ratio >= 0.04 and fill >= 0.86


def _largest_component_bbox_and_fill(mask: np.ndarray) -> tuple[tuple[int, int, int, int] | None, float, int]:
    """
    Return bbox (x0,y0,x1,y1), fill ratio inside bbox, and component area
    for the largest connected component.
    """
    binary = (mask > 0).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if n <= 1:
        return None, 0.0, 0

    areas = stats[1:, cv2.CC_STAT_AREA]
    idx = int(np.argmax(areas)) + 1
    x = int(stats[idx, cv2.CC_STAT_LEFT])
    y = int(stats[idx, cv2.CC_STAT_TOP])
    w = int(stats[idx, cv2.CC_STAT_WIDTH])
    h = int(stats[idx, cv2.CC_STAT_HEIGHT])
    area = int(stats[idx, cv2.CC_STAT_AREA])
    if w <= 0 or h <= 0:
        return None, 0.0, area
    fill = float(area / float(w * h))
    return (x, y, x + w, y + h), fill, area


def _extract_text_mask_from_plate_roi(roi_rgb: np.ndarray) -> np.ndarray:
    """
    Extract text-like foreground from a plate/card ROI.
    Keeps colorful letters and dark contours, rejects pale background.
    """
    # Upscale before segmentation to reduce staircase artifacts on final edges.
    roi_up = cv2.resize(roi_rgb, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    hsv = cv2.cvtColor(roi_up, cv2.COLOR_RGB2HSV)
    h, s, v = cv2.split(hsv)

    # Colorful glyphs (pink/yellow/green/blue etc.)
    colorful = ((s > 45) & (v > 35)).astype(np.uint8) * 255
    # Dark glyphs/contours (black script/outline)
    dark = (v < 95).astype(np.uint8) * 255

    # Combine and suppress obvious background-like pixels.
    mask = cv2.bitwise_or(colorful, dark)
    pale_bg = ((s < 25) & (v > 170)).astype(np.uint8) * 255
    mask = cv2.bitwise_and(mask, cv2.bitwise_not(pale_bg))

    kernel = np.ones((2, 2), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

    # Remove tiny noise components.
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    clean = np.zeros_like(mask)
    if n > 1:
        areas = stats[1:, cv2.CC_STAT_AREA].astype(np.int64)
        largest = int(areas.max())
        min_area = max(40, int(0.00012 * roi_up.shape[0] * roi_up.shape[1]), int(largest * 0.004))
        main_area = max(min_area, int(largest * 0.06))
        attach_area = max(min_area, int(largest * 0.008))

        # 1) Keep only major glyph components.
        main = np.zeros_like(mask)
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area >= main_area:
                main[labels == i] = 255

        # 2) Keep nearby small accents if they are close to major glyphs.
        if np.count_nonzero(main) > 0:
            near = cv2.dilate(main, np.ones((7, 7), np.uint8), iterations=1)
            for i in range(1, n):
                area = int(stats[i, cv2.CC_STAT_AREA])
                if area < attach_area or area >= main_area:
                    continue
                comp = (labels == i)
                if np.any(near[comp] > 0):
                    main[comp] = 255
            clean = main

    # Slightly thicken thin glyph strokes.
    clean = cv2.dilate(clean, np.ones((2, 2), np.uint8), iterations=1)
    # Downscale back to original ROI size.
    clean = cv2.resize(clean, (roi_rgb.shape[1], roi_rgb.shape[0]), interpolation=cv2.INTER_AREA)
    _, clean = cv2.threshold(clean, 64, 255, cv2.THRESH_BINARY)
    return clean


def _build_core_soft_masks(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Build two-layer masks:
    - core: solid inner glyph body
    - soft: outer ring for smooth edges / outline retention
    """
    binary = (mask > 0).astype(np.uint8)
    core = cv2.erode(binary, np.ones((3, 3), np.uint8), iterations=1)
    if np.count_nonzero(core) == 0:
        core = binary.copy()
    soft = cv2.dilate(binary, np.ones((3, 3), np.uint8), iterations=1)
    return core, soft


def _soft_alpha_from_binary_mask(mask: np.ndarray) -> np.ndarray:
    """
    Convert a binary mask to alpha using dual-layer (core+soft) strategy.
    """
    core, soft = _build_core_soft_masks(mask)
    soft_blur = cv2.GaussianBlur((soft * 255).astype(np.float32), (0, 0), sigmaX=1.15, sigmaY=1.15)
    soft_blur = np.clip(soft_blur, 0, 255).astype(np.uint8)

    alpha = np.zeros_like(mask, dtype=np.uint8)
    soft_band = (soft > 0) & (core == 0)
    alpha[soft_band] = np.maximum(alpha[soft_band], soft_blur[soft_band])
    alpha[core > 0] = 255
    alpha[alpha < 10] = 0
    return alpha


def _decontaminate_edge_rgb(rgba: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """
    Anti-halo: reduce old background color contamination on text edges.
    """
    out = rgba.copy()
    binary = (mask > 0).astype(np.uint8)
    if np.count_nonzero(binary) == 0:
        return out

    core = cv2.erode(binary, np.ones((3, 3), np.uint8), iterations=1)
    if np.count_nonzero(core) == 0:
        core = binary
    edge_in = (binary > 0) & (core == 0)

    interior_pixels = out[:, :, :3][core > 0]
    if interior_pixels.size == 0:
        return out
    interior_mean = np.mean(interior_pixels.astype(np.float32), axis=0)

    rgb = out[:, :, :3].astype(np.float32)
    rgb[edge_in] = rgb[edge_in] * 0.72 + interior_mean * 0.28
    out[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    return out


def _finalize_text_mask(mask: np.ndarray, img_rgb: np.ndarray) -> np.ndarray:
    """
    Final cleanup pass:
    - remove border noise,
    - recover thin strokes near current mask edges,
    - remove tiny artifacts.
    """
    h, w = mask.shape
    _, mask = cv2.threshold(mask, 64, 255, cv2.THRESH_BINARY)
    mask = _remove_border_touching_components(mask)
    mask = _suppress_dense_background_blobs(mask, img_rgb)

    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 55, 160)
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]
    text_like = ((v < 165) | (s > 34)).astype(np.uint8) * 255
    near = cv2.dilate((mask > 0).astype(np.uint8) * 255, np.ones((3, 3), np.uint8), iterations=1)
    recovered = cv2.bitwise_and(edges, near)
    recovered = cv2.bitwise_and(recovered, text_like)
    mask = cv2.bitwise_or(mask, recovered)

    kernel = np.ones((2, 2), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    min_area = max(12, int(0.00001 * h * w))
    mask = _remove_small_components(mask, min_area=min_area)
    mask = _remove_frame_like_components(mask)
    mask = _keep_central_large_components(mask)
    mask = _drop_lower_decorative_components(mask)
    return mask


def _suppress_dense_background_blobs(mask: np.ndarray, img_rgb: np.ndarray) -> np.ndarray:
    """
    Remove large smooth background blobs that get merged with text while
    keeping thick glyph interiors close to high-gradient edges.
    """
    if np.count_nonzero(mask > 0) == 0:
        return mask

    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.magnitude(grad_x, grad_y)

    fg = mask > 0
    grad_fg = grad[fg]
    if grad_fg.size == 0:
        return mask

    thr = float(max(14.0, np.percentile(grad_fg, 56)))
    seed = ((grad >= thr) & fg).astype(np.uint8)
    if np.count_nonzero(seed > 0) < 10:
        return mask

    inv_seed = (seed == 0).astype(np.uint8)
    dist = cv2.distanceTransform(inv_seed, cv2.DIST_L2, 3)
    max_dist = max(4.0, min(mask.shape) * 0.012)
    keep = fg & ((seed > 0) | (dist <= max_dist))
    keep_u8 = keep.astype(np.uint8) * 255
    keep_u8 = cv2.morphologyEx(keep_u8, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8), iterations=1)
    return keep_u8


def _remove_frame_like_components(mask: np.ndarray) -> np.ndarray:
    """
    Drop thin frame/bracket artifacts near canvas edges.
    """
    h, w = mask.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask

    out = np.zeros_like(mask, dtype=np.uint8)
    for i in range(1, n):
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area <= 0 or bw <= 0 or bh <= 0:
            continue

        touch_side = (x <= int(0.10 * w)) or (x + bw >= int(0.90 * w))
        touch_top_bottom = (y <= int(0.10 * h)) or (y + bh >= int(0.90 * h))
        is_thin_vertical = bw <= int(0.12 * w) and bh >= int(0.24 * h)
        is_thin_horizontal = bh <= int(0.12 * h) and bw >= int(0.24 * w)
        if ((touch_side and is_thin_vertical) or (touch_top_bottom and is_thin_horizontal)) and area < int(0.28 * h * w):
            continue
        out[labels == i] = 255
    return out


def _drop_lower_decorative_components(mask: np.ndarray) -> np.ndarray:
    """
    Remove detached lower decorative blocks (flowers/subtitles) while keeping
    the main wordmark and nearby accents.
    """
    h, w = mask.shape
    n, labels, stats, centroids = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask

    recs = []
    for i in range(1, n):
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area <= 0 or bw <= 0 or bh <= 0:
            continue
        cx, cy = centroids[i]
        recs.append((i, x, y, bw, bh, area, float(cx), float(cy)))

    if not recs:
        return mask

    upper = [r for r in recs if (r[7] / max(1.0, h)) <= 0.58]
    anchor = max(upper, key=lambda r: r[5]) if upper else max(recs, key=lambda r: r[5])
    _, ax, ay, aw, ah, a_area, a_cx, a_cy = anchor
    y_limit = min(float(h), float(a_cy + 0.13 * h), float(ay + ah + 0.09 * h))
    y_floor = max(0.0, float(ay - 0.12 * h))
    min_area = max(16, int(a_area * 0.004))
    large_lower_area = 0
    large_lower_count = 0
    lower_text_like_count = 0
    lower_x0 = w
    lower_x1 = 0
    for _, x, y, bw, bh, area, _, cy in recs:
        if cy <= y_limit:
            continue
        if area >= int(a_area * 0.01) and area <= int(a_area * 0.22) and bw <= int(0.45 * w) and bh >= int(0.04 * h):
            lower_text_like_count += 1
            lower_x0 = min(lower_x0, x)
            lower_x1 = max(lower_x1, x + bw)
        if area >= int(a_area * 0.12) and y >= int(ay + 0.45 * ah):
            large_lower_area += area
            large_lower_count += 1

    # If lower region looks like a true second text line, do not trim it.
    if lower_text_like_count >= 3:
        return mask
    if lower_text_like_count >= 2 and (lower_x1 - lower_x0) >= int(0.42 * w):
        return mask

    # Activate removal only when we clearly see detached lower decorative mass.
    if large_lower_count == 0 and large_lower_area < int(a_area * 0.22):
        # Still allow cleanup of tiny decorative elements above anchor.
        keep_upper_only = np.zeros_like(mask, dtype=np.uint8)
        # Build coarse main x-range from largest components.
        main_records = sorted(recs, key=lambda r: r[5], reverse=True)[:3]
        main_x0 = min(r[1] for r in main_records)
        main_x1 = max(r[1] + r[3] for r in main_records)
        for i, x, y, bw, bh, area, _, cy in recs:
            if area < min_area:
                continue
            # Remove isolated components too far from main text x-range.
            cx = x + bw / 2.0
            if cx < (main_x0 - 0.08 * w) or cx > (main_x1 + 0.08 * w):
                if area < int(a_area * 0.2):
                    continue
            # Remove very high tiny icons and thin top strips.
            if cy < y_floor or cy < (a_cy - 0.06 * h):
                aspect = bw / max(1.0, bh)
                if area < int(a_area * 0.08) or (aspect > 3.2 and bh < int(0.14 * h)):
                    continue
            keep_upper_only[labels == i] = 255
        if np.count_nonzero(keep_upper_only > 0) >= int(0.7 * np.count_nonzero(mask > 0)):
            return keep_upper_only
        return mask

    keep = np.zeros_like(mask, dtype=np.uint8)
    for i, x, y, bw, bh, area, _, cy in recs:
        if area < min_area:
            continue
        if cy > y_limit:
            continue
        if cy < y_floor or cy < (a_cy - 0.06 * h):
            aspect = bw / max(1.0, bh)
            if area < int(a_area * 0.08) or (aspect > 3.2 and bh < int(0.14 * h)):
                continue
        keep[labels == i] = 255

    if np.count_nonzero(keep > 0) < int(0.12 * a_area):
        return mask
    return keep


def _small_image_scale(h: int, w: int) -> float:
    max_dim = max(h, w)
    if max_dim < 700:
        return 3.0
    if max_dim < 1000:
        return 2.0
    return 1.0


def _remove_small_components(mask: np.ndarray, min_area: int) -> np.ndarray:
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    clean = np.zeros_like(mask, dtype=np.uint8)
    for i in range(1, n):
        if int(stats[i, cv2.CC_STAT_AREA]) >= min_area:
            clean[labels == i] = 255
    return clean


def _keep_central_large_components(mask: np.ndarray) -> np.ndarray:
    """
    Keep only large central connected components (main object cluster).
    Removes corner icons/logos and distant islands.
    """
    h, w = mask.shape
    n, labels, stats, centroids = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask

    center = np.array([w / 2.0, h / 2.0], dtype=np.float32)
    records = []
    for i in range(1, n):
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area <= 0:
            continue
        cx, cy = centroids[i]
        dist = float(np.linalg.norm(np.array([cx, cy], dtype=np.float32) - center) / max(w, h))
        score = area * (1.0 - min(0.9, dist))
        records.append((i, x, y, bw, bh, area, cx, cy, dist, score))

    if not records:
        return mask

    anchor = max(records, key=lambda r: r[9])
    _, ax, ay, aw, ah, a_area, _, _, _, _ = anchor
    min_area = max(14, int(a_area * 0.01), int(0.000015 * h * w))
    x0 = max(0, int(ax - 0.22 * w))
    x1 = min(w, int(ax + aw + 0.22 * w))
    y0 = max(0, int(ay - 0.20 * h))
    y1 = min(h, int(ay + ah + 0.28 * h))

    keep = np.zeros_like(mask, dtype=np.uint8)
    for i, x, y, bw, bh, area, cx, cy, dist, _ in records:
        if area < min_area:
            continue
        in_window = (cx >= x0 and cx <= x1 and cy >= y0 and cy <= y1)
        corner = (x < 0.12 * w and y < 0.12 * h) or (x + bw > 0.88 * w and y < 0.12 * h) or (x < 0.12 * w and y + bh > 0.88 * h) or (x + bw > 0.88 * w and y + bh > 0.88 * h)
        if corner and area < int(a_area * 0.25):
            continue
        if in_window or area >= int(a_area * 0.22):
            keep[labels == i] = 255

    if np.count_nonzero(keep > 0) < int(0.20 * a_area):
        return mask
    return keep


def _keep_main_text_components(mask: np.ndarray) -> np.ndarray:
    """
    Keep components likely belonging to the main title:
    larger, closer to image center, and composing most foreground area.
    """
    h, w = mask.shape
    n, labels, stats, centroids = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask

    center = np.array([w / 2.0, h / 2.0], dtype=np.float32)
    records: list[tuple[int, float, int]] = []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        cx, cy = centroids[i]
        dist = float(np.linalg.norm(np.array([cx, cy], dtype=np.float32) - center) / max(w, h))
        score = area * (1.0 - min(0.85, dist))
        records.append((i, score, area))

    records.sort(key=lambda x: x[1], reverse=True)
    if not records:
        return mask

    max_area = max(r[2] for r in records)
    min_keep = max(18, int(max_area * 0.01), int(0.00002 * h * w))
    total_area = float(sum(r[2] for r in records if r[2] >= min_keep))
    if total_area <= 0:
        return mask

    keep = np.zeros_like(mask, dtype=np.uint8)
    acc = 0.0
    for i, _, area in records:
        if area < min_keep:
            continue
        keep[labels == i] = 255
        acc += area
        if acc / total_area >= 0.985:
            break
    return keep


def _keep_primary_wordmark_components(mask: np.ndarray) -> np.ndarray:
    """
    Keep the primary wordmark line and drop lower decorative blocks/subtitles.
    This targets common preview layouts where the main font name is the largest
    text group in the upper-middle area.
    """
    h, w = mask.shape
    n, labels, stats, centroids = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask

    records = []
    for i in range(1, n):
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area <= 0 or bw <= 0 or bh <= 0:
            continue
        cx, cy = centroids[i]
        center_score = max(0.0, 1.0 - abs((cx - (w / 2.0)) / max(1.0, w / 2.0)))
        upper_penalty = max(0.0, (cy / max(1.0, h)) - 0.52)
        upper_score = max(0.0, 1.0 - upper_penalty * 2.4)
        span_score = max(0.2, min(1.0, bw / max(1.0, w * 0.45)))
        aspect = bw / max(1.0, bh)
        aspect_score = max(0.3, min(1.35, aspect / 1.25))
        score = float(area) * (0.45 + 0.55 * center_score) * (0.55 + 0.45 * upper_score) * span_score * aspect_score
        records.append((i, x, y, bw, bh, area, score, cy))

    if not records:
        return mask

    upper_records = [r for r in records if (r[7] / max(1.0, h)) <= 0.58]
    anchor = max(upper_records, key=lambda r: r[6]) if upper_records else max(records, key=lambda r: r[6])
    _, ax, ay, aw, ah, a_area, _, a_cy = anchor
    band_y0 = max(0, int(ay - 0.10 * h))
    band_y1 = min(h, int(ay + ah + 0.08 * h))
    cy_limit = min(float(band_y1), float(a_cy + 0.12 * h))
    band_x0 = max(0, int(ax - 0.18 * w))
    band_x1 = min(w, int(ax + aw + 0.18 * w))
    min_area = max(18, int(a_area * 0.01), int(0.000015 * h * w))

    keep = np.zeros_like(mask, dtype=np.uint8)
    for i, x, y, bw, bh, area, _, cy in records:
        if area < min_area:
            continue
        y0 = y
        y1 = y + bh
        cx = x + bw / 2.0
        overlaps_band = not (y1 < band_y0 or y0 > band_y1)
        if not overlaps_band or cy > cy_limit:
            continue
        if cx < band_x0 or cx > band_x1:
            if area < int(a_area * 0.24):
                continue
        if bh >= int(0.42 * h) and bw <= int(0.08 * w) and area < int(a_area * 0.40):
            continue
        keep[labels == i] = 255

    # Safety fallback: if filtering was too aggressive, return original main-components logic.
    if np.count_nonzero(keep > 0) < max(50, int(0.18 * a_area)):
        return _keep_main_text_components(mask)
    return keep


def _extract_text_mask_general(img_rgb: np.ndarray, profile: str = "default") -> np.ndarray:
    """General text mask for non-card images (and fallback)."""
    h, w = img_rgb.shape[:2]
    scale = _small_image_scale(h, w)
    if scale > 1.0:
        up = cv2.resize(img_rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    else:
        up = img_rgb

    hsv = cv2.cvtColor(up, cv2.COLOR_RGB2HSV)
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]
    gray = cv2.cvtColor(up, cv2.COLOR_RGB2GRAY)

    if profile == "aggressive":
        dark_thr = min(165, int(np.percentile(v, 44) + 25))
        s_thr = 28
        pale_s = 30
        pale_v = 188
        canny_a, canny_b = 55, 145
    elif profile == "conservative":
        dark_thr = min(135, int(np.percentile(v, 34) + 18))
        s_thr = 44
        pale_s = 20
        pale_v = 176
        canny_a, canny_b = 85, 190
    else:
        dark_thr = min(145, int(np.percentile(v, 38) + 22))
        s_thr = 36
        pale_s = 24
        pale_v = 180
        canny_a, canny_b = 70, 170

    dark = (v < dark_thr).astype(np.uint8) * 255
    colorful = ((s > s_thr) & (v > 28)).astype(np.uint8) * 255
    _, inv_otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    edges = cv2.Canny(gray, canny_a, canny_b)

    mask = cv2.bitwise_or(dark, colorful)
    mask = cv2.bitwise_or(mask, inv_otsu)
    mask = cv2.bitwise_or(mask, edges)

    # Suppress pale background pixels.
    pale_bg = ((s < pale_s) & (v > pale_v)).astype(np.uint8) * 255
    mask = cv2.bitwise_and(mask, cv2.bitwise_not(pale_bg))

    k = 2 if scale >= 2 else 1
    kernel = np.ones((k + 1, k + 1), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

    mask = _remove_border_touching_components(mask)
    min_area = max(14, int(0.000015 * up.shape[0] * up.shape[1]))
    if profile == "aggressive":
        min_area = max(10, int(min_area * 0.6))
    elif profile == "conservative":
        min_area = int(min_area * 1.4)
    mask = _remove_small_components(mask, min_area=min_area)
    mask = _keep_main_text_components(mask)

    if scale > 1.0:
        mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_AREA)
        _, mask = cv2.threshold(mask, 72, 255, cv2.THRESH_BINARY)
    return mask


def _detect_card_roi(img_rgb: np.ndarray) -> tuple[int, int, int, int] | None:
    """
    Detect central card/plate region (common for CF preview thumbnails inside pins).
    """
    h, w = img_rgb.shape[:2]
    border = max(4, min(h, w) // 45)
    border_pixels = np.concatenate(
        [
            img_rgb[:border, :, :].reshape(-1, 3),
            img_rgb[h - border :, :, :].reshape(-1, 3),
            img_rgb[:, :border, :].reshape(-1, 3),
            img_rgb[:, w - border :, :].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float32)
    bg = np.median(border_pixels, axis=0)
    dist = np.linalg.norm(img_rgb.astype(np.float32) - bg[None, None, :], axis=2)
    raw = np.where(dist > 16.0, 255, 0).astype(np.uint8)
    raw = cv2.morphologyEx(raw, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)
    raw = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)

    n, _, stats, centroids = cv2.connectedComponentsWithStats((raw > 0).astype(np.uint8), connectivity=8)
    best = None
    best_score = -1.0
    for i in range(1, n):
        x, y, bw, bh, area = [int(v) for v in stats[i]]
        if bw <= 0 or bh <= 0:
            continue
        area_ratio = area / float(h * w)
        fill = area / float(bw * bh)
        aspect = bw / float(bh)
        if not (0.03 <= area_ratio <= 0.55):
            continue
        if not (0.85 <= aspect <= 3.0):
            continue
        if fill < 0.62:
            continue
        cx, cy = centroids[i]
        dist_center = float(np.linalg.norm(np.array([cx - w / 2.0, cy - h / 2.0])) / max(w, h))
        score = area * fill * (1.0 - min(0.9, dist_center))
        if score > best_score:
            best_score = score
            best = (x, y, x + bw, y + bh)
    return best


def _build_card_mode_mask(img_rgb: np.ndarray) -> np.ndarray | None:
    bbox = _detect_card_roi(img_rgb)
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    roi = img_rgb[y0:y1, x0:x1, :]
    roi_mask = _extract_text_mask_from_plate_roi(roi)
    roi_mask = _keep_main_text_components(roi_mask)
    if _foreground_ratio(roi_mask) < 0.003:
        return None
    full = np.zeros(img_rgb.shape[:2], dtype=np.uint8)
    full[y0:y1, x0:x1] = roi_mask
    return full


def _score_mask(mask: np.ndarray, img_rgb: np.ndarray | None = None, source_profile: dict | None = None) -> float:
    fg = _foreground_ratio(mask)
    if fg <= 0.0:
        return 0.0
    # Prefer plausible text coverage.
    if 0.02 <= fg <= 0.22:
        fg_score = 1.0
    elif fg < 0.02:
        fg_score = fg / 0.02
    else:
        fg_score = max(0.0, 1.0 - (fg - 0.22) / 0.55)

    bbox, fill, area = _largest_component_bbox_and_fill(mask)
    rect_penalty = 0.0
    if bbox is not None:
        area_ratio = area / float(mask.size)
        if area_ratio > 0.04 and fill > 0.82:
            rect_penalty = 0.35
        elif area_ratio > 0.09 and fill > 0.68:
            rect_penalty = 0.24
        elif area_ratio > 0.16 and fill > 0.52:
            rect_penalty = 0.20

    structure_bonus = 0.0
    if img_rgb is not None:
        n, _, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
        comp_count = max(0, n - 1)
        if comp_count > 0:
            # Penalize both too fragmented and too monolithic masks.
            if comp_count <= 2:
                structure_bonus -= 0.08
            elif comp_count > 120:
                structure_bonus -= min(0.22, (comp_count - 120) / 700.0)
            else:
                structure_bonus += 0.04

        edges = cv2.Canny(mask, 40, 120)
        edge_density = float(np.count_nonzero(edges > 0) / max(1, np.count_nonzero(mask > 0)))
        if 0.12 <= edge_density <= 0.42:
            structure_bonus += 0.06
        else:
            structure_bonus -= 0.08

    source_bias = 0.0
    if source_profile:
        if source_profile.get("high_contrast", False):
            source_bias += 0.04
        if float(source_profile.get("texture", 0.0)) > 0.55:
            source_bias -= 0.03

    return max(0.0, min(1.0, fg_score - rect_penalty + structure_bonus + source_bias))


def _fit_rgba_to_standard_canvas(rgba: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict, dict]:
    """
    Place extracted glyphs on a transparent 1500x1500 canvas.
    Returns (canvas_rgba, canvas_mask, bbox_px, bbox_norm).
    """
    alpha = rgba[:, :, 3]
    ys, xs = np.where(alpha > 0)
    size = STANDARD_OVERLAY_SIZE

    canvas_rgba = np.zeros((size, size, 4), dtype=np.uint8)
    canvas_mask = np.zeros((size, size), dtype=np.uint8)
    empty_bbox = {"x": 0, "y": 0, "w": 0, "h": 0}
    empty_bbox_norm = {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}

    if ys.size == 0:
        return canvas_rgba, canvas_mask, empty_bbox, empty_bbox_norm

    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    crop = rgba[y0:y1, x0:x1, :]

    ch, cw = crop.shape[:2]
    if ch <= 0 or cw <= 0:
        return canvas_rgba, canvas_mask, empty_bbox, empty_bbox_norm

    scale = min(STANDARD_CONTENT_MAX_PX / cw, STANDARD_CONTENT_MAX_PX / ch)
    new_w = max(1, int(round(cw * scale)))
    new_h = max(1, int(round(ch * scale)))
    resized = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

    px = (size - new_w) // 2
    py = (size - new_h) // 2
    canvas_rgba[py : py + new_h, px : px + new_w, :] = resized

    alpha_canvas = canvas_rgba[:, :, 3]
    canvas_mask[alpha_canvas > 0] = 255

    bbox_px = {"x": int(px), "y": int(py), "w": int(new_w), "h": int(new_h)}
    bbox_norm = {
        "x": round(px / size, 6),
        "y": round(py / size, 6),
        "w": round(new_w / size, 6),
        "h": round(new_h / size, 6),
    }
    return canvas_rgba, canvas_mask, bbox_px, bbox_norm


def _rgb_to_hex(rgb: np.ndarray) -> str:
    return "#{:02x}{:02x}{:02x}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _extract_font_color_info(canvas_rgba: np.ndarray) -> dict:
    alpha = canvas_rgba[:, :, 3]
    rgb = canvas_rgba[:, :, :3]
    pixels = rgb[alpha > 30]
    if pixels.size == 0:
        return {
            "dominant_colors": [],
            "is_multicolor": False,
            "lightness_score": 0.0,
            "mean_color": "#000000",
            "median_color": "#000000",
            "contrast_hint": "use_mid_bg",
        }

    # Quantize and pick top palette buckets.
    q = ((pixels // 16) * 16).astype(np.uint8)
    uniq, counts = np.unique(q, axis=0, return_counts=True)
    order = np.argsort(counts)[::-1]

    dominant = []
    for idx in order[:5]:
        dominant.append(_rgb_to_hex(uniq[idx]))

    mean_rgb = pixels.mean(axis=0)
    median_rgb = np.median(pixels, axis=0)
    lightness = float((0.2126 * mean_rgb[0] + 0.7152 * mean_rgb[1] + 0.0722 * mean_rgb[2]) / 255.0)

    rgb_std = float(np.mean(np.std(pixels.astype(np.float32), axis=0)))
    is_multicolor = len(dominant) >= 2 and rgb_std > 18.0

    if lightness >= 0.62:
        contrast_hint = "use_dark_bg"
    elif lightness <= 0.38:
        contrast_hint = "use_light_bg"
    else:
        contrast_hint = "use_mid_bg"

    return {
        "dominant_colors": dominant,
        "is_multicolor": is_multicolor,
        "lightness_score": round(lightness, 4),
        "mean_color": _rgb_to_hex(mean_rgb),
        "median_color": _rgb_to_hex(median_rgb),
        "contrast_hint": contrast_hint,
    }


def _letter_mask_from_rect_component(img_rgb: np.ndarray, rough_mask: np.ndarray) -> np.ndarray | None:
    """
    If rough mask looks like a rectangular card, extract text-only mask from inside it.
    """
    bbox, fill, area = _largest_component_bbox_and_fill(rough_mask)
    if bbox is None:
        return None

    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    aspect = float(bw / bh) if bh else 0.0
    area_ratio = float(area / rough_mask.size)

    # Typical "card kept as foreground" pattern.
    if fill < 0.68:
        return None
    if not (1.0 <= aspect <= 2.8):
        return None
    if area_ratio < 0.04:
        return None

    roi = img_rgb[y0:y1, x0:x1, :]
    roi_mask = _extract_text_mask_from_plate_roi(roi)
    if _foreground_ratio(roi_mask) < 0.004:
        return None

    full = np.zeros(rough_mask.shape, dtype=np.uint8)
    full[y0:y1, x0:x1] = roi_mask
    return full


def _remove_border_touching_components(mask: np.ndarray) -> np.ndarray:
    """Keep only connected components that do not touch image borders."""
    h, w = mask.shape
    num_labels, labels = cv2.connectedComponents((mask > 0).astype(np.uint8), connectivity=8)
    keep = np.zeros_like(mask, dtype=np.uint8)
    min_area = max(30, int(0.00005 * h * w))

    for label in range(1, num_labels):
        ys, xs = np.where(labels == label)
        if ys.size == 0:
            continue
        area = ys.size
        if area < min_area:
            continue
        touches_border = np.any(ys == 0) or np.any(ys == h - 1) or np.any(xs == 0) or np.any(xs == w - 1)
        if not touches_border:
            keep[ys, xs] = 255
    return keep


def _border_distance_mask(img_rgb: np.ndarray, simple_bg: bool) -> np.ndarray:
    """Build foreground mask as pixels that differ from dominant border color."""
    h, w = img_rgb.shape[:2]
    border = max(4, min(h, w) // 40)
    border_pixels = np.concatenate(
        [
            img_rgb[:border, :, :].reshape(-1, 3),
            img_rgb[h - border :, :, :].reshape(-1, 3),
            img_rgb[:, :border, :].reshape(-1, 3),
            img_rgb[:, w - border :, :].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float32)

    bg_color = np.median(border_pixels, axis=0).astype(np.float32)
    distances = np.linalg.norm(img_rgb.astype(np.float32) - bg_color[None, None, :], axis=2)
    # Tighter threshold for simpler backgrounds, looser for textured cards.
    threshold = 16.0 if simple_bg else 24.0
    raw = np.where(distances > threshold, 255, 0).astype(np.uint8)

    kernel = np.ones((2, 2), np.uint8)
    raw = cv2.morphologyEx(raw, cv2.MORPH_OPEN, kernel, iterations=1)
    raw = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, kernel, iterations=1)
    return _remove_border_touching_components(raw)


def _otsu_text_mask(img_rgb: np.ndarray) -> np.ndarray:
    """Try binary masks for dark/light text and keep a plausible one."""
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, inv = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    _, direct = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    inv = _remove_border_touching_components(inv)
    direct = _remove_border_touching_components(direct)

    inv_ratio = _foreground_ratio(inv)
    direct_ratio = _foreground_ratio(direct)

    def score(r: float) -> float:
        # Prefer masks roughly in text-coverage range
        if 0.01 <= r <= 0.40:
            return 1.0
        if r < 0.01:
            return r / 0.01
        return max(0.0, 1.0 - (r - 0.40) / 0.60)

    return inv if score(inv_ratio) >= score(direct_ratio) else direct


def _threshold_text_mask(img_rgb: np.ndarray) -> np.ndarray:
    """
    Grayscale -> contrast enhancement -> Otsu threshold (dark text focus).
    """
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    enhanced = _enhance_gray(gray)
    blur = cv2.GaussianBlur(enhanced, (3, 3), 0)
    _, inv = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    inv = _remove_border_touching_components(inv)
    inv = _remove_small_components(inv, min_area=max(14, int(0.000012 * img_rgb.shape[0] * img_rgb.shape[1])))
    return inv


def _adaptive_threshold_mask(img_rgb: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    enhanced = _enhance_gray(gray)
    local = cv2.adaptiveThreshold(
        enhanced,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        6,
    )
    k = np.ones((2, 2), np.uint8)
    local = cv2.morphologyEx(local, cv2.MORPH_OPEN, k, iterations=1)
    local = cv2.morphologyEx(local, cv2.MORPH_CLOSE, k, iterations=1)
    local = _remove_border_touching_components(local)
    local = _remove_small_components(local, min_area=max(12, int(0.00001 * img_rgb.shape[0] * img_rgb.shape[1])))
    return local


def _lab_hsv_separation_mask(img_rgb: np.ndarray) -> np.ndarray:
    """
    Separate text by color/lightness in LAB + HSV spaces.
    """
    lab = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2LAB)
    l = lab[:, :, 0]
    a = lab[:, :, 1]
    b = lab[:, :, 2]
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]

    dark = (l < np.percentile(l, 46)).astype(np.uint8) * 255
    colorful = ((s > 30) & (v > 28)).astype(np.uint8) * 255
    chroma = (np.abs(a.astype(np.int16) - 128) + np.abs(b.astype(np.int16) - 128) > 20).astype(np.uint8) * 255
    white_outline = ((s < 36) & (v > 172)).astype(np.uint8) * 255

    mask = cv2.bitwise_or(dark, colorful)
    mask = cv2.bitwise_or(mask, chroma)
    near_colored = cv2.dilate(colorful, np.ones((5, 5), np.uint8), iterations=1)
    white_outline = cv2.bitwise_and(white_outline, near_colored)
    mask = cv2.bitwise_or(mask, white_outline)

    pale = ((s < 20) & (v > 186)).astype(np.uint8) * 255
    mask = cv2.bitwise_and(mask, cv2.bitwise_not(pale))

    k = np.ones((2, 2), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=1)
    mask = _remove_border_touching_components(mask)
    mask = _remove_small_components(mask, min_area=max(12, int(0.00001 * img_rgb.shape[0] * img_rgb.shape[1])))
    return mask


def _strict_letter_mask(img_rgb: np.ndarray, simple_bg: bool) -> np.ndarray:
    """
    Fallback for 'rectangle kept as foreground':
    combine border-color segmentation and Otsu text extraction, keep stricter candidate.
    """
    by_color = _border_distance_mask(img_rgb, simple_bg=simple_bg)
    by_otsu = _otsu_text_mask(img_rgb)

    color_ratio = _foreground_ratio(by_color)
    otsu_ratio = _foreground_ratio(by_otsu)

    # Choose mask that is non-empty and less likely to keep background.
    candidates = []
    if color_ratio > 0:
        candidates.append((by_color, color_ratio))
    if otsu_ratio > 0:
        candidates.append((by_otsu, otsu_ratio))

    if not candidates:
        return np.zeros(img_rgb.shape[:2], dtype=np.uint8)

    # Prefer explicit card ROI text extraction if a rectangular component is detected.
    card_text = _letter_mask_from_rect_component(img_rgb, by_otsu)
    if card_text is not None and _foreground_ratio(card_text) > 0.003:
        chosen = card_text
    else:
        # Keep the leaner mask if it remains plausible for text.
        plausible = [c for c in candidates if 0.005 <= c[1] <= 0.45]
        if plausible:
            chosen = min(plausible, key=lambda x: x[1])[0]
        else:
            chosen = min(candidates, key=lambda x: x[1])[0]

    # Edge smoothing without expanding to background.
    blurred = cv2.GaussianBlur(chosen, (3, 3), 0)
    _, chosen = cv2.threshold(blurred, 24, 255, cv2.THRESH_BINARY)
    return chosen


def _dark_script_mask(img_rgb: np.ndarray) -> np.ndarray:
    """
    Candidate for dark script text on light card backgrounds with light stroke.
    """
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)

    p35 = int(np.percentile(gray, 35))
    dark = (gray < min(132, p35 + 16)).astype(np.uint8) * 255
    white = ((s < 34) & (v > 152)).astype(np.uint8) * 255
    near_dark = cv2.dilate(dark, np.ones((5, 5), np.uint8), iterations=1)
    outline = cv2.bitwise_and(white, near_dark)

    mask = cv2.bitwise_or(dark, outline)
    edges = cv2.Canny(gray, 65, 165)
    edges = cv2.bitwise_and(edges, cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1))
    mask = cv2.bitwise_or(mask, edges)

    mask = _remove_border_touching_components(mask)
    mask = _remove_small_components(mask, min_area=max(14, int(0.000012 * img_rgb.shape[0] * img_rgb.shape[1])))
    mask = _remove_frame_like_components(mask)
    mask = _keep_main_text_components(mask)
    return mask


def _compute_quality(mask: np.ndarray) -> tuple[float, float, float]:
    total = float(mask.size)
    foreground_ratio = float(np.count_nonzero(mask > 0) / total)
    transparency_ratio = 1.0 - foreground_ratio

    # Good extraction usually keeps 4%..35% of pixels as foreground.
    foreground_ok = 1.0
    if foreground_ratio < 0.04:
        foreground_ok = max(0.0, foreground_ratio / 0.04)
    elif foreground_ratio > 0.35:
        foreground_ok = max(0.0, 1.0 - (foreground_ratio - 0.35) / 0.40)

    transparency_ok = np.clip((transparency_ratio - 0.40) / 0.55, 0.0, 1.0)
    quality = float(np.clip(0.55 * foreground_ok + 0.45 * transparency_ok, 0.0, 1.0))
    return quality, foreground_ratio, transparency_ratio


def _compute_qc_metrics(mask: np.ndarray, alpha: np.ndarray) -> dict:
    total = float(mask.size)
    fg = float(np.count_nonzero(mask > 0) / total)

    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    comp_count = max(0, n - 1)
    largest_area = 0
    if n > 1:
        largest_area = int(stats[1:, cv2.CC_STAT_AREA].max())

    # Noise score: many tiny components relative to foreground.
    tiny_area_thr = max(12, int(0.00001 * mask.shape[0] * mask.shape[1]))
    tiny_components = 0
    for i in range(1, n):
        if int(stats[i, cv2.CC_STAT_AREA]) < tiny_area_thr:
            tiny_components += 1
    noise_score = float(min(1.0, tiny_components / 40.0))

    # Stroke preservation proxy: edge density inside foreground.
    edges = cv2.Canny(mask, 40, 120)
    edge_density = float(np.count_nonzero(edges > 0) / max(1, np.count_nonzero(mask > 0)))
    stroke_loss_score = float(max(0.0, 1.0 - min(1.0, edge_density / 0.32)))

    # Edge artifact proxy from soft alpha.
    alpha_nonzero = alpha[alpha > 0]
    if alpha_nonzero.size == 0:
        edge_artifact_score = 1.0
    else:
        weak = np.count_nonzero((alpha > 0) & (alpha < 40))
        edge_artifact_score = float(min(1.0, weak / max(1, np.count_nonzero(alpha > 0))))

    return {
        "component_count": comp_count,
        "largest_component_area": largest_area,
        "noise_score": round(noise_score, 4),
        "stroke_loss_score": round(stroke_loss_score, 4),
        "edge_artifact_score": round(edge_artifact_score, 4),
        "foreground_ratio": round(fg, 4),
    }


def _qc_decision(quality: float, fg_ratio: float, metrics: dict) -> tuple[str, str]:
    noise = float(metrics.get("noise_score", 1.0))
    stroke_loss = float(metrics.get("stroke_loss_score", 1.0))
    edge_art = float(metrics.get("edge_artifact_score", 1.0))

    if quality < 0.52 or fg_ratio < 0.012 or fg_ratio > 0.52:
        return "MANUAL_CHECK", "hard_quality_fail"
    if stroke_loss > 0.86:
        return "MANUAL_CHECK", "stroke_loss_high"
    if quality < 0.70 or noise > 0.92 or edge_art > 0.38 or stroke_loss > 0.70:
        return "RETRY", "borderline_quality"
    return "PASS", ""


def _save_report(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_overlay(preview_path: str | Path, output_root: str | Path = "output") -> ExtractionResult:
    src = Path(preview_path)
    if not src.exists():
        raise FileNotFoundError(f"Input file not found: {src}")

    font_id = src.stem
    out_dir = Path(output_root) / font_id
    out_dir.mkdir(parents=True, exist_ok=True)

    source_img_raw = Image.open(src).convert("RGBA")
    source_img, work_crop = _crop_working_zone_rgba(source_img_raw)
    rgb = np.array(source_img.convert("RGB"))
    source_profile = _analyze_source_profile(rgb)
    simple_bg = bool(source_profile["simple_bg"])
    extraction_mode = "plain_mode"
    fast_candidate_masks: list[tuple[str, np.ndarray]] = []

    # CV-first candidates
    card_mask = _build_card_mode_mask(rgb)
    if card_mask is not None:
        extraction_mode = "card_mode"
        fast_candidate_masks.append(("card_mode", card_mask))
    fast_candidate_masks.append(("plain_mode", _extract_text_mask_general(rgb, profile="default")))
    fast_candidate_masks.append(("dark_script", _dark_script_mask(rgb)))
    fast_candidate_masks.append(("threshold_otsu", _threshold_text_mask(rgb)))
    fast_candidate_masks.append(("adaptive_threshold", _adaptive_threshold_mask(rgb)))
    fast_candidate_masks.append(("lab_hsv_separation", _lab_hsv_separation_mask(rgb)))
    if simple_bg:
        fast_candidate_masks.append(("color_key", _border_distance_mask(rgb, simple_bg=True)))
    fast_candidate_masks.append(("strict_fallback", _strict_letter_mask(rgb, simple_bg=simple_bg)))

    def choose_best(cands: list[tuple[str, np.ndarray]]) -> tuple[str, np.ndarray]:
        best_name_local = "plain_mode"
        best_mask_local = np.zeros(rgb.shape[:2], dtype=np.uint8)
        best_score_local = -1.0
        for name, cmask in cands:
            if cmask is None:
                continue
            score = _score_mask(cmask, img_rgb=rgb, source_profile=source_profile)
            if score > best_score_local:
                best_score_local = score
                best_mask_local = cmask
                best_name_local = name
        primary = _keep_primary_wordmark_components(best_mask_local)
        return best_name_local, _keep_main_text_components(primary)

    retry_count = 0
    best_name, mask = choose_best(fast_candidate_masks)
    extraction_mode = best_name

    def render_and_qc(mask_in: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict, dict, dict, float, float, float, dict]:
        mask_refined_local = _finalize_text_mask(mask_in, rgb)
        alpha_soft_local = _soft_alpha_from_binary_mask(mask_refined_local)
        rgba_local = np.array(source_img)
        rgba_local[:, :, 3] = alpha_soft_local
        rgba_local = _decontaminate_edge_rgb(rgba_local, mask_refined_local)
        canvas_rgba_local, canvas_mask_local, bbox_px_local, bbox_norm_local = _fit_rgba_to_standard_canvas(rgba_local)
        # Final guard on standardized canvas: cut detached lower decorative blobs.
        canvas_mask_local = _drop_lower_decorative_components(canvas_mask_local)
        canvas_mask_local = _keep_central_large_components(canvas_mask_local)
        canvas_mask_local = _remove_frame_like_components(canvas_mask_local)
        canvas_mask_local = _remove_border_touching_components(canvas_mask_local)
        canvas_alpha_local = _soft_alpha_from_binary_mask(canvas_mask_local)
        canvas_rgba_local[:, :, 3] = canvas_alpha_local
        canvas_rgba_local = _decontaminate_edge_rgb(canvas_rgba_local, canvas_mask_local)
        quality_local, fg_local, tr_local = _compute_quality(canvas_mask_local)
        qc_metrics_local = _compute_qc_metrics(canvas_mask_local, canvas_rgba_local[:, :, 3])
        return (
            canvas_rgba_local,
            canvas_mask_local,
            bbox_px_local,
            bbox_norm_local,
            qc_metrics_local,
            quality_local,
            fg_local,
            tr_local,
            _extract_font_color_info(canvas_rgba_local),
        )

    (
        canvas_rgba,
        canvas_mask,
        bbox_px,
        bbox_norm,
        qc_metrics,
        quality,
        fg_ratio,
        tr_ratio,
        font_colors,
    ) = render_and_qc(mask)

    qc_decision, qc_reason = _qc_decision(quality, fg_ratio, qc_metrics)

    # Retry policy:
    # - RETRY: borderline quality
    # - MANUAL_CHECK: run heavy rembg ensemble before giving up
    if qc_decision in {"RETRY", "MANUAL_CHECK"}:
        retry_count = 1
        retry_candidates = list(fast_candidate_masks)
        rembg_candidates = _collect_rembg_masks(source_img, simple_bg=simple_bg)
        if rembg_candidates:
            for model_name, rembg_mask in rembg_candidates:
                if _looks_like_full_rectangle(rembg_mask):
                    roi_mask = _letter_mask_from_rect_component(rgb, rembg_mask)
                    if roi_mask is not None and _foreground_ratio(roi_mask) > 0.003:
                        retry_candidates.append((f"{model_name}_rect_refined", roi_mask))
                    retry_candidates.append((f"{model_name}_strict", _strict_letter_mask(rgb, simple_bg=simple_bg)))
                else:
                    retry_candidates.append((model_name, rembg_mask))
        elif REMBG_MODEL_CANDIDATES:
            logger.warning("[%s] all rembg models unavailable. Keeping CV-only retry.", font_id)

        retry_candidates.append(("plain_mode_aggressive", _extract_text_mask_general(rgb, profile="aggressive")))
        retry_candidates.append(("plain_mode_conservative", _extract_text_mask_general(rgb, profile="conservative")))
        retry_candidates.append(("strict_retry", _strict_letter_mask(rgb, simple_bg=simple_bg)))
        best_name, mask = choose_best(retry_candidates)
        extraction_mode = best_name
        (
            canvas_rgba,
            canvas_mask,
            bbox_px,
            bbox_norm,
            qc_metrics,
            quality,
            fg_ratio,
            tr_ratio,
            font_colors,
        ) = render_and_qc(mask)
        qc_decision, qc_reason = _qc_decision(quality, fg_ratio, qc_metrics)
        # Accept borderline outputs after one retry only when artifact signals are low.
        if qc_decision == "RETRY":
            noise = float(qc_metrics.get("noise_score", 1.0))
            stroke = float(qc_metrics.get("stroke_loss_score", 1.0))
            edge_art = float(qc_metrics.get("edge_artifact_score", 1.0))
            if stroke <= 0.74 and noise <= 0.70 and edge_art <= 0.25:
                qc_decision = "PASS"
                qc_reason = "accepted_after_retry"
            else:
                qc_decision = "MANUAL_CHECK"
                qc_reason = "retry_quality_still_low"

    overlay = Image.fromarray(canvas_rgba, mode="RGBA")
    mask_image = Image.fromarray(canvas_mask, mode="L")

    needs_manual_check = qc_decision == "MANUAL_CHECK"
    manual_reason = qc_reason if needs_manual_check else ""

    # Always write latest artifacts to the root font folder.
    overlay_path = out_dir / "extracted_overlay.png"
    mask_path = out_dir / "mask.png"
    report_path = out_dir / "extraction_report.json"

    overlay.save(overlay_path, "PNG")
    mask_image.save(mask_path, "PNG")
    report_payload = {
        "font_id": font_id,
        "source_path": str(src),
        "working_crop": work_crop,
        "extraction_mode": extraction_mode,
        "overlay_path": str(overlay_path),
        "mask_path": str(mask_path),
        "overlay_size": [STANDARD_OVERLAY_SIZE, STANDARD_OVERLAY_SIZE],
        "bbox_px": bbox_px,
        "bbox_norm": bbox_norm,
        "recommended_scale_pct": round(bbox_norm["w"] * 100.0, 2),
        "font_colors": font_colors,
        "qc_decision": qc_decision,
        "qc_reason": qc_reason,
        "retry_count": retry_count,
        "qc_metrics": qc_metrics,
        "quality_score": round(quality, 4),
        "foreground_ratio": round(fg_ratio, 4),
        "transparency_ratio": round(tr_ratio, 4),
        "simple_background": simple_bg,
        "needs_manual_check": needs_manual_check,
        "manual_reason": manual_reason,
    }
    _save_report(
        report_path,
        report_payload,
    )

    # If marked as manual-check, also keep a copy in manual_check for inspection queue.
    if needs_manual_check:
        manual_dir = out_dir / "manual_check"
        manual_dir.mkdir(parents=True, exist_ok=True)
        manual_overlay = manual_dir / "extracted_overlay.png"
        manual_mask = manual_dir / "mask.png"
        manual_report = manual_dir / "extraction_report.json"
        shutil.copy2(overlay_path, manual_overlay)
        shutil.copy2(mask_path, manual_mask)

        manual_payload = dict(report_payload)
        manual_payload["overlay_path"] = str(manual_overlay)
        manual_payload["mask_path"] = str(manual_mask)
        _save_report(manual_report, manual_payload)

    logger.info(
        "[%s] mode=%s quality=%.3f foreground=%.3f qc=%s retry=%d manual_check=%s",
        font_id,
        extraction_mode,
        quality,
        fg_ratio,
        qc_decision,
        retry_count,
        needs_manual_check,
    )

    return ExtractionResult(
        font_id=font_id,
        source_path=str(src),
        overlay_path=str(overlay_path),
        mask_path=str(mask_path),
        report_path=str(report_path),
        quality_score=quality,
        foreground_ratio=fg_ratio,
        transparency_ratio=tr_ratio,
        needs_manual_check=needs_manual_check,
        manual_reason=manual_reason,
        simple_background=simple_bg,
        overlay_size=(STANDARD_OVERLAY_SIZE, STANDARD_OVERLAY_SIZE),
        bbox_px=bbox_px,
        bbox_norm=bbox_norm,
        font_colors=font_colors,
        extraction_mode=extraction_mode,
        qc_metrics=qc_metrics,
        qc_decision=qc_decision,
        retry_count=retry_count,
    )


def _iter_input_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(f"Input path not found: {input_path}")
    return sorted(p for p in input_path.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES)


def run_batch(input_path: str | Path, output_root: str | Path = "output") -> list[ExtractionResult]:
    src = Path(input_path)
    files = _iter_input_files(src)
    results: list[ExtractionResult] = []
    for file_path in files:
        try:
            results.append(extract_overlay(file_path, output_root=output_root))
        except Exception as exc:
            logger.exception("Failed to process %s: %s", file_path, exc)
    _write_batch_reports(results, output_root=output_root)
    return results


def _write_batch_reports(results: list[ExtractionResult], output_root: str | Path) -> None:
    out_root = Path(output_root)
    reports_dir = out_root / "_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = len(results)
    pass_count = sum(1 for r in results if r.qc_decision == "PASS")
    retry_count = sum(1 for r in results if r.retry_count > 0)
    manual_count = sum(1 for r in results if r.needs_manual_check)

    by_mode: dict[str, int] = {}
    for r in results:
        by_mode[r.extraction_mode] = by_mode.get(r.extraction_mode, 0) + 1

    summary = {
        "generated_at": now,
        "total": total,
        "pass_count": pass_count,
        "retry_count": retry_count,
        "manual_check_count": manual_count,
        "pass_rate": round((pass_count / total), 4) if total else 0.0,
        "manual_check_rate": round((manual_count / total), 4) if total else 0.0,
        "by_extraction_mode": by_mode,
    }

    json_path = reports_dir / "extractor_batch_report.json"
    json_path.write_text(json.dumps({"summary": summary, "items": [r.__dict__ for r in results]}, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = reports_dir / "extractor_batch_report.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "font_id",
                "source_path",
                "extraction_mode",
                "qc_decision",
                "retry_count",
                "needs_manual_check",
                "manual_reason",
                "quality_score",
                "foreground_ratio",
                "transparency_ratio",
                "dominant_color_1",
                "contrast_hint",
                "overlay_path",
                "mask_path",
                "report_path",
            ],
        )
        writer.writeheader()
        for r in results:
            dominant = r.font_colors.get("dominant_colors", [])
            writer.writerow(
                {
                    "font_id": r.font_id,
                    "source_path": r.source_path,
                    "extraction_mode": r.extraction_mode,
                    "qc_decision": r.qc_decision,
                    "retry_count": r.retry_count,
                    "needs_manual_check": r.needs_manual_check,
                    "manual_reason": r.manual_reason,
                    "quality_score": round(r.quality_score, 4),
                    "foreground_ratio": round(r.foreground_ratio, 4),
                    "transparency_ratio": round(r.transparency_ratio, 4),
                    "dominant_color_1": dominant[0] if dominant else "",
                    "contrast_hint": r.font_colors.get("contrast_hint", ""),
                    "overlay_path": r.overlay_path,
                    "mask_path": r.mask_path,
                    "report_path": r.report_path,
                }
            )

    logger.info("Batch reports saved: %s and %s", json_path, csv_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract transparent font overlay from preview images.")
    parser.add_argument("--input", required=True, help="Path to image file or folder with previews.")
    parser.add_argument("--output", default="output", help="Output root directory (default: output).")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    results = run_batch(args.input, output_root=args.output)
    logger.info("Done. Processed %d file(s).", len(results))


if __name__ == "__main__":
    main()
