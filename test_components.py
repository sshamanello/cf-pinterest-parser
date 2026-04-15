"""
Тестовый скрипт для проверки работоспособности компонентов проекта.
Запуск: python test_components.py
"""

import os
import sys
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def test_env_variables():
    """Проверка наличия необходимых переменных окружения."""
    logger.info("=== Тест 1: Переменные окружения ===")

    from dotenv import load_dotenv

    load_dotenv()

    required = ["GOOGLE_SHEET_ID", "CF_AFFILIATE_ID"]
    optional = ["GOOGLE_CREDENTIALS_PATH", "PAGES_PER_RUN", "COMFY_URL"]

    missing = []
    for var in required:
        value = os.environ.get(var)
        if value:
            logger.info(f"✓ {var}: {'*' * 10} (скрыто)")
        else:
            logger.error(f"✗ {var}: НЕ НАЙДЕНА")
            missing.append(var)

    for var in optional:
        value = os.environ.get(var)
        if value:
            logger.info(f"✓ {var}: {value}")
        else:
            logger.warning(f"⚠ {var}: не установлена (опционально)")

    if missing:
        logger.error(f"Отсутствуют обязательные переменные: {', '.join(missing)}")
        return False

    logger.info("✓ Все обязательные переменные найдены\n")
    return True


def test_credentials_file():
    """Проверка наличия credentials.json."""
    logger.info("=== Тест 2: Файл credentials.json ===")

    from config import GOOGLE_CREDENTIALS_PATH

    if Path(GOOGLE_CREDENTIALS_PATH).exists():
        logger.info(f"✓ Файл найден: {GOOGLE_CREDENTIALS_PATH}")

        # Проверка валидности JSON
        try:
            import json

            with open(GOOGLE_CREDENTIALS_PATH, "r") as f:
                creds = json.load(f)
                if "client_email" in creds:
                    logger.info(f"✓ client_email: {creds['client_email']}")
                else:
                    logger.error("✗ Отсутствует client_email в credentials.json")
                    return False
        except Exception as e:
            logger.error(f"✗ Ошибка чтения credentials.json: {e}")
            return False
    else:
        logger.error(f"✗ Файл не найден: {GOOGLE_CREDENTIALS_PATH}")
        logger.info("Создайте credentials.json согласно README.md")
        return False

    logger.info("✓ credentials.json валиден\n")
    return True


def test_google_sheets_connection():
    """Проверка подключения к Google Sheets."""
    logger.info("=== Тест 3: Подключение к Google Sheets ===")

    try:
        from sheets import get_sheet_client

        spreadsheet = get_sheet_client()
        logger.info(f"✓ Подключено к таблице: {spreadsheet.title}")
        logger.info(f"✓ URL: {spreadsheet.url}")

        # Проверка вкладок
        tabs = [ws.title for ws in spreadsheet.worksheets()]
        logger.info(f"✓ Найдено вкладок: {len(tabs)}")
        logger.info(f"  Вкладки: {', '.join(tabs[:5])}{'...' if len(tabs) > 5 else ''}")

    except Exception as e:
        logger.error(f"✗ Ошибка подключения к Google Sheets: {e}")
        return False

    logger.info("✓ Google Sheets доступен\n")
    return True


def test_playwright():
    """Проверка работы Playwright."""
    logger.info("=== Тест 4: Playwright ===")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://www.creativefabrica.com/fonts/", timeout=15000)
            title = page.title()
            browser.close()

            logger.info(f"✓ Playwright работает")
            logger.info(f"✓ Загружена страница: {title[:50]}...")

    except Exception as e:
        logger.error(f"✗ Ошибка Playwright: {e}")
        logger.info("Запустите: playwright install chromium")
        return False

    logger.info("✓ Playwright готов к работе\n")
    return True


def test_comfyui_availability():
    """Проверка доступности ComfyUI (опционально)."""
    logger.info("=== Тест 5: ComfyUI (опционально) ===")

    try:
        from comfy_processor import _comfy_available, COMFY_URL

        if _comfy_available():
            logger.info(f"✓ ComfyUI доступен: {COMFY_URL}")
            logger.info("  Изображения будут обрабатываться через AI")
        else:
            logger.warning(f"⚠ ComfyUI недоступен: {COMFY_URL}")
            logger.info("  Будет использован Pillow-only режим (это нормально)")

    except Exception as e:
        logger.warning(f"⚠ Ошибка проверки ComfyUI: {e}")

    logger.info("✓ Проверка ComfyUI завершена\n")
    return True


