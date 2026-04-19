#!/usr/bin/env python3
"""
CF Pinterest Parser - quick launcher

Usage:
    python run.py parse          # parse + pins + sheets (all)
    python run.py pins          # pins only
    python run.py test          # test one category
    python run.py --help        # help

Examples:
    python run.py parse                          # 3 pages, all categories
    python run.py parse --pages 50               # 50 pages
    python run.py parse --niche fonts            # fonts only
    python run.py test                           # test fonts (1 page)
"""

import argparse
import logging
import sys

from config import CATEGORIES
from auto_pin_pipeline import run_auto_pin_batch
from comfy_processor import process_products, create_pin
from extractor import run_batch
from font_generator import generate_font_asset, GEN_MODES
from font_publish_pipeline import run_publish_batch
from parser import parse_category
from sheets import append_products, ensure_tabs, get_sheet_client, test_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def cmd_parse(niche: str = None, pages: int = None):
    """Full cycle: parse -> pins -> sheets."""
    spreadsheet = get_sheet_client()
    logger.info("[OK] Google Sheet: %s", spreadsheet.title)

    ensure_tabs(spreadsheet)

    niches_to_parse = {niche: CATEGORIES[niche]} if niche else CATEGORIES
    total_new, total_skipped = 0, 0

    for name, url in niches_to_parse.items():
        logger.info("")
        logger.info("=== %s ===", name)

        # 1. Parse
        logger.info("-> Parsing...")
        products = parse_category(url, name, pages=pages or 3)
        logger.info("[OK] Found: %d products", len(products))

        if not products:
            logger.warning("[!] No products, skipping")
            continue

        # 2. Generate pins
        logger.info("-> Generating pins...")
        products = process_products(products)
        logger.info("[OK] Pins created")

        # 3. Write to sheets
        logger.info("-> Writing to Google Sheets...")
        new_count, skipped_count = append_products(spreadsheet, name, products)
        logger.info("[OK] New: %d | Duplicates: %d", new_count, skipped_count)

        total_new += new_count
        total_skipped += skipped_count

    logger.info("")
    logger.info("===========================================")
    logger.info("[DONE] Total new: %d | Duplicates: %d", total_new, total_skipped)
    logger.info("===========================================")


def cmd_pins(niche: str = None, all_pages: bool = False):
    """Generate pins only (without parsing and sheet writing)."""
    logger.info("Mode: pins only")

    spreadsheet = get_sheet_client()
    niches_to_process = {niche: CATEGORIES[niche]} if niche else CATEGORIES

    for name, url in niches_to_process.items():
        logger.info("=== %s ===", name)

        products = parse_category(url, name, pages=1 if not all_pages else 50)

        for p in products:
            path = create_pin(
                title=p.get("title", ""),
                image_url=p.get("image_url", ""),
                slug=p["slug"],
                niche=p.get("niche", ""),
            )
            if path:
                logger.info("  [OK] %s", p["slug"])

    logger.info("[OK] Pins ready in output/")


def cmd_test():
    """Test: one category, one page."""
    logger.info("Mode: TEST (1 page fonts)")

    spreadsheet = get_sheet_client()
    test_connection(spreadsheet)

    products = parse_category(
        "https://www.creativefabrica.com/fonts/", "fonts", pages=1
    )
    logger.info("[OK] Found: %d products", len(products))

    if products:
        logger.info("Example: %s", products[0]["title"])
        path = create_pin(
            title=products[0]["title"],
            image_url=products[0]["image_url"],
            slug=products[0]["slug"],
            niche="fonts",
        )
        logger.info("[OK] Pin created: %s", path)


