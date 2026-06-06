import logging
import random
import time

from shared.config import CATEGORIES, MIN_DELAY, MAX_DELAY
from process.comfy_processor import process_products
from shared.logging_utils import configure_logging
from download.parser import parse_category
from download.sheets import append_products, ensure_tabs, get_sheet_client, test_connection

logger = configure_logging("main", default_log_name="main.log")


def main() -> None:
    logger.info("=== CF Pinterest Parser starting ===")

    spreadsheet = get_sheet_client()
    logger.info("Connected to Google Sheet: %s", spreadsheet.title)

    test_connection(spreadsheet)
    ensure_tabs(spreadsheet)

    total_new = 0
    total_skipped = 0

    for niche, url in CATEGORIES.items():
        logger.info("--- Parsing niche: %s ---", niche)
        products = parse_category(url, niche)
        logger.info("[%s] Total scraped: %d products", niche, len(products))

        if products:
            logger.info("[%s] Processing images...", niche)
            products = process_products(products)
            new_count, skipped_count = append_products(spreadsheet, niche, products)
            logger.info(
                "[%s] Written: %d new | Skipped (duplicates): %d",
                niche,
                new_count,
                skipped_count,
            )
            total_new += new_count
            total_skipped += skipped_count
        else:
            logger.warning("[%s] No products found, nothing written.", niche)

        # Polite delay between categories
        if list(CATEGORIES.keys())[-1] != niche:
            delay = random.uniform(MIN_DELAY, MAX_DELAY)
            logger.debug("Sleeping %.1f s before next category.", delay)
            time.sleep(delay)

    logger.info(
        "=== Done. Total new rows: %d | Total skipped: %d ===",
        total_new,
        total_skipped,
    )


if __name__ == "__main__":
    main()
