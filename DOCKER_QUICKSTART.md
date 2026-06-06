# Docker quickstart

## 1. Prepare secrets

```bash
cp .env.example .env
# add real values to .env
# place credentials.json in the repo root
```

## 2. Build

```bash
docker compose build
```

## 3. Run one command

Show CLI help:
```bash
docker compose run --rm cf-runner
```

Run smoke checks:
```bash
docker compose run --rm cf-runner run python test_components.py
```

Run a production fonts batch:
```bash
docker compose run --rm cf-runner run python run.py prod-auto-pin --niche fonts --pages 1 --limit 20 --output output/docker_prod --upload-vds --sync-sheet
```

## 4. Start background services

Cron batch service:
```bash
docker compose --profile cron up -d cf-batch-cron
```

Phone scheduler service:
```bash
docker compose --profile phone up -d cf-phone-scheduler
```

## 5. Watch logs

```bash
tail -f logs/docker-cron.log
```

```bash
docker compose logs -f cf-batch-cron
```

```bash
docker compose logs -f cf-phone-scheduler
```

## 6. Stop everything

```bash
docker compose --profile cron --profile phone down
```

## Notes

- `cf-runner` is for one-off commands.
- `cf-batch-cron` is for repeated content generation.
- `cf-phone-scheduler` is for Android posting automation and needs Linux USB access.
- If you need maximum stability for ADB, host-level `systemd` is still a good choice.

## More details

See [DOCKER.md](DOCKER.md).
