# Docker — Руководство по запуску

> Два режима работы: разовый запуск и автоматический cron-демон

---

## Быстрый старт

### 1. Подготовка файлов

```bash
# Убедитесь, что есть:
ls -la
# .env                  ← переменные окружения
# credentials.json      ← Google Service Account ключ
# docker-compose.yml    ← конфигурация Docker
```

### 2. Сборка образа

```bash
docker compose build
```

Это займёт 3-5 минут (установка Chromium + зависимости).

---

## Режим 1: Разовый запуск

Запустить парсинг один раз и выйти:

```bash
docker compose run --rm cf-parser
```

**Что происходит:**
- Контейнер запускается
- Выполняется `python main.py`
- Результаты сохраняются в `./output/` и Google Sheets
- Контейнер удаляется (`--rm`)

**Когда использовать:**
- Тестирование
- Ручной запуск
- Первоначальный массовый сбор

**Пример с переопределением переменных:**

```bash
# Собрать 50 страниц вместо 3
docker compose run --rm -e PAGES_PER_RUN=50 cf-parser
```

---

## Режим 2: Автоматический cron-демон

Запустить контейнер, который будет работать постоянно и выполнять парсинг по расписанию:

```bash
docker compose up -d cf-parser-cron
```

**Что происходит:**
- Контейнер запускается в фоне (`-d`)
- Внутри настраивается cron
- Парсинг выполняется по расписанию (по умолчанию: 09:00 UTC ежедневно)
- Логи пишутся в `./logs/cf-parser.log`

### Настройка расписания

Отредактируйте `.env`:

```env
# Формат: минута час день месяц день_недели
CRON_SCHEDULE=0 9 * * *     # 09:00 UTC ежедневно (по умолчанию)
CRON_SCHEDULE=0 */6 * * *   # каждые 6 часов
CRON_SCHEDULE=30 8 * * 1    # каждый понедельник в 08:30 UTC
CRON_SCHEDULE=0 0 1 * *     # 1-го числа каждого месяца в 00:00 UTC
```

После изменения `.env`:

```bash
docker compose down
docker compose up -d cf-parser-cron
```

### Управление cron-демоном

```bash
# Просмотр логов в реальном времени
docker compose logs -f cf-parser-cron

# Остановить демон
docker compose down

# Перезапустить демон
docker compose restart cf-parser-cron

# Проверить статус
docker compose ps
```

### Просмотр логов парсинга

```bash
# Логи cron-демона
tail -f logs/cf-parser.log

# Или через Docker
docker compose exec cf-parser-cron tail -f /var/log/cf-parser.log
```

---

## Структура файлов

```
cf-pinterest-parser/
├── .env                      ← переменные окружения
├── credentials.json          ← Google Service Account (НЕ коммитить!)
├── docker-compose.yml        ← конфигурация Docker
├── Dockerfile                ← образ контейнера
├── docker-entrypoint.sh      ← entrypoint-скрипт
├── output/                   ← сгенерированные пины (bind-mount)
│   └── *.jpg
├── logs/                     ← логи cron-демона (создаётся автоматически)
│   └── cf-parser.log
└── *.py                      ← исходный код
```

---

## Переменные окружения (.env)

```env
# Обязательные
GOOGLE_SHEET_ID=1h6ZYtQUwT77z66-feJMZD84XIwIFmy83ClMy-_iWbWg
CF_AFFILIATE_ID=7029352
GOOGLE_CREDENTIALS_PATH=credentials.json

# Опциональные
PAGES_PER_RUN=3
CRON_SCHEDULE=0 9 * * *  # только для cf-parser-cron

# ComfyUI (если запущен на хосте)
COMFY_URL=http://host.docker.internal:8188  # Windows/Mac
# COMFY_URL=http://172.17.0.1:8188          # Linux
COMFY_MODEL=realisticVisionV51.safetensors
COMFY_DENOISE=0.50
```

---

## Volumes (монтирование)

| Путь в контейнере | Путь на хосте | Описание |
|---|---|---|
| `/app/output` | `./output` | Сгенерированные Pinterest-пины |
| `/app/credentials.json` | `./credentials.json` | Google Service Account ключ (read-only) |
| `/var/log` | `./logs` | Логи cron-демона (только cf-parser-cron) |

**Важно:** `credentials.json` монтируется в режиме read-only (`:ro`) для безопасности.

---

## Примеры использования

### Тестовый запуск (1 страница fonts)

```bash
docker compose run --rm -e PAGES_PER_RUN=1 cf-parser
```

