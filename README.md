# CF Pinterest Parser

Автоматически парсит товары с Creative Fabrica, создаёт уникальные Pinterest-пины (1000×1500 px)
и записывает всё в Google Sheets. Запускается ежедневно в 09:00 UTC через GitHub Actions.

---

## Как это работает

```
1. Playwright (headless Chrome) открывает страницы CF по категориям
2. JS-скрипт извлекает: название, slug, URL картинки, URL товара
3. Картинка обрабатывается:
   - если запущен ComfyUI → img2img (SD 1.5, denoising 0.5) → уникальная версия
   - иначе → используется оригинал с CF
4. Pillow накладывает overlay: название, бейдж категории, CTA → output/{slug}.jpg
5. Новые товары записываются в Google Sheets (дубли пропускаются по slug)
6. Повторяется для 7 категорий, 3 страницы каждая (по умолчанию)
```

---

## Структура файлов

```
cf-pinterest-parser/
├── main.py               # точка входа
├── parser.py             # Playwright-скрапер CF (новый браузер на каждую страницу)
├── comfy_processor.py    # ComfyUI img2img + Pillow overlay → output/{slug}.jpg
├── image_processor.py    # устаревший Pillow-only вариант (не используется)
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
   - Положить в корень проекта (`cf-pinterest-parser/credentials.json`)
6. Дать доступ к Google Sheet:
   - Открыть свой Google Sheet → **Поделиться**
   - Вставить `client_email` из `credentials.json` → права **Редактор**

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
- Подключится к Google Sheets (smoke-test)
- Создаст 7 вкладок: fonts, graphics, 3d-svg, 3d-printing, embroidery, laser-cutting, bundles
- Спарсит по 3 страницы каждой категории через headless Chrome
- Создаст Pinterest-пины в папке `output/`
- Запишет новые товары в шит, пропустит дубли

---

## Первоначальный массовый сбор

Для первого запуска соберите больше товаров:

**Windows (cmd):**
```cmd
set PAGES_PER_RUN=50 && python main.py
```

**macOS / Linux:**
```bash
PAGES_PER_RUN=50 python main.py
```

| Страниц | Товаров/категорию | Итого (7 ниш) |
|---|---|---|
| 3 (дефолт) | ~156 | ~7 мин |
| 20 | ~700 | ~28 мин |
| 50 | ~1800 | ~1 час |

> ⚠️ `set PAGES_PER_RUN=50` сохраняется до закрытия cmd-окна.
> Для сброса: `set PAGES_PER_RUN=3 && python main.py`

---

## Уникализация изображений через ComfyUI (опционально)

Если ComfyUI запущен локально, каждая картинка автоматически прогоняется через
**Stable Diffusion img2img** (denoising 0.5) — Pinterest получает уникальные изображения.
Если ComfyUI не запущен, скрипт молча переходит в Pillow-only режим.

### Установка ComfyUI

```bash
git clone https://github.com/comfyanonymous/ComfyUI
cd ComfyUI
pip install -r requirements.txt
```

Скачать модель (SD 1.5, бесплатно):
- [RealisticVision v5.1](https://civitai.com/models/4201) → положить в `ComfyUI/models/checkpoints/`

### Запуск

```bash
# Терминал 1 — ComfyUI
cd ComfyUI && python main.py --listen

# Терминал 2 — парсер (сам подхватит ComfyUI)
cd cf-pinterest-parser && python main.py
```

### Настройка ComfyUI в .env

```env
COMFY_URL=http://127.0.0.1:8188
COMFY_MODEL=realisticVisionV51.safetensors
COMFY_DENOISE=0.50
COMFY_STEPS=20
COMFY_CFG=7.0
```

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
| `posted` | `FALSE` по умолчанию; меняется на `TRUE` после публикации пина |
| `pin_id` | ID пина в Pinterest (заполняется после постинга) |
| `created_at` | Дата/время добавления строки (UTC) |

---

## GitHub Actions — автоматический запуск

### Добавить секреты

**Settings → Secrets and variables → Actions → New repository secret**

| Секрет | Значение |
|---|---|
| `GOOGLE_CREDENTIALS` | Полное содержимое файла `credentials.json` |
| `GOOGLE_SHEET_ID` | `1h6ZYtQUwT77z66-feJMZD84XIwIFmy83ClMy-_iWbWg` |
| `CF_AFFILIATE_ID` | `7029352` |

```bash
# Получить содержимое credentials.json:
cat credentials.json
# Скопировать весь JSON и вставить как значение секрета GOOGLE_CREDENTIALS
```

### Расписание

- **Автоматически:** каждый день в 09:00 UTC
- **Вручную:** Actions → CF Pinterest Parser → Run workflow

---

## Добавить новую категорию

Открыть `config.py`, добавить строку в `CATEGORIES`:

```python
"new-niche": "https://www.creativefabrica.com/new-niche/",
```

Вкладка в шите создастся автоматически при следующем запуске.

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

- **Playwright вместо requests** — CF за Cloudflare, обычные HTTP дают 403
- **Новый браузер на каждую страницу** — иначе CF блокирует страницу 2+
- **Страница 1 fonts** — ~84 товара (Popular + New), страницы 2+ — ~36 каждая
- **Lazy-load скролл** — embroidery/bundles/laser-cutting грузят карточки только после скролла
- **noscript title bug** — для некоторых категорий `textContent` возвращает сырой HTML;
  исправлено через извлечение `alt` атрибута regex'ом
- **ComfyUI fallback** — если ComfyUI недоступен, изображения создаются без AI
- **SSL warning при старте** — `SSLEOFError` от gspread нормален, авторетрай встроен
