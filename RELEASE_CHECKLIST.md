# Release Checklist

This checklist is a practical preflight for stable runs on the refactored branch.

## 1) Environment and dependencies

- `python3 -m pip install -r requirements.txt`
- Verify `.env` is configured.
- Verify `credentials.json` or `GOOGLE_CREDENTIALS` is configured if Google Sheets sync is required.

## 2) Local quality gates

- Syntax:
  - `python3 -m py_compile run.py cf_pinterest/*.py test/test_queue_db.py`
- Unit tests:
  - `python3 -m unittest discover -s test -v`

## 3) Operational preflight (single command)

- Default (external checks warn-only):
  - `python3 run.py ops-check --db-path output/_state/queue.db --tab fonts --runs-limit 10`
- Strict external validation (network + Playwright + Sheets required):
  - `python3 run.py ops-check --db-path output/_state/queue.db --tab fonts --runs-limit 10 --strict-external`

## 4) Queue DB controls

- DB integrity:
  - `python3 run.py db-health --db-path output/_state/queue.db`
- Analytics snapshot:
  - `python3 run.py queue-stats --db-path output/_state/queue.db --all-niches --runs-limit 20`
- Rebuild final publish table from technical queue data:
  - `python3 run.py queue-rebuild-publish --db-path output/_state/queue.db --all-niches`
- Prune old data (dry-run first):
  - `python3 run.py queue-prune --db-path output/_state/queue.db --keep-sync-runs 200 --prune-rejected-older-than-days 30`
- Apply prune:
  - `python3 run.py queue-prune --db-path output/_state/queue.db --keep-sync-runs 200 --prune-rejected-older-than-days 30 --apply`

## 5) Import/export workflows

- Import historical spreadsheet data:
  - `python3 run.py import-queue-file --file ./queue_export.xlsx --tab fonts --db-path output/_state/queue.db`
- Export for n8n (CSV):
  - `python3 run.py export-n8n-queue --tab fonts --output output/n8n/fonts_publish.csv --format csv --profile n8n_default --statuses generated,uploaded`
- Export for n8n (JSON):
  - `python3 run.py export-n8n-queue --tab fonts --output output/n8n/fonts_publish.json --format json --profile n8n_default --statuses generated,uploaded`

## 6) Production run path

- Build batch and sync queue DB:
  - `python3 run.py prod-auto-pin --niche fonts --pages 1 --limit 20 --output output/prod/fonts --upload-vds`
- Optional Google Sheets sync:
  - add `--sync-sheet`
- Upload from existing report if needed:
  - `python3 run.py upload-vds --report ./output/prod/fonts/_reports/auto_pin_batch_report.json`

## 7) Release acceptance criteria

- All commands in section 2 pass.
- `ops-check` passes in the target environment mode.
- `db-health` passes with zero issues.
- Required queue export file for n8n is generated and non-empty.
- If production run is executed: report exists and has expected generated/uploaded counts.
