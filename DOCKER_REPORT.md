# Docker packaging report

**Date:** 2026-05-17
**Branch:** `feature/android-phone-automation`

## What changed

The Docker layer was rebuilt around the real 2026 production flow.

### Replaced the old assumptions
Removed the old documentation/configuration bias around:
- `python main.py`
- the old `cf-parser` / `cf-parser-cron` naming
- single-purpose cron parsing only

### Introduced current service roles
The Docker setup now models three roles:
- `cf-runner`
- `cf-batch-cron`
- `cf-phone-scheduler`

### Updated the image contents
The image now includes:
- Python dependencies from `requirements.txt`
- Playwright Chromium
- `adb`
- `cron`
- `openssh-client`
- `expect`

### Updated the entrypoint
The entrypoint now supports:
- `run`
- `cron`
- `scheduler`
- `shell`

### Aligned logging
Dockerized runs write to the same `logs/` structure as local runs, with rotating-file logging configured by the Python entrypoints.

## Files updated in this pass
- [Dockerfile](Dockerfile)
- [docker-compose.yml](docker-compose.yml)
- [docker-entrypoint.sh](docker-entrypoint.sh)
- [DOCKER.md](DOCKER.md)
- [DOCKER_QUICKSTART.md](DOCKER_QUICKSTART.md)

## Validation status

### Verified in this environment
- Docker manifests were rewritten to match the actual runtime commands.
- The Python code used by the containers passed local syntax compilation.
- Smoke checks for the non-Docker pipeline were executed separately.

### Not verified in this environment
A full `docker compose build` / `docker compose run` was not executed during this refresh because Docker CLI is not installed on the current workstation.

That means the Docker layer is prepared and documented, but final build/runtime validation still needs to happen on a host with Docker installed.

## Recommended validation on the first Docker-capable host

```bash
docker compose build
```

```bash
docker compose run --rm cf-runner run python test_components.py
```

```bash
docker compose run --rm cf-runner run python run.py prod-auto-pin --niche fonts --pages 1 --limit 5 --output output/docker_prod_smoke --upload-vds --sync-sheet
```

## Final note

The repository is now prepared for GitHub with a Docker layer that reflects the real production topology. The remaining gap is runtime confirmation on a machine where Docker is actually installed.
