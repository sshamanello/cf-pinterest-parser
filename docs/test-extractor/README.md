# Extractor test sandbox

Положите 1-2 тестовые изображения в папку:

`test/extractor/input/`

Запуск:

```bash
cd /Users/nick/code/cf-pinterest-parser
python run.py extract --input ./test/extractor/input --output ./test/extractor/output
```

Результаты появятся в:

`test/extractor/output/<font_id>/`

Ключевые файлы:

- `extracted_overlay.png`
- `mask.png`
- `extraction_report.json`
- `manual_check/` (если качество вырезки низкое)
