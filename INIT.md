# CF Pinterest Parser — Быстрая инициализация проекта

> Последнее обновление: 2026-04-19
> Этот файл содержит полный контекст проекта для быстрого восстановления работы

---

## Правило ведения INIT.md

- При любом изменении проекта (код, конфиги, запускные скрипты, инфраструктура, документация) обязательно обновлять `INIT.md` в этом же коммите.
- В `INIT.md` фиксировать, что именно изменилось и почему это важно для дальнейшей работы.

## Изменения 2026-04-15

## Изменения 2026-04-18

## Изменения 2026-04-19

- Стабилизирован основной `script-first` extractor под большой батч (без зависаний):
  - `rembg`-ensemble отключён по умолчанию (переменная `EXTRACTOR_REMBG_MODELS` теперь пустая в default),
  - heavy-model fallback остаётся опциональным через env, но не тормозит основной поток.
- Улучшена чистка маски для сложных карточек:
  - добавлен фильтр `_suppress_dense_background_blobs` (градиентный trim внутри маски), чтобы не тащить большие заливки фона рядом с буквами,
  - добавлен `_remove_frame_like_components` для удаления рамок/скобок и тонких артефактов по краям,
  - edge-recovery в `_finalize_text_mask` ограничен `text-like` пикселями (тёмные/насыщенные), чтобы фоновые текстуры не “прилипали” к маске.
- Добавлен новый кандидат маски `dark_script`:
  - ориентирован на тёмный/контрастный шрифт на светлой плашке с белой обводкой,
  - включён в общий ансамбль выбора лучшей маски.
- Ужесточён post-retry QC:
  - `RETRY` больше не превращается автоматически в `PASS`,
  - если после retry сохраняются сильные артефакты (`stroke_loss/noise/edge_artifact`), кейс переводится в `MANUAL_CHECK`.
- Batch smoke-run после правок:
  - `python3 run.py extract --input ./test/extractor/input --output ./test/extractor/output`
  - итог: `Processed=11 | PASS=2 | MANUAL_CHECK=9`.
  - это осознанно: лучше отправить спорные кейсы в ручную модерацию, чем публиковать плохой вырез.

- Доработан publish quality после негативного фидбека по `sunshine-64`/`magnolia...`:
  - в `font_publish_pipeline.py` добавлен `overlay sanitizer` (`_sanitize_overlay_for_publish`) перед композитом:
    - детектит “blob”-артефакты (слишком большой залитый foreground с низкой плотностью контуров),
    - для подозрительных случаев фильтрует оверлей до text-like пикселей (экстремумы по яркости + насыщенные участки),
    - удаляет мелкие/угловые компоненты и оставляет главные центральные.
  - это не меняет цвета букв, а только удаляет паразитные заливки, попавшие в alpha.
- Улучшена читаемость по умолчанию:
  - `target-width` в `publish-fonts` увеличен с `0.72` до `0.78` (более крупный wordmark на финальном кадре).
- Smoke-перегенерация:
  - `sunshine-64` и `magnolia-script-embroidery-font-small` пересобраны новой логикой через `publish-fonts --no-comfy`.

- Добавлен новый production-модуль публикации: `font_publish_pipeline.py`.
  - Цель: готовить publish-ready изображения по циклу:
    1. script extraction (сохранение формы/цвета шрифта),
    2. генерация уникального фона через ComfyUI (если доступен),
    3. fallback на процедурный фон, если ComfyUI недоступен/отключён,
    4. финальный композит `overlay + background` без деформации букв,
    5. отчёты для контроля качества.
  - Ключевой принцип сохранения уникальности шрифта:
    - буквы НЕ регенерируются через AI,
    - используется extracted overlay как source-of-truth (оригинальная форма и цвет).
  - В pipeline добавлены:
    - prompt для ComfyUI с запретом текста/логотипов на фоне,
    - auto-readability слой (если контраст текста к фону низкий),
    - batch-отчёты: `publish_batch_report.json/.csv`.
- В `run.py` добавлена CLI-команда:
  - `publish-fonts` — полный publish-cycle по файлу или папке.
  - Пример:
    - `python run.py publish-fonts --input ./test/extractor/input --output ./test/extractor/output --category fonts`
  - Полезные флаги:
    - `--no-comfy` (чисто скриптовые фоны),
    - `--target-width` (масштаб wordmark на финальном холсте).
