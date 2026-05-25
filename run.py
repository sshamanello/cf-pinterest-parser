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
from pathlib import Path

from cf_pinterest.queue_service import (
    export_n8n_ready_json,
    export_n8n_ready_csv,
    get_db_health,
    get_export_profiles,
    get_queue_summary,
    import_sheet_file_to_queue_db,
    sync_report_to_queue_db,
)
from config import CATEGORIES
from auto_pin_pipeline import run_auto_pin_batch
from comfy_processor import process_products, create_pin
from extractor import run_batch
from font_generator import generate_font_asset, GEN_MODES
from font_publish_pipeline import run_publish_batch
from parser import parse_category
from prod_auto_pin_pipeline import run_prod_auto_pin, upload_report_pins_to_vds
from sheets import append_products, ensure_tabs, get_sheet_client, test_connection, upsert_auto_pin_report
from logging_utils import configure_logging

logger = configure_logging("run", default_log_name="run.log")
QUEUE_DB_PATH = Path("output/_state/queue.db")
EXPORT_PROFILE_CHOICES = sorted(get_export_profiles().keys())


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


def cmd_sync_auto_pin(report_path: str, tab: str):
    """Sync auto-pin batch report into Google Sheet tab (upsert by slug)."""
    logger.info("Mode: sync auto-pin to sheet")
    logger.info("Report: %s", report_path)
    logger.info("Tab: %s", tab)
    rp = Path(report_path)
    if not rp.exists():
        raise FileNotFoundError(f"Report not found: {rp}")

    spreadsheet = get_sheet_client()
    ensure_tabs(spreadsheet)
    inserted, updated = upsert_auto_pin_report(spreadsheet=spreadsheet, tab=tab, report_path=rp)
    logger.info("[OK] Sheet sync complete | inserted: %d | updated: %d", inserted, updated)


def cmd_sync_queue_db(report_path: str, tab: str, db_path: str):
    """Sync auto-pin batch report into local SQLite queue."""
    logger.info("Mode: sync queue db")
    logger.info("Report: %s", report_path)
    logger.info("Tab: %s", tab)
    logger.info("DB path: %s", db_path)
    result = sync_report_to_queue_db(report_path=report_path, db_path=db_path, niche=tab)
    logger.info(
        "[OK] Queue DB sync complete | parsed=%d | upserted=%d | skipped=%d | generated=%d | uploaded=%d | rejected=%d",
        result.parsed_items,
        result.upserted_items,
        result.skipped_items,
        result.generated_items,
        result.uploaded_items,
        result.rejected_items,
    )


def cmd_import_queue_file(file_path: str, tab: str, db_path: str):
    """Import CSV/XLSX file into local SQLite queue."""
    logger.info("Mode: import queue file")
    logger.info("File: %s", file_path)
    logger.info("Tab: %s", tab)
    logger.info("DB path: %s", db_path)
    result = import_sheet_file_to_queue_db(file_path=file_path, db_path=db_path, niche=tab)
    logger.info(
        "[OK] Queue file import complete | parsed=%d | upserted=%d | skipped=%d | generated=%d | uploaded=%d | rejected=%d",
        result.parsed_items,
        result.upserted_items,
        result.skipped_items,
        result.generated_items,
        result.uploaded_items,
        result.rejected_items,
    )
    summary = get_queue_summary(db_path=db_path, niche=tab)
    logger.info(
        "[OK] Queue summary | niche=%s | total=%d | generated=%d | uploaded=%d | rejected=%d",
        summary["niche"],
        summary["total_items"],
        summary["generated_items"],
        summary["uploaded_items"],
        summary["rejected_items"],
    )


def cmd_export_n8n_queue(
    tab: str,
    db_path: str,
    output_path: str,
    limit: int,
    profile: str,
    statuses: str,
    export_format: str,
):
    """Export final publish table into CSV/JSON for n8n ingestion."""
    logger.info("Mode: export n8n queue")
    logger.info("Tab: %s", tab)
    logger.info("DB path: %s", db_path)
    logger.info("Output: %s", output_path)
    logger.info("Profile: %s", profile)
    status_values = tuple(x.strip() for x in statuses.split(",") if x.strip())
    logger.info("Statuses: %s", ",".join(status_values) if status_values else "all")
    if export_format == "json":
        exported = export_n8n_ready_json(
            db_path=db_path,
            niche=tab,
            output_path=output_path,
            limit=limit,
            profile=profile,
            statuses=status_values,
        )
        logger.info("[OK] n8n JSON export complete | rows=%d", exported)
    else:
        exported = export_n8n_ready_csv(
            db_path=db_path,
            niche=tab,
            output_path=output_path,
            limit=limit,
            profile=profile,
            statuses=status_values,
        )
        logger.info("[OK] n8n CSV export complete | rows=%d", exported)


