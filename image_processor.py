import io
import logging
import os
import textwrap
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# Pinterest optimal size (2:3 ratio)
PIN_W, PIN_H = 1000, 1500

# Design colours
BG_TOP    = (245, 245, 245)   # light grey top
BG_BOTTOM = (30,  30,  30)    # dark bottom bar
ACCENT    = (255, 87,  34)    # orange accent
WHITE     = (255, 255, 255)
LIGHT     = (220, 220, 220)

# Font — tries to find a system font, falls back to PIL default
def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "arialbd.ttf" if bold else "arial.ttf",
        "Arial Bold.ttf" if bold else "Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def _download_image(url: str) -> Image.Image | None:
    """Download image from URL and return as PIL Image."""
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0"
        })
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:
        logger.warning("Could not download image from %s: %s", url, exc)
        return None


def _fit_image(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """Resize image to fit within bounds, maintaining aspect ratio."""
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    return img


def create_pin(
    title: str,
    image_url: str,
    slug: str,
    niche: str,
) -> Path | None:
    """
    Create a Pinterest-optimised image (1000x1500 px).
    Saves to output/{slug}.jpg and returns the path.
    Returns None if image could not be created.
    """
    out_path = OUTPUT_DIR / f"{slug}.jpg"
    if out_path.exists():
        logger.debug("Pin already exists: %s", out_path)
        return out_path

    # --- Download product image ---
    product_img = _download_image(image_url) if image_url else None

    # --- Canvas ---
    canvas = Image.new("RGB", (PIN_W, PIN_H), BG_TOP)
    draw = ImageDraw.Draw(canvas)

    # === TOP AREA: product image (occupies top 65%) ===
    top_h = int(PIN_H * 0.65)
    if product_img:
        product_rgb = Image.new("RGB", product_img.size, BG_TOP)
        product_rgb.paste(product_img, mask=product_img.split()[3] if product_img.mode == "RGBA" else None)
        product_img = product_rgb

        fitted = _fit_image(product_img.copy(), PIN_W - 80, top_h - 80)
        x_off = (PIN_W - fitted.width) // 2
        y_off = (top_h - fitted.height) // 2
        canvas.paste(fitted, (x_off, y_off))
    else:
        # Placeholder if no image
        draw.rectangle([40, 40, PIN_W - 40, top_h - 40], fill=LIGHT)
        ph_font = _load_font(32)
        draw.text((PIN_W // 2, top_h // 2), "No Image", fill=(180, 180, 180),
                  font=ph_font, anchor="mm")

    # === BOTTOM AREA: dark bar with title + CTA ===
    bar_y = top_h
    draw.rectangle([0, bar_y, PIN_W, PIN_H], fill=BG_BOTTOM)

    # Accent line
    draw.rectangle([0, bar_y, PIN_W, bar_y + 5], fill=ACCENT)

    # Niche badge
    badge_font = _load_font(22)
    niche_label = niche.upper().replace("-", " ")
    badge_w, badge_h = 160, 36
    badge_x = 40
    badge_y = bar_y + 30
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=6, fill=ACCENT
    )
    draw.text(
        (badge_x + badge_w // 2, badge_y + badge_h // 2),
        niche_label, fill=WHITE, font=badge_font, anchor="mm"
    )

    # Title
    title_font = _load_font(52, bold=True)
    max_chars = 22
    wrapped = "\n".join(textwrap.wrap(title, width=max_chars))
    title_y = badge_y + badge_h + 28
    draw.text((40, title_y), wrapped, fill=WHITE, font=title_font)

    # Estimate how many lines for spacing
    line_count = len(wrapped.split("\n"))
    title_bottom = title_y + line_count * 62

    # CTA
    cta_font = _load_font(30)
    cta_text = "Free Today with All Access"
    cta_y = title_bottom + 20
    draw.text((40, cta_y), cta_text, fill=ACCENT, font=cta_font)

    # Branding
    brand_font = _load_font(24)
    draw.text(
        (PIN_W - 40, PIN_H - 36),
        "creativefabrica.com",
        fill=(120, 120, 120),
        font=brand_font,
        anchor="rm"
    )

    # Save
    canvas.save(out_path, "JPEG", quality=92)
    logger.info("Saved pin: %s", out_path)
    return out_path


def process_products(products: list[dict]) -> list[dict]:
    """
    Process a list of product dicts, adding 'local_image_path' to each.
    Products without a usable image still get processed (placeholder used).
    """
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