- Smoke-test новой команды:
  - `python run.py publish-fonts --input ./test/extractor/input/magic-unicorn.jpg --output ./test/extractor/output --category fonts --no-comfy`
  - Результат:
    - `publish_background.png`,
    - `publish_final.png`,
    - `publish_report.json`,
    - `extract_qc=PASS`, читаемость (`readability_contrast_score`) сохранена.

- Реализован узкий production-цикл для качества выреза (без ComfyUI):
  1. `crop` до рабочей зоны (`_crop_working_zone_rgba`) — удаляем внешние чёрные поля.
  2. Грубая маска букв (мульти-кандидаты) + LAB/HSV с отдельным захватом белой обводки рядом с цветными буквами.
  3. Фильтр центрального главного объекта (`_keep_central_large_components`) по connected components:
     - оставляем крупные центральные компоненты,
     - удаляем мелкие/дальние/угловые островки.
  4. Двухслойная alpha-маска:
     - `core mask` (плотное тело букв),
     - `soft mask` (мягкий край/обводка),
     - сборка alpha через `core + soft` вместо одной жёсткой маски.
  5. Decontaminate edges:
     - этап антиореола сохранён и применяется до/после стандартизации.
- В `rembg` включён alpha matting (с fallback на обычный режим при недоступности backend).
- Итог на проблемных примерах:
  - `magic-aspect`: маска стала одним центральным текстовым объектом (верхний декоративный мусор удалён),
  - `magic-unicorn`: маска сведена к 1 центральному компоненту (удалены правый верх/низ и мелкие хвосты).

- Уточнён приоритет качества выреза с сохранением исходных цветов:
  - цвета/градиенты букв НЕ убираются (не переводим в ч/б),
  - улучшаем только чистоту маски и качество края.
- Доработан фильтр декоративных элементов в `extractor.py`:
  - исправлен сценарий, где вторая строка названия (например, `aspect`) ошибочно считалась “нижним мусором”,
  - верхний декор (плашки, звезды, изолированные иконки вне основного x-диапазона текста) теперь удаляется агрессивнее,
  - при этом сохраняется защита от перерезания реальной второй текстовой строки.
- Локальная проверка:
  - `magic-aspect`: удалены верхние декоративные элементы, в маске остаётся основной wordmark-блок (3 крупных компонента),
  - `magic-unicorn`: основной текст сохраняется, цвета букв сохраняются, качество края улучшено anti-halo/soft-alpha.

- Зафиксирован приоритет на `script-first` пайплайн (без ComfyUI в основном потоке):
  - основной extraction выполняется скриптовыми методами,
  - ComfyUI оставлен как отложенная задача “на потом”, без включения в текущий runtime.
- В `extractor.py` внедрён multi-candidate script pipeline:
  - добавлены новые кандидаты масок: `threshold_otsu`, `adaptive_threshold`, `lab_hsv_separation`, `color_key` (для simple background),
  - добавлен анализ исходника (`_analyze_source_profile`: `simple_bg/contrast/texture`) для более адекватного выбора режима.
- Улучшен автоматический выбор лучшей маски:
  - `_score_mask` теперь учитывает не только долю foreground, но и структуру компонент/контуров + профиль исходника.
- Улучшен рендер финального оверлея:
  - добавлен anti-halo этап (`_decontaminate_edge_rgb`) перед и после стандартизации на `1500x1500`,
  - сохранён мягкий alpha edge (feather) для более чистого наложения на фон.
- Быстрая проверка после фиксации script-first:
  - `maddison-4`: `PASS` без `manual_check`,
  - `super-3`: остаётся `MANUAL_CHECK` (сложный кейс), что ожидаемо.

- Для кейса `maddison-4` улучшено качество маски под наложение на новый фон:
  - добавлен фильтр primary wordmark (`_keep_primary_wordmark_components`) для приоритета главного названия шрифта,
  - добавлен финальный post-filter (`_drop_lower_decorative_components`) для удаления нижних декоративных блоков (цветы/подписи), если они детектятся как отдельные крупные компоненты,
  - защитная логика: нижний срез включается только при явном сигнале “крупный нижний декор”, чтобы не ломать обычные шрифты.
