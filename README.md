# CF Pinterest Parser

Автоматически парсит товары с Creative Fabrica, создаёт Pinterest-пины (1000×1500 px)
и записывает всё в Google Sheets. Запускается ежедневно в 09:00 UTC через GitHub Actions.

---

## Как это работает

```
1. Playwright (headless Chrome) открывает страницы CF по категориям
2. JS-скрипт извлекает: название, slug, URL картинки, URL товара
3. Pillow скачивает картинку и создаёт Pinterest-пин → output/{slug}.jpg
4. Новые товары записываются в Google Sheets (дубли пропускаются по slug)
5. Повторяется для 7 категорий, 3 страницы каждая (по умолчанию)
```

---

## Структура файлов

```
cf-pinterest-parser/
├── main.py               # точка входа
├── parser.py             # Playwright-скрапер CF
├── image_processor.py    # обработка картинок (Pillow → Pinterest-пины)
├── sheets.py             # чтение/запись Google Sheets
├── config.py             # константы и env-переменные
├── requirements.txt
├── .env.example          # шаблон переменных окружения
├── .gitignore
├── CLAUDE.md             # инструкции для AI-ассистентов
└── .github/
    └── workflows/
        └── cron.yml      # GitHub Actions cron (09:00 UTC ежедневно)
```

---

## Быстрый старт (локально)

### Шаг 1. Клонировать и установить зависимости

```bash
git clone https://github.com/sshamanello/cf-pinterest-parser
cd cf-pinterest-parser
pip install -r requirements.txt
playwright install chromium
```

### Шаг 2. Создать Google Service Account

1. Перейти на [console.cloud.google.com](https://console.cloud.google.com)
2. Создать проект (или выбрать существующий)
3. Включить два API:
   - **API & Services → Library → Google Sheets API → Enable**
   - **API & Services → Library → Google Drive API → Enable**
4. Создать сервисный аккаунт:
   - **IAM & Admin → Service Accounts → Create Service Account**
   - Имя: например `cf-parser`, остальное по умолчанию
5. Создать JSON-ключ:
   - Открыть созданный аккаунт → вкладка **Keys** → **Add Key → Create new key → JSON**
   - Скачать файл → переименовать в `credentials.json`
   - Положить в корень проекта (`E:\code\cf-pinterest-parser\credentials.json`)
6. Дать доступ к Google Sheet:
   - Открыть свой Google Sheet
   - Нажать **Поделиться** (Share)
   - Вставить `client_email` из `credentials.json` (выглядит как `cf-parser@project.iam.gserviceaccount.com`)
   - Права: **Редактор (Editor)**

### Шаг 3. Настроить переменные окружения

```bash
cp .env.example .env
```

Открыть `.env` и заполнить:

```env
GOOGLE_SHEET_ID=1h6ZYtQUwT77z66-feJMZD84XIwIFmy83ClMy-_iWbWg
CF_AFFILIATE_ID=7029352
GOOGLE_CREDENTIALS_PATH=credentials.json
PAGES_PER_RUN=3
```

### Шаг 4. Запустить

```bash
python main.py
```

**Что произойдёт:**
- Подключится к Google Sheets (smoke-test: запишет и сотрёт тестовую строку)
- Создаст 7 вкладок если их нет: fonts, graphics, 3d-svg, 3d-printing, embroidery, laser-cutting, bundles
- Спарсит по 3 страницы каждой категории через headless Chrome
- Скачает картинки и создаст Pinterest-пины в папке `output/`
- Запишет новые товары в шит, пропустит дубли

---

## Первоначальный массовый сбор

Для первого запуска соберите больше товаров (потом крон сам добавляет новинки):

**Windows (cmd):**
```cmd
set PAGES_PER_RUN=50 && python main.py
```

**macOS / Linux:**
```bash
PAGES_PER_RUN=50 python main.py
```

| Страниц | Товаров/категорию | Время/категорию | Итого (7 ниш) |
|---|---|---|---|
| 3 (дефолт) | ~156 | ~1 мин | ~7 мин |
| 20 | ~700 | ~4 мин | ~28 мин |
| 50 | ~1800 | ~8 мин | ~1 час |

---

## Google Sheets — структура

**Вкладки:** `fonts` `graphics` `3d-svg` `3d-printing` `embroidery` `laser-cutting` `bundles`

**Колонки:**

| Колонка | Описание |
|---|---|
| `title` | Название товара |
| `image_url` | URL картинки с CDN Creative Fabrica |
| `cf_url` | Прямая ссылка на страницу товара |
| `affiliate_url` | Аффилиатная ссылка (`/ref/7029352/?sharedfrom=pdp`) |
| `slug` | Slug товара — ключ для дедупликации |
| `posted` | `FALSE` по умолчанию; вручную или скриптом меняется на `TRUE` после пина |
| `pin_id` | ID пина в Pinterest (заполняется после публикации) |
| `created_at` | Дата/время добавления строки (UTC) |

---

## Обработка изображений (Pillow)

Для каждого товара создаётся файл `output/{slug}.jpg`:

- Размер: **1000×1500 px** (соотношение 2:3 — оптимально для Pinterest)
- Верхние 65%: картинка товара с CF
- Нижние 35%: тёмная плашка — название, бейдж категории, CTA "Free Today with All Access"
- Уже существующие файлы не перегенерируются (идемпотентно)

---

## GitHub Actions — автоматический запуск

### Добавить секреты

В репозитории: **Settings → Secrets and variables → Actions → New repository secret**

| Секрет | Значение |
|---|---|
| `GOOGLE_CREDENTIALS` | Полное содержимое файла `credentials.json` |
| `GOOGLE_SHEET_ID` | `1h6ZYtQUwT77z66-feJMZD84XIwIFmy83ClMy-_iWbWg` |
| `CF_AFFILIATE_ID` | `7029352` |

Как получить содержимое `credentials.json`:
```bash
cat credentials.json
# скопировать весь JSON (от { до }) и вставить как значение секрета
```

### Расписание

- **Автоматически:** каждый день в 09:00 UTC
- **Вручную:** Actions → CF Pinterest Parser → Run workflow (кнопка)

---

## Добавить новую категорию

Открыть `config.py`, добавить строку в словарь `CATEGORIES`:

```python
"new-niche": "https://www.creativefabrica.com/new-niche/",
```

При следующем запуске вкладка в шите создастся автоматически.

---

## Защита от дублей — три уровня

| Уровень | Где | Механизм |
|---|---|---|
| Внутри страницы | браузер | JS `seen` Set в `_EXTRACT_JS` |
| Между страницами одного запуска | `parser.py` | `seen_slugs` set в `parse_category()` |
| Между запусками (ежедневный крон) | `sheets.py` | `get_existing_slugs()` читает шит перед записью |

Один и тот же товар **никогда не попадёт в шит дважды**.

---

## Важно — не коммитить

```
credentials.json   # ключ Google сервисного аккаунта
.env               # локальные секреты
output/            # сгенерированные Pinterest-пины
```

Все три уже в `.gitignore`.

---

## Известные особенности

- **Playwright вместо requests** — CF использует Cloudflare и React SPA, обычные HTTP-запросы получают 403
- **Страница 1 vs страницы 2+** — страница 1 (~84 товара) содержит секции "Popular" и "New releases", страницы 2+ (~36 товаров) — чистый пагинированный список
- **SSL warning при старте** — `SSLEOFError` при первом обращении к Google API — это нормально, gspread делает retry автоматически
- **Аффилиатная ссылка** — формат `https://www.creativefabrica.com/product/{slug}/ref/7029352/?sharedfrom=pdp`
