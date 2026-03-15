"""
ComfyUI img2img processor.

Pipeline:
  1. Download original CF product image
  2. Upload to ComfyUI → run img2img (denoising ~0.5) → download result
  3. Apply Pillow overlay (title, niche badge, CTA) → save to output/{slug}.jpg

Requirements:
  - ComfyUI running locally: python main.py --listen
  - Default URL: http://127.0.0.1:8188
  - A SD 1.5 checkpoint in ComfyUI/models/checkpoints/
  - Set COMFY_MODEL in .env to the exact filename (e.g. realisticVisionV51.safetensors)
"""

import io
import json
import logging
import os
import time
import urllib.request
import urllib.parse
import uuid
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
import textwrap

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
COMFY_URL   = os.environ.get("COMFY_URL",   "http://127.0.0.1:8188")
COMFY_MODEL = os.environ.get("COMFY_MODEL", "realisticVisionV51.safetensors")
DENOISE     = float(os.environ.get("COMFY_DENOISE", "0.50"))  # 0.4–0.65 recommended
STEPS       = int(os.environ.get("COMFY_STEPS",   "20"))
CFG         = float(os.environ.get("COMFY_CFG",   "7.0"))

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# Pinterest size
PIN_W, PIN_H = 1000, 1500

# Colours for Pillow overlay
BG_BOTTOM = (30,  30,  30)
ACCENT    = (255, 87,  34)
WHITE     = (255, 255, 255)
LIGHT     = (220, 220, 220)
BG_TOP    = (245, 245, 245)

# ── Prompts per niche ─────────────────────────────────────────────────────────
NICHE_PROMPTS = {
    "fonts": (
        "professional font specimen poster, clean white background, "
        "elegant typography showcase, high quality product photo, Pinterest"
    ),
    "graphics": (
        "digital art product mockup, clean background, "
        "professional graphic design showcase, high quality"
    ),
    "3d-svg": (
        "3d paper craft product photo, clean white background, "
        "handmade art showcase, professional photo"
    ),
    "3d-printing": (
        "3d printed product on clean white background, "
        "professional product photo, studio lighting"
    ),
    "embroidery": (
        "embroidery design on fabric, clean background, "
        "handcraft showcase, professional product photo"
    ),
    "laser-cutting": (
        "laser cut wood product, clean background, "
        "handcraft art showcase, professional studio photo"
    ),
    "bundles": (
        "digital design bundle mockup, clean white background, "
        "professional product showcase, high quality"
    ),
}
DEFAULT_PROMPT = (
    "professional product photo, clean white background, "
    "high quality, Pinterest style"
)
NEGATIVE_PROMPT = (
    "blurry, low quality, watermark, text overlay, bad composition, "
    "oversaturated, deformed"
)


# ── ComfyUI helpers ───────────────────────────────────────────────────────────