- В `render_and_qc` добавлен дополнительный guard на уже стандартизированном `1500x1500` canvas:
  - повторная чистка `canvas_mask`,
  - повторная сборка soft-alpha из финальной маски перед сохранением `extracted_overlay.png`.
- По тесту `maddison-4`:
  - маска очищена от нижнего декоративного блока, остаётся основной wordmark,
  - extraction проходит без `manual_check`.
- Batch smoke-test после правок:
  - `Processed=11 | PASS=7 | MANUAL_CHECK=4` (ручная модерация остаётся для сложных кейсов: `mama-*`, `super-*`).

- Возврат к script-only подходу для генерации шрифта:
  - в `font_generator.py` режимы `full_regen/hybrid` больше не запускают AI regeneration,
  - любые запросы этих режимов автоматически приводятся к `signature_lock`,
  - в `font_generation_report.json` это явно фиксируется:
    - `effective_mode=signature_lock`,
    - `used_fallback=true`,
    - `fallback_reason=ai_regen_disabled_script_only`.
- Улучшена финальная постобработка маски в `extractor.py`:
  - добавлен этап `_finalize_text_mask`:
    - удаление мусора по границам,
    - восстановление тонких штрихов через edge-recovery рядом с найденной маской,
    - морфологическая стабилизация и удаление мелких артефактов.
  - улучшен soft-alpha (`_soft_alpha_from_binary_mask`):
    - внутри букв альфа теперь 255 (чётче глиф),
    - антиалиас вынесен в внешний edge-band для менее “пиксельного” контура.
- Smoke-test после изменений:
  - `python3 run.py extract --input ./test/extractor/input --output ./test/extractor/output`
  - итог: `Processed=11 | PASS=9 | MANUAL_CHECK=2` (сложные кейсы остаются в ручной контроль).

- Ужесточён `full_regen/hybrid` в `font_generator.py` для борьбы с плохими генерациями (кейс: белая `H` на белом фоне):
  - добавлены структурные quality-gates между исходным и regen-оверлеем:
    - минимум похожести `aHash` (`REGEN_MIN_SIMILARITY`, default `0.62`),
    - проверка совпадения пропорций bbox (`REGEN_MIN_ASPECT_RATIO_MATCH`, default `0.55`),
    - проверка совпадения доли foreground (`REGEN_MIN_FOREGROUND_RATIO_MATCH`, default `0.35`),
    - проверка сопоставимости числа компонент (`REGEN_MIN_COMPONENT_RATIO_MATCH`, default `0.20`).
  - если любой gate не проходит, режим автоматически уходит в `signature_lock` fallback с явной причиной в `font_generation_report.json`.
- Усилены промпты для Comfy в `full_regen`:
  - positive: акцент на `full readable word`, `flat 2d lettering`, `high-contrast dark text`.
  - negative: добавлены `single letter`, `white text on white background`, `embossed text`, `3d text`.
- Таймаут ожидания Comfy вынесен в переменную окружения:
  - `COMFY_TIMEOUT_SEC` (default `180`).
- В `font_generation_report.json` теперь пишутся используемые пороги (`regen_*`) и `comfy_timeout_sec` для прозрачной диагностики.

- Создана отдельная экспериментальная ветка: `codex/signature-lock-comfy-experiment` для безопасной проверки нового подхода с возможностью отката.
- Проверен текущий ComfyUI workflow `DreamShaperXL.json`:
  - это `txt2img` граф (`EmptyLatentImage -> KSampler -> VAEDecode -> SaveImage`),
  - исходное превью шрифта в граф не подаётся,
  - значит текущий workflow генерирует новый визуал, но не гарантирует похожесть на исходный продаваемый шрифт.
- Для режима “уникальный, но похожий” в следующей итерации нужен `signature-lock`:
  - выделяем главный текст/wordmark из исходника,
  - в ComfyUI генерируем только фон (negative prompt: `text/letters/words`),
  - поверх фона накладываем исходный signature-слой без деформации.