def cmd_db_health(db_path: str):
    """Validate SQLite schema, indexes, and key data invariants."""
    logger.info("Mode: db health")
    logger.info("DB path: %s", db_path)
    health = get_db_health(db_path=db_path)
    if health["ok"]:
        stats = health["stats"]
        logger.info(
            "[OK] DB health passed | queue_items=%d | publish_items=%d | publish_orphans=%d",
            stats["queue_items_total"],
            stats["publish_items_total"],
            stats["publish_orphans"],
        )
        return
    logger.error("[FAIL] DB health failed | issues=%s", "; ".join(health["issues"]))
    raise SystemExit(2)


def cmd_prod_auto_pin(niche: str, limit: int, output_root: str, pages: int, sync_sheet: bool, upload_vds: bool):
    """Production flow: parse products, download previews, generate pins, optionally sync sheet."""
    logger.info("Mode: prod auto-pin")
    logger.info("Niche: %s", niche)
    logger.info("Limit: %d", limit)
    logger.info("Output root: %s", output_root)

    result = run_prod_auto_pin(
        niche=niche,
        limit=limit,
        output_root=output_root,
        pages=pages,
        upload_vds=upload_vds,
    )
    logger.info(
        "[OK] Prod auto-pin | pages: %d-%d | parsed: %d | selected: %d | downloaded: %d | generated: %d | rejected: %d | uploaded: %d",
        result.start_page,
        result.end_page,
        result.parsed_count,
        result.selected_count,
        result.downloaded_count,
        result.generated_count,
        result.rejected_count,
        result.uploaded_count,
    )
    logger.info("Report: %s", result.report_path)
    sync_result = sync_report_to_queue_db(report_path=result.report_path, db_path=QUEUE_DB_PATH, niche=niche)
    logger.info(
        "[OK] Queue DB sync complete | parsed=%d | upserted=%d | skipped=%d | db=%s",
        sync_result.parsed_items,
        sync_result.upserted_items,
        sync_result.skipped_items,
        QUEUE_DB_PATH,
    )

    if sync_sheet:
        spreadsheet = get_sheet_client()
        ensure_tabs(spreadsheet)
        inserted, updated = upsert_auto_pin_report(
            spreadsheet=spreadsheet,
            tab=niche,
            report_path=result.report_path,
        )
        logger.info("[OK] Sheet sync complete | inserted: %d | updated: %d", inserted, updated)


def cmd_upload_vds(report_path: str, sync_sheet: bool, tab: str):
    """Upload generated pin jpg files from an existing auto-pin report to VDS."""
    logger.info("Mode: upload VDS")
    logger.info("Report: %s", report_path)
    uploaded = upload_report_pins_to_vds(report_path)
    logger.info("[OK] VDS upload complete | uploaded: %d", uploaded)

    if sync_sheet:
        spreadsheet = get_sheet_client()
        ensure_tabs(spreadsheet)
        inserted, updated = upsert_auto_pin_report(
            spreadsheet=spreadsheet,
            tab=tab,
            report_path=report_path,
        )
        logger.info("[OK] Sheet sync complete | inserted: %d | updated: %d", inserted, updated)


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
  sync-auto-pin  Upsert auto-pin report to Google Sheet by slug
  sync-queue-db  Upsert auto-pin report into local SQLite queue
  import-queue-file  Import CSV/XLSX file into local SQLite queue
  export-n8n-queue  Export final publish queue CSV for n8n
  db-health  Validate SQLite schema and data invariants
  prod-auto-pin  Parse first products, generate prod pins, optionally sync Sheet
  upload-vds  Upload generated pin jpg files from report to VDS

