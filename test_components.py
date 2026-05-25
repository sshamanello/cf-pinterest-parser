#!/usr/bin/env python3
"""Project smoke checks for the current production pipeline."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from auto_pin_pipeline import run_auto_pin_batch
from config import GOOGLE_CREDENTIALS_PATH
from logging_utils import configure_logging
from parser import parse_category
from sheets import get_sheet_client

ROOT = Path(__file__).resolve().parent
logger = configure_logging("test_components", default_log_name="test_components.log")
load_dotenv()
STRICT_EXTERNAL = os.environ.get("CF_SMOKE_STRICT_EXTERNAL", "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class CheckResult:
    name: str
    ok: bool
    details: str
    required: bool = True


def _result(name: str, ok: bool, details: str, *, required: bool = True) -> CheckResult:
    prefix = "PASS" if ok else ("WARN" if not required else "FAIL")
    logger.info("[%s] %s | %s", prefix, name, details)
    return CheckResult(name=name, ok=ok, details=details, required=required)


def check_env() -> CheckResult:
    required = ["GOOGLE_SHEET_ID", "CF_AFFILIATE_ID"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        return _result("env", False, f"Missing required variables: {', '.join(missing)}")

    optional = [
        "COMFY_URL",
        "VDS_SSH_HOST",
        "VDS_SSH_USER",
        "VDS_PUBLIC_BASE_URL",
        "PHONE_ACCOUNTS",
        "CF_LOG_LEVEL",
    ]
    present = [name for name in optional if os.environ.get(name)]
    return _result(
        "env",
        True,
        f"Required variables present. Optional configured: {', '.join(present) if present else 'none'}",
    )


def check_credentials() -> CheckResult:
    if os.environ.get("GOOGLE_CREDENTIALS"):
        try:
            raw = json.loads(os.environ["GOOGLE_CREDENTIALS"])
            return _result("credentials", True, f"GOOGLE_CREDENTIALS JSON loaded for {raw.get('client_email', 'unknown client')}")
        except Exception as exc:
            return _result("credentials", False, f"GOOGLE_CREDENTIALS is invalid JSON: {exc}")

    path = Path(GOOGLE_CREDENTIALS_PATH)
    if not path.exists():
        return _result("credentials", False, f"File not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _result("credentials", True, f"Loaded credentials file for {raw.get('client_email', 'unknown client')}")
    except Exception as exc:
        return _result("credentials", False, f"Failed to read credentials file: {exc}")


def check_google_sheets() -> CheckResult:
    try:
        spreadsheet = get_sheet_client()
        tabs = [ws.title for ws in spreadsheet.worksheets()]
        return _result(
            "google_sheets",
            True,
            f"Connected to '{spreadsheet.title}' with {len(tabs)} tabs",
            required=STRICT_EXTERNAL,
        )
    except Exception as exc:
        return _result("google_sheets", False, f"Connection failed: {exc}", required=STRICT_EXTERNAL)


def check_playwright() -> CheckResult:
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://www.creativefabrica.com/fonts/", wait_until="domcontentloaded", timeout=30000)
            title = page.title()
            browser.close()
        return _result(
            "playwright",
            True,
            f"Chromium launched successfully; page title='{title[:80]}'",
            required=STRICT_EXTERNAL,
        )
    except Exception as exc:
        return _result("playwright", False, f"Playwright smoke failed: {exc}", required=STRICT_EXTERNAL)


def check_parser() -> CheckResult:
    try:
        products = parse_category("https://www.creativefabrica.com/fonts/", "fonts", pages=1)
        if not products:
            return _result("parser", False, "No products returned for fonts page 1", required=STRICT_EXTERNAL)
        sample = products[0]
        return _result(
            "parser",
            True,
            f"Parsed {len(products)} products; sample slug={sample['slug']}",
            required=STRICT_EXTERNAL,
        )
    except Exception as exc:
        return _result("parser", False, f"Parser failed: {exc}", required=STRICT_EXTERNAL)


def check_auto_pin_sample() -> CheckResult:
    candidates = sorted(
        p for p in (ROOT / "test" / "extractor" / "input").iterdir()
        if p.is_file() and not p.name.startswith(".")
    )
    if not candidates:
        return _result("auto_pin_sample", False, "No sample images in test/extractor/input", required=False)

    sample = candidates[0]
    with tempfile.TemporaryDirectory(prefix="cf_auto_pin_smoke_") as tmpdir:
        try:
            results = run_auto_pin_batch(sample, Path(tmpdir))
            if not results:
                return _result("auto_pin_sample", False, f"No output generated for {sample.name}")
            item = results[0]
            return _result(
                "auto_pin_sample",
                True,
                f"Processed {sample.name}; status={item.status}; mode={item.mode}",
            )
        except Exception as exc:
            return _result("auto_pin_sample", False, f"Auto-pin smoke failed: {exc}")


def check_scheduler_dry_run() -> CheckResult:
    try:
        proc = subprocess.run(
            [sys.executable, "scheduler.py", "--dry-run"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            return _result("scheduler_dry_run", False, f"Exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()}")
        line = (proc.stdout or "").strip().splitlines()[-1]
        return _result("scheduler_dry_run", True, line)
    except Exception as exc:
        return _result("scheduler_dry_run", False, f"Dry-run failed: {exc}")


def check_phone_stack() -> CheckResult:
    adb = shutil.which("adb")
    if not adb:
        return _result("phone_stack", False, "adb is not installed on this host", required=False)
    proc = subprocess.run([adb, "devices"], capture_output=True, text=True, check=False)
    lines = [line for line in proc.stdout.splitlines()[1:] if line.strip()]
    devices = [line.split("\t", 1)[0] for line in lines if "\tdevice" in line]
    if devices:
        return _result("phone_stack", True, f"adb available; connected devices: {', '.join(devices)}", required=False)
    return _result("phone_stack", False, "adb available but no connected devices", required=False)


def main() -> int:
    logger.info("External checks mode: %s", "strict" if STRICT_EXTERNAL else "warn-only")
    checks = [
        check_env,
        check_credentials,
        check_google_sheets,
        check_playwright,
        check_parser,
        check_auto_pin_sample,
        check_scheduler_dry_run,
        check_phone_stack,
    ]

    results = [fn() for fn in checks]
    required_failed = [r for r in results if r.required and not r.ok]
    optional_warn = [r for r in results if (not r.required) and not r.ok]

    logger.info("=" * 72)
    logger.info("Smoke summary | required_failed=%d | optional_warnings=%d", len(required_failed), len(optional_warn))
    for item in results:
        state = "PASS" if item.ok else ("WARN" if not item.required else "FAIL")
        logger.info("%-5s %-20s %s", state, item.name, item.details)

    if required_failed:
        logger.error("Smoke checks failed. Fix the required issues before relying on production runs.")
        return 1

    logger.info("Smoke checks passed for the current required pipeline components.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
