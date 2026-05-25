# CF Pinterest Parser

Production-oriented toolkit for turning Creative Fabrica products into a Pinterest publishing queue.

The repository now covers the full workflow for the `fonts` MVP:
- scrape Creative Fabrica product previews;
- generate Pinterest pins with the automatic layout pipeline;
- upload ready JPG assets to a VDS;
- sync metadata into Google Sheets;
- publish through either `n8n` or Android phone automation;
- keep daily posting warm and human-looking through a jittered scheduler.
- store a local SQLite queue for stable retries and analytics experiments.

## Current architecture

```text
Creative Fabrica -> parser.py -> prod_auto_pin_pipeline.py -> auto_pin_pipeline.py
                -> output/prod/... -> VDS upload -> Google Sheet queue
                -> n8n or Android phone automation -> Pinterest
```

## Main entrypoints

### Content pipeline
- `python run.py parse`
  Legacy all-category parse + image processing + Google Sheets write.
- `python run.py auto-pin --input <file-or-dir> --output <dir>`
  Runs the automatic pin builder on existing preview files.
- `python run.py prod-auto-pin --niche fonts --pages 10 --limit 300 --output output/prod/fonts_YYYYMMDD --upload-vds --sync-sheet`
  Main production batch: parse -> download previews -> generate pins -> upload JPG to VDS -> sync Google Sheet.
- `python run.py upload-vds --report <report.json> --sync-sheet --tab fonts`
  Uploads already-generated JPG assets from an existing report.
- `python run.py sync-queue-db --report <report.json> --tab fonts --db-path output/_state/queue.db`
  Upserts report data into local SQLite queue storage.
- `python run.py import-queue-file --file <queue.csv|queue.xlsx> --tab fonts --db-path output/_state/queue.db`
  Imports historical/exported spreadsheet data into local queue DB.
- `python run.py export-n8n-queue --tab fonts --output output/n8n/fonts_publish.csv`
  Exports final publish dataset for n8n ingestion.
  Optional flags:
  - `--profile n8n_default|n8n_minimal`
  - `--statuses generated,uploaded` (or empty for all statuses)

### Phone automation
- `python run_phones.py devices`
- `python run_phones.py warmup`
- `python run_phones.py post --tab fonts`
- `python scheduler.py`

### Smoke checks
- `python test_components.py`

## Repository layout

```text
run.py                     Main CLI for scraping, pin generation, VDS upload, and sheet sync
cf_pinterest/              Modular core (queue models, DB schema, sync service)
prod_auto_pin_pipeline.py  Production batch orchestrator
auto_pin_pipeline.py       Automatic pin builder and reporting
parser.py                  Playwright scraper for Creative Fabrica category pages
sheets.py                  Google Sheets auth, upsert, and queue helpers
run_phones.py              CLI for Android phone warmup/post flows
pinterest_warmup.py        Human-like Pinterest warmup behaviors
pinterest_post.py          Android UI automation for posting one ready pin
scheduler.py               5-slot daily phone scheduler with jitter
logging_utils.py           Shared rotating-file + console logging setup
deploy/                    systemd unit, udev rule, and bootstrap helper for phone server
docker-compose.yml         Container roles for one-off runs, cron batches, and phone scheduler
n8n/                       Importable n8n workflows for publish/cleanup
```

## Environment

Create `.env` from the example and add `credentials.json`:

```bash
cp .env.example .env
```

Minimum required variables:

```env
GOOGLE_SHEET_ID=...
CF_AFFILIATE_ID=7029352
GOOGLE_CREDENTIALS_PATH=credentials.json
```

Important optional groups:
- `VDS_*` for autonomous upload + publishing.
- `PHONE_ACCOUNTS` for mapping ADB serials to Pinterest accounts.
- `CF_LOG_*` for log level, retention, and log directory.
- `CF_CRON_SCHEDULE` / `CF_RUN_COMMAND` for the Docker cron service.

## Local setup

### Python runtime
The project has been exercised with Python `3.12` locally and `3.11` on Linux servers.

### Install dependencies

```bash
python3.12 -m pip install -r requirements.txt
python3.12 -m playwright install chromium
```

### Run smoke checks

```bash
python3.12 test_components.py
```

The smoke script validates:
- environment and credentials;
- Google Sheets connectivity;
- Playwright browser availability;
- Creative Fabrica parsing attempt;
- sample auto-pin generation from `test/extractor/input`;
- scheduler dry-run;
- optional local `adb` visibility.

By default, external checks (Sheets/Playwright/parser) are warn-only, so smoke tests stay stable in restricted environments.
Set strict mode when validating full external readiness:

```bash
CF_SMOKE_STRICT_EXTERNAL=1 python3.12 test_components.py
```

## Daily production flow

### 1. Generate the queue on a Mac or workstation