Examples:
  python run.py parse                          # all, 3 pages
  python run.py parse --niche fonts --pages 10 # fonts only, 10 pages
  python run.py test                            # test
  python run.py extract --input ./previews      # extraction only
  python run.py generate-font --input ./preview.jpg --font-name \"Amore\" --category wedding
  python run.py publish-fonts --input ./test/extractor/input --output ./output --category fonts
  python run.py auto-pin --input ./test/extractor/input --output ./output
  python run.py sync-auto-pin --report ./test/extractor/output/_reports/auto_pin_batch_report.json --tab fonts
  python run.py sync-queue-db --report ./test/extractor/output/_reports/auto_pin_batch_report.json --tab fonts
  python run.py import-queue-file --file ./queue_export.xlsx --tab fonts
  python run.py export-n8n-queue --tab fonts --output ./output/n8n/fonts_publish.csv
  python run.py db-health --db-path ./output/_state/queue.db
  python run.py prod-auto-pin --niche fonts --limit 20 --sync-sheet
  python run.py upload-vds --report ./output/prod/fonts/_reports/auto_pin_batch_report.json --sync-sheet
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
    p_sync = subparsers.add_parser("sync-auto-pin", help="Sync auto-pin batch report to Google Sheet")
    p_sync.add_argument(
        "--report",
        "-r",
        default="./test/extractor/output/_reports/auto_pin_batch_report.json",
        help="Path to auto_pin_batch_report.json",
    )
    p_sync.add_argument(
        "--tab",
        "-t",
        default="fonts",
        choices=list(CATEGORIES.keys()),
        help="Sheet tab/category (default: fonts)",
    )
    p_sync_db = subparsers.add_parser("sync-queue-db", help="Sync auto-pin batch report to local SQLite queue")
    p_sync_db.add_argument(
        "--report",
        "-r",
        required=True,
        help="Path to auto_pin_batch_report.json",
    )
    p_sync_db.add_argument(
        "--tab",
        "-t",
        default="fonts",
        choices=list(CATEGORIES.keys()),
        help="Niche/category (default: fonts)",
    )
    p_sync_db.add_argument(
        "--db-path",
        default=str(QUEUE_DB_PATH),
        help="SQLite queue file path (default: output/_state/queue.db)",
    )
    p_import = subparsers.add_parser("import-queue-file", help="Import CSV/XLSX into local SQLite queue")
    p_import.add_argument(
        "--file",
        "-f",
        required=True,
        help="Input file (.csv or .xlsx)",
    )
    p_import.add_argument(
        "--tab",
        "-t",
        default="fonts",
        choices=list(CATEGORIES.keys()),
        help="Niche/category (default: fonts)",
    )
    p_import.add_argument(
        "--db-path",
        default=str(QUEUE_DB_PATH),
        help="SQLite queue file path (default: output/_state/queue.db)",
    )
    p_export = subparsers.add_parser("export-n8n-queue", help="Export final publish queue CSV for n8n")
    p_export.add_argument(
        "--tab",
        "-t",
        default="fonts",
        choices=list(CATEGORIES.keys()),
        help="Niche/category (default: fonts)",
    )
    p_export.add_argument(
        "--db-path",
        default=str(QUEUE_DB_PATH),
        help="SQLite queue file path (default: output/_state/queue.db)",
    )
    p_export.add_argument(
        "--output",
        "-o",
        default="output/n8n/fonts_publish.csv",
        help="Output CSV path for n8n",
    )
    p_export.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum exported rows (default: 500)",
    )
    p_export.add_argument(
        "--profile",
        default="n8n_default",
        choices=EXPORT_PROFILE_CHOICES,
        help="Export profile (default: n8n_default)",
    )
    p_export.add_argument(
        "--statuses",
        default="generated,uploaded",
        help="Comma-separated statuses to export; empty means all",
    )
    p_export.add_argument(
        "--format",
        default="csv",
        choices=["csv", "json"],
        help="Export format (default: csv)",
    )
    p_db_health = subparsers.add_parser("db-health", help="Validate SQLite schema and key data invariants")
    p_db_health.add_argument(
        "--db-path",
        default=str(QUEUE_DB_PATH),
        help="SQLite queue file path (default: output/_state/queue.db)",
    )
    p_prod = subparsers.add_parser("prod-auto-pin", help="Production auto-pin run from Creative Fabrica")
    p_prod.add_argument("--niche", "-n", default="fonts", choices=list(CATEGORIES.keys()), help="Category tab (default: fonts)")
    p_prod.add_argument("--limit", "-l", type=int, default=20, help="How many products to process (default: 20)")
    p_prod.add_argument("--pages", "-p", type=int, default=1, help="How many category pages to parse (default: 1)")
    p_prod.add_argument("--output", "-o", default="output/prod/fonts", help="Production output root")
    p_prod.add_argument("--sync-sheet", action="store_true", help="Sync generated prod report to Google Sheet")
    p_prod.add_argument("--upload-vds", action="store_true", help="Upload generated pin_01.jpg files to VDS before Sheet sync")
    p_upload = subparsers.add_parser("upload-vds", help="Upload generated pin jpg files from a report to VDS")
    p_upload.add_argument(
        "--report",
        "-r",
        default="./output/prod/fonts/_reports/auto_pin_batch_report.json",
        help="Path to auto_pin_batch_report.json",
    )
    p_upload.add_argument("--sync-sheet", action="store_true", help="Sync report to Google Sheet after upload")
    p_upload.add_argument(
        "--tab",
        "-t",
        default="fonts",
        choices=list(CATEGORIES.keys()),
        help="Sheet tab/category (default: fonts)",
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
    elif args.command == "sync-auto-pin":
        cmd_sync_auto_pin(report_path=args.report, tab=args.tab)
    elif args.command == "sync-queue-db":
        cmd_sync_queue_db(report_path=args.report, tab=args.tab, db_path=args.db_path)
    elif args.command == "import-queue-file":
        cmd_import_queue_file(file_path=args.file, tab=args.tab, db_path=args.db_path)
    elif args.command == "export-n8n-queue":
        cmd_export_n8n_queue(
            tab=args.tab,
            db_path=args.db_path,
            output_path=args.output,
            limit=args.limit,
            profile=args.profile,
            statuses=args.statuses,
            export_format=args.format,
        )
    elif args.command == "db-health":
        cmd_db_health(db_path=args.db_path)
    elif args.command == "prod-auto-pin":
        cmd_prod_auto_pin(
            niche=args.niche,
            limit=args.limit,
            output_root=args.output,
            pages=args.pages,
            sync_sheet=args.sync_sheet,
            upload_vds=args.upload_vds,
        )
    elif args.command == "upload-vds":
        cmd_upload_vds(report_path=args.report, sync_sheet=args.sync_sheet, tab=args.tab)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
