import json
import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from extractor import SUPPORTED_SUFFIXES, extract_overlay

logger = logging.getLogger(__name__)

PIN_W = 1000
PIN_H = 1500

STYLE_THEME_MAP = {
    "kids": "playful_bright",
    "wedding": "soft_floral",
    "luxury": "dark_editorial",
    "retro": "warm_vintage",
    "minimal": "clean_neutral",
    "handwritten": "paper_soft",
    "feminine": "pink_cream",
    "gothic": "dark_fog",
}


@dataclass
class AutoPinResult:
    source_file: str
    status: str
    mode: str
    style: str
    bbox: dict
    complexity_score: float
    quality_score: float
    template_used: str
    background_theme: str
    rotation_deg: float
    scale_pct: int
    pin_png: str
    pin_jpg: str
    meta_json: str
    reject_reason: str


def _iter_input_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(f"Input path not found: {input_path}")
    return sorted([p for p in input_path.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES])


def _estimate_simple_background(img_rgb: np.ndarray) -> bool:
    h, w = img_rgb.shape[:2]
    b = max(4, min(h, w) // 40)
    border = np.concatenate(
        [
            img_rgb[:b, :, :].reshape(-1, 3),
            img_rgb[h - b :, :, :].reshape(-1, 3),
            img_rgb[:, :b, :].reshape(-1, 3),
            img_rgb[:, w - b :, :].reshape(-1, 3),
        ],
        axis=0,
    )
    q = (border // 16).astype(np.uint8)
    _, counts = np.unique(q, axis=0, return_counts=True)
    if counts.size == 0:
        return False
    return float(counts.max() / counts.sum()) >= 0.78


def _detect_preview_bbox(img_rgb: np.ndarray) -> tuple[dict, float]:
    h, w = img_rgb.shape[:2]
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)

    # 1) Remove outer black bars/noise.
    non_black = (gray > 12).astype(np.uint8) * 255
    ys, xs = np.where(non_black > 0)
    if ys.size > 0 and xs.size > 0:
        y0_nb, y1_nb = int(ys.min()), int(ys.max()) + 1
        x0_nb, x1_nb = int(xs.min()), int(xs.max()) + 1
    else:
        y0_nb, y1_nb, x0_nb, x1_nb = 0, h, 0, w

    cropped = img_rgb[y0_nb:y1_nb, x0_nb:x1_nb, :]
    ch, cw = cropped.shape[:2]
    if ch <= 0 or cw <= 0:
        return {"x": 0, "y": 0, "width": w, "height": h}, 0.2

    # 2) Detect dominant rectangular preview-like component.
    b = max(4, min(ch, cw) // 48)
    border = np.concatenate(
        [
            cropped[:b, :, :].reshape(-1, 3),
            cropped[ch - b :, :, :].reshape(-1, 3),
            cropped[:, :b, :].reshape(-1, 3),
            cropped[:, cw - b :, :].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float32)
    bg = np.median(border, axis=0)
    dist = np.linalg.norm(cropped.astype(np.float32) - bg[None, None, :], axis=2)
    mask = np.where(dist > 14.0, 255, 0).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=1)

    n, _, stats, centroids = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    best = None
    best_score = -1.0
    for i in range(1, n):
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if bw <= 0 or bh <= 0 or area <= 0:
            continue
        area_ratio = area / float(ch * cw)
        fill = area / float(max(1, bw * bh))
        asp = bw / float(max(1, bh))
        if area_ratio < 0.06 or area_ratio > 0.95:
            continue
        if asp < 0.45 or asp > 4.5:
            continue
        cx, cy = centroids[i]
        center_dist = np.linalg.norm(np.array([cx - cw / 2.0, cy - ch / 2.0])) / max(cw, ch)
        y_norm = float(cy / max(1.0, ch))
        upper_score = max(0.0, 1.0 - max(0.0, (y_norm - 0.52)) * 2.2)
        roi = gray[y : y + bh, x : x + bw]
        mean_g = float(np.mean(roi)) if roi.size else 0.0
        brightness_score = max(0.1, min(1.0, (mean_g - 34.0) / 120.0))
        dark_lower_penalty = 0.35 if (y_norm > 0.62 and mean_g < 85.0) else 1.0
        score = area * fill * (1.0 - min(0.95, center_dist)) * (0.55 + 0.45 * upper_score) * brightness_score * dark_lower_penalty
        if score > best_score:
            best_score = score
            best = (x, y, bw, bh, area_ratio, fill, center_dist)

    if best is None:
        return {"x": int(x0_nb), "y": int(y0_nb), "width": int(cw), "height": int(ch)}, 0.52

    x, y, bw, bh, area_ratio, fill, center_dist = best
    x += x0_nb
    y += y0_nb
    conf = 0.52
    conf += 0.20 * max(0.0, min(1.0, (area_ratio - 0.10) / 0.55))
    conf += 0.16 * max(0.0, min(1.0, (fill - 0.45) / 0.5))
    conf += 0.14 * (1.0 - max(0.0, min(1.0, center_dist / 0.45)))
    conf = float(max(0.0, min(0.99, conf)))
    return {"x": int(x), "y": int(y), "width": int(bw), "height": int(bh)}, conf


def _blur_score(img_rgb: np.ndarray) -> float:
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return max(0.0, min(1.0, var / 600.0))


def _complexity_score(img_rgb: np.ndarray) -> float:
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    edges = cv2.Canny(gray, 70, 180)
    edge_ratio = float(np.count_nonzero(edges > 0) / max(1, edges.size))
    c = 0.6 * min(1.0, lap_var / 2400.0) + 0.4 * min(1.0, edge_ratio / 0.24)
    return float(max(0.0, min(1.0, c)))


def _quality_score(img_rgb: np.ndarray) -> float:
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    contrast = float(np.std(gray) / 255.0)
    blur = _blur_score(img_rgb)
    return float(max(0.0, min(1.0, 0.55 * min(1.0, contrast / 0.28) + 0.45 * blur)))


def _classify_style(source_name: str, crop_rgb: np.ndarray) -> str:
    s = source_name.lower()
    if any(k in s for k in ["kids", "kid", "unicorn", "magic", "cute", "baby"]):
        return "kids"
    if any(k in s for k in ["wedding", "bride", "floral", "script", "love"]):
        return "wedding"
    if any(k in s for k in ["luxury", "gold", "premium", "royal"]):
        return "luxury"
    if any(k in s for k in ["retro", "vintage"]):
        return "retro"
    if any(k in s for k in ["gothic", "dark", "blackletter"]):
        return "gothic"
    if any(k in s for k in ["hand", "handwritten"]):
        return "handwritten"
    if any(k in s for k in ["feminine", "pink", "girly"]):
        return "feminine"

    hsv = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2HSV)
    mean_s = float(np.mean(hsv[:, :, 1]))
    mean_v = float(np.mean(hsv[:, :, 2]))
    if mean_s > 70 and mean_v > 150:
        return "kids"
    if mean_v < 90:
        return "gothic"
    if mean_s < 35:
        return "minimal"
    return "handwritten"


def _template_for_mode(mode: str, style: str) -> str:
    if mode == "fallback_mode":
        return "framed_catalog"
    if mode == "extract_mode":
        return "centered_card"
    if mode == "card_mode" and style in {"kids", "feminine"}:
        return "sticker_center"
    if mode == "card_mode" and style in {"luxury", "minimal"}:
        return "top_card"
    return "centered_card"


def _layout_from_template(template: str, mode: str) -> dict:
    if mode == "fallback_mode":
        return {
            "template": "framed_catalog",
            "rotation_deg": 0.0,
            "scale_pct": 70,
            "position_y": "center",
            "corner_radius": 24,
            "shadow": True,
            "frame": True,
        }
    if template == "sticker_center":
        return {
            "template": template,
            "rotation_deg": -1.2,
            "scale_pct": 74,
            "position_y": "upper_middle",
            "corner_radius": 22,
            "shadow": True,
            "frame": False,
        }
    if template == "top_card":
        return {
            "template": template,
            "rotation_deg": 0.0,
            "scale_pct": 72,
            "position_y": "top",
            "corner_radius": 18,
            "shadow": True,
            "frame": False,
        }
    if template == "bottom_card":
        return {
            "template": template,
            "rotation_deg": 0.0,
            "scale_pct": 72,
            "position_y": "bottom",
            "corner_radius": 18,
            "shadow": True,
            "frame": False,
        }
    return {
        "template": "centered_card",
        "rotation_deg": 0.0,
        "scale_pct": 76 if mode == "extract_mode" else 72,
        "position_y": "center",
        "corner_radius": 18,
        "shadow": True,
        "frame": False,
    }


def _theme_palette(style: str) -> list[tuple[int, int, int]]:
    theme = STYLE_THEME_MAP.get(style, "clean_neutral")
    if theme == "playful_bright":
        return [(243, 217, 228), (177, 79, 156), (95, 110, 220)]
    if theme == "soft_floral":
        return [(246, 234, 228), (213, 173, 173), (186, 160, 197)]
    if theme == "dark_editorial":
        return [(27, 31, 41), (108, 90, 62), (62, 70, 84)]
    if theme == "warm_vintage":
        return [(226, 203, 170), (174, 130, 88), (112, 84, 60)]
    if theme == "paper_soft":
        return [(244, 238, 227), (211, 196, 168), (157, 140, 114)]
    if theme == "pink_cream":
        return [(250, 229, 236), (226, 165, 190), (208, 190, 170)]
    if theme == "dark_fog":
        return [(28, 30, 40), (70, 74, 92), (122, 126, 145)]
    return [(236, 236, 236), (184, 190, 199), (145, 151, 161)]


def _render_background(style: str, seed: int) -> Image.Image:
    rng = np.random.default_rng(seed)
    pal = _theme_palette(style)
    h, w = PIN_H, PIN_W
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    xx = xx / float(max(1, w - 1))
    yy = yy / float(max(1, h - 1))
    rad = np.sqrt((xx - 0.5) ** 2 + (yy - 0.4) ** 2)
    t = np.clip(0.6 * xx + 0.4 * (1.0 - rad / 0.9), 0.0, 1.0)
    arr = np.zeros((h, w, 3), dtype=np.float32)
    for c in range(3):
        arr[:, :, c] = pal[0][c] * (1.0 - t) + pal[1][c] * t
    arr += rng.normal(0.0, 4.5, size=(h, w, 1))
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    base = Image.fromarray(arr, mode="RGB").convert("RGBA")

    decor = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(decor)
    for _ in range(5):
        cx = int(rng.integers(int(0.1 * w), int(0.9 * w)))
        cy = int(rng.integers(int(0.1 * h), int(0.9 * h)))
        r = int(rng.integers(int(0.1 * w), int(0.24 * w)))
        c = pal[int(rng.integers(0, len(pal)))]
        a = int(rng.integers(16, 46))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(c[0], c[1], c[2], a))
    decor = decor.filter(ImageFilter.GaussianBlur(radius=38))
    base.alpha_composite(decor)
    return base.convert("RGB")