```bash
python3.12 run.py prod-auto-pin \
  --niche fonts \
  --pages 10 \
  --limit 300 \
  --output output/prod/fonts_$(date +%Y%m%d) \
  --upload-vds \
  --sync-sheet
```

This does the following:
1. scrapes Creative Fabrica;
2. downloads the preview cards;
3. generates Pinterest pins and metadata;
4. uploads `pin_01.jpg` to the VDS;
5. writes queue metadata into the `fonts` sheet.

### 2. Publish from the queue

You have two production options:

#### Option A. n8n + Pinterest API
Use the workflows in `/n8n`:
- `pinterest_cf_fonts_publish.json`
- `pinterest_cf_fonts_cleanup.json`

#### Option B. Android phone automation
- `scheduler.py` runs 5 times per day with jitter;
- every slot does `warmup -> random wait 5-15 min -> post`;
- intended for a Linux server with a permanently attached Android phone.

## Logging

Every main entrypoint now writes both to stdout and a rotating file in `logs/`.

Default files:
- `logs/run.log`
- `logs/phones.log`
- `logs/scheduler.log`
- `logs/test_components.log`
- `logs/main.log`

Controls:

```env
CF_LOG_LEVEL=INFO
CF_LOG_DIR=logs
CF_LOG_MAX_BYTES=5242880
CF_LOG_BACKUP_COUNT=5
```

## Local queue database

Default path: `output/_state/queue.db`

Use cases:
- reliable local queue state between runs;
- analytics and experiments without Google Sheets dependency;
- foundation for modular architecture evolution.

Data model:
- `queue_items` (technical): full operational fields (`slug/status/pin_jpg/source_file/...`) for retries and debugging.
- `publish_items` (final): cleaned publish payload (`title/description/image_url/target_url/status`) for integrations like n8n.
- `queue_sync_runs`: history of each sync/import for auditability and run analytics.

## Docker

The repository includes three container roles:
- `cf-runner` — one-off command runner;
- `cf-batch-cron` — daily cron batch service;
- `cf-phone-scheduler` — phone automation scheduler container.

See:
- [DOCKER.md](DOCKER.md)
- [DOCKER_QUICKSTART.md](DOCKER_QUICKSTART.md)

## Server deployment

Phone automation deployment assets live in `deploy/`:
- `deploy/cf-pinterest-scheduler.service`
- `deploy/99-android-xiaomi.rules`
- `deploy/bootstrap_phone_server.sh`

Typical server flow:

```bash
scp -r . nick@server:/home/nick/cf-pinterest-parser
ssh nick@server
cd /home/nick/cf-pinterest-parser
bash deploy/bootstrap_phone_server.sh
```

For the current Linux phone host, the active service name is:
- `cf-pinterest-scheduler.service`

## Known operational risks

### 1. Creative Fabrica / Cloudflare
Creative Fabrica is the least deterministic part of the stack. A workstation can occasionally get stuck on `Just a moment...` instead of returning product cards. The parser now logs challenge detection and waits longer before failing, but Cloudflare is still an external risk.

### 2. Android ADB availability
Phone automation is only reliable when the server permanently sees the device as `device` in `adb devices`. USB resets, revoked RSA trust, or disconnected cables will cause the scheduler to skip slots instead of crashing.

### 3. Docker + USB
The phone scheduler container is included, but for maximum reliability many setups still prefer host-level `systemd` + `adb` instead of containerized USB automation.

## Validation status

### Confirmed working historically
- `2026-05-02` production batch:
  - processed `500` items;
  - generated `462` pins;
  - rejected `38`;
  - uploaded `462` JPG files to VDS;
  - synced the sheet successfully.

### Confirmed in this refresh pass (`2026-05-17`)
- Google Sheets connectivity works.
- Playwright Chromium is installed and launches.
- Auto-pin smoke processing on local sample input works.
- Scheduler dry-run works.
- New phone server `192.168.10.105` has the scheduler service running.

### Still requiring attention
- Live Creative Fabrica parsing on the current local workstation is intermittently blocked by Cloudflare (`Just a moment...`).
- Docker manifests have been updated, but a full `docker compose build` was not executed in this workstation because Docker CLI is not installed here.
- The new phone server was alive during validation, but at the moment of the last check it had no `adb` device attached.

## Useful commands

```bash
# Local smoke check
python3.12 test_components.py

# One-off production batch
python3.12 run.py prod-auto-pin --niche fonts --pages 1 --limit 20 --output output/prod/fonts --upload-vds --sync-sheet

# Scheduler dry-run
python3.12 scheduler.py --dry-run

# Phone warmup
python3.12 run_phones.py warmup
```

## GitHub readiness checklist

- [ ] `.env` is not committed.
- [ ] `credentials.json` is not committed.
- [ ] `output/` and `logs/` are excluded.
- [ ] `README.md`, Docker docs, smoke report, and `INIT.md` reflect the current workflow.
- [ ] VDS, Google Sheets, and Pinterest credentials are documented outside the repository.