def _comfy_available() -> bool:
    """Quick check: is ComfyUI running?"""
    try:
        r = requests.get(f"{COMFY_URL}/system_stats", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _upload_image(img: Image.Image, filename: str) -> str:
    """Upload a PIL Image to ComfyUI and return the server filename."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    response = requests.post(
        f"{COMFY_URL}/upload/image",
        files={"image": (filename, buf, "image/png")},
        data={"overwrite": "true"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["name"]


def _build_workflow(input_filename: str, positive_prompt: str) -> dict:
    """
    Build a ComfyUI img2img workflow dict.
    Node IDs are arbitrary but must be consistent within the dict.
    """
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": COMFY_MODEL},
        },
        "2": {
            "class_type": "LoadImage",
            "inputs": {"image": input_filename},
        },
        "3": {
            "class_type": "VAEEncode",
            "inputs": {
                "pixels": ["2", 0],
                "vae":    ["1", 2],
            },
        },
        "4": {
            "class_type": "KSampler",
            "inputs": {
                "model":          ["1", 0],
                "positive":       ["5", 0],
                "negative":       ["6", 0],
                "latent_image":   ["3", 0],
                "seed":           int(uuid.uuid4()) % (2**31),
                "steps":          STEPS,
                "cfg":            CFG,
                "sampler_name":   "euler_ancestral",
                "scheduler":      "karras",
                "denoise":        DENOISE,
            },
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": positive_prompt,
                "clip": ["1", 1],
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": NEGATIVE_PROMPT,
                "clip": ["1", 1],
            },
        },
        "7": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["4", 0],
                "vae":     ["1", 2],
            },
        },
        "8": {
            "class_type": "SaveImage",
            "inputs": {
                "images":   ["7", 0],
                "filename_prefix": "cf_pin",
            },
        },
    }


def _queue_prompt(workflow: dict) -> str:
    """Send workflow to ComfyUI queue, return prompt_id."""
    payload = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["prompt_id"]


def _wait_for_result(prompt_id: str, timeout: int = 120) -> dict | None:
    """Poll /history until the prompt is done. Returns output node data."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            history = r.json()
            if prompt_id in history:
                outputs = history[prompt_id].get("outputs", {})
                return outputs
        except Exception:
            pass
        time.sleep(2)
    return None


def _download_result(outputs: dict) -> Image.Image | None:
    """Find the first saved image in outputs and download it."""
    for node_output in outputs.values():
        images = node_output.get("images", [])
        for img_info in images:
            params = urllib.parse.urlencode({
                "filename": img_info["filename"],
                "subfolder": img_info.get("subfolder", ""),
                "type": img_info.get("type", "output"),
            })
            url = f"{COMFY_URL}/view?{params}"
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                return Image.open(io.BytesIO(r.content)).convert("RGB")
            except Exception as exc:
                logger.warning("Could not download result image: %s", exc)
    return None


def _run_img2img(source: Image.Image, niche: str) -> Image.Image | None:
    """
    Send source image through ComfyUI img2img.
    Returns the AI-processed image, or None on failure.
    """
    tmp_name = f"cf_input_{uuid.uuid4().hex[:8]}.png"
    prompt = NICHE_PROMPTS.get(niche, DEFAULT_PROMPT)

    try:
        server_name = _upload_image(source, tmp_name)
        workflow = _build_workflow(server_name, prompt)
        prompt_id = _queue_prompt(workflow)
        logger.debug("ComfyUI job queued: %s", prompt_id)

        outputs = _wait_for_result(prompt_id, timeout=120)
        if not outputs:
            logger.warning("ComfyUI timed out for prompt %s", prompt_id)
            return None

        return _download_result(outputs)

    except Exception as exc:
        logger.warning("ComfyUI img2img failed: %s", exc)
        return None


# ── Pillow overlay ────────────────────────────────────────────────────────────

def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "arialbd.ttf" if bold else "arial.ttf",
        "Arial Bold.ttf" if bold else "Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def _apply_overlay(img: Image.Image, title: str, niche: str) -> Image.Image:
    """
    Compose final Pinterest pin:
      - top 65%: AI-processed product image
      - bottom 35%: dark bar with niche badge, title, CTA
    """
    canvas = Image.new("RGB", (PIN_W, PIN_H), BG_TOP)
    draw = ImageDraw.Draw(canvas)

    top_h = int(PIN_H * 0.65)

    # Product image
    fitted = img.copy()
    fitted.thumbnail((PIN_W - 60, top_h - 60), Image.LANCZOS)
    x = (PIN_W - fitted.width) // 2
    y = (top_h - fitted.height) // 2
    canvas.paste(fitted, (x, y))

    # Dark bar
    draw.rectangle([0, top_h, PIN_W, PIN_H], fill=BG_BOTTOM)
    draw.rectangle([0, top_h, PIN_W, top_h + 5], fill=ACCENT)  # accent line

    # Niche badge
    badge_font = _load_font(22)
    label = niche.upper().replace("-", " ")
    bx, by, bw, bh = 40, top_h + 28, 160, 36
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=6, fill=ACCENT)
    draw.text((bx + bw // 2, by + bh // 2), label, fill=WHITE, font=badge_font, anchor="mm")

    # Title
    title_font = _load_font(50, bold=True)
    wrapped = "\n".join(textwrap.wrap(title, width=22))
    title_y = by + bh + 24
    draw.text((40, title_y), wrapped, fill=WHITE, font=title_font)

    # CTA
    cta_font = _load_font(28)
    cta_y = title_y + len(wrapped.split("\n")) * 60 + 16
    draw.text((40, cta_y), "Free Today with All Access", fill=ACCENT, font=cta_font)

    # Branding
    brand_font = _load_font(22)
    draw.text((PIN_W - 40, PIN_H - 34), "creativefabrica.com",
              fill=(110, 110, 110), font=brand_font, anchor="rm")

    return canvas


# ── Public API ────────────────────────────────────────────────────────────────

def _download_source(url: str) -> Image.Image | None:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as exc:
        logger.warning("Could not download source image %s: %s", url, exc)
        return None


def create_pin(title: str, image_url: str, slug: str, niche: str) -> Path | None:
    """
    Full pipeline: download → ComfyUI img2img → Pillow overlay → save.
    Falls back to Pillow-only if ComfyUI is unavailable.
    Returns output path or None.
    """
    out_path = OUTPUT_DIR / f"{slug}.jpg"
    if out_path.exists():
        logger.debug("Pin already exists, skipping: %s", out_path)
        return out_path

    source = _download_source(image_url) if image_url else None

    # Try ComfyUI img2img
    ai_image = None
    if source and _comfy_available():
        logger.debug("[%s] Sending to ComfyUI (denoise=%.2f)...", slug, DENOISE)
        ai_image = _run_img2img(source, niche)
        if ai_image:
            logger.debug("[%s] ComfyUI returned image.", slug)
        else:
            logger.warning("[%s] ComfyUI failed, falling back to original.", slug)
    elif source:
        logger.debug("[%s] ComfyUI not available, using original image.", slug)

    base_image = ai_image or source
    if base_image is None:
        # No image at all — create placeholder
        base_image = Image.new("RGB", (800, 800), LIGHT)

    pin = _apply_overlay(base_image, title, niche)
    pin.save(out_path, "JPEG", quality=92)
    logger.info("Saved pin: %s", out_path)
    return out_path


def process_products(products: list[dict]) -> list[dict]:
    """Process a list of product dicts, adding 'local_image_path'."""
    comfy_ok = _comfy_available()
    if comfy_ok:
        logger.info("ComfyUI is available at %s (model: %s)", COMFY_URL, COMFY_MODEL)
    else:
        logger.info("ComfyUI not available — using Pillow-only mode.")

    result = []
    for p in products:
        path = create_pin(
            title=p.get("title", ""),
            image_url=p.get("image_url", ""),
            slug=p["slug"],
            niche=p.get("niche", ""),
        )
        p["local_image_path"] = str(path) if path else ""
        result.append(p)
    return result
