# CLAUDE.md — Project Instructions for AI Assistants

## Project overview

This project scrapes Creative Fabrica product listings using a real Chromium browser (Playwright),
processes product images into Pinterest-optimised pins (Pillow), and writes results to Google Sheets.
A GitHub Actions cron job runs it daily at 09:00 UTC.

## Architecture

```
main.py            — entry point: connects Sheets, runs all categories, logs stats
parser.py          — Playwright scraper: launches headless Chrome, extracts products via JS
image_processor.py — Pillow image pipeline: downloads CF image, creates 1000x1500 Pinterest pin
sheets.py          — gspread helpers: auth, tab management, dedup write
config.py          — all constants loaded from .env / environment variables
```

## Key decisions and constraints

### Why Playwright, not requests/BeautifulSoup
Creative Fabrica is a React SPA behind Cloudflare. Plain HTTP requests return 403.
Playwright runs a real headless Chrome that passes Cloudflare checks.
The product data is extracted via `page.evaluate()` running JS in the browser context.

### Product extraction JS logic (in parser.py `_EXTRACT_JS`)
CF category pages render products as pairs of `<a href="/product/slug/">` elements:
- First link: contains `<img>` with `src` pointing to CDN (cdn.creativefabrica.com), no text
- Second link: contains only the product title text, same href

The JS snippet deduplicates by slug within a single page using a `seen` Set.

### Pagination URL format
- Page 1: `https://www.creativefabrica.com/fonts/`
- Page 2+: `https://www.creativefabrica.com/fonts/page/2/`
The `_build_page_url()` function handles this.

### Affiliate URL format (confirmed working)
```
https://www.creativefabrica.com/product/{slug}/ref/7029352/?sharedfrom=pdp
```
Built from `AFFILIATE_URL_TEMPLATE` in config.py using `CF_AFFILIATE_ID` env var.

### Three-layer deduplication
1. **Within a page**: JS `seen` Set in `_EXTRACT_JS`
2. **Across pages in one run**: `seen_slugs` set in `parse_category()`
3. **Across runs**: `get_existing_slugs()` reads the sheet before every write

### Google Sheets auth
- **Locally**: reads `credentials.json` path from `GOOGLE_CREDENTIALS_PATH` env var
- **GitHub Actions**: reads JSON string from `GOOGLE_CREDENTIALS` env var, writes to temp file

### Image processing (image_processor.py)
- Output: `output/{slug}.jpg`, 1000×1500 px (Pinterest 2:3 ratio)
- Skips if file already exists (idempotent)
- Layout: product image in top 65%, dark branded bar at bottom with title + CTA
- Font: tries system fonts (Arial/DejaVu), falls back to PIL default

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_SHEET_ID` | yes | Google Sheet ID |
| `CF_AFFILIATE_ID` | yes | CF affiliate ID (7029352) |
| `GOOGLE_CREDENTIALS_PATH` | local only | Path to credentials.json (default: credentials.json) |
| `GOOGLE_CREDENTIALS` | CI only | Full JSON content of credentials.json |
| `PAGES_PER_RUN` | no | Pages per category per run (default: 3) |

## Google Sheet tabs and columns

Tabs: `fonts`, `graphics`, `3d-svg`, `3d-printing`, `embroidery`, `laser-cutting`, `bundles`

Columns (in order):
```
title | image_url | cf_url | affiliate_url | slug | posted | pin_id | created_at
```

- `posted`: set to `TRUE` manually or by Pinterest poster after a pin is created
- `pin_id`: filled in after Pinterest posting
- `created_at`: UTC timestamp set at write time

## CF category URLs (confirmed live)

```python
"fonts":        "https://www.creativefabrica.com/fonts/"
"graphics":     "https://www.creativefabrica.com/graphics/"
"3d-svg":       "https://www.creativefabrica.com/3d-svg/"
"3d-printing":  "https://www.creativefabrica.com/3d-printing/"
"embroidery":   "https://www.creativefabrica.com/embroidery/"
"laser-cutting":"https://www.creativefabrica.com/laser-cutting/"
"bundles":      "https://www.creativefabrica.com/bundles/"
```

## Common tasks

### Scrape more pages for initial bulk import
```bash
# Windows
set PAGES_PER_RUN=50 && python main.py

# macOS/Linux
PAGES_PER_RUN=50 python main.py
```

### Add a new category
1. Add entry to `CATEGORIES` dict in `config.py`
2. That's it — `ensure_tabs()` will create the new sheet tab automatically

### Change affiliate ID
Update `CF_AFFILIATE_ID` in `.env` (locally) and the GitHub Secret (CI).
Do NOT hardcode it in any Python file.

### Run only one category (for testing)
Temporarily comment out other entries in `CATEGORIES` in `config.py`.

## Dependencies

```
playwright==1.44.0     # headless Chrome scraping
Pillow==10.3.0         # image processing
gspread==6.1.2         # Google Sheets API
google-auth==2.30.0    # service account auth
requests==2.32.3       # image downloads in image_processor
python-dotenv==1.0.1   # .env loading
beautifulsoup4==4.12.3 # HTML parsing (secondary use)
```

After installing dependencies, run once:
```bash
playwright install chromium
```

## Files that must never be committed

- `credentials.json` — Google service account key
- `.env` — local secrets
- `output/` — generated Pinterest images (can be large)

All three are in `.gitignore`.

## GitHub Actions secrets required

| Secret name | Value |
|---|---|
| `GOOGLE_CREDENTIALS` | Full contents of `credentials.json` |
| `GOOGLE_SHEET_ID` | The Google Sheet ID |
| `CF_AFFILIATE_ID` | `7029352` |
