# CF Pinterest Parser — Быстрая инициализация проекта

> Последнее обновление: 2026-03-24
> Этот файл содержит полный контекст проекта для быстрого восстановления работы

---

## 📋 Краткое описание проекта

**Цель:** Автоматический парсинг товаров Creative Fabrica → генерация Pinterest-пинов → запись в Google Sheets

**Технологии:**
- Playwright (headless Chrome) — обход Cloudflare
- ComfyUI + Stable Diffusion — уникализация изображений (опционально)
- Pillow — наложение текста и графики
- Google Sheets API — хранение данных
- Docker + cron — запуск на любом устройстве
- GitHub Actions — ежедневный cron в 09:00 UTC (альтернатива)

**Статус:** Production-ready, работает в автоматическом режиме

---

## 🏗️ Архитектура проекта

```
main.py                 — точка входа, оркестрация всего процесса
├── parser.py           — Playwright-скрапер (КРИТИЧНО: новый браузер на каждую страницу!)
├── comfy_processor.py  — ComfyUI img2img + Pillow overlay → output/{slug}.jpg
├── image_processor.py  — legacy Pillow-only (не используется в main.py)
├── sheets.py           — Google Sheets: auth, dedup, write
└── config.py           — все константы из .env
```

### Поток данных:

```
1. parse_category() → Playwright открывает CF категорию
   ↓ JS-скрипт извлекает: slug, title, image_url, cf_url
   
2. process_products() → для каждого товара:
   ↓ скачать image_url
   ↓ если ComfyUI доступен → img2img (denoise 0.5)
   ↓ Pillow overlay: title + niche badge + CTA
   ↓ сохранить в output/{slug}.jpg
   
3. append_products() → записать в Google Sheets
   ↓ get_existing_slugs() — проверка дублей
   ↓ append_rows() — только новые товары
```

---

## 🔑 Критические особенности (НЕЛЬЗЯ МЕНЯТЬ!)

### 1. Новый браузер на каждую страницу
```python
# parser.py:89
browser = pw.chromium.launch(headless=True)  # ВНУТРИ цикла for page_num
```
**Почему:** CF rate-limit блокирует повторные запросы из одной сессии.
**Если изменить:** страницы 2+ вернут 403 или пустой результат.

### 2. Скролл перед wait_for_selector
```python
# parser.py:111-113
for scroll_pos in [300, 600, 1000, 1500, 2500, 4000]:
    page_obj.evaluate(f"window.scrollTo(0, {scroll_pos})")
    page_obj.wait_for_timeout(400)
```
**Почему:** embroidery/bundles/laser-cutting используют lazy-load.
**Если убрать:** эти категории вернут 0 товаров.

### 3. JS-экстрактор с noscript-фиксом
```python
# parser.py:38-46
if rawText.startsWith('<'):
    const altMatch = rawText.match(/alt="([^"]+)"/);
    title = altMatch ? altMatch[1] : (img ? img.alt : '');
```
**Почему:** некоторые категории оборачивают `<img>` в `<noscript>`, textContent возвращает сырой HTML.
**Если убрать:** title будет `<img src="..." alt="Product Name" />` вместо "Product Name".

### 4. Трёхуровневая дедупликация
```
Уровень 1: JS seen Set (parser.py:23, _EXTRACT_JS)
Уровень 2: Python seen_slugs (parser.py:81)
Уровень 3: get_existing_slugs() перед записью (sheets.py:63)
```
**Результат:** один товар НИКОГДА не попадёт в шит дважды.

---

## 📦 Зависимости

```txt
playwright==1.49.1       # headless Chrome
Pillow==10.3.0           # image processing
gspread==6.1.2           # Google Sheets API
google-auth==2.30.0      # service account auth
requests==2.32.3         # HTTP requests
python-dotenv==1.0.1     # .env loader
beautifulsoup4==4.12.3   # HTML parsing (вспомогательно)
```

**После установки обязательно:**
```bash
playwright install chromium
```

