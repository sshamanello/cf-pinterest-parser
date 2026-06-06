import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from shared.config import (
    AFFILIATE_URL_TEMPLATE,
    CF_AFFILIATE_ID,
    COLUMNS,
    GOOGLE_CREDENTIALS_PATH,
    GOOGLE_SHEET_ID,
    SHEET_TABS,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEETS_RETRY_ATTEMPTS = int(os.environ.get("CF_SHEETS_RETRY_ATTEMPTS", "4"))
SHEETS_RETRY_DELAY = float(os.environ.get("CF_SHEETS_RETRY_DELAY", "2"))


def _call_with_retry(description: str, func, *args, **kwargs):
    last_exc = None
    for attempt in range(1, SHEETS_RETRY_ATTEMPTS + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt >= SHEETS_RETRY_ATTEMPTS:
                break
            delay = SHEETS_RETRY_DELAY * attempt
            logger.warning(
                "Sheets call failed (%s), retry %d/%d in %.1fs: %s",
                description,
                attempt,
                SHEETS_RETRY_ATTEMPTS,
                delay,
                exc,
            )
            time.sleep(delay)
    raise last_exc


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
    return _call_with_retry("open_by_key", client.open_by_key, GOOGLE_SHEET_ID)


def ensure_tabs(spreadsheet: gspread.Spreadsheet) -> None:
    """Create any missing tabs and write the header row if the tab is new."""
    existing = {ws.title for ws in _call_with_retry("worksheets", spreadsheet.worksheets)}
    for tab in SHEET_TABS:
        if tab not in existing:
            logger.info("Creating tab: %s", tab)
            ws = _call_with_retry(
                f"add_worksheet:{tab}",
                spreadsheet.add_worksheet,
                title=tab,
                rows=1000,
                cols=len(COLUMNS),
            )
            _call_with_retry(f"append_row:{tab}", ws.append_row, COLUMNS, value_input_option="RAW")
        else:
            # Make sure header row exists
            ws = _call_with_retry(f"worksheet:{tab}", spreadsheet.worksheet, tab)
            header = _call_with_retry(f"row_values:{tab}", ws.row_values, 1)
            if header != COLUMNS:
                logger.info("Updating header row in tab: %s", tab)
                if len(header) < len(COLUMNS):
                    _call_with_retry(f"add_cols:{tab}", ws.add_cols, len(COLUMNS) - len(header))
                _call_with_retry(
                    f"update_header:{tab}",
                    ws.update,
                    f"A1:{_col_to_a1(len(COLUMNS))}1",
                    [COLUMNS],
                    value_input_option="RAW",
                )


def get_existing_slugs(spreadsheet: gspread.Spreadsheet, tab: str) -> set[str]:
    """Return the set of slugs already present in the given tab."""
    ws = _call_with_retry(f"worksheet:{tab}", spreadsheet.worksheet, tab)
    try:
        slug_col_index = COLUMNS.index("slug") + 1  # gspread is 1-indexed
        values = _call_with_retry(f"col_values:{tab}:slug", ws.col_values, slug_col_index)
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
    ws = _call_with_retry(f"worksheet:{tab}", spreadsheet.worksheet, tab)
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
        _call_with_retry(f"append_rows:{tab}", ws.append_rows, rows_to_append, value_input_option="RAW")

    return len(rows_to_append), skipped


def test_connection(spreadsheet: gspread.Spreadsheet) -> None:
    """Write a test value and delete it — quick smoke-test for Sheets access."""
    ws = _call_with_retry("worksheets", spreadsheet.worksheets)[0]
    test_val = f"[connection_test] {datetime.now(timezone.utc).isoformat()}"
    _call_with_retry("update_cell:test", ws.update_cell, 1, 1, test_val)
    logger.info("Test write to '%s'!A1 succeeded.", ws.title)
    # Restore header
    _call_with_retry("update_cell:restore_header", ws.update_cell, 1, 1, COLUMNS[0])
    logger.info("Restored header in '%s'!A1.", ws.title)


def _col_to_a1(col_idx_1based: int) -> str:
    n = col_idx_1based
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _ensure_columns(ws: gspread.Worksheet, required_columns: list[str]) -> list[str]:
    header = _call_with_retry(f"row_values:{ws.title}", ws.row_values, 1)
    if not header:
        if ws.col_count < len(required_columns):
            _call_with_retry(f"add_cols:{ws.title}", ws.add_cols, len(required_columns) - ws.col_count)
        _call_with_retry(
            f"update_header:{ws.title}",
            ws.update,
            f"A1:{_col_to_a1(len(required_columns))}1",
            [required_columns],
            value_input_option="RAW",
        )
        return required_columns

    merged = list(header)
    for col in required_columns:
        if col not in merged:
            merged.append(col)

    if ws.col_count < len(merged):
        _call_with_retry(f"add_cols:{ws.title}", ws.add_cols, len(merged) - ws.col_count)

    if merged != header:
        _call_with_retry(
            f"update_header:{ws.title}",
            ws.update,
            f"A1:{_col_to_a1(len(merged))}1",
            [merged],
            value_input_option="RAW",
        )
    return merged


def _slug_to_title(slug: str) -> str:
    if not slug:
        return ""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part)


def _cf_url_from_slug(slug: str) -> str:
    return f"https://www.creativefabrica.com/product/{slug}/" if slug else ""


def _affiliate_url_from_slug(slug: str) -> str:
    if not slug:
        return ""
    return AFFILIATE_URL_TEMPLATE.format(slug=slug, affiliate_id=CF_AFFILIATE_ID)


def _default_publish_status(mode: str, status: str) -> str:
    if status != "generated" or mode == "reject_mode":
        return "rejected"
    return "ready"


def upsert_auto_pin_report(
    spreadsheet: gspread.Spreadsheet,
    tab: str,
    report_path: str | Path,
) -> tuple[int, int]:
    """
    Upsert auto-pin results into a tab by `slug`.
    Returns (inserted_count, updated_count).
    """
    ws = _call_with_retry(f"worksheet:{tab}", spreadsheet.worksheet, tab)
    required = COLUMNS + [
        "pin_path",
        "mode",
        "template_used",
        "hook_enabled",
        "hook_text",
        "publish_status",
        "published_at",
        "error_reason",
        "public_image_url",
        "remote_image_path",
        "vds_upload_status",
        "upload_backend",
        "uploaded_at",
        "cleanup_status",
        "cleanup_at",
    ]
    header = _ensure_columns(ws, required)
    col = {name: i + 1 for i, name in enumerate(header)}

    raw = json.loads(Path(report_path).read_text(encoding="utf-8"))
    items = raw.get("items", [])
    if not items:
        return 0, 0

    all_values = _call_with_retry(f"get_all_values:{tab}", ws.get_all_values)
    slug_to_row: dict[str, int] = {}
    for idx, row in enumerate(all_values[1:], start=2):
        value = row[col["slug"] - 1] if len(row) >= col["slug"] else ""
        v = value.strip()
        if v and v not in slug_to_row:
            slug_to_row[v] = idx

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    updated = 0
    next_row = max(2, len(all_values) + 1)
    updates: list[dict] = []
    rows_to_append: list[list] = []

    for item in items:
        source_file = item.get("source_file", "")
        slug = Path(source_file).stem
        if not slug:
            continue
        mode = item.get("mode", "")
        status = item.get("status", "")
        reject_reason = item.get("reject_reason", "")
        pin_path = item.get("pin_jpg") or item.get("pin_png") or ""
        hook_enabled = "TRUE" if bool(item.get("hook_enabled", False)) else "FALSE"

        meta_path = item.get("meta_json", "")
        hook_text = ""
        if meta_path and Path(meta_path).exists():
            try:
                meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
                hook_text = str(meta.get("hook_text", "")).strip()
            except Exception:
                hook_text = ""

        row_map = {
            "title": item.get("title") or _slug_to_title(slug),
            "image_url": item.get("image_url", ""),
            "cf_url": item.get("cf_url") or _cf_url_from_slug(slug),
            "affiliate_url": item.get("affiliate_url") or _affiliate_url_from_slug(slug),
            "slug": slug,
            "posted": "FALSE",
            "pin_id": "",
            "created_at": now,
            "pin_path": pin_path,
            "mode": mode,
            "template_used": item.get("template_used", ""),
            "hook_enabled": hook_enabled,
            "hook_text": hook_text,
            "publish_status": _default_publish_status(mode=mode, status=status),
            "published_at": "",
            "error_reason": reject_reason,
            "public_image_url": item.get("public_image_url", ""),
            "remote_image_path": item.get("remote_image_path", ""),
            "vds_upload_status": item.get("vds_upload_status", ""),
            "upload_backend": item.get("upload_backend", ""),
            "uploaded_at": item.get("uploaded_at", ""),
            "cleanup_status": item.get("cleanup_status", ""),
            "cleanup_at": "",
        }

        if item.get("vds_upload_status") and item.get("vds_upload_status") != "uploaded":
            row_map["publish_status"] = "upload_failed"
            row_map["error_reason"] = item.get("upload_error", reject_reason)

        if slug in slug_to_row:
            row_idx = slug_to_row[slug]
            existing = all_values[row_idx - 1] if row_idx - 1 < len(all_values) else []
            # Preserve existing core values if already present.
            def get_existing(name: str) -> str:
                c = col.get(name)
                if not c:
                    return ""
                pos = c - 1
                return existing[pos] if pos < len(existing) else ""

            for keep_col in ["title", "image_url", "cf_url", "affiliate_url", "posted", "pin_id", "created_at", "published_at"]:
                ex = get_existing(keep_col).strip()
                if ex:
                    row_map[keep_col] = ex

            if get_existing("posted").strip().upper() == "TRUE":
                row_map["publish_status"] = "published"

            row_values = [row_map.get(name, get_existing(name)) for name in header]
            updates.append(
                {
                    "range": f"A{row_idx}:{_col_to_a1(len(header))}{row_idx}",
                    "values": [row_values],
                }
            )
            updated += 1
        else:
            row_values = [row_map.get(name, "") for name in header]
            rows_to_append.append(row_values)
            inserted += 1
            slug_to_row[slug] = next_row
            next_row += 1

    if updates:
        _call_with_retry(f"batch_update:{tab}", ws.batch_update, updates, value_input_option="RAW")
    if rows_to_append:
        _call_with_retry(f"append_rows:{tab}:upsert", ws.append_rows, rows_to_append, value_input_option="RAW")

    return inserted, updated