def cmd_extract(input_path: str, output_root: str = "output"):
    """Extract transparent overlays from preview image(s)."""
    logger.info("Mode: extract overlays")
    logger.info("Input: %s", input_path)
    logger.info("Output root: %s", output_root)

    results = run_batch(input_path, output_root=output_root)
    if not results:
        logger.warning("[!] No images processed")
        return

    manual_count = sum(1 for r in results if r.needs_manual_check)
    pass_count = sum(1 for r in results if r.qc_decision == "PASS")
    retry_count = sum(1 for r in results if r.retry_count > 0)
    logger.info(
        "[OK] Processed: %d | pass: %d | retried: %d | manual_check: %d",
        len(results),
        pass_count,
        retry_count,
        manual_count,
    )


def cmd_generate_font(
    input_path: str,
    output_root: str,
    font_name: str,
    category: str,
    mode: str,
):
    """Generate font wordmark asset with signature-lock/full-regen modes."""
    logger.info("Mode: generate font")
    logger.info("Input: %s", input_path)
    logger.info("Output root: %s", output_root)
    logger.info("Requested mode: %s", mode)

    result = generate_font_asset(
        source_preview_path=input_path,
        output_root=output_root,
        font_name=font_name,
        category=category,
        mode=mode,
    )
    logger.info(
        "[OK] Generated: %s | effective_mode=%s | fallback=%s",
        result.output_wordmark_path,
        result.mode,
        result.used_fallback,
    )


def cmd_publish_fonts(
    input_path: str,
    output_root: str,
    category: str,
    font_name: str,
    use_comfy_background: bool,
    target_width_pct: float,
):
    """Build publish-ready assets: extract overlay -> generate bg -> compose final image."""
    logger.info("Mode: publish fonts")
    logger.info("Input: %s", input_path)
    logger.info("Output root: %s", output_root)
    logger.info("Category: %s", category)
    logger.info("Comfy backgrounds: %s", use_comfy_background)

    results = run_publish_batch(
        input_path=input_path,
        output_root=output_root,
        category=category,
        font_name=font_name,
        use_comfy_background=use_comfy_background,
        target_width_pct=target_width_pct,
    )
    if not results:
        logger.warning("[!] No images published")
        return

    comfy_count = sum(1 for r in results if r.comfy_used)
    fallback_count = sum(1 for r in results if r.used_fallback)
    pass_extract = sum(1 for r in results if r.extraction_qc == "PASS")
    logger.info(
        "[OK] Published: %d | comfy: %d | fallback: %d | extract_pass: %d",
        len(results),
        comfy_count,
        fallback_count,
        pass_extract,
    )


def cmd_auto_pin(input_path: str, output_root: str):
    """Fully automatic pin pipeline: bbox -> mode -> pin/meta."""
    logger.info("Mode: auto-pin")
    logger.info("Input: %s", input_path)
    logger.info("Output root: %s", output_root)
    results = run_auto_pin_batch(input_path=input_path, output_root=output_root)
    if not results:
        logger.warning("[!] No images processed")
        return
    gen = sum(1 for r in results if r.status == "generated")
    rej = sum(1 for r in results if r.status == "rejected")
    logger.info("[OK] Auto-pin processed: %d | generated: %d | rejected: %d", len(results), gen, rej)


