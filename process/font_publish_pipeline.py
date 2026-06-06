import csv
import io
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter

from download.extractor import SUPPORTED_SUFFIXES, extract_overlay

logger = logging.getLogger(__name__)

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
COMFY_BG_MODEL = os.environ.get("COMFY_BG_MODEL", os.environ.get("COMFY_MODEL", "realisticVisionV51.safetensors"))
COMFY_BG_STEPS = int(os.environ.get("COMFY_BG_STEPS", "18"))
COMFY_BG_CFG = float(os.environ.get("COMFY_BG_CFG", "6.0"))
COMFY_BG_SAMPLER = os.environ.get("COMFY_BG_SAMPLER", "dpmpp_2m")
COMFY_BG_SCHEDULER = os.environ.get("COMFY_BG_SCHEDULER", "karras")
COMFY_BG_TIMEOUT_SEC = int(os.environ.get("COMFY_BG_TIMEOUT_SEC", "120"))
COMFY_BG_WIDTH = int(os.environ.get("COMFY_BG_WIDTH", "1024"))
COMFY_BG_HEIGHT = int(os.environ.get("COMFY_BG_HEIGHT", "1024"))

FINAL_SIZE = 1500


@dataclass
class PublishResult:
    font_id: str
    source_preview_path: str
    overlay_path: str
    background_path: str
    final_image_path: str
    report_path: str
    extraction_mode: str
    extraction_qc: str
    extraction_quality: float
    comfy_used: bool
    comfy_prompt_id: str
    comfy_seed: int
    background_prompt: str
    used_fallback: bool
    fallback_reason: str
    readability_contrast_score: float


def _iter_input_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(f"Input path not found: {input_path}")
    return sorted(p for p in input_path.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES)


def _comfy_available() -> bool:
    try:
        r = requests.get(f"{COMFY_URL}/system_stats", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _queue_prompt(prompt_graph: dict) -> str:
    payload = json.dumps({"prompt": prompt_graph}).encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["prompt_id"]


def _wait_for_output(prompt_id: str, timeout_sec: int) -> dict | None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            data = r.json()
            if prompt_id in data:
                return data[prompt_id].get("outputs", {})
        except Exception:
            pass
        time.sleep(2)
    return None


def _download_first_output_image(outputs: dict) -> Image.Image | None:
    for node_output in outputs.values():
        images = node_output.get("images", [])
        for img in images:
            params = urllib.parse.urlencode(
                {
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                }
            )
            url = f"{COMFY_URL}/view?{params}"
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                return Image.open(io.BytesIO(r.content)).convert("RGB")
            except Exception:
                continue
    return None


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.strip().lstrip("#")
    if len(v) != 6:
        return (120, 120, 120)
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(round(a[0] * (1.0 - t) + b[0] * t)),
        int(round(a[1] * (1.0 - t) + b[1] * t)),
        int(round(a[2] * (1.0 - t) + b[2] * t)),
    )


def _darken(c: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return _mix(c, (0, 0, 0), amount)


def _lighten(c: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return _mix(c, (255, 255, 255), amount)


def _dominant_palette(font_colors: dict) -> list[tuple[int, int, int]]:
    dom = font_colors.get("dominant_colors", [])
    out: list[tuple[int, int, int]] = []
    for h in dom[:4]:
        out.append(_hex_to_rgb(h))
    if not out:
        out = [(125, 125, 125), (92, 92, 92)]
    return out


def _build_background_prompt(
    font_name: str,
    category: str,
    font_colors: dict,
) -> str:
    contrast_hint = font_colors.get("contrast_hint", "use_mid_bg")
    palette = font_colors.get("dominant_colors", [])
    palette_txt = ", ".join(palette[:3]) if palette else "soft neutral palette"
    if contrast_hint == "use_dark_bg":
        mood = "dark cinematic abstract background, rich gradients, subtle texture"
    elif contrast_hint == "use_light_bg":
        mood = "light airy abstract background, clean gradients, subtle paper grain"
    else:
        mood = "balanced abstract gradient background with soft depth and bokeh"
    return (
        f"{mood}, premium pinterest poster backdrop, category {category}, "
        f"color harmony inspired by {palette_txt}, no text, no letters, no logo, "
        f"high detail, clean composition, centered visual balance for typography overlay"
    )


def _build_negative_prompt() -> str:
    return (
        "text, letters, words, typography, logo, watermark, signature, frame, border, "
        "messy composition, low quality, blur, artifacts"
    )


def _build_comfy_bg_workflow(prompt: str, negative_prompt: str, seed: int) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": COMFY_BG_MODEL}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": ["1", 1]}},
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": COMFY_BG_WIDTH, "height": COMFY_BG_HEIGHT, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": int(seed),
                "steps": COMFY_BG_STEPS,
                "cfg": COMFY_BG_CFG,
                "sampler_name": COMFY_BG_SAMPLER,
                "scheduler": COMFY_BG_SCHEDULER,
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "cf_font_bg"}},
    }


