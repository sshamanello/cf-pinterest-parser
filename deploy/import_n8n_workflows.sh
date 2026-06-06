#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! docker compose ps n8n >/dev/null 2>&1; then
  echo "n8n service is not available in docker compose config"
  exit 1
fi

docker compose --profile n8n up -d n8n

docker compose exec -T n8n n8n import:workflow --input=/workflows/pinterest_cf_fonts_publish.json
if ! docker compose exec -T n8n n8n import:workflow --input=/workflows/pinterest_cf_fonts_cleanup.json; then
  echo "Warning: cleanup workflow import failed; publish workflow is imported and usable."
fi

echo "Workflows imported. Verify the Postgres + Pinterest credentials are present in n8n."
