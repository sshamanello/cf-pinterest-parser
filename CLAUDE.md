# CLAUDE.md — Project Instructions for AI Assistants

## Project overview

This project scrapes Creative Fabrica product listings using a real Chromium browser (Playwright),
processes product images into Pinterest-optimised pins (Pillow + optional ComfyUI img2img),
and writes results to Google Sheets. A GitHub Actions cron job runs it daily at 09:00 UTC.

## Architecture

```
main.py             — entry point: connects Sheets, runs all categories, logs stats
parser.py           — Playwright scraper: fresh browser per page, extracts products via JS
comfy_processor.py  — image pipeline: ComfyUI img2img → Pillow overlay → output/{slug}.jpg
                      falls back to Pillow-only if ComfyUI is not running
image_processor.py  — legacy Pillow-only pipeline (kept for reference, not used by main.py)
sheets.py           — gspread helpers: auth, tab management, dedup write
config.py           — all constants loaded from .env / environment variables
```

## Key decisions and constraints

### Why Playwright, not requests/BeautifulSoup
Creative Fabrica is a React SPA behind Cloudflare. Plain HTTP requests return 403.
Playwright runs a real headless Chrome that passes Cloudflare checks.
The product data is extracted via `page.evaluate()` running JS in the browser context.

### Fresh browser per page (critical)
CF rate-limits or challenges the same browser session after page 1.
Solution: a brand-new `browser = pw.chromium.launch()` is created for every page request.
This is intentional and must not be changed back to a shared session.

### Product extraction JS logic (in parser.py `_EXTRACT_JS`)
CF category pages render products as `<a href="/product/slug/">` elements:
- Image link: contains `<img>`, may have `data-src` (lazy) or `src` (eager)
- Title link: contains only product title text, same href
- **noscript bug**: some categories wrap img in `<noscript>`, so `textContent` returns raw HTML
  like `<img src="..." alt="Product Title" />`. Fixed by extracting `alt` attribute via regex.

The JS snippet deduplicates by slug within a single page using a `seen` Set.
Python `seen_slugs` deduplicates across pages within one run.

### Pagination URL format
- Page 1: `https://www.creativefabrica.com/fonts/`
- Page 2+: `https://www.creativefabrica.com/fonts/page/2/`
The `_build_page_url()` function handles this.
Pages 1 of fonts/graphics return ~84/32 products (include Popular + New sections).
Pages 2+ return ~36 products each.

### Scroll before waiting for products
Some categories (embroidery, bundles, laser-cutting) lazy-load product cards.
The parser scrolls down gradually before calling `wait_for_selector`.
This is required — removing it breaks embroidery/bundles parsing.

### Retry logic
Each page gets up to 2 attempts with an 8-second pause between them.
If both fail, the category stops and moves on (does not crash the whole run).

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

### Image processing (comfy_processor.py)
- Output: `output/{slug}.jpg`, 1000×1500 px (Pinterest 2:3 ratio)
- Skips if file already exists (idempotent)
- **With ComfyUI running** (`http://127.0.0.1:8188`):
  - Downloads CF image → uploads to ComfyUI → runs img2img (denoising ~0.50) → downloads result
  - Per-niche prompts defined in `NICHE_PROMPTS` dict
  - Falls back to original image if ComfyUI job fails/times out
- **Without ComfyUI**: uses original CF image directly
- Both paths: apply Pillow overlay (top 65% product image, bottom 35% dark bar with title + CTA)
- Font: tries system fonts (Arial/DejaVu), falls back to PIL default

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_SHEET_ID` | yes | Google Sheet ID |
| `CF_AFFILIATE_ID` | yes | CF affiliate ID (7029352) |
| `GOOGLE_CREDENTIALS_PATH` | local only | Path to credentials.json (default: credentials.json) |
| `GOOGLE_CREDENTIALS` | CI only | Full JSON content of credentials.json |
| `PAGES_PER_RUN` | no | Pages per category per run (default: 3) |
| `COMFY_URL` | no | ComfyUI API URL (default: http://127.0.0.1:8188) |
| `COMFY_MODEL` | no | SD checkpoint filename (default: realisticVisionV51.safetensors) |
| `COMFY_DENOISE` | no | img2img denoising strength 0.0–1.0 (default: 0.50) |
| `COMFY_STEPS` | no | Sampling steps (default: 20) |
| `COMFY_CFG` | no | CFG scale (default: 7.0) |

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
"fonts":         "https://www.creativefabrica.com/fonts/"
"graphics":      "https://www.creativefabrica.com/graphics/"
"3d-svg":        "https://www.creativefabrica.com/3d-svg/"
"3d-printing":   "https://www.creativefabrica.com/3d-printing/"
"embroidery":    "https://www.creativefabrica.com/embroidery/"
"laser-cutting": "https://www.creativefabrica.com/laser-cutting/"
"bundles":       "https://www.creativefabrica.com/bundles/"
```

## Common tasks

### Scrape more pages for initial bulk import
```bash
# Windows
set PAGES_PER_RUN=50 && python main.py

# macOS/Linux
PAGES_PER_RUN=50 python main.py
```

### Run with ComfyUI image uniquification
```bash
# 1. Start ComfyUI in one terminal
cd ComfyUI && python main.py --listen

# 2. Run parser in another terminal (auto-detects ComfyUI)
cd cf-pinterest-parser && python main.py
```

### Add a new category
1. Add entry to `CATEGORIES` dict in `config.py`
2. That's it — `ensure_tabs()` will create the new sheet tab automatically

### Change affiliate ID
Update `CF_AFFILIATE_ID` in `.env` (locally) and the GitHub Secret (CI).
Do NOT hardcode it in any Python file.

### Run only one category (for testing)
Temporarily comment out other entries in `CATEGORIES` in `config.py`.

### Re-generate images (e.g. after changing overlay design)
Delete files from `output/` folder — they will be regenerated on next run.
The sheet is not affected (image_url column stores original CF CDN URL).

## Dependencies

```
playwright==1.44.0     # headless Chrome scraping
Pillow==10.3.0         # image processing / overlay
gspread==6.1.2         # Google Sheets API
google-auth==2.30.0    # service account auth
requests==2.32.3       # image downloads
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

## Known issues and quirks

- **embroidery / bundles / laser-cutting**: occasionally time out even on page 1.
  This is CF-side rate limiting — retry logic handles it automatically.
- **fonts page 1**: returns 84 products (Popular + New sections combined).
  Pages 2+ return ~36 each (pure paginated list).
- **ComfyUI workflow**: uses KSampler with euler_ancestral + karras scheduler.
  If the model name in COMFY_MODEL doesn't match exactly what's in ComfyUI/models/checkpoints/,
  the job will error and fall back to Pillow-only mode.
- **SSL warning on startup**: `SSLEOFError` from gspread on first connect is normal,
  gspread retries automatically.
