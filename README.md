# CF Pinterest Parser

Production-oriented toolkit for turning Creative Fabrica products into a Pinterest publishing queue.

The repository is organized around three exact stages:

1. Download.
2. Process.
3. Publish.

That is the operational model for the `fonts` MVP:
- download Creative Fabrica product previews and metadata;
- process them into ready-to-publish pin assets;
- publish through either `n8n` or Android phone automation;
- keep daily posting warm and human-looking through a jittered scheduler;
- store a local SQLite queue for stable retries and analytics experiments.

How to read the repo:
- `download` = scraping, queue import/export, and source metadata;
- `process` = image extraction, overlay generation, and final pin rendering;
- `publish` = Pinterest delivery, retries, cleanup, and status updates.

## Current architecture

```text
Creative Fabrica
  -> download
  -> process
  -> publish

download:
  parser.py / extractor.py / cf_pinterest queue sync

process:
  image_processor.py / comfy_processor.py / font_publish_pipeline.py
  -> output/prod/.../pin_01.jpg

publish:
  n8n (base64 from local pin_path) or Android phone automation
  -> Pinterest
```

## Main entrypoints

### 1. Download
- `python run.py parse`
  Legacy all-category parse + image processing + Google Sheets write.
- `python run.py test`
  Quick one-page parse smoke.
- `python run.py pins`
  Generate pins from source data without the full batch flow.
- `python run.py import-queue-file --file <queue.csv|queue.xlsx> --tab fonts --db-path output/_state/queue.db`
  Imports historical/exported spreadsheet data into local queue DB.
- `python run.py export-n8n-queue --tab fonts --destination local`
  Exports final publish dataset for n8n ingestion (CSV by default) with destination preset (`local` or `remote`).
  Optional flags:
  - `--destination local|remote`
  - `--format csv|json`
  - `--profile n8n_default|n8n_minimal`
  - `--statuses generated,uploaded` (or empty for all statuses)
  - `--output <custom-file>` (override destination default path)
  Profiles are loaded from `cf_pinterest/export_profiles.json`.
  You can override profile config path with `CF_N8N_EXPORT_PROFILES_PATH=/path/to/profiles.json`.
- `python run.py prepare-n8n-export --tab fonts --destination local`
  One-shot helper for local/remote n8n: syncs latest `auto_pin_batch_report*.json` to queue DB, then exports publish dataset.
  Optional flags:
  - `--report <report.json>` (if omitted, latest report from `output/prod/<tab>/_reports/` is used)
  - `--skip-sync` (export from current DB state only)
  - all export flags from `export-n8n-queue` (`--format`, `--profile`, `--statuses`, `--output`, `--limit`)

### 2. Process
- `python run.py auto-pin --input <file-or-dir> --output <dir>`
  Runs the automatic pin builder on existing preview files.
- `python run.py prod-auto-pin --niche fonts --pages 10 --limit 300 --output output/prod/fonts_YYYYMMDD --upload-vds --sync-sheet`
  Main production batch: parse -> download previews -> generate pins -> publish JPG assets locally or to the configured image host -> sync Google Sheet.
- `python run.py upload-vds --report <report.json> --sync-sheet --tab fonts`
  Publishes already-generated JPG assets from an existing report, writing `public_image_url`, `remote_image_path`, and upload status into the report/queue metadata.
- `python run.py upload-vds-batch --reports-glob 'output/**/_reports/auto_pin_batch_report.json' --sync-sheet --tab fonts`
  Batch-publishes JPG assets from many reports using one Google Sheets session, useful for migrating old queue rows to a new host or local publish path.
- `python run.py sync-queue-db --report <report.json> --tab fonts --db-path output/_state/queue.db`
  Upserts report data into local SQLite queue storage.
- `python run.py db-health --db-path output/_state/queue.db`
  Validates DB schema/indexes and key invariants (including orphan rows in `publish_items`).
- `python run.py queue-stats --db-path output/_state/queue.db --tab fonts --runs-limit 10`
  Shows queue analytics summary (status counts + recent sync runs). Use `--all-niches` for global view.
- `python run.py queue-rebuild-publish --db-path output/_state/queue.db --all-niches`
  Rebuilds `publish_items` from `queue_items` after manual fixes/migrations.
- `python run.py queue-prune --db-path output/_state/queue.db --keep-sync-runs 200 --prune-rejected-older-than-days 30`
  Shows what would be deleted (dry-run). Add `--apply` to actually delete old data.
- `python run.py ops-check --db-path output/_state/queue.db --tab fonts --runs-limit 10`
  Runs preflight checks in one command: smoke checks, DB health, and queue stats.
- `python run.py prod-auto-pin --niche fonts --limit 20 --products-file ./test/products_seed.json`
  Runs production pipeline from local product JSON (bypasses Creative Fabrica parsing; useful during Cloudflare blocks).
  Product item may contain either:
  - `image_url` (download path), or
  - `local_image_path` (offline local file path).

### 3. Publish
- `python run_phones.py devices`
- `python run_phones.py warmup`
- `python run_phones.py post --tab fonts`
- `python scheduler.py`
- `python test_components.py`

## Repository layout