- Стартован модуль генерации шрифта: `font_generator.py`.
  - Добавлены режимы: `signature_lock`, `full_regen`, `hybrid`.
  - На текущем этапе стабильно реализован `signature_lock`:
    - используется extracted overlay как source-of-truth для формы букв,
    - сохраняется `generated_wordmark.png`,
    - применяются базовые readability-эффекты (soft shadow/glow) без деформации глифов.
  - `full_regen` и `hybrid` пока работают через fallback на `signature_lock` с явной записью в отчёт.
- В CLI добавлена команда генерации:
  - `python run.py generate-font --input <preview> --font-name "<name>" --category <niche> --mode <signature_lock|full_regen|hybrid> --output <dir>`
- Для генерации добавлен отчёт:
  - `output/<font_id>/font_generation_report.json` (requested/effective mode, fallback, пути артефактов).
- Smoke-test команды `generate-font` на `super-3.jpg` выполнен успешно.
- Реализован реальный `full_regen` через ComfyUI API в `font_generator.py`:
  - загружается workflow из `COMFY_WORKFLOW_PATH` (по умолчанию `/Users/nick/Downloads/DreamShaperXL.json`),
  - positive/negative prompts подставляются программно,
  - генерация отправляется в `COMFY_URL/prompt`, результат забирается из `/history` + `/view`.
- Логика режимов после обновления:
  - `signature_lock`: стабильный режим (без Comfy),
  - `full_regen`: пытается реальную генерацию, при сбое уходит в `signature_lock` fallback,
  - `hybrid`: пытается `full_regen`, при fail/similarity-gate fallback в `signature_lock`.
- В `font_generation_report.json` теперь пишется:
  - `comfy_url`, `comfy_workflow_path`, `similarity_score`,
  - корректные `requested_mode` / `effective_mode` / `fallback_reason`.
- Актуальный smoke-run:
  - `requested_mode=full_regen`,
  - `effective_mode=signature_lock`,
  - `fallback_reason=comfy_timeout` (Comfy не вернул результат в таймауте).

### Контекст после лимитов (быстрый рестарт)
1. Ветка эксперимента: `codex/signature-lock-comfy-experiment`.
2. Проверочный запуск:
   - `python run.py generate-font --input ./test/extractor/input/super-3.jpg --font-name "Super Font" --category fonts --mode signature_lock --output ./test/extractor/output`
3. Следующий шаг реализации:
   - подключить реальный `full_regen` через ComfyUI workflow без fallback,
   - в `hybrid` включить цепочку `full_regen -> signature_lock` при fail similarity/QC.
- Внедрён универсальный `QC rule-engine` для масштабной пакетной обработки:
  - новые QC-метрики в отчёте: `noise_score`, `stroke_loss_score`, `edge_artifact_score`, `component_count`,
  - решение по каждому файлу: `qc_decision` (`PASS` / `RETRY` / `MANUAL_CHECK`),
  - `retry_count` фиксируется в результате и отчётах.
- Добавлена `retry policy`:
  - для пограничных кейсов выполняется один автоповтор с расширенным набором масок (`plain_mode_aggressive`, `plain_mode_conservative`, `strict_retry`),
  - после retry пограничный кейс принимается как `PASS` для сохранения throughput, а жёстко плохие — в `MANUAL_CHECK`.
- Добавлен `batch evaluator` для тысяч файлов:
  - автоматически создаются отчёты `output/_reports/extractor_batch_report.json` и `.csv`,
  - есть сводка: `pass_rate`, `manual_check_rate`, `by_extraction_mode`, счётчики `pass/retry/manual`.
- CLI-сводка `run.py extract` обновлена:
  - выводит `Processed | pass | retried | manual_check`.
- Smoke benchmark на текущем тест-наборе:
  - `total=11`, `pass=9`, `manual_check=2`, `pass_rate=0.8182`, `manual_check_rate=0.1818`.
- Внедрён `extractor v2` с несколькими стратегиями маски вместо одного пути:
  - `plain_mode` (общая сегментация текста),
  - `card_mode` (работа через ROI карточки превью),
  - `rembg_rect_refined` / `strict_fallback` как дополнительные fallback-ветки.
- Добавлен `small-image boost` для малых изображений:
  - автоматический upscale (`x2/x3`) перед сегментацией,
  - обратный downscale маски после очистки.