def _rounded_card(img: Image.Image, radius: int) -> Image.Image:
    rgba = img.convert("RGBA")
    w, h = rgba.size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, w, h], radius=radius, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(rgba, (0, 0), mask)
    return out


def _extract_alpha_bbox(alpha: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(alpha > 0)
    if ys.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _apply_shadow(canvas: Image.Image, obj: Image.Image, x: int, y: int, blur: int = 14, alpha: int = 105) -> None:
    a = np.array(obj.convert("RGBA"))[:, :, 3]
    if np.count_nonzero(a > 0) == 0:
        return
    shadow = Image.new("RGBA", obj.size, (0, 0, 0, 0))
    sh_arr = np.array(shadow)
    sh_arr[:, :, 3] = (a * (alpha / 255.0)).astype(np.uint8)
    shadow = Image.fromarray(sh_arr, mode="RGBA").filter(ImageFilter.GaussianBlur(radius=blur))
    canvas.alpha_composite(shadow, (x + 8, y + 10))


def _paste_layout(canvas: Image.Image, obj: Image.Image, layout: dict) -> None:
    template = layout["template"]
    scale_pct = int(layout["scale_pct"])
    rotation = float(layout["rotation_deg"])
    shadow = bool(layout["shadow"])
    frame = bool(layout["frame"])

    ow, oh = obj.size
    target_w = int(PIN_W * max(0.45, min(0.88, scale_pct / 100.0)))
    k = target_w / float(max(1, ow))
    nw = max(1, int(round(ow * k)))
    nh = max(1, int(round(oh * k)))
    if nh > int(PIN_H * 0.66):
        kk = (PIN_H * 0.66) / float(nh)
        nw = max(1, int(round(nw * kk)))
        nh = max(1, int(round(nh * kk)))

    obj = obj.resize((nw, nh), Image.Resampling.LANCZOS)
    if rotation != 0:
        obj = obj.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True)
        nw, nh = obj.size

    if frame:
        framed = Image.new("RGBA", (nw + 28, nh + 28), (255, 255, 255, 0))
        d = ImageDraw.Draw(framed)
        d.rounded_rectangle([0, 0, nw + 27, nh + 27], radius=24, fill=(255, 255, 255, 210), outline=(235, 235, 235, 255), width=2)
        framed.alpha_composite(obj, (14, 14))
        obj = framed
        nw, nh = obj.size

    py_map = {
        "top": int(PIN_H * 0.18),
        "upper_middle": int(PIN_H * 0.24),
        "center": int((PIN_H - nh) // 2),
        "bottom": int(PIN_H * 0.54),
    }
    py = py_map.get(layout["position_y"], py_map["center"])
    py = max(20, min(PIN_H - nh - 20, py))
    px = (PIN_W - nw) // 2

    if shadow:
        _apply_shadow(canvas, obj, px, py)
    canvas.alpha_composite(obj, (px, py))

    if template == "sticker_center":
        d = ImageDraw.Draw(canvas)
        d.rounded_rectangle(
            [max(12, px - 12), max(12, py - 12), min(PIN_W - 12, px + nw + 12), min(PIN_H - 12, py + nh + 12)],
            radius=18,
            outline=(255, 255, 255, 180),
            width=3,
        )


def _crop_bbox(img: Image.Image, bbox: dict) -> Image.Image:
    w, h = img.size
    x = max(0, min(w - 1, int(bbox["x"])))
    y = max(0, min(h - 1, int(bbox["y"])))
    bw = max(1, min(w - x, int(bbox["width"])))
    bh = max(1, min(h - y, int(bbox["height"])))
    return img.crop((x, y, x + bw, y + bh))


def _edge_noise_ratio(mask: np.ndarray) -> float:
    if np.count_nonzero(mask > 0) == 0:
        return 1.0
    h, w = mask.shape
    m = (mask > 0).astype(np.uint8)
    band = max(2, int(min(h, w) * 0.03))
    edge_band = np.zeros_like(m)
    edge_band[:band, :] = 1
    edge_band[h - band :, :] = 1
    edge_band[:, :band] = 1
    edge_band[:, w - band :] = 1
    noise = np.count_nonzero((m > 0) & (edge_band > 0))
    fg = np.count_nonzero(m > 0)
    return float(noise / max(1, fg))


def _extract_success(extract_report: dict, mask_path: Path, preview_bbox_w: int) -> bool:
    fg_ratio = float(extract_report.get("foreground_ratio", 0.0))
    try:
        mask = np.array(Image.open(mask_path).convert("L"))
    except Exception:
        return False
    edge_noise = _edge_noise_ratio(mask)
    ys, xs = np.where(mask > 0)
    if ys.size == 0:
        return False
    obj_w = int(xs.max()) - int(xs.min()) + 1
    width_ratio = float(obj_w / max(1, preview_bbox_w))
    readable = float(extract_report.get("quality_score", 0.0)) >= 0.58
    return (
        0.15 <= fg_ratio <= 0.75
        and edge_noise <= 0.08
        and width_ratio >= 0.35
        and readable
    )


def _choose_mode(bbox: dict, det_conf: float, complexity: float, quality: float, blur: float, img_area: int) -> tuple[str, str]:
    bw = int(bbox["width"])
    bh = int(bbox["height"])
    bbox_area_ratio = (bw * bh) / float(max(1, img_area))
    if bw < 250 or bh < 150:
        return "reject_mode", "bbox_too_small"
    if blur < 0.10:
        return "reject_mode", "blur_too_high"
    if bbox_area_ratio < 0.12:
        return "reject_mode", "preview_area_too_small"
    if bbox_area_ratio < 0.20 and det_conf < 0.70:
        return "reject_mode", "preview_area_too_small"
    if det_conf < 0.45:
        return "reject_mode", "bbox_confidence_low"

    if (0.45 <= det_conf <= 0.65) or complexity > 0.75 or quality < 0.60:
        return "fallback_mode", "fallback_rules"
    return "card_mode", ""


def _build_meta(
    source: Path,
    bbox: dict,
    mode: str,
    style: str,
    complexity: float,
    quality: float,
    layout: dict,
    status: str,
    reject_reason: str = "",
) -> dict:
    theme = STYLE_THEME_MAP.get(style, "clean_neutral")
    return {
        "source_file": source.name,
        "bbox": bbox,
        "mode": mode,
        "style": style,
        "complexity_score": round(complexity, 4),
        "quality_score": round(quality, 4),
        "template_used": layout["template"],
        "background_theme": theme,
        "rotation_deg": float(layout["rotation_deg"]),
        "scale_pct": int(layout["scale_pct"]),
        "layout": layout,
        "status": status,
        "reject_reason": reject_reason,
    }


def run_auto_pin_batch(input_path: str | Path, output_root: str | Path = "output") -> list[AutoPinResult]:
    src_path = Path(input_path)
    out_root = Path(output_root)
    out_root.mkdir(parents=True, exist_ok=True)
    files = _iter_input_files(src_path)
    results: list[AutoPinResult] = []

    for src in files:
        font_id = src.stem
        out_dir = out_root / font_id / "auto_pin"
        out_dir.mkdir(parents=True, exist_ok=True)
        pin_png = out_dir / "pin_01.png"
        pin_jpg = out_dir / "pin_01.jpg"
        meta_json = out_dir / "meta.json"

        img = Image.open(src).convert("RGB")
        img_rgb = np.array(img)
        h, w = img_rgb.shape[:2]
        bbox, det_conf = _detect_preview_bbox(img_rgb)
        crop = _crop_bbox(img, bbox)
        crop_rgb = np.array(crop.convert("RGB"))
        style = _classify_style(src.stem, crop_rgb)
        complexity = _complexity_score(crop_rgb)
        quality = _quality_score(crop_rgb)
        blur = _blur_score(crop_rgb)

        mode, reason = _choose_mode(
            bbox=bbox,
            det_conf=det_conf,
            complexity=complexity,
            quality=quality,
            blur=blur,
            img_area=h * w,
        )

        simple_bg = _estimate_simple_background(crop_rgb)
        if mode == "card_mode" and simple_bg and quality >= 0.68 and complexity <= 0.72 and det_conf >= 0.66:
            mode = "extract_mode"

        template = _template_for_mode(mode, style)
        layout = _layout_from_template(template, mode)

        if mode == "reject_mode":
            rejected_copy = out_dir / f"source_original{src.suffix.lower()}"
            shutil.copy2(src, rejected_copy)
            meta = _build_meta(src, bbox, mode, style, complexity, quality, layout, "rejected", reason)
            meta_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append(
                AutoPinResult(
                    source_file=str(src),
                    status="rejected",
                    mode=mode,
                    style=style,
                    bbox=bbox,
                    complexity_score=complexity,
                    quality_score=quality,
                    template_used=layout["template"],
                    background_theme=STYLE_THEME_MAP.get(style, "clean_neutral"),
                    rotation_deg=float(layout["rotation_deg"]),
                    scale_pct=int(layout["scale_pct"]),
                    pin_png="",
                    pin_jpg="",
                    meta_json=str(meta_json),
                    reject_reason=reason,
                )
            )
            logger.info("[%s] status=rejected reason=%s", font_id, reason)
            continue

        bg = _render_background(style=style, seed=(abs(hash(src.name)) % (2**31)))
        canvas = bg.convert("RGBA")

        object_img: Image.Image
        final_mode = mode
        if mode == "extract_mode":
            # Run extraction on original image (already includes robust crop/mask logic).
            extraction = extract_overlay(src, output_root=out_root)
            report = json.loads(Path(extraction.report_path).read_text(encoding="utf-8"))
            ok = _extract_success(report, Path(extraction.mask_path), preview_bbox_w=bbox["width"])
            if not ok:
                final_mode = "card_mode"
                template = _template_for_mode(final_mode, style)
                layout = _layout_from_template(template, final_mode)
                object_img = _rounded_card(crop, radius=int(layout["corner_radius"]))
            else:
                object_img = Image.open(extraction.overlay_path).convert("RGBA")
        elif mode == "fallback_mode":
            object_img = _rounded_card(crop, radius=24)
        else:
            object_img = _rounded_card(crop, radius=int(layout["corner_radius"]))

        _paste_layout(canvas, object_img, layout)

        canvas.convert("RGB").save(pin_png, "PNG")
        canvas.convert("RGB").save(pin_jpg, "JPEG", quality=94, optimize=True)

        meta = _build_meta(
            source=src,
            bbox=bbox,
            mode=final_mode,
            style=style,
            complexity=complexity,
            quality=quality,
            layout=layout,
            status="generated",
        )
        meta_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append(
            AutoPinResult(
                source_file=str(src),
                status="generated",
                mode=final_mode,
                style=style,
                bbox=bbox,
                complexity_score=complexity,
                quality_score=quality,
                template_used=layout["template"],
                background_theme=STYLE_THEME_MAP.get(style, "clean_neutral"),
                rotation_deg=float(layout["rotation_deg"]),
                scale_pct=int(layout["scale_pct"]),
                pin_png=str(pin_png),
                pin_jpg=str(pin_jpg),
                meta_json=str(meta_json),
                reject_reason="",
            )
        )
        logger.info(
            "[%s] status=generated mode=%s style=%s template=%s q=%.3f c=%.3f",
            font_id,
            final_mode,
            style,
            layout["template"],
            quality,
            complexity,
        )

    report = {
        "total": len(results),
        "generated": sum(1 for r in results if r.status == "generated"),
        "rejected": sum(1 for r in results if r.status == "rejected"),
        "by_mode": {
            "extract_mode": sum(1 for r in results if r.mode == "extract_mode"),
            "card_mode": sum(1 for r in results if r.mode == "card_mode"),
            "fallback_mode": sum(1 for r in results if r.mode == "fallback_mode"),
            "reject_mode": sum(1 for r in results if r.mode == "reject_mode"),
        },
        "items": [asdict(r) for r in results],
    }
    rep_dir = out_root / "_reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / "auto_pin_batch_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Auto-pin report saved: %s", rep_dir / "auto_pin_batch_report.json")
    return results
