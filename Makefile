# Makefile для CF Pinterest Parser
# Быстрые команды для запуска

.PHONY: help parse test pins clean logs

# ═══════════════════════════════════════════════════════════════
# ЛОКАЛЬНЫЙ ЗАПУСК (без Docker)
# ═══════════════════════════════════════════════════════════════

# Полный парсинг (3 страницы, все категории)
parse:
	python run.py parse

# Полный парсинг с кастомным количеством страниц
parse-50:
	python run.py parse --pages 50

# Тест (1 категория, 1 страница)
test:
	python run.py test

# Только одна категория
parse-fonts:
	python run.py parse --niche fonts

parse-graphics:
	python run.py parse --niche graphics

parse-3d-printing:
	python run.py parse --niche 3d-printing

# Только генерация пинов
pins:
	python run.py pins

# ═══════════════════════════════════════════════════════════════
# DOCKER ЗАПУСК
# ═══════════════════════════════════════════════════════════════

# Сборка образа
build:
	docker compose build

# Разовый запуск (всё)
run:
	docker compose run --rm cf-parser

# Разовый запуск с кастомными страницами
run-50:
	docker compose run --rm -e PAGES_PER_RUN=50 cf-parser

# Тест
run-test:
	docker compose run --rm cf-parser python run.py test

# Cron-демон (автоматический)
cron:
	docker compose up -d cf-parser-cron

# Просмотр логов cron
logs:
	docker compose logs -f cf-parser-cron

# Остановить cron
stop:
	docker compose down

# ═══════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════

# Очистка сгенерированных пинов
clean:
	rm -rf output/*.jpg
	rm -rf __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Очистка логов
clean-logs:
	rm -f logs/*.log

# Полная очистка
dist: clean clean-logs

# Проверка компонентов
check:
	python test_components.py

# Справка
help:
	@echo "CF Pinterest Parser — Быстрые команды"
	@echo ""
	@echo "ЛОКАЛЬНЫЙ ЗАПУСК:"
	@echo "  make parse         — полный парсинг (3 стр., все категории)"
	@echo "  make parse-50      — массовый сбор (50 стр.)"
	@echo "  make parse-fonts   — только fonts"
	@echo "  make test          — тест (1 стр. fonts)"
	@echo ""
	@echo "DOCKER:"
	@echo "  make build         — собрать образ"
	@echo "  make run           — разовый запуск"
	@echo "  make run-50        — 50 страниц"
	@echo "  make cron          — запустить cron-демон"
	@echo "  make logs          — смотреть логи"
	@echo "  make stop          — остановить демон"
	@echo ""
	@echo "УТИЛИТЫ:"
	@echo "  make clean         — очистить пины и кэш"
	@echo "  make check         — проверить компоненты"
	@echo "  make help          — эта справка"
