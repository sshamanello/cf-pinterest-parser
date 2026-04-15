import argparse
import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from rembg import remove

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


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
    """Detect failure mode when background was not removed and full card remains foreground."""
    return _foreground_ratio(mask) >= 0.82


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
            mask = _strict_letter_mask(rgb, simple_bg=simple_bg)
    except Exception as exc:
        logger.warning("[%s] rembg unavailable/failed (%s). Using strict CV mask only.", font_id, exc)
        mask = _strict_letter_mask(rgb, simple_bg=simple_bg)

    rgba_np[:, :, 3] = mask
    overlay = Image.fromarray(rgba_np, mode="RGBA")
    mask_image = Image.fromarray(mask, mode="L")

    quality, fg_ratio, tr_ratio = _compute_quality(mask)

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

    save_dir = out_dir
    if needs_manual_check:
        save_dir = out_dir / "manual_check"
        save_dir.mkdir(parents=True, exist_ok=True)

    overlay_path = save_dir / "extracted_overlay.png"
    mask_path = save_dir / "mask.png"
    report_path = save_dir / "extraction_report.json"

    overlay.save(overlay_path, "PNG")
    mask_image.save(mask_path, "PNG")
    _save_report(
        report_path,
        {
            "font_id": font_id,
            "source_path": str(src),
            "overlay_path": str(overlay_path),
            "mask_path": str(mask_path),
            "quality_score": round(quality, 4),
            "foreground_ratio": round(fg_ratio, 4),
            "transparency_ratio": round(tr_ratio, 4),
            "simple_background": simple_bg,
            "needs_manual_check": needs_manual_check,
            "manual_reason": manual_reason,
        },
    )

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
