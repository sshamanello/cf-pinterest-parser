# CLAUDE.md — Project instructions for AI assistants

## Project purpose

This repository builds and operates a Pinterest content pipeline around Creative Fabrica products.

The current production focus is the `fonts` niche:
- scrape Creative Fabrica product previews;
- build Pinterest-ready pins with the automatic layout system;
- upload JPG assets to a VDS;
- sync queue metadata to Google Sheets;
- publish either via `n8n` or Android phone automation.

## Current architecture

```text
parser.py -> prod_auto_pin_pipeline.py -> auto_pin_pipeline.py -> VDS upload -> Google Sheet
                                                              -> n8n / Android post flow
```

## Primary entrypoints

### Content pipeline
- `run.py`
  Main CLI.
- `prod_auto_pin_pipeline.py`
  Main production orchestrator for parse -> download -> generate -> upload.
- `auto_pin_pipeline.py`
  Automatic pin generation and reporting.
- `sheets.py`
  Google Sheets access and upsert logic.

### Phone automation
- `run_phones.py`
  CLI for `devices`, `warmup`, and `post`.
- `pinterest_warmup.py`
  Human-like warmup behaviors.
- `pinterest_post.py`
  Android UI automation for posting one ready pin.
- `scheduler.py`
  Jittered daily scheduler.

### Operations and deployment
- `logging_utils.py`
  Shared rotating-file logging.
- `deploy/`
  systemd unit, udev rule, bootstrap script.
- `docker-compose.yml`
  Container roles for one-off runs, cron batches, and phone scheduler.
- `n8n/`
  Workflow exports for publish and cleanup.

## Key realities

### 1. Creative Fabrica is the unstable edge
The parser depends on Playwright because CF is behind Cloudflare and dynamic rendering.

Important:
- parsing can intermittently fail with `Just a moment...`;
- this is an external anti-bot behavior, not always a code bug;
- the parser now logs Cloudflare detection and waits longer before failing.

### 2. The real production command is `prod-auto-pin`
For fonts MVP, the most important command is:

```bash
python run.py prod-auto-pin --niche fonts --pages 10 --limit 300 --output output/prod/fonts_YYYYMMDD --upload-vds --sync-sheet
```

That is the actual queue-building workflow.

### 3. Phone automation is separate from batch generation
The posting machine should ideally be a Linux host with:
- `adb`
- one attached Android phone
- `scheduler.py` under `systemd`

Do not assume the content-generation workstation and the posting host are the same machine.

### 4. n8n and phone automation are different lanes
- `n8n` is used for workflow orchestration and API-based publishing paths.
- `scheduler.py` + `run_phones.py` are the Android automation lane.
- They can coexist, but they are not the same system.

## Logging

All main entrypoints should use `logging_utils.configure_logging(...)`.

Default log files:
- `logs/run.log`
- `logs/main.log`
- `logs/phones.log`
- `logs/scheduler.log`
- `logs/test_components.log`

Relevant env vars:
- `CF_LOG_LEVEL`
- `CF_LOG_DIR`
- `CF_LOG_MAX_BYTES`
- `CF_LOG_BACKUP_COUNT`

## Environment variables

### Required
- `GOOGLE_SHEET_ID`
- `CF_AFFILIATE_ID`
- `GOOGLE_CREDENTIALS_PATH` or `GOOGLE_CREDENTIALS`

### VDS upload
- `VDS_SSH_HOST`
- `VDS_SSH_USER`
- `VDS_SSH_PORT`
- `VDS_SSH_PASSWORD`
- `VDS_REMOTE_DIR`
- `VDS_PUBLIC_BASE_URL`

### Phone automation
- `PHONE_ACCOUNTS`

### Docker cron defaults
- `CF_CRON_SCHEDULE`
- `CF_RUN_COMMAND`

## Validation expectations

When touching the pipeline, prefer these checks:

```bash
python test_components.py
```

```bash
python scheduler.py --dry-run
```

```bash
python run.py prod-auto-pin --niche fonts --pages 1 --limit 5 --output output/prod/smoke
```

If Docker is part of the change:

```bash
docker compose build
```

```bash
docker compose run --rm cf-runner run python test_components.py
```

## Files that must not be committed
- `.env`
- `credentials.json`
- `output/`
- `logs/`

## Important working rule
Every project change must also update `INIT.md` in the same commit.
