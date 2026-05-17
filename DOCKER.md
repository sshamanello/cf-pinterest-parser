# Docker guide

This repository ships with Docker manifests for the real current workflow, not the old `main.py` cron-only setup.

## Services

### `cf-runner`
One-off command runner.

Default behavior:
```bash
docker compose run --rm cf-runner
```

Override with any command, for example:
```bash
docker compose run --rm cf-runner run python run.py prod-auto-pin --niche fonts --pages 1 --limit 20 --output output/docker_prod --upload-vds --sync-sheet
```

### `cf-batch-cron`
Background cron container that repeatedly runs the batch command from `CF_RUN_COMMAND` using the schedule in `CF_CRON_SCHEDULE`.

Start it:
```bash
docker compose --profile cron up -d cf-batch-cron
```

### `cf-phone-scheduler`
Containerized phone scheduler.

Start it:
```bash
docker compose --profile phone up -d cf-phone-scheduler
```

Important:
- it mounts `/dev/bus/usb` from the Linux host;
- it is meant for Linux only;
- for highest reliability, host-level `systemd` is still a valid alternative.

## Build

```bash
docker compose build
```

What the image contains:
- Python dependencies from `requirements.txt`;
- Playwright Chromium;
- `adb`;
- `cron`;
- `openssh-client` + `expect` for VDS upload flows.

## Entrypoint modes

The image entrypoint supports these modes:

### `run`
```bash
docker compose run --rm cf-runner run python run.py --help
```

### `cron`
Used by `cf-batch-cron`.

It writes a cron job like:
```text
CF_CRON_SCHEDULE cd /app && CF_RUN_COMMAND >> /app/logs/docker-cron.log 2>&1
```

### `scheduler`
Used by `cf-phone-scheduler`.

It launches:
```bash
python scheduler.py
```

### `shell`
```bash
docker compose run --rm cf-runner shell
```

## Required mounted files

The compose file mounts:
- `./output:/app/output`
- `./logs:/app/logs`
- `./credentials.json:/app/credentials.json:ro`

You are expected to provide:
- `.env`
- `credentials.json`

## Environment variables

### Core
```env
GOOGLE_SHEET_ID=...
CF_AFFILIATE_ID=7029352
GOOGLE_CREDENTIALS_PATH=credentials.json
```

### Batch / VDS
```env
VDS_SSH_HOST=...
VDS_SSH_USER=...
VDS_SSH_PORT=22
VDS_SSH_PASSWORD=
VDS_REMOTE_DIR=/var/www/html/pins/ready
VDS_PUBLIC_BASE_URL=https://example.com/pins/ready
```

### Logging
```env
CF_LOG_LEVEL=INFO
CF_LOG_DIR=/app/logs
CF_LOG_MAX_BYTES=5242880
CF_LOG_BACKUP_COUNT=5
```

### Cron container
```env
CF_CRON_SCHEDULE=0 2 * * *
CF_RUN_COMMAND=python run.py prod-auto-pin --niche fonts --pages 1 --limit 20 --output output/docker_prod --upload-vds --sync-sheet
```

## Common commands

### Smoke checks in Docker
```bash
docker compose run --rm cf-runner run python test_components.py
```

### One-off production batch
```bash
docker compose run --rm cf-runner run python run.py prod-auto-pin --niche fonts --pages 1 --limit 20 --output output/docker_prod --upload-vds --sync-sheet
```

### Start the cron batch service
```bash
docker compose --profile cron up -d cf-batch-cron
```

### Start the phone scheduler service
```bash
docker compose --profile phone up -d cf-phone-scheduler
```

### Stop background services
```bash
docker compose --profile cron --profile phone down
```

## Logs

Host-side logs:
- `./logs/docker-cron.log`
- `./logs/run.log`
- `./logs/phones.log`
- `./logs/scheduler.log`
- `./logs/test_components.log`

Useful commands:
```bash
tail -f logs/docker-cron.log
```

```bash
docker compose logs -f cf-batch-cron
```

```bash
docker compose logs -f cf-phone-scheduler
```

## Recommended production split

For a stable production system, this split works best:

### Workstation / Mac
Run the heavy batch generation manually or on demand:
```bash
python3.12 run.py prod-auto-pin --niche fonts --pages 10 --limit 300 --output output/prod/fonts_$(date +%Y%m%d) --upload-vds --sync-sheet
```

### Linux phone server
Run only the posting scheduler:
- host `adb`
- Android phone over USB
- `scheduler.py` via `systemd` or `cf-phone-scheduler`

This keeps image processing away from the phone host and lets the posting machine stay small and stable.

## Troubleshooting

### Docker CLI missing on the current machine
If `docker compose` is not available, the manifests can still be reviewed and committed, but build/run validation must happen on a machine with Docker installed.

### `credentials.json` mount errors
Make sure the file exists next to `docker-compose.yml`.

### `adb devices` empty inside `cf-phone-scheduler`
Check on the host first:
```bash
adb devices -l
```

If the host also sees no device, fix USB/debugging before touching the container.

### Creative Fabrica returns `Just a moment...`
This is Cloudflare. The parser now waits longer and logs the challenge, but repeated failures still require rerunning from a cleaner IP/session.
