import json
import logging
import os
import shutil
import time
import uuid
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

import requests
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
    similarity_score: float


COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
COMFY_WORKFLOW_PATH = os.environ.get("COMFY_WORKFLOW_PATH", "/Users/nick/Downloads/DreamShaperXL.json")
COMFY_TIMEOUT_SEC = int(os.environ.get("COMFY_TIMEOUT_SEC", "180"))
REGEN_MIN_SIMILARITY = float(os.environ.get("REGEN_MIN_SIMILARITY", "0.62"))
REGEN_MIN_ASPECT_RATIO_MATCH = float(os.environ.get("REGEN_MIN_ASPECT_RATIO_MATCH", "0.55"))
REGEN_MIN_FOREGROUND_RATIO_MATCH = float(os.environ.get("REGEN_MIN_FOREGROUND_RATIO_MATCH", "0.35"))
REGEN_MIN_COMPONENT_RATIO_MATCH = float(os.environ.get("REGEN_MIN_COMPONENT_RATIO_MATCH", "0.20"))


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


def _comfy_available() -> bool:
    try:
        r = requests.get(f"{COMFY_URL}/system_stats", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _load_workflow_graph(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Comfy workflow not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _build_positive_prompt(font_name: str, category: str) -> str:
    return (
        f"typography wordmark for '{font_name}', {category} style, full readable word, "
        "flat 2d lettering, high-contrast dark text on light plain background, centered composition, "
        "clean edges, no decorative objects"
    )


def _build_negative_prompt() -> str:
    return (
        "single letter, monogram, white text on white background, low contrast, embossed text, 3d text, "
        "watermark, logo, signature, frame, border, low quality, blurry, distorted, illegible text"
    )


def _parse_workflow_template(graph: dict, positive_prompt: str, negative_prompt: str) -> dict:
    """
    Parse LiteGraph JSON exported by ComfyUI and produce API /prompt payload.
    This parser is scoped to the DreamShaperXL workflow structure currently used.
    """
    nodes = graph.get("nodes", [])
    by_type: dict[str, list[dict]] = {}
    for n in nodes:
        by_type.setdefault(n.get("type", ""), []).append(n)

    ckpt = by_type.get("CheckpointLoaderSimple", [{}])[0].get("widgets_values", ["DreamShaperXL_Lightning.safetensors"])[0]
    vae_name = by_type.get("VAELoader", [{}])[0].get("widgets_values", ["sdxl_vae.safetensors"])[0]

    # Keep sampler settings from current workflow by default.
    ks_widgets = by_type.get("KSampler", [{}])[0].get("widgets_values", [int(uuid.uuid4().int % (2**31)), "randomize", 8, 1.5, "dpmpp_sde", "sgm_uniform", 1])
    seed = int(uuid.uuid4().int % (2**31))
    steps = int(ks_widgets[2])
    cfg = float(ks_widgets[3])
    sampler_name = str(ks_widgets[4])
    scheduler = str(ks_widgets[5])
    denoise = float(ks_widgets[6])

    latent_widgets = by_type.get("EmptyLatentImage", [{}])[0].get("widgets_values", [832, 1248, 1])
    width = int(latent_widgets[0])
    height = int(latent_widgets[1])
    batch_size = int(latent_widgets[2]) if len(latent_widgets) >= 3 else 1

    save_prefix = by_type.get("SaveImage", [{}])[0].get("widgets_values", ["ComfyUI"])[0]
    save_prefix = f"{save_prefix}_font_regen"

    # Build Comfy API graph.
    prompt = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "8": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": positive_prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": batch_size}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler_name,
                "scheduler": scheduler,
                "denoise": denoise,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["8", 0]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": save_prefix}},
    }
    return prompt


def _queue_prompt(prompt_graph: dict) -> str:
    payload = json.dumps({"prompt": prompt_graph}).encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["prompt_id"]


def _wait_for_output(prompt_id: str, timeout_sec: int = 180) -> dict | None:
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


def _download_first_output_image(outputs: dict, save_path: Path) -> bool:
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
                save_path.write_bytes(r.content)
                return True
            except Exception:
                continue
    return False