def _script_background(font_colors: dict, seed: int) -> Image.Image:
    rng = np.random.default_rng(seed)
    palette = _dominant_palette(font_colors)
    contrast_hint = font_colors.get("contrast_hint", "use_mid_bg")

    base = palette[0]
    if contrast_hint == "use_dark_bg":
        c1 = _darken(base, 0.60)
        c2 = _darken(palette[min(1, len(palette) - 1)], 0.45)
    elif contrast_hint == "use_light_bg":
        c1 = _lighten(base, 0.62)
        c2 = _lighten(palette[min(1, len(palette) - 1)], 0.48)
    else:
        c1 = _mix(base, palette[min(1, len(palette) - 1)], 0.35)
        c2 = _mix(_lighten(base, 0.38), _darken(palette[min(1, len(palette) - 1)], 0.24), 0.5)

    h = w = FINAL_SIZE
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    xx = xx / float(max(1, w - 1))
    yy = yy / float(max(1, h - 1))
    rad = np.sqrt((xx - 0.52) ** 2 + (yy - 0.45) ** 2)
    rad = np.clip(rad / 0.85, 0.0, 1.0)
    blend = np.clip(0.65 * xx + 0.35 * (1.0 - rad), 0.0, 1.0)

    arr = np.zeros((h, w, 3), dtype=np.float32)
    for c in range(3):
        arr[:, :, c] = c1[c] * (1.0 - blend) + c2[c] * blend

    grain = rng.normal(0.0, 5.8, size=(h, w, 1)).astype(np.float32)
    arr += grain
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr, mode="RGB")

    # Soft abstract blobs for uniqueness.
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for _ in range(7):
        bx = int(rng.integers(int(0.08 * w), int(0.92 * w)))
        by = int(rng.integers(int(0.08 * h), int(0.92 * h)))
        br = int(rng.integers(int(0.10 * w), int(0.22 * w)))
        color = palette[int(rng.integers(0, len(palette)))]
        alpha = int(rng.integers(22, 56))
        draw.ellipse([bx - br, by - br, bx + br, by + br], fill=(color[0], color[1], color[2], alpha))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=38))
    out = img.convert("RGBA")
    out.alpha_composite(overlay)
    return out.convert("RGB")


def _generate_background(
    font_id: str,
    font_name: str,
    category: str,
    font_colors: dict,
    use_comfy: bool,
) -> tuple[Image.Image, bool, str, str, int]:
    seed = int(uuid.uuid5(uuid.NAMESPACE_DNS, f"{font_id}-{time.time_ns()}").int % (2**31))
    prompt = _build_background_prompt(font_name=font_name, category=category, font_colors=font_colors)

    if use_comfy and _comfy_available():
        try:
            workflow = _build_comfy_bg_workflow(prompt=prompt, negative_prompt=_build_negative_prompt(), seed=seed)
            prompt_id = _queue_prompt(workflow)
            outputs = _wait_for_output(prompt_id, timeout_sec=COMFY_BG_TIMEOUT_SEC)
            if outputs:
                comfy_img = _download_first_output_image(outputs)
                if comfy_img is not None:
                    return comfy_img, True, "", prompt_id, seed
            return _script_background(font_colors=font_colors, seed=seed), False, "comfy_timeout_or_empty", "", seed
        except Exception as exc:
            return _script_background(font_colors=font_colors, seed=seed), False, f"comfy_exception:{exc}", "", seed

    reason = "comfy_disabled" if not use_comfy else "comfy_unavailable"
    return _script_background(font_colors=font_colors, seed=seed), False, reason, "", seed