def main():
    parser = argparse.ArgumentParser(
        description="CF Pinterest Parser - quick launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  parse    Full cycle: parse -> pins -> sheets (default)
  pins     Pins only
  test     Test: one category, one page
  extract  Remove background and save transparent overlays
  generate-font  Generate font wordmark asset (signature-lock/full-regen scaffold)
  publish-fonts  Build publish-ready images (overlay + unique background)
  auto-pin  Full automatic pin pipeline (bbox/mode/layout + pin/meta)

Examples:
  python run.py parse                          # all, 3 pages
  python run.py parse --niche fonts --pages 10 # fonts only, 10 pages
  python run.py test                            # test
  python run.py extract --input ./previews      # extraction only
  python run.py generate-font --input ./preview.jpg --font-name \"Amore\" --category wedding
  python run.py publish-fonts --input ./test/extractor/input --output ./output --category fonts
  python run.py auto-pin --input ./test/extractor/input --output ./output
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command")

    p_parse = subparsers.add_parser("parse", help="Full cycle")
    p_parse.add_argument(
        "--niche", "-n", choices=list(CATEGORIES.keys()), help="Single category"
    )
    p_parse.add_argument(
        "--pages", "-p", type=int, default=3, help="Pages per category (default: 3)"
    )

    p_pins = subparsers.add_parser("pins", help="Pins only")
    p_pins.add_argument(
        "--niche", "-n", choices=list(CATEGORIES.keys()), help="Single category"
    )
    p_pins.add_argument("--all", "-a", action="store_true", help="All pages (50)")

    subparsers.add_parser("test", help="Test")
    p_extract = subparsers.add_parser("extract", help="Extract transparent overlays")
    p_extract.add_argument(
        "--input",
        "-i",
        required=True,
        help="File or folder with source preview images",
    )
    p_extract.add_argument(
        "--output",
        "-o",
        default="output",
        help="Output root folder (default: output)",
    )
    p_gen = subparsers.add_parser("generate-font", help="Generate font wordmark asset")
    p_gen.add_argument("--input", "-i", required=True, help="Source preview image path")
    p_gen.add_argument("--output", "-o", default="output", help="Output root folder (default: output)")
    p_gen.add_argument("--font-name", required=True, help="Font display name for reports")
    p_gen.add_argument("--category", default="fonts", help="Niche/category label (default: fonts)")
    p_gen.add_argument(
        "--mode",
        default="signature_lock",
        choices=sorted(GEN_MODES),
        help="Generation mode (default: signature_lock)",
    )
    p_pub = subparsers.add_parser("publish-fonts", help="Build publish-ready images")
    p_pub.add_argument(
        "--input",
        "-i",
        required=True,
        help="File or folder with source preview images",
    )
    p_pub.add_argument(
        "--output",
        "-o",
        default="output",
        help="Output root folder (default: output)",
    )
    p_pub.add_argument(
        "--category",
        default="fonts",
        help="Niche/category label for background prompts (default: fonts)",
    )
    p_pub.add_argument(
        "--font-name",
        default="",
        help="Optional display name (single file mode). In folder mode file names are used.",
    )
    p_pub.add_argument(
        "--no-comfy",
        action="store_true",
        help="Disable ComfyUI and use script backgrounds only",
    )
    p_pub.add_argument(
        "--target-width",
        type=float,
        default=0.78,
        help="Target width ratio for wordmark on canvas, 0.45..0.88 (default: 0.78)",
    )
    p_auto = subparsers.add_parser("auto-pin", help="Fully automatic pin generation")
    p_auto.add_argument(
        "--input",
        "-i",
        required=True,
        help="File or folder with source preview images",
    )
    p_auto.add_argument(
        "--output",
        "-o",
        default="output",
        help="Output root folder (default: output)",
    )

    args = parser.parse_args()

    if args.command == "parse" or args.command is None:
        cmd_parse(niche=args.niche, pages=vars(args).get("pages"))
    elif args.command == "pins":
        cmd_pins(niche=args.niche, all_pages=vars(args).get("all", False))
    elif args.command == "test":
        cmd_test()
    elif args.command == "extract":
        cmd_extract(input_path=args.input, output_root=args.output)
    elif args.command == "generate-font":
        cmd_generate_font(
            input_path=args.input,
            output_root=args.output,
            font_name=args.font_name,
            category=args.category,
            mode=args.mode,
        )
    elif args.command == "publish-fonts":
        cmd_publish_fonts(
            input_path=args.input,
            output_root=args.output,
            category=args.category,
            font_name=args.font_name,
            use_comfy_background=not args.no_comfy,
            target_width_pct=args.target_width,
        )
    elif args.command == "auto-pin":
        cmd_auto_pin(input_path=args.input, output_root=args.output)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