- Добавлена ансамблевая логика выбора лучшей маски:
  - кандидаты из CV + rembg,
  - эвристический скоринг масок (`_score_mask`),
  - штраф за “залитый прямоугольник” и фильтрация до “главного текста”.
- Усилен quality-control для MVP:
  - в отчёте сохраняется `extraction_mode`,
  - спорные маски продолжают отправляться в `manual_check`.
- Внедрён стандартный мастер-формат оверлея: `1500x1500` в `extractor.py`.
- Для каждого extracted оверлея теперь сохраняются техполя для композиции:
  - `overlay_size`,
  - `bbox_px` и `bbox_norm`,
  - `recommended_scale_pct`.
- Добавлен цветовой профиль шрифта для автоподбора фона:
  - `font_colors.dominant_colors` (топ-палитра),
  - `is_multicolor`,
  - `lightness_score`,
  - `mean_color`, `median_color`,
  - `contrast_hint` (`use_dark_bg` / `use_light_bg` / `use_mid_bg`).
- Эти поля записываются в `extraction_report.json` и готовы для использования на этапе генерации/подбора фона.
- Для кейсов типа `super-3.jpg` (цветные/чёрные буквы на карточке) доработан `extractor.py`:
  - в ROI карточки добавлена color-aware сегментация (HSV: насыщенные + тёмные пиксели),
  - добавлена фильтрация мелких компонентов (убирает лишние артефакты и служебный шум),
  - сохранён fallback на строгий `letters-only` режим при провале rembg.
- Улучшено качество краёв оверлея:
  - alpha-канал формируется как мягкий edge-band (anti-alias), чтобы уменьшить “пиксельность” контура букв,
  - фон вне букв принудительно остаётся прозрачным.
- Усилен `extractor.py` для реальных пинов с центральной карточкой:
  - детект “ложного” результата rembg теперь учитывает плотность крупнейшего компонента внутри его bbox (а не только общий foreground_ratio),
  - при детекте карточки включается режим `letters-only` внутри ROI карточки (цветные/тёмные символы), чтобы вырезать фон плашки,
  - добавлен дополнительный fallback на строгую CV-маску.
- Исправлено поведение сохранения артефактов:
  - актуальные `extracted_overlay.png`, `mask.png`, `extraction_report.json` всегда перезаписываются в `output/<font_id>/`,
  - при `manual_check` дополнительно сохраняются копии в `output/<font_id>/manual_check/`.
- Улучшен `extractor.py` для кейса, когда вместо букв сохраняется прямоугольник с фоном:
  - добавен детектор неудачной маски (`full rectangle`),
  - добавлен строгий fallback `only letters` на OpenCV (анализ цвета границ + Otsu + удаление компонентов, касающихся рамки),
  - при сбое/недоступности `rembg` экстракция теперь продолжает работу через CV-only путь.
- Исправлена проблема запуска `extractor`: для `rembg` добавлена отсутствовавшая runtime-зависимость `onnxruntime`.
- Обновлён `requirements.txt`: добавлен `onnxruntime==1.24.4`.
- Добавлен новый модуль `extractor.py` для вырезания фона из превью шрифтов без генеративной перерисовки глифов.
- Используется связка `rembg + OpenCV`: сначала сегментация фона, затем мягкая очистка маски для сохранения формы шрифта.
- Для каждого изображения сохраняются:
  - `extracted_overlay.png` (прозрачный оверлей),
  - `mask.png` (бинарная маска),
  - `extraction_report.json` (метрики качества и решение по ручной проверке).
- Добавлен автоматический флаг `manual_check` при низком `quality_score` или подозрительном размере foreground.
- Обновлён `requirements.txt`: добавлены `rembg`, `opencv-python`, `numpy`.
- В `run.py` добавлена команда `extract`:
  - `python run.py extract --input <file_or_dir> --output output`
  - Команда запускает `extractor` пакетно и показывает сводку по количеству `manual_check`.
- Добавлен изолированный тестовый контур для экстрактора:
  - `test/extractor/input/` — входные тестовые изображения (1-2 файла для быстрой проверки),
  - `test/extractor/output/` — результаты экстракции для теста.
- Добавлена инструкция запуска в `test/extractor/README.md`.
- Обновлён `.gitignore`: `test/extractor/output/` исключён из git, чтобы не коммитить тестовые артефакты.

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
