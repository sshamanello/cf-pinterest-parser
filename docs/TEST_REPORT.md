# Validation report

**Date:** 2026-05-17
**Branch:** `feature/android-phone-automation`

This report reflects the latest validation pass done while preparing the project for GitHub and production documentation cleanup.

## Summary

### Passing checks
- Environment variables are present.
- `credentials.json` is valid and readable.
- Google Sheets connection works.
- Playwright Chromium is installed and launches.
- Local auto-pin processing on a sample preview image works.
- `scheduler.py --dry-run` works.
- New Linux phone server has the scheduler service running.

### Warnings
- Local workstation does not currently have `adb` installed, so direct phone checks were not executed from this Mac.
- The new phone server was alive during the last check, but had no attached ADB device at that exact moment.

### Failing check
- Live Creative Fabrica parsing on this workstation is currently intermittent because Cloudflare serves `Just a moment...` instead of product cards.

## Detailed results

### Smoke check command
```bash
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 test_components.py
```

### Result matrix
- `env` -> PASS
- `credentials` -> PASS
- `google_sheets` -> PASS
- `playwright` -> PASS
- `parser` -> FAIL
- `auto_pin_sample` -> PASS
- `scheduler_dry_run` -> PASS
- `phone_stack` -> WARN

### Parser failure details
Observed behavior:
- Playwright opens Creative Fabrica successfully.
- Page title resolves to `Just a moment...`.
- Parser retries and now explicitly logs Cloudflare challenge detection.
- Product selector still does not appear within the extended wait window.

This is not a silent failure anymore; it is now surfaced clearly in logs.

## Historical production proof

The system has already succeeded on real content generation previously.

### Successful batch from 2026-05-02
- processed: `500`
- generated: `462`
- rejected: `38`
- VDS uploads: `462`
- Google Sheet rows updated and marked ready

Mode breakdown:
- `card_mode`: `268`
- `fallback_mode`: `194`
- `extract_mode`: `0`
- `reject_mode`: `38`

This confirms that the generation, VDS upload, and sheet synchronization pipeline has already worked in production conditions.

## Server check

Validation command used remotely:
```bash
adb devices -l
tail -n 40 /home/nick/cf-pinterest-parser/logs/scheduler.log
sudo systemctl status cf-pinterest-scheduler.service
```

Observed status:
- `cf-pinterest-scheduler.service` -> `active (running)`
- scheduler log present
- current daily schedule logged correctly
- `adb devices` returned no attached devices at the moment of the check

## What was improved during this pass
- Added shared rotating-file logging via `logging_utils.py`.
- Upgraded smoke checks to match the real 2026 architecture.
- Fixed smoke sample selection to ignore `.DS_Store` and hidden files.
- Added more explicit parser logging around Cloudflare waits.
- Added richer logging in production upload and Android posting helpers.

## Current release readiness verdict

### Ready
- documentation cleanup
- GitHub preparation
- logging and operational runbooks
- phone scheduler deployment assets
- auto-pin smoke generation
- Google Sheets connectivity

### Not fully green yet
- live Creative Fabrica scraping from the current workstation
- Docker runtime build validation on an actual Docker-capable host
- live ADB device presence on the phone server during the check window

## Recommended next checks
1. Re-run parser smoke from the machine/IP that will actually do batch generation.
2. Run `docker compose build` on a host with Docker installed.
3. Re-attach the Android phone to the phone server and confirm `adb devices -l` shows `device`.
4. After that, run one real `run_phones.py warmup` and one real `run_phones.py post --tab fonts` on the new server.