def _ahash_rgba_alpha(path: Path, size: int = 16) -> int:
    img = Image.open(path).convert("RGBA")
    alpha = img.split()[3].resize((size, size), Image.LANCZOS)
    px = list(alpha.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for i, p in enumerate(px):
        if p >= avg:
            bits |= (1 << i)
    return bits


def _hamming_similarity(a: int, b: int, bits: int = 256) -> float:
    dist = (a ^ b).bit_count()
    return max(0.0, 1.0 - (dist / bits))


def _safe_aspect_from_bbox(bbox_norm: dict) -> float:
    w = float(bbox_norm.get("w", 0.0))
    h = float(bbox_norm.get("h", 0.0))
    if w <= 0.0 or h <= 0.0:
        return 0.0
    return w / h


def _ratio_match(a: float, b: float) -> float:
    if a <= 0.0 or b <= 0.0:
        return 0.0
    lo = min(a, b)
    hi = max(a, b)
    return lo / hi


def _validate_regen_similarity(
    source_extraction,
    regen_extraction,
    source_overlay: Path,
    regen_overlay: Path,
) -> tuple[bool, float, str]:
    src_hash = _ahash_rgba_alpha(source_overlay)
    regen_hash = _ahash_rgba_alpha(regen_overlay)
    similarity = _hamming_similarity(src_hash, regen_hash)
    if similarity < REGEN_MIN_SIMILARITY:
        return False, similarity, f"regen_similarity_low:{similarity:.3f}"

    src_aspect = _safe_aspect_from_bbox(source_extraction.bbox_norm)
    regen_aspect = _safe_aspect_from_bbox(regen_extraction.bbox_norm)
    aspect_match = _ratio_match(src_aspect, regen_aspect)
    if aspect_match < REGEN_MIN_ASPECT_RATIO_MATCH:
        return False, similarity, f"regen_aspect_mismatch:{aspect_match:.3f}"

    fg_match = _ratio_match(float(source_extraction.foreground_ratio), float(regen_extraction.foreground_ratio))
    if fg_match < REGEN_MIN_FOREGROUND_RATIO_MATCH:
        return False, similarity, f"regen_foreground_mismatch:{fg_match:.3f}"

    src_comp = float(source_extraction.qc_metrics.get("component_count", 1) or 1)
    regen_comp = float(regen_extraction.qc_metrics.get("component_count", 1) or 1)
    comp_match = _ratio_match(src_comp, regen_comp)
    if comp_match < REGEN_MIN_COMPONENT_RATIO_MATCH:
        return False, similarity, f"regen_component_mismatch:{comp_match:.3f}"

    return True, similarity, ""


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

    source_extraction = extract_overlay(src, output_root=output_root)
    source_overlay = Path(source_extraction.overlay_path)
    generated_wordmark = out_dir / "generated_wordmark.png"

    used_fallback = False
    fallback_reason = ""
    effective_mode = mode
    similarity_score = 0.0

    # Stable signature-lock path.
    if mode == "signature_lock":
        shutil.copy2(source_overlay, generated_wordmark)
    else:
        full_regen_ok = False
        regen_overlay_path = None
        regen_reason = ""

        if not _comfy_available():
            regen_reason = "comfy_unavailable"
        else:
            try:
                graph = _load_workflow_graph(COMFY_WORKFLOW_PATH)
                prompt_graph = _parse_workflow_template(
                    graph,
                    positive_prompt=_build_positive_prompt(font_name=font_name, category=category),
                    negative_prompt=_build_negative_prompt(),
                )
                prompt_id = _queue_prompt(prompt_graph)
                outputs = _wait_for_output(prompt_id, timeout_sec=COMFY_TIMEOUT_SEC)
                if not outputs:
                    regen_reason = "comfy_timeout"
                else:
                    raw_img_path = out_dir / "full_regen_raw.png"
                    if not _download_first_output_image(outputs, raw_img_path):
                        regen_reason = "comfy_download_failed"
                    else:
                        regen_extraction = extract_overlay(raw_img_path, output_root=output_root)
                        regen_overlay_path = Path(regen_extraction.overlay_path)
                        regen_ok, similarity_score, regen_reason = _validate_regen_similarity(
                            source_extraction=source_extraction,
                            regen_extraction=regen_extraction,
                            source_overlay=source_overlay,
                            regen_overlay=regen_overlay_path,
                        )
                        if regen_extraction.needs_manual_check:
                            regen_reason = f"regen_overlay_low_quality:{regen_extraction.manual_reason or 'manual_check'}"
                        elif not regen_ok:
                            pass
                        else:
                            full_regen_ok = True
            except Exception as exc:
                regen_reason = f"full_regen_exception:{exc}"

        if mode == "full_regen":
            if full_regen_ok and regen_overlay_path is not None:
                shutil.copy2(regen_overlay_path, generated_wordmark)
                effective_mode = "full_regen"
            else:
                shutil.copy2(source_overlay, generated_wordmark)
                effective_mode = "signature_lock"
                used_fallback = True
                fallback_reason = regen_reason or "full_regen_failed"

        elif mode == "hybrid":
            if full_regen_ok and regen_overlay_path is not None:
                shutil.copy2(regen_overlay_path, generated_wordmark)
                effective_mode = "full_regen"
            else:
                shutil.copy2(source_overlay, generated_wordmark)
                effective_mode = "signature_lock"
                used_fallback = True
                fallback_reason = regen_reason or "hybrid_full_regen_failed"

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
        "comfy_url": COMFY_URL,
        "comfy_workflow_path": COMFY_WORKFLOW_PATH,
        "comfy_timeout_sec": COMFY_TIMEOUT_SEC,
        "regen_min_similarity": REGEN_MIN_SIMILARITY,
        "regen_min_aspect_ratio_match": REGEN_MIN_ASPECT_RATIO_MATCH,
        "regen_min_foreground_ratio_match": REGEN_MIN_FOREGROUND_RATIO_MATCH,
        "regen_min_component_ratio_match": REGEN_MIN_COMPONENT_RATIO_MATCH,
        "similarity_score": round(similarity_score, 4),
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
        similarity_score=similarity_score,
    )


def result_to_dict(result: FontGenerationResult) -> dict:
    return asdict(result)