---

## 🔐 Переменные окружения (.env)

```env
# Обязательные
GOOGLE_SHEET_ID=1h6ZYtQUwT77z66-feJMZD84XIwIFmy83ClMy-_iWbWg
CF_AFFILIATE_ID=7029352
GOOGLE_CREDENTIALS_PATH=credentials.json

# Опциональные
PAGES_PER_RUN=3                              # страниц на категорию
MIN_DELAY=2                                  # секунд между страницами
MAX_DELAY=5

# ComfyUI (если не запущен — автофолбэк на Pillow-only)
COMFY_URL=http://127.0.0.1:8188
COMFY_MODEL=realisticVisionV51.safetensors
COMFY_DENOISE=0.50                           # 0.4-0.65 рекомендуется
COMFY_STEPS=20
COMFY_CFG=7.0
```

---

## 📊 Google Sheets структура

**Вкладки (7 шт):**
```
fonts | graphics | 3d-svg | 3d-printing | embroidery | laser-cutting | bundles
```

**Колонки (8 шт):**
```
title | image_url | cf_url | affiliate_url | slug | posted | pin_id | created_at
```

- `slug` — ключ дедупликации
- `posted` — FALSE по умолчанию, TRUE после публикации
- `pin_id` — заполняется Pinterest-постером
- `created_at` — UTC timestamp

---

## 🎯 Категории Creative Fabrica

```python
CATEGORIES = {
    "fonts":         "https://www.creativefabrica.com/fonts/",
    "graphics":      "https://www.creativefabrica.com/graphics/",
    "3d-svg":        "https://www.creativefabrica.com/3d-svg/",
    "3d-printing":   "https://www.creativefabrica.com/3d-printing/",
    "embroidery":    "https://www.creativefabrica.com/embroidery/",
    "laser-cutting": "https://www.creativefabrica.com/laser-cutting/",
    "bundles":       "https://www.creativefabrica.com/bundles/",
}
```

**Формат пагинации:**
- Страница 1: `https://www.creativefabrica.com/fonts/`
- Страница 2+: `https://www.creativefabrica.com/fonts/page/2/`

**Товаров на странице:**
- Страница 1 fonts: ~84 (Popular + New секции)
- Страница 1 graphics: ~32
- Страницы 2+: ~36 каждая

---

## 🖼️ Обработка изображений

### Размер Pinterest-пина
```python
PIN_W, PIN_H = 1000, 1500  # соотношение 2:3
```

### Композиция пина
```
┌─────────────────────────┐
│                         │
│   Верх 65% (975px)      │ ← AI-обработанное изображение товара
│   Product Image         │   (или оригинал, если ComfyUI недоступен)
│                         │
├─────────────────────────┤ ← оранжевая линия (accent)
│ [NICHE BADGE]           │
│                         │
│ Product Title           │ ← тёмный фон (30,30,30)
│ (wrapped, bold 50px)    │
│                         │
│ Free Today with         │ ← оранжевый CTA
│ All Access              │
│                         │
│         creativefabrica │ ← брендинг (серый, справа)
└─────────────────────────┘
   Низ 35% (525px)
```

### ComfyUI workflow
```python
CheckpointLoaderSimple → model: realisticVisionV51.safetensors
LoadImage → uploaded CF image
VAEEncode → latent
KSampler:
  - sampler: euler_ancestral
  - scheduler: karras
  - denoise: 0.50 (ключевой параметр!)
  - steps: 20
  - cfg: 7.0
VAEDecode → result image
```

**Промпты по нишам:**
```python
NICHE_PROMPTS = {
    "fonts": "professional font specimen poster, clean white background...",
    "graphics": "digital art product mockup, clean background...",
    # и т.д. — см. comfy_processor.py:53-82
}
```

---

## 🚀 Команды запуска

### ⚡ Быстрый старт (Makefile)