### Первоначальный массовый сбор (50 страниц)

```bash
docker compose run --rm -e PAGES_PER_RUN=50 cf-parser
```

### Запуск cron-демона с кастомным расписанием

```bash
# В .env добавить:
# CRON_SCHEDULE=0 */4 * * *

docker compose up -d cf-parser-cron
```

### Проверка работы cron

```bash
# Войти в контейнер
docker compose exec cf-parser-cron bash

# Проверить crontab
crontab -l

# Проверить логи
tail -f /var/log/cf-parser.log
```

---

## Деплой на сервер

### Вариант 1: Cron внутри контейнера (рекомендуется)

```bash
# На сервере
git clone https://github.com/sshamanello/cf-pinterest-parser
cd cf-pinterest-parser

# Создать .env и credentials.json
nano .env
nano credentials.json

# Собрать и запустить
docker compose build
docker compose up -d cf-parser-cron

# Проверить логи
docker compose logs -f cf-parser-cron
```

### Вариант 2: Внешний cron на хосте

```bash
# Собрать образ
docker compose build

# Добавить в crontab хоста
crontab -e

# Добавить строку:
0 9 * * * cd /path/to/cf-pinterest-parser && docker compose run --rm cf-parser >> /var/log/cf-parser-host.log 2>&1
```

---

## Обновление кода

```bash
# Остановить демон (если запущен)
docker compose down

# Обновить код
git pull

# Пересобрать образ
docker compose build

# Запустить снова
docker compose up -d cf-parser-cron
```

---

## Troubleshooting

### Проблема: "credentials.json not found"

```bash
# Проверить, что файл существует
ls -la credentials.json

# Проверить монтирование
docker compose run --rm cf-parser ls -la /app/credentials.json
```

### Проблема: Chromium падает с ошибкой

```bash
# Увеличить shm_size в docker-compose.yml
shm_size: '2gb'  # было 1gb

docker compose down
docker compose build
docker compose up -d cf-parser-cron
```

### Проблема: Cron не запускается

```bash
# Проверить логи контейнера
docker compose logs cf-parser-cron

# Войти в контейнер и проверить cron
docker compose exec cf-parser-cron bash
crontab -l
ps aux | grep cron
```

### Проблема: Нет доступа к ComfyUI на хосте

```bash
# Windows/Mac: использовать host.docker.internal
COMFY_URL=http://host.docker.internal:8188

# Linux: использовать IP хоста
COMFY_URL=http://172.17.0.1:8188

# Или запустить ComfyUI в отдельном контейнере
```

---

## Мониторинг

### Просмотр статуса

```bash
docker compose ps
```

### Просмотр логов

```bash
# Логи контейнера
docker compose logs -f cf-parser-cron

# Логи парсинга
tail -f logs/cf-parser.log
```

### Проверка использования ресурсов

```bash
docker stats cf-parser-cron
```

---

## Безопасность

1. **credentials.json** — никогда не коммитить в Git
2. **.env** — никогда не коммитить в Git
3. **credentials.json** монтируется в режиме read-only (`:ro`)
4. **output/** и **logs/** можно коммитить в `.gitignore`

---

## Сравнение режимов

| Параметр | Разовый запуск | Cron-демон |
|---|---|---|
| Команда | `docker compose run --rm cf-parser` | `docker compose up -d cf-parser-cron` |
| Автоматизация | Нет (ручной запуск) | Да (по расписанию) |
| Логи | В консоль | В `logs/cf-parser.log` |
| Использование | Тестирование, разовые задачи | Production, автоматический режим |
| Перезапуск | Нет | Да (`restart: unless-stopped`) |

---

## Полезные команды

```bash
# Сборка без кэша
docker compose build --no-cache

# Удалить все контейнеры и образы
docker compose down --rmi all

# Очистить volumes
docker compose down -v

# Войти в контейнер
docker compose exec cf-parser-cron bash

# Запустить команду в контейнере
docker compose exec cf-parser-cron python test_components.py

# Просмотр размера образа
docker images | grep cf-pinterest-parser
```

---

## Чеклист перед запуском

- [ ] `.env` создан и заполнен
- [ ] `credentials.json` в корне проекта
- [ ] Docker и Docker Compose установлены
- [ ] Образ собран: `docker compose build`
- [ ] (Опционально) `CRON_SCHEDULE` настроен в `.env`
- [ ] Папка `output/` существует (создастся автоматически)
- [ ] Папка `logs/` существует (создастся автоматически)

---

**Готово!** Теперь проект можно запускать на любом устройстве с Docker.
