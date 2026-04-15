#!/bin/bash
set -e

echo "[$(date)] CF Pinterest Parser - Docker Entrypoint"

# Если передан аргумент "cron" — запускаем cron-демон
if [ "$1" = "cron" ]; then
    echo "[$(date)] Starting in CRON mode..."
    
    # Создаём crontab из переменной окружения CRON_SCHEDULE (по умолчанию 09:00 UTC)
    CRON_SCHEDULE="${CRON_SCHEDULE:-0 9 * * *}"
    
    echo "[$(date)] Cron schedule: $CRON_SCHEDULE"
    echo "$CRON_SCHEDULE cd /app && python main.py >> /var/log/cf-parser.log 2>&1" > /etc/cron.d/cf-parser
    
    # Права на crontab
    chmod 0644 /etc/cron.d/cf-parser
    
    # Применяем crontab
    crontab /etc/cron.d/cf-parser
    
    # Создаём лог-файл
    touch /var/log/cf-parser.log
    
    echo "[$(date)] Cron configured. Logs: /var/log/cf-parser.log"
    echo "[$(date)] Starting cron daemon..."
    
    # Запускаем cron в foreground и tail логов
    cron && tail -f /var/log/cf-parser.log
    
else
    # Обычный режим — запуск один раз
    echo "[$(date)] Starting in ONE-TIME mode..."
    exec python main.py
fi