```bash
# Полный парсинг (3 стр., все категории)
make parse

# Массовый сбор (50 стр.)
make parse-50

# Тест (1 стр. fonts)
make test

# Только одна категория
make parse-fonts
make parse-graphics
make parse-3d-printing
```

### 🐳 Docker

```bash
# Разовый запуск
make run

# 50 страниц
make run-50

# Cron-демон
make cron

# Логи
make logs
```

### 📋 Все команды

```bash
make help   # показать все команды
```

| Команда | Описание |
|---------|----------|
| `make parse` | Полный цикл: парсинг → пины → шит |
| `make parse-50` | Массовый сбор (50 стр.) |
| `make test` | Тест: 1 стр. fonts |
| `make parse-fonts` | Только fonts |
| `make pins` | Только генерация пинов |
| `make run` | Docker: разовый запуск |
| `make cron` | Docker: cron-демон |
| `make logs` | Docker: логи демона |
| `make clean` | Очистка пинов и кэша |

---

## 🧪 Тестирование компонентов

### 1. Проверка Google Sheets подключения
```python
from sheets import get_sheet_client, test_connection
spreadsheet = get_sheet_client()
test_connection(spreadsheet)
# Должно вывести: "Test write to '...'!A1 succeeded."
```

### 2. Проверка парсера (1 страница fonts)
```python
from parser import parse_category
products = parse_category("https://www.creativefabrica.com/fonts/", "fonts", pages=1)
print(f"Найдено товаров: {len(products)}")
# Ожидается: ~84 товара
```

### 3. Проверка ComfyUI
```python
from comfy_processor import _comfy_available
print(_comfy_available())
# True — если ComfyUI запущен на http://127.0.0.1:8188
```

### 4. Проверка генерации пина
```python
from comfy_processor import create_pin
path = create_pin(
    title="Test Product",
    image_url="https://example.com/image.jpg",
    slug="test-product",
    niche="fonts"
)
print(path)  # output/test-product.jpg
```

---

## ⚠️ Известные проблемы и решения

### Проблема: Timeout на embroidery/bundles
**Причина:** CF rate-limiting или медленная загрузка lazy-load карточек
**Решение:** Встроен retry (2 попытки с паузой 8 сек), затем skip категории

### Проблема: ComfyUI возвращает ошибку "model not found"
**Причина:** `COMFY_MODEL` в .env не совпадает с именем файла в `ComfyUI/models/checkpoints/`
**Решение:** Проверить точное имя файла (с расширением .safetensors)

### Проблема: SSL warning при старте
```
SSLEOFError: EOF occurred in violation of protocol
```
**Причина:** gspread иногда получает обрыв SSL при первом коннекте
**Решение:** Это нормально, gspread автоматически ретраит. Игнорировать.

### Проблема: Дубли в шите
**Причина:** Невозможно при правильной работе (3 уровня дедупликации)
**Диагностика:** Проверить логи — должно быть "Skipped (duplicates): N"

---

## 📁 Структура output/

```
output/
├── 3d-wind-spinner-stl-3mf.jpg
├── alina-monogram-font.jpg
├── %f0%9f%8d%84-mushroom-birdhouse-fantasy-3d-print.jpg  ← URL-encoded emoji
└── ...
```

**Формат имени:** `{slug}.jpg` (slug берётся из CF URL)
**Размер:** 1000×1500 px, JPEG quality 92
**Идемпотентность:** если файл существует — пропускается

---

## 🔄 GitHub Actions (автоматический режим)

**Файл:** `.github/workflows/cron.yml`

**Расписание:**
```yaml
schedule:
  - cron: '0 9 * * *'  # 09:00 UTC ежедневно
```

**Секреты (Settings → Secrets):**
```
GOOGLE_CREDENTIALS    — полное содержимое credentials.json
GOOGLE_SHEET_ID       — ID Google Sheet
CF_AFFILIATE_ID       — 7029352
```

**Логи:** Actions → последний запуск → cf-parser job

---

## 🛠️ Частые задачи