def _extract_alpha_bbox(alpha: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(alpha > 0)
    if ys.size == 0:
        return None
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    return x0, y0, x1, y1


def _reframe_overlay(overlay: Image.Image, target_width_pct: float = 0.72) -> Image.Image:
    rgba = np.array(overlay.convert("RGBA"))
    alpha = rgba[:, :, 3]
    bbox = _extract_alpha_bbox(alpha)
    size = FINAL_SIZE
    canvas = np.zeros((size, size, 4), dtype=np.uint8)
    if bbox is None:
        return Image.fromarray(canvas, mode="RGBA")

    x0, y0, x1, y1 = bbox
    crop = rgba[y0:y1, x0:x1, :]
    ch, cw = crop.shape[:2]
    if ch <= 0 or cw <= 0:
        return Image.fromarray(canvas, mode="RGBA")

    target_w = int(size * max(0.45, min(0.88, target_width_pct)))
    scale = target_w / float(max(1, cw))
    new_w = max(1, int(round(cw * scale)))
    new_h = max(1, int(round(ch * scale)))
    if new_h > int(size * 0.62):
        k = (size * 0.62) / float(new_h)
        new_w = max(1, int(round(new_w * k)))
        new_h = max(1, int(round(new_h * k)))

    resized = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
    px = (size - new_w) // 2
    py = int(size * 0.46) - (new_h // 2)
    py = max(0, min(size - new_h, py))
    canvas[py : py + new_h, px : px + new_w, :] = resized
    return Image.fromarray(canvas, mode="RGBA")


def _luminance(rgb: np.ndarray) -> np.ndarray:
    return (0.2126 * rgb[:, 0] + 0.7152 * rgb[:, 1] + 0.0722 * rgb[:, 2]) / 255.0


def _contrast_score(background: Image.Image, overlay: Image.Image) -> float:
    bg = np.array(background.convert("RGB"))
    ov = np.array(overlay.convert("RGBA"))
    alpha = ov[:, :, 3]
    mask = alpha > 36
    if np.count_nonzero(mask) == 0:
        return 0.0
    fg_rgb = ov[:, :, :3][mask]
    bg_rgb = bg[mask]
    fg_l = _luminance(fg_rgb.astype(np.float32))
    bg_l = _luminance(bg_rgb.astype(np.float32))
    return float(np.mean(np.abs(fg_l - bg_l)))


def _add_readability_support(background: Image.Image, overlay: Image.Image) -> tuple[Image.Image, float]:
    score = _contrast_score(background, overlay)
    if score >= 0.18:
        return overlay, score

    rgba = np.array(overlay.convert("RGBA"))
    alpha = rgba[:, :, 3]
    if np.count_nonzero(alpha > 0) == 0:
        return overlay, score

    fg_pixels = rgba[:, :, :3][alpha > 24].astype(np.float32)
    fg_l = float(np.mean(_luminance(fg_pixels))) if fg_pixels.size else 0.5
    stroke_rgb = (0, 0, 0) if fg_l >= 0.52 else (255, 255, 255)
    stroke_alpha = 140

    binary = (alpha > 0).astype(np.uint8)
    ring = cv2.dilate(binary, np.ones((5, 5), np.uint8), iterations=1)
    ring = np.where((ring > 0) & (binary == 0), 255, 0).astype(np.uint8)
    ring = cv2.GaussianBlur(ring, (0, 0), sigmaX=1.1, sigmaY=1.1)

    stroke = np.zeros_like(rgba)
    stroke[:, :, 0] = stroke_rgb[0]
    stroke[:, :, 1] = stroke_rgb[1]
    stroke[:, :, 2] = stroke_rgb[2]
    stroke[:, :, 3] = np.clip((ring.astype(np.float32) / 255.0) * stroke_alpha, 0, 255).astype(np.uint8)

    out = Image.fromarray(stroke, mode="RGBA")
    out.alpha_composite(Image.fromarray(rgba, mode="RGBA"))
    return out, _contrast_score(background, out)


def _cover_resize(img: Image.Image, size: int) -> Image.Image:
    src = img.convert("RGB")
    w, h = src.size
    if w == size and h == size:
        return src
    scale = max(size / float(max(1, w)), size / float(max(1, h)))
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    resized = src.resize((nw, nh), Image.LANCZOS)
    x = (nw - size) // 2
    y = (nh - size) // 2
    return resized.crop((x, y, x + size, y + size))


def _compose_final(background: Image.Image, overlay: Image.Image, target_width_pct: float) -> tuple[Image.Image, float]:
    bg = _cover_resize(background, FINAL_SIZE).convert("RGBA")
    ov = _reframe_overlay(overlay, target_width_pct=target_width_pct)
    ov, score = _add_readability_support(background=bg.convert("RGB"), overlay=ov)
    bg.alpha_composite(ov)
    return bg.convert("RGB"), score


def _mask_edge_density(mask: np.ndarray) -> float:
    fg = np.count_nonzero(mask > 0)
    if fg == 0:
        return 0.0
    edges = cv2.Canny((mask > 0).astype(np.uint8) * 255, 40, 120)
    return float(np.count_nonzero(edges > 0) / fg)


def _keep_main_components(mask: np.ndarray) -> np.ndarray:
    h, w = mask.shape
    n, labels, stats, centroids = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    if n <= 1:
        return mask
    center = np.array([w / 2.0, h / 2.0], dtype=np.float32)
    rows = []
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
        rows.append((i, x, y, bw, bh, area, cx, cy, score))
    if not rows:
        return mask
    rows.sort(key=lambda r: r[8], reverse=True)
    max_area = max(r[5] for r in rows)
    min_keep = max(40, int(max_area * 0.01), int(0.00002 * h * w))
    keep = np.zeros_like(mask, dtype=np.uint8)
    acc = 0
    total = sum(r[5] for r in rows if r[5] >= min_keep)
    for i, x, y, bw, bh, area, cx, cy, _ in rows:
        if area < min_keep:
            continue
        # remove tiny corner logos/hearts/icons
        corner = (x < 0.12 * w and y < 0.12 * h) or (x + bw > 0.88 * w and y < 0.12 * h) or (x < 0.12 * w and y + bh > 0.88 * h) or (x + bw > 0.88 * w and y + bh > 0.88 * h)
        if corner and area < int(max_area * 0.25):
            continue
        keep[labels == i] = 255
        acc += area
        if total > 0 and (acc / total) >= 0.995:
            break
    return keep


def _sanitize_overlay_for_publish(overlay: Image.Image) -> Image.Image:
    """
    Remove severe background-blob artifacts from extracted overlay while
    preserving original font colors/outline.
    """
    rgba = np.array(overlay.convert("RGBA"))
    alpha = rgba[:, :, 3]
    mask = (alpha > 20).astype(np.uint8) * 255
    fg_ratio = float(np.count_nonzero(mask > 0) / max(1, mask.size))
    edge_d = _mask_edge_density(mask)
    # suspicious: too much filled foreground with weak contour density
    suspicious_blob = fg_ratio > 0.16 and edge_d < 0.042
    if not suspicious_blob:
        return overlay

    rgb = rgba[:, :, :3]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]

    text_like = ((v < 72) | (v > 172) | (s > 48)).astype(np.uint8) * 255
    text_like = cv2.bitwise_and(text_like, mask)
    text_like = cv2.morphologyEx(text_like, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)
    text_like = cv2.morphologyEx(text_like, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8), iterations=1)
    text_like = _keep_main_components(text_like)

    new_ratio = float(np.count_nonzero(text_like > 0) / max(1, text_like.size))
    # If sanitizer over-prunes, keep original.
    if new_ratio < 0.02 or new_ratio > 0.22:
        return overlay

    new_alpha = np.zeros_like(alpha, dtype=np.uint8)
    new_alpha[text_like > 0] = alpha[text_like > 0]
    rgba[:, :, 3] = new_alpha
    return Image.fromarray(rgba, mode="RGBA")