```text
root launchers
  run.py                     Launcher for the full CLI
  main.py                    Legacy startup wrapper
  run_phones.py              Launcher for Android phone automation
  scheduler.py               Launcher for the daily phone scheduler
  test_components.py         Smoke checks

download/
  parser.py                  Playwright scraper for Creative Fabrica category pages
  extractor.py               Image extraction and masking pipeline
  sheets.py                  Google Sheets auth, upsert, and queue helpers

process/
  image_processor.py         Automatic pin builder from downloaded previews
  comfy_processor.py         Optional ComfyUI-assisted image processing
  font_generator.py          Font asset generation helpers
  font_publish_pipeline.py    Publish-ready image composition pipeline
  auto_pin_pipeline.py       Automatic pin batch orchestrator
  prod_auto_pin_pipeline.py  Production batch orchestrator
  fix_headers.py             Sheet header repair helper

publish/
  n8n/                       Importable n8n workflows for publish/cleanup
  pinterest_post.py          Android UI automation for posting one ready pin
  pinterest_warmup.py        Human-like Pinterest warmup behaviors
shared/
  run.py                     Main CLI across download/process/publish stages
  main.py                    Compatibility entrypoint
  config.py                  Shared constants and categories
  logging_utils.py           Shared rotating-file + console logging setup

deploy/
  systemd units and bootstrap helpers for phone/server deployment

root infra
  docker-compose.yml         Container roles for one-off runs, cron batches, and phone scheduler
  Dockerfile                 Container image for the pipeline
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
- `VDS_*` for legacy remote publishing, or `CF_IMAGE_BACKEND` + `CF_LOCAL_*` for hosting images directly on this server.
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

### 1. Download

This stage does the following:
1. scrapes Creative Fabrica;
2. downloads the preview cards;
3. writes queue metadata into the `fonts` sheet;
4. exports ready rows to local SQLite and n8n-friendly payloads.

### 2. Process

```bash
python3.12 run.py prod-auto-pin \
  --niche fonts \
  --pages 10 \
  --limit 300 \
  --output output/prod/fonts_$(date +%Y%m%d) \
  --upload-vds \
  --sync-sheet
```

This stage does the following:
1. generates Pinterest pins and metadata;
2. produces the final `pin_01.jpg`;
3. uploads the JPG to the configured image host, or keeps it local for base64 publication.

### 3. Publish from the queue

You have two production options:

#### Option A. n8n + Pinterest API
Use the workflows in `/n8n`:
- `pinterest_cf_fonts_publish.json`
- `pinterest_cf_fonts_cleanup.json`

The current CF fonts publish workflow is backed by PostgreSQL, reads the processed JPG directly from `pin_path`, converts it to base64, and posts that payload to Pinterest. `public_image_url` is still supported in the broader batch pipeline and queue metadata, but it is no longer required by the live CF fonts workflow.

#### Option B. Android phone automation
- `scheduler.py` runs 5 times per day with jitter;
- every slot does `warmup -> random wait 5-15 min -> post`;
- intended for a Linux server with a permanently attached Android phone.

## Logging

Every main entrypoint now writes both to stdout and a rotating file in `logs/`.
Structured publish events also go to `data/logs/events.log`.

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

The repository includes four container roles:
- `cf-runner` — one-off command runner;
- `cf-batch-cron` — daily cron batch service;
- `cf-phone-scheduler` — phone automation scheduler container.
- `n8n` — workflow automation + Pinterest posting.

See:
- [DOCKER.md](DOCKER.md)
- [DOCKER_QUICKSTART.md](DOCKER_QUICKSTART.md)

## Server deployment

Phone automation deployment assets live in `deploy/`:
- `deploy/cf-pinterest-scheduler.service`
- `deploy/cf-pinterest-media.service`
- `deploy/99-android-xiaomi.rules`
- `deploy/bootstrap_phone_server.sh`
- `deploy/import_n8n_workflows.sh`

Typical server flow:

```bash
rsync -av --delete \
  --exclude '.git' --exclude '.venv' --exclude 'output' --exclude 'logs' \
  ./ nick@YOUR_SERVER_IP:/home/nick/cf-pinterest-parser/

ssh nick@server
cd /home/nick/cf-pinterest-parser
bash deploy/bootstrap_phone_server.sh
docker compose --profile n8n up -d n8n
bash deploy/import_n8n_workflows.sh
```

For the current Linux phone host, the active service name is:
- `cf-pinterest-scheduler.service`
- `cf-pinterest-media.service`

Minimal validation on server:

```bash
systemctl status cf-pinterest-media.service --no-pager
docker compose ps n8n cf-batch-cron
curl -I http://YOUR_SERVER_IP:8088/pins/ready/
curl -I http://YOUR_SERVER_IP:5680/
```

Note: on `YOUR_SERVER_IP` port `5678` may already be occupied by a host-level n8n process, so project docker n8n defaults to `5680`.

Operational chain (target state):
1. `cf-batch-cron` runs `run.py prod-auto-pin` and creates pins.
2. Local backend publishes images into `published/pins/ready` and serves them on `:8088`.
3. `n8n` workflow `Pinterest CF Fonts Publish` picks `ready` rows from Google Sheet and posts to Pinterest.

Required env for Pinterest API posting in n8n workflow:
- `PINTEREST_ACCESS_TOKEN` (Pinterest API bearer token with pin create scope)
- `PINTEREST_BOARD_ID` (target board id)

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
- New phone server `YOUR_PHONE_SERVER_IP` has the scheduler service running.

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