### Добавить новую категорию
```python
# config.py
CATEGORIES = {
    # ...существующие...
    "new-category": "https://www.creativefabrica.com/new-category/",
}
```
Вкладка в шите создастся автоматически.

### Изменить affiliate ID
```bash
# .env (локально)
CF_AFFILIATE_ID=новый_id

# GitHub Actions
Settings → Secrets → CF_AFFILIATE_ID → Update
```

### Пересоздать все изображения
```bash
rm -rf output/*
python main.py
```
Шит не изменится (image_url хранит оригинальный CF CDN URL).

### Запустить только одну категорию
```python
# config.py — временно закомментировать остальные
CATEGORIES = {
    "fonts": "https://www.creativefabrica.com/fonts/",
    # "graphics": "...",  # закомментировано
}
```

---

## 📝 Чеклист перед запуском

### Для Docker (рекомендуется):
- [ ] Docker и Docker Compose установлены
- [ ] `.env` создан и заполнен
- [ ] `credentials.json` в корне проекта
- [ ] Google Sheet создан и расшарен на `client_email` из credentials.json
- [ ] Образ собран: `docker compose build`

### Для локального запуска:
- [ ] `pip install -r requirements.txt`
- [ ] `playwright install chromium`
- [ ] `.env` создан и заполнен
- [ ] `credentials.json` в корне проекта
- [ ] Google Sheet создан и расшарен на `client_email` из credentials.json

### Опционально:
- [ ] ComfyUI запущен с моделью в `models/checkpoints/`

---

## 🐛 Отладка

### Включить debug-логи
```python
# main.py:11
logging.basicConfig(level=logging.DEBUG)
```

### Проверить, что парсер видит на странице
```python
# parser.py:129 — добавить перед evaluate()
page_obj.screenshot(path="debug.png")
```

### Проверить ComfyUI workflow вручную
```bash
curl http://127.0.0.1:8188/system_stats
# Должен вернуть JSON с system.cpu_utilization и т.д.
```

---

## 📚 Полезные ссылки

- [Playwright Python Docs](https://playwright.dev/python/docs/intro)
- [ComfyUI GitHub](https://github.com/comfyanonymous/ComfyUI)
- [gspread Documentation](https://docs.gspread.org/)
- [Google Cloud Console](https://console.cloud.google.com)
- [Creative Fabrica](https://www.creativefabrica.com)

---

## 🎓 Контекст для AI-ассистентов

**Если ты AI и читаешь этот файл:**

1. **Никогда не меняй** логику создания нового браузера на каждую страницу (parser.py:89)
2. **Никогда не удаляй** скролл перед wait_for_selector (parser.py:111-113)
3. **Всегда сохраняй** трёхуровневую дедупликацию
4. **Не создавай** новые markdown-файлы без явного запроса пользователя
5. **Используй** существующие файлы для изменений (Edit, не Write)
6. **Проверяй** наличие .env и credentials.json перед запуском
7. **Помни:** ComfyUI опционален, fallback на Pillow-only встроен
8. **Docker:** проект поддерживает Docker, есть два режима работы

**Ключевые файлы для изменений:**
- Логика парсинга → `parser.py`
- Обработка изображений → `comfy_processor.py`
- Работа с шитом → `sheets.py`
- Константы → `config.py`
- Оркестрация → `main.py`

**Docker-файлы:**
- `Dockerfile` — образ контейнера
- `docker-compose.yml` — конфигурация сервисов (два режима)
- `docker-entrypoint.sh` — entrypoint с поддержкой cron

**Документация:**
- `DOCKER.md` — полная Docker-документация
- `DOCKER_QUICKSTART.md` — шпаргалка
- `README.md` — основная документация
- `CLAUDE.md` — подробные инструкции для AI
- `INIT.md` (этот файл) — быстрый контекст

---

**Версия:** 2.0 (с Docker)  
**Дата:** 2026-03-24  
**Автор:** sshamanello  
**Статус:** Production-ready
