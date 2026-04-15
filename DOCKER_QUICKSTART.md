# Docker Quick Start

> Быстрая шпаргалка по запуску проекта в Docker

---

## Два режима работы

### 1️⃣ Разовый запуск
```bash
docker compose run --rm cf-parser
```
Запускается один раз, выполняет парсинг, выходит.

### 2️⃣ Автоматический cron-демон
```bash
docker compose up -d cf-parser-cron
```
Работает постоянно, выполняет парсинг по расписанию (по умолчанию: 09:00 UTC ежедневно).

---

## Быстрая установка

```bash
# 1. Убедитесь, что Docker запущен
docker --version

# 2. Создайте .env и credentials.json
cp .env.example .env
# Отредактируйте .env и добавьте credentials.json

# 3. Соберите образ
docker compose build

# 4. Запустите (выберите режим)
docker compose run --rm cf-parser              # разовый запуск
# ИЛИ
docker compose up -d cf-parser-cron            # cron-демон
```

---

## Управление cron-демоном

```bash
# Запустить
docker compose up -d cf-parser-cron

# Остановить
docker compose down

# Логи в реальном времени
docker compose logs -f cf-parser-cron

# Логи парсинга
tail -f logs/cf-parser.log

# Статус
docker compose ps
```

---

## Настройка расписания

Отредактируйте `.env`:

```env
CRON_SCHEDULE=0 9 * * *     # 09:00 UTC ежедневно (по умолчанию)
CRON_SCHEDULE=0 */6 * * *   # каждые 6 часов
CRON_SCHEDULE=30 8 * * 1    # каждый понедельник в 08:30
```

Перезапустите:
```bash
docker compose down
docker compose up -d cf-parser-cron
```

---

## Примеры

```bash
# Тестовый запуск (1 страница)
docker compose run --rm -e PAGES_PER_RUN=1 cf-parser

# Массовый сбор (50 страниц)
docker compose run --rm -e PAGES_PER_RUN=50 cf-parser

# Обновить код и пересобрать
git pull
docker compose build
docker compose up -d cf-parser-cron
```

---

## Структура файлов

```
cf-pinterest-parser/
├── .env                    ← переменные окружения
├── credentials.json        ← Google Service Account ключ
├── docker-compose.yml      ← конфигурация
├── Dockerfile              ← образ
├── docker-entrypoint.sh    ← entrypoint
├── output/                 ← сгенерированные пины (bind-mount)
└── logs/                   ← логи cron (создаётся автоматически)
    └── cf-parser.log
```

---

## Troubleshooting

### Docker не запущен
```bash
# Windows: запустите Docker Desktop
# Linux: sudo systemctl start docker
```

### Chromium падает
```bash
# Увеличьте shm_size в docker-compose.yml
shm_size: '2gb'
```

### Нет доступа к ComfyUI на хосте
```bash
# В .env используйте:
# Windows/Mac:
COMFY_URL=http://host.docker.internal:8188

# Linux:
COMFY_URL=http://172.17.0.1:8188
```

---

**Полная документация:** см. `DOCKER.md`
