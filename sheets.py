import json
import logging
import os
import tempfile
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from config import COLUMNS, GOOGLE_CREDENTIALS_PATH, GOOGLE_SHEET_ID, SHEET_TABS

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_credentials() -> Credentials:
    """
    Load service account credentials.
    In GitHub Actions, GOOGLE_CREDENTIALS env var holds the JSON content.
    Locally, GOOGLE_CREDENTIALS_PATH points to the credentials.json file.
    """
    raw = os.environ.get("GOOGLE_CREDENTIALS")
    if raw:
        # GitHub Actions: write to a temp file and load from there
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        creds = Credentials.from_service_account_file(tmp_path, scopes=SCOPES)
        os.unlink(tmp_path)
        return creds

    return Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)


def get_sheet_client() -> gspread.Spreadsheet:
    """Return an authorised gspread Spreadsheet object."""
    creds = _get_credentials()
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)


def ensure_tabs(spreadsheet: gspread.Spreadsheet) -> None:
    """Create any missing tabs and write the header row if the tab is new."""
    existing = {ws.title for ws in spreadsheet.worksheets()}
    for tab in SHEET_TABS:
        if tab not in existing:
            logger.info("Creating tab: %s", tab)
            ws = spreadsheet.add_worksheet(title=tab, rows=1000, cols=len(COLUMNS))
            ws.append_row(COLUMNS, value_input_option="RAW")
        else:
            # Make sure header row exists
            ws = spreadsheet.worksheet(tab)
            header = ws.row_values(1)
            if header != COLUMNS:
                logger.info("Writing header row to tab: %s", tab)
                ws.insert_row(COLUMNS, index=1, value_input_option="RAW")


def get_existing_slugs(spreadsheet: gspread.Spreadsheet, tab: str) -> set[str]:
    """Return the set of slugs already present in the given tab."""
    ws = spreadsheet.worksheet(tab)
    try:
        slug_col_index = COLUMNS.index("slug") + 1  # gspread is 1-indexed
        values = ws.col_values(slug_col_index)
        # Skip header row
        return set(values[1:]) if len(values) > 1 else set()
    except Exception as exc:
        logger.warning("Could not fetch existing slugs for tab '%s': %s", tab, exc)
        return set()


def append_products(
    spreadsheet: gspread.Spreadsheet,
    tab: str,
    products: list[dict],
) -> tuple[int, int]:
    """
    Append new products to the given tab, skipping duplicates by slug.
    Returns (new_count, skipped_count).
    """
    ws = spreadsheet.worksheet(tab)
    existing_slugs = get_existing_slugs(spreadsheet, tab)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows_to_append: list[list] = []
    skipped = 0

    for product in products:
        slug = product.get("slug", "")
        if slug in existing_slugs:
            skipped += 1
            continue

        row = [
            product.get("title", ""),
            product.get("image_url", ""),
            product.get("cf_url", ""),
            product.get("affiliate_url", ""),
            slug,
            "FALSE",   # posted
            "",        # pin_id
            now,       # created_at
        ]
        rows_to_append.append(row)
        existing_slugs.add(slug)  # avoid intra-batch duplicates

    if rows_to_append:
        ws.append_rows(rows_to_append, value_input_option="RAW")

    return len(rows_to_append), skipped


def test_connection(spreadsheet: gspread.Spreadsheet) -> None:
    """Write a test value and delete it — quick smoke-test for Sheets access."""
    ws = spreadsheet.worksheets()[0]
    test_val = f"[connection_test] {datetime.now(timezone.utc).isoformat()}"
    ws.update_cell(1, 1, test_val)
    logger.info("Test write to '%s'!A1 succeeded.", ws.title)
    # Restore header
    ws.update_cell(1, 1, COLUMNS[0])
    logger.info("Restored header in '%s'!A1.", ws.title)
