#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/app
LOG_DIR=${CF_LOG_DIR:-/app/logs}
CRON_LOG_FILE=${CF_CRON_LOG_FILE:-$LOG_DIR/docker-cron.log}
CRON_SCHEDULE=${CF_CRON_SCHEDULE:-15 */4 * * *}
RUN_COMMAND_DEFAULT="python run.py prod-auto-pin --niche fonts --pages 10 --limit 10 --output output/docker_prod --upload-vds --sync-sheet"
RUN_COMMAND=${CF_RUN_COMMAND:-$RUN_COMMAND_DEFAULT}

mkdir -p "$APP_DIR/output" "$LOG_DIR"
cd "$APP_DIR"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

start_cron() {
  local command="${*:-$RUN_COMMAND}"
  log "Starting cron mode"
  log "Cron schedule: $CRON_SCHEDULE"
  log "Cron command: $command"

  # Cron does not inherit container env reliably, so persist it explicitly.
  printenv | while IFS='=' read -r name value; do
    printf 'export %s=%q\n' "$name" "$value"
  done > /tmp/cf_env.sh
  chmod 0600 /tmp/cf_env.sh

  cat > /usr/local/bin/cf-cron-run.sh <<RUNNER
#!/usr/bin/env bash
set -euo pipefail
set -a
source /tmp/cf_env.sh
set +a
cd /app
$command
RUNNER
  chmod 0755 /usr/local/bin/cf-cron-run.sh

  cat > /etc/cron.d/cf-pinterest <<CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$CRON_SCHEDULE /usr/local/bin/cf-cron-run.sh >> $CRON_LOG_FILE 2>&1
CRON

  chmod 0644 /etc/cron.d/cf-pinterest
  crontab /etc/cron.d/cf-pinterest
  touch "$CRON_LOG_FILE"

  cron
  exec tail -F "$CRON_LOG_FILE"
}

case "${1:-run}" in
  run)
    shift || true
    if [ "$#" -eq 0 ]; then
      exec python run.py --help
    fi
    log "Running one-off command: $*"
    exec "$@"
    ;;
  cron)
    shift || true
    start_cron "$@"
    ;;
  scheduler)
    shift || true
    log "Starting phone scheduler"
    exec python scheduler.py "$@"
    ;;
  shell)
    shift || true
    exec bash "$@"
    ;;
  *)
    log "Running custom entrypoint command: $*"
    exec "$@"
    ;;
esac
