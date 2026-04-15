import json
import logging
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path

from PIL import Image, ImageFilter

from extractor import extract_overlay

logger = logging.getLogger(__name__)

GEN_MODES = {"signature_lock", "full_regen", "hybrid"}


@dataclass
class FontGenerationResult:
    font_id: str
    mode: str
    source_preview_path: str
    source_overlay_path: str
    output_wordmark_path: str
    output_report_path: str
    category: str
    font_name: str
    used_fallback: bool
    fallback_reason: str


def _apply_readability_effects(wordmark_path: Path) -> None:
    """
    Minimal post-effects that improve readability while preserving glyph shape.
    """
    img = Image.open(wordmark_path).convert("RGBA")

    alpha = img.split()[3]
    shadow = alpha.filter(ImageFilter.GaussianBlur(radius=4))
    glow = alpha.filter(ImageFilter.GaussianBlur(radius=2))

    shadow_rgba = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow_rgba.putalpha(shadow)
    glow_rgba = Image.new("RGBA", img.size, (255, 255, 255, 0))
    glow_rgba.putalpha(glow)

    # Compose: soft shadow -> glow -> original glyph
    canvas = Image.new("RGBA", img.size, (0, 0, 0, 0))
    canvas.alpha_composite(shadow_rgba)
    canvas.alpha_composite(glow_rgba)
    canvas.alpha_composite(img)
    canvas.save(wordmark_path, "PNG")


def generate_font_asset(
    source_preview_path: str | Path,
    output_root: str | Path,
    font_name: str,
    category: str,
    mode: str = "signature_lock",
) -> FontGenerationResult:
    """
    Step-1 generator:
      - signature_lock: preserve extracted glyph shape and apply readability effects.
      - full_regen / hybrid: currently fallback to signature_lock scaffolding.
    """
    if mode not in GEN_MODES:
        raise ValueError(f"Unsupported mode '{mode}'. Allowed: {sorted(GEN_MODES)}")

    src = Path(source_preview_path)
    if not src.exists():
        raise FileNotFoundError(f"Input file not found: {src}")

    font_id = src.stem
    out_dir = Path(output_root) / font_id
    out_dir.mkdir(parents=True, exist_ok=True)

    extraction = extract_overlay(src, output_root=output_root)
    source_overlay = Path(extraction.overlay_path)
    generated_wordmark = out_dir / "generated_wordmark.png"

    used_fallback = False
    fallback_reason = ""
    effective_mode = mode

    # Current stable implementation uses signature-lock layer.
    if mode == "signature_lock":
        shutil.copy2(source_overlay, generated_wordmark)
    else:
        shutil.copy2(source_overlay, generated_wordmark)
        used_fallback = True
        fallback_reason = "mode_not_implemented_yet_using_signature_lock"
        effective_mode = "signature_lock"

    _apply_readability_effects(generated_wordmark)

    report = {
        "font_id": font_id,
        "font_name": font_name,
        "category": category,
        "requested_mode": mode,
        "effective_mode": effective_mode,
        "used_fallback": used_fallback,
        "fallback_reason": fallback_reason,
        "source_preview_path": str(src),
        "source_overlay_path": str(source_overlay),
        "generated_wordmark_path": str(generated_wordmark),
    }
    report_path = out_dir / "font_generation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(
        "[%s] generated mode=%s effective=%s fallback=%s",
        font_id,
        mode,
        effective_mode,
        used_fallback,
    )

    return FontGenerationResult(
        font_id=font_id,
        mode=effective_mode,
        source_preview_path=str(src),
        source_overlay_path=str(source_overlay),
        output_wordmark_path=str(generated_wordmark),
        output_report_path=str(report_path),
        category=category,
        font_name=font_name,
        used_fallback=used_fallback,
        fallback_reason=fallback_reason,
    )


def result_to_dict(result: FontGenerationResult) -> dict:
    return asdict(result)
