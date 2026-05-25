# Makefile for CF Pinterest Parser

.PHONY: help parse parse-fonts parse-graphics parse-3d-printing pins test prod-auto-pin \
	docker-build docker-help docker-shell docker-run-help docker-smoke docker-prod \
	docker-cron docker-phone logs stop clean clean-logs dist check \
	ops-check ops-check-strict db-health queue-stats queue-prune-dry queue-prune-apply

parse:
	python run.py parse

parse-50:
	python run.py parse --pages 50

test:
	python run.py test

parse-fonts:
	python run.py parse --niche fonts

parse-graphics:
	python run.py parse --niche graphics

parse-3d-printing:
	python run.py parse --niche 3d-printing

pins:
	python run.py pins

prod-auto-pin:
	python run.py prod-auto-pin --niche fonts --pages 1 --limit 20 --output output/prod/fonts --upload-vds --sync-sheet

check:
	python test_components.py

ops-check:
	python run.py ops-check --db-path output/_state/queue.db --tab fonts --runs-limit 10

ops-check-strict:
	python run.py ops-check --db-path output/_state/queue.db --tab fonts --runs-limit 10 --strict-external

db-health:
	python run.py db-health --db-path output/_state/queue.db

queue-stats:
	python run.py queue-stats --db-path output/_state/queue.db --all-niches --runs-limit 20

queue-prune-dry:
	python run.py queue-prune --db-path output/_state/queue.db --keep-sync-runs 200 --prune-rejected-older-than-days 30

queue-prune-apply:
	python run.py queue-prune --db-path output/_state/queue.db --keep-sync-runs 200 --prune-rejected-older-than-days 30 --apply

# Docker

docker-build:
	docker compose build

docker-run-help:
	docker compose run --rm cf-runner

docker-shell:
	docker compose run --rm cf-runner shell

docker-smoke:
	docker compose run --rm cf-runner run python test_components.py

docker-prod:
	docker compose run --rm cf-runner run python run.py prod-auto-pin --niche fonts --pages 1 --limit 20 --output output/docker_prod --upload-vds --sync-sheet

docker-cron:
	docker compose --profile cron up -d cf-batch-cron

docker-phone:
	docker compose --profile phone up -d cf-phone-scheduler

logs:
	tail -f logs/*.log

stop:
	docker compose --profile cron --profile phone down

clean:
	rm -rf output/*.jpg
	rm -rf __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

clean-logs:
	rm -f logs/*.log

dist: clean clean-logs

help:
	@echo "CF Pinterest Parser"
	@echo ""
	@echo "Local commands:"
	@echo "  make parse            - parse + pins + sheet sync"
	@echo "  make parse-50         - bulk parsing across categories"
	@echo "  make prod-auto-pin    - production fonts batch with VDS upload + sheet sync"
	@echo "  make check            - run smoke checks"
	@echo "  make ops-check        - smoke + db-health + queue-stats preflight"
	@echo "  make ops-check-strict - strict external preflight (network/Sheets/Playwright)"
	@echo "  make db-health        - validate queue DB schema and invariants"
	@echo "  make queue-stats      - show queue analytics snapshot"
	@echo "  make queue-prune-dry  - preview cleanup actions"
	@echo "  make queue-prune-apply - apply cleanup actions"
	@echo ""
	@echo "Docker commands:"
	@echo "  make docker-build     - build the Docker image"
	@echo "  make docker-run-help  - open the CLI help inside Docker"
	@echo "  make docker-smoke     - run smoke checks in Docker"
	@echo "  make docker-prod      - run the production fonts batch in Docker"
	@echo "  make docker-cron      - start the cron batch service"
	@echo "  make docker-phone     - start the phone scheduler service"
	@echo "  make stop             - stop Docker services"