def _derive_font_name(path: Path) -> str:
    return path.stem.replace("-", " ").replace("_", " ").strip().title()


def publish_font_image(
    source_preview_path: str | Path,
    output_root: str | Path = "output",
    font_name: str = "",
    category: str = "fonts",
    use_comfy_background: bool = True,
    target_width_pct: float = 0.72,
) -> PublishResult:
    src = Path(source_preview_path)
    if not src.exists():
        raise FileNotFoundError(f"Input file not found: {src}")

    extraction = extract_overlay(src, output_root=output_root)
    font_id = extraction.font_id
    out_dir = Path(output_root) / font_id
    out_dir.mkdir(parents=True, exist_ok=True)

    overlay_path = Path(extraction.overlay_path)
    overlay = Image.open(overlay_path).convert("RGBA")
    overlay = _sanitize_overlay_for_publish(overlay)

    effective_font_name = font_name.strip() or _derive_font_name(src)
    bg_img, comfy_used, fallback_reason, prompt_id, seed = _generate_background(
        font_id=font_id,
        font_name=effective_font_name,
        category=category,
        font_colors=extraction.font_colors,
        use_comfy=use_comfy_background,
    )

    final_img, contrast_score = _compose_final(
        background=bg_img,
        overlay=overlay,
        target_width_pct=target_width_pct,
    )

    bg_path = out_dir / "publish_background.png"
    final_path = out_dir / "publish_final.png"
    report_path = out_dir / "publish_report.json"
    bg_img.save(bg_path, "PNG")
    final_img.save(final_path, "PNG")

    bg_prompt = _build_background_prompt(
        font_name=effective_font_name,
        category=category,
        font_colors=extraction.font_colors,
    )

    report = {
        "font_id": font_id,
        "font_name": effective_font_name,
        "category": category,
        "source_preview_path": str(src),
        "overlay_path": str(overlay_path),
        "background_path": str(bg_path),
        "final_image_path": str(final_path),
        "extraction_mode": extraction.extraction_mode,
        "extraction_qc": extraction.qc_decision,
        "extraction_quality": round(extraction.quality_score, 4),
        "font_colors": extraction.font_colors,
        "use_comfy_background": use_comfy_background,
        "comfy_used": comfy_used,
        "comfy_url": COMFY_URL,
        "comfy_bg_model": COMFY_BG_MODEL,
        "comfy_prompt_id": prompt_id,
        "comfy_seed": int(seed),
        "used_fallback": not comfy_used,
        "fallback_reason": fallback_reason,
        "background_prompt": bg_prompt,
        "readability_contrast_score": round(contrast_score, 4),
        "target_width_pct": round(float(target_width_pct), 4),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(
        "[%s] publish ready | comfy=%s fallback=%s contrast=%.3f",
        font_id,
        comfy_used,
        not comfy_used,
        contrast_score,
    )

    return PublishResult(
        font_id=font_id,
        source_preview_path=str(src),
        overlay_path=str(overlay_path),
        background_path=str(bg_path),
        final_image_path=str(final_path),
        report_path=str(report_path),
        extraction_mode=extraction.extraction_mode,
        extraction_qc=extraction.qc_decision,
        extraction_quality=extraction.quality_score,
        comfy_used=comfy_used,
        comfy_prompt_id=prompt_id,
        comfy_seed=seed,
        background_prompt=bg_prompt,
        used_fallback=not comfy_used,
        fallback_reason=fallback_reason,
        readability_contrast_score=contrast_score,
    )


def run_publish_batch(
    input_path: str | Path,
    output_root: str | Path = "output",
    category: str = "fonts",
    font_name: str = "",
    use_comfy_background: bool = True,
    target_width_pct: float = 0.78,
) -> list[PublishResult]:
    src = Path(input_path)
    files = _iter_input_files(src)
    results: list[PublishResult] = []
    for file_path in files:
        try:
            res = publish_font_image(
                source_preview_path=file_path,
                output_root=output_root,
                font_name=font_name,
                category=category,
                use_comfy_background=use_comfy_background,
                target_width_pct=target_width_pct,
            )
            results.append(res)
        except Exception as exc:
            logger.exception("Publish pipeline failed for %s: %s", file_path, exc)
    _write_publish_reports(results, output_root=output_root)
    return results


def _write_publish_reports(results: list[PublishResult], output_root: str | Path) -> None:
    out_root = Path(output_root)
    reports_dir = out_root / "_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    total = len(results)
    comfy_used = sum(1 for r in results if r.comfy_used)
    fallback = sum(1 for r in results if r.used_fallback)
    pass_extract = sum(1 for r in results if r.extraction_qc == "PASS")
    avg_contrast = float(np.mean([r.readability_contrast_score for r in results])) if results else 0.0
    summary = {
        "total": total,
        "comfy_used_count": comfy_used,
        "fallback_count": fallback,
        "extract_pass_count": pass_extract,
        "extract_pass_rate": round((pass_extract / total), 4) if total else 0.0,
        "avg_readability_contrast_score": round(avg_contrast, 4),
    }

    json_path = reports_dir / "publish_batch_report.json"
    json_path.write_text(
        json.dumps({"summary": summary, "items": [asdict(r) for r in results]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    csv_path = reports_dir / "publish_batch_report.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "font_id",
                "source_preview_path",
                "extraction_mode",
                "extraction_qc",
                "extraction_quality",
                "comfy_used",
                "used_fallback",
                "fallback_reason",
                "readability_contrast_score",
                "background_path",
                "final_image_path",
                "report_path",
            ],
        )
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "font_id": r.font_id,
                    "source_preview_path": r.source_preview_path,
                    "extraction_mode": r.extraction_mode,
                    "extraction_qc": r.extraction_qc,
                    "extraction_quality": round(r.extraction_quality, 4),
                    "comfy_used": r.comfy_used,
                    "used_fallback": r.used_fallback,
                    "fallback_reason": r.fallback_reason,
                    "readability_contrast_score": round(r.readability_contrast_score, 4),
                    "background_path": r.background_path,
                    "final_image_path": r.final_image_path,
                    "report_path": r.report_path,
                }
            )

    logger.info("Publish reports saved: %s and %s", json_path, csv_path)
