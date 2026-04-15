import argparse
import io
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from rembg import remove

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
STANDARD_OVERLAY_SIZE = 1500
STANDARD_CONTENT_MAX_PX = 1200


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


def _remove_background_with_rembg(image: Image.Image) -> Image.Image:
    in_buf = io.BytesIO()
    image.save(in_buf, format="PNG")
    output_bytes = remove(in_buf.getvalue())
    return Image.open(io.BytesIO(output_bytes)).convert("RGBA")


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


def _soft_alpha_from_binary_mask(mask: np.ndarray) -> np.ndarray:
    """
    Convert a binary mask to soft alpha for cleaner, less pixelated edges.
    """
    binary = (mask > 0).astype(np.uint8)
    soft = cv2.GaussianBlur((binary * 255).astype(np.float32), (0, 0), sigmaX=0.9, sigmaY=0.9)
    soft = np.clip(soft, 0, 255).astype(np.uint8)

    alpha = np.zeros_like(mask, dtype=np.uint8)
    edge_band = cv2.dilate(binary, np.ones((3, 3), np.uint8), iterations=1) > 0
    alpha[edge_band] = soft[edge_band]
    alpha[binary > 0] = np.maximum(alpha[binary > 0], 245)
    alpha[alpha < 10] = 0
    return alpha


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


def _save_report(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_overlay(preview_path: str | Path, output_root: str | Path = "output") -> ExtractionResult:
    src = Path(preview_path)
    if not src.exists():
        raise FileNotFoundError(f"Input file not found: {src}")

    font_id = src.stem
    out_dir = Path(output_root) / font_id
    out_dir.mkdir(parents=True, exist_ok=True)

    source_img = Image.open(src).convert("RGBA")
    rgb = np.array(source_img.convert("RGB"))
    simple_bg = _estimate_simple_background(rgb)

    rgba_np = np.array(source_img)
    try:
        rgba = _remove_background_with_rembg(source_img)
        rgba_np = np.array(rgba)
        alpha = rgba_np[:, :, 3]
        mask = _postprocess_alpha(alpha, simple_bg=simple_bg)
        if _looks_like_full_rectangle(mask):
            logger.info("[%s] rembg kept full rectangle, switching to strict letter mask", font_id)
            roi_mask = _letter_mask_from_rect_component(rgb, mask)
            if roi_mask is not None and _foreground_ratio(roi_mask) > 0.003:
                mask = roi_mask
            else:
                mask = _strict_letter_mask(rgb, simple_bg=simple_bg)
    except Exception as exc:
        logger.warning("[%s] rembg unavailable/failed (%s). Using strict CV mask only.", font_id, exc)
        mask = _strict_letter_mask(rgb, simple_bg=simple_bg)

    alpha_soft = _soft_alpha_from_binary_mask(mask)
    rgba_np[:, :, 3] = alpha_soft

    canvas_rgba, canvas_mask, bbox_px, bbox_norm = _fit_rgba_to_standard_canvas(rgba_np)
    overlay = Image.fromarray(canvas_rgba, mode="RGBA")
    mask_image = Image.fromarray(canvas_mask, mode="L")
    font_colors = _extract_font_color_info(canvas_rgba)

    quality, fg_ratio, tr_ratio = _compute_quality(canvas_mask)

    manual_reason = ""
    needs_manual_check = False
    if quality < 0.55:
        needs_manual_check = True
        manual_reason = "low_quality_score"
    elif fg_ratio < 0.02:
        needs_manual_check = True
        manual_reason = "too_little_foreground"
    elif fg_ratio > 0.48:
        needs_manual_check = True
        manual_reason = "too_much_foreground"

    # Always write latest artifacts to the root font folder.
    overlay_path = out_dir / "extracted_overlay.png"
    mask_path = out_dir / "mask.png"
    report_path = out_dir / "extraction_report.json"

    overlay.save(overlay_path, "PNG")
    mask_image.save(mask_path, "PNG")
    report_payload = {
        "font_id": font_id,
        "source_path": str(src),
        "overlay_path": str(overlay_path),
        "mask_path": str(mask_path),
        "overlay_size": [STANDARD_OVERLAY_SIZE, STANDARD_OVERLAY_SIZE],
        "bbox_px": bbox_px,
        "bbox_norm": bbox_norm,
        "recommended_scale_pct": round(bbox_norm["w"] * 100.0, 2),
        "font_colors": font_colors,
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
        "[%s] quality=%.3f foreground=%.3f manual_check=%s",
        font_id,
        quality,
        fg_ratio,
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
    return results


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