def test_parser_minimal():
    """Минимальный тест парсера (1 страница fonts)."""
    logger.info("=== Тест 6: Парсер (1 страница fonts) ===")
    logger.info("⚠ Этот тест займёт ~30 секунд...")

    try:
        from parser import parse_category

        products = parse_category(
            url="https://www.creativefabrica.com/fonts/", niche="fonts", pages=1
        )

        if products:
            logger.info(f"✓ Найдено товаров: {len(products)}")
            logger.info(f"✓ Пример товара:")
            p = products[0]
            logger.info(f"  - title: {p['title'][:50]}...")
            logger.info(f"  - slug: {p['slug']}")
            logger.info(f"  - image_url: {p['image_url'][:60]}...")
        else:
            logger.error("✗ Парсер не вернул товары")
            return False

    except Exception as e:
        logger.error(f"✗ Ошибка парсера: {e}")
        import traceback

        traceback.print_exc()
        return False

    logger.info("✓ Парсер работает корректно\n")
    return True


def test_image_processor():
    """Тест генерации Pinterest-пина."""
    logger.info("=== Тест 7: Генерация изображения ===")

    try:
        from comfy_processor import create_pin

        # Тестовый пин
        test_slug = "test-pin-" + str(int(os.times().elapsed * 1000))
        path = create_pin(
            title="Test Product for Pinterest",
            image_url="https://www.creativefabrica.com/wp-content/uploads/2023/01/01/Alina-Monogram-Font-Graphics-59558485-1-580x387.jpg",
            slug=test_slug,
            niche="fonts",
        )

        if path and path.exists():
            logger.info(f"✓ Пин создан: {path}")
            logger.info(f"✓ Размер файла: {path.stat().st_size / 1024:.1f} KB")

            # Проверка размера изображения
            from PIL import Image

            img = Image.open(path)
            logger.info(f"✓ Размер изображения: {img.width}x{img.height} px")

            if img.width == 1000 and img.height == 1500:
                logger.info("✓ Размер соответствует Pinterest (1000x1500)")
            else:
                logger.warning(f"⚠ Неожиданный размер (ожидалось 1000x1500)")
        else:
            logger.error("✗ Не удалось создать пин")
            return False

    except Exception as e:
        logger.error(f"✗ Ошибка генерации изображения: {e}")
        import traceback

        traceback.print_exc()
        return False

    logger.info("✓ Генерация изображений работает\n")
    return True


def main():
    """Запуск всех тестов."""
    logger.info("╔═══════════════════════════════════════════════════════╗")
    logger.info("║   CF Pinterest Parser - Тестирование компонентов     ║")
    logger.info("╚═══════════════════════════════════════════════════════╝\n")

    tests = [
        ("Переменные окружения", test_env_variables),
        ("Файл credentials.json", test_credentials_file),
        ("Google Sheets", test_google_sheets_connection),
        ("Playwright", test_playwright),
        ("ComfyUI", test_comfyui_availability),
        ("Парсер", test_parser_minimal),
        ("Генерация изображений", test_image_processor),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except KeyboardInterrupt:
            logger.warning("\n⚠ Тестирование прервано пользователем")
            sys.exit(1)
        except Exception as e:
            logger.error(f"✗ Критическая ошибка в тесте '{name}': {e}")
            results.append((name, False))

    # Итоги
    logger.info("╔═══════════════════════════════════════════════════════╗")
    logger.info("║                   ИТОГИ ТЕСТИРОВАНИЯ                  ║")
    logger.info("╚═══════════════════════════════════════════════════════╝\n")

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        logger.info(f"{status:8} | {name}")

    logger.info(f"\nРезультат: {passed}/{total} тестов пройдено")

    if passed == total:
        logger.info("✓ Все компоненты работают корректно!")
        logger.info("✓ Проект готов к запуску: python main.py")
        return 0
    else:
        logger.error(f"✗ Провалено тестов: {total - passed}")
        logger.error("Исправьте ошибки перед запуском main.py")
        return 1


if __name__ == "__main__":
    sys.exit(main())
