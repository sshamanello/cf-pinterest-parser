"""
Insert a header row into all sheet tabs that are missing one.
Safe to run multiple times — checks before inserting.
"""
import os
from dotenv import load_dotenv
from sheets import get_sheet_client

load_dotenv()

HEADER = [
    "title", "image_url", "cf_url", "affiliate_url", "slug",
    "posted", "pin_id", "created_at",
    "pin_path", "mode", "template_used", "hook_enabled", "hook_text",
    "publish_status", "published_at", "error_reason",
    "public_image_url", "remote_image_path",
    "vds_upload_status", "uploaded_at",
    "cleanup_status", "cleanup_at",
]

TABS = ["fonts", "graphics", "3d-svg", "3d-printing", "embroidery", "laser-cutting", "bundles"]

def fix_headers():
    spreadsheet = get_sheet_client()
    for tab in TABS:
        try:
            ws = spreadsheet.worksheet(tab)
        except Exception as e:
            print(f"  [{tab}] skipped — not found: {e}")
            continue

        row1 = ws.row_values(1)

        # Check if row 1 already looks like a header
        if row1 and row1[0] == "title":
            print(f"  [{tab}] already has headers ({len(row1)} cols) — skipping")
            continue

        # Ensure enough columns
        if ws.col_count < len(HEADER):
            ws.add_cols(len(HEADER) - ws.col_count)

        # Insert blank row at position 1, then write headers
        ws.insert_row(HEADER, index=1, value_input_option="RAW")
        print(f"  [{tab}] ✓ header row inserted ({len(HEADER)} cols)")

if __name__ == "__main__":
    print("Fixing sheet headers...")
    fix_headers()
    print("Done.")
