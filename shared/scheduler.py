#!/usr/bin/env python3
"""
Autonomous daily scheduler for Pinterest phone automation.

Default behavior:
  - runs forever
  - each day creates 5 jittered slots:
      09:00 ± 15 min
      12:00 ± 15 min
      15:00 ± 15 min
      18:00 ± 15 min
      21:00 ± 15 min
  - for each slot:
      1. warmup
      2. random pause 5-15 minutes
      3. post 1 pin from the fonts tab

Useful checks:
  python scheduler.py --dry-run
  python scheduler.py --run-now --force-no-phone
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from shared.logging_utils import configure_logging

ROOT = Path(__file__).resolve().parent

BASE_SLOTS = (
    (9, 0),
    (12, 0),
    (15, 0),
    (18, 0),
    (21, 0),
)
JITTER_MINUTES = 15
PAUSE_MIN_MINUTES = 5
PAUSE_MAX_MINUTES = 15
SLEEP_CHUNK_SECONDS = 30

WARMUP_CMD = [sys.executable, "run_phones.py", "warmup"]
POST_CMD = [sys.executable, "run_phones.py", "post", "--tab", "fonts"]


@dataclass(frozen=True)
class Slot:
    name: str
    scheduled_for: dt.datetime
    jitter_minutes: int

logger = configure_logging("scheduler", default_log_name="scheduler.log")


def build_daily_schedule(day: dt.date) -> list[Slot]:
    slots: list[Slot] = []
    for hour, minute in BASE_SLOTS:
        jitter = random.randint(-JITTER_MINUTES, JITTER_MINUTES)
        scheduled = dt.datetime.combine(day, dt.time(hour=hour, minute=minute)) + dt.timedelta(minutes=jitter)
        slots.append(
            Slot(
                name=f"{hour:02d}:{minute:02d}",
                scheduled_for=scheduled,
                jitter_minutes=jitter,
            )
        )
    slots.sort(key=lambda slot: slot.scheduled_for)
    logger.info(
        "Daily schedule for %s: %s",
        day.isoformat(),
        ", ".join(
            f"{slot.name}->{slot.scheduled_for.strftime('%H:%M')} ({slot.jitter_minutes:+d}m)"
            for slot in slots
        ),
    )
    return slots


def get_connected_devices() -> list[str]:
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    serials: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        if state.strip() == "device":
            serials.append(serial.strip())
    return serials


def sleep_until(target: dt.datetime) -> None:
    while True:
        now = dt.datetime.now()
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(SLEEP_CHUNK_SECONDS, remaining))


def sleep_minutes(minutes: int) -> None:
    seconds = minutes * 60
    while seconds > 0:
        chunk = min(SLEEP_CHUNK_SECONDS, seconds)
        time.sleep(chunk)
        seconds -= chunk


def run_command(cmd: list[str], label: str) -> bool:
    logger.info("Running %s: %s", label, " ".join(cmd))
    started = time.time()
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    duration = time.time() - started

    output = (result.stdout or "").strip()
    errors = (result.stderr or "").strip()
    if output:
        for line in output.splitlines():
            logger.info("[%s stdout] %s", label, line)
    if errors:
        for line in errors.splitlines():
            logger.warning("[%s stderr] %s", label, line)

    if result.returncode == 0:
        logger.info("%s finished successfully in %.1fs", label, duration)
        return True

    logger.error("%s failed with exit code %s after %.1fs", label, result.returncode, duration)
    return False


def execute_slot(slot: Slot, *, force_no_phone: bool = False) -> None:
    logger.info(
        "Slot started: base=%s actual=%s jitter=%+dmin",
        slot.name,
        slot.scheduled_for.strftime("%Y-%m-%d %H:%M:%S"),
        slot.jitter_minutes,
    )

    devices = [] if force_no_phone else get_connected_devices()
    if not devices:
        logger.warning("No phone connected. Skipping slot %s.", slot.name)
        return

    logger.info("Connected devices for slot %s: %s", slot.name, ", ".join(devices))
    if not run_command(WARMUP_CMD, label="warmup"):
        logger.warning("Warmup failed. Skipping post for slot %s.", slot.name)
        return

    pause_minutes = random.randint(PAUSE_MIN_MINUTES, PAUSE_MAX_MINUTES)
    logger.info("Waiting %d minute(s) between warmup and post", pause_minutes)
    sleep_minutes(pause_minutes)

    devices = [] if force_no_phone else get_connected_devices()
    if not devices:
        logger.warning("Phone disappeared before post. Skipping post for slot %s.", slot.name)
        return

    run_command(POST_CMD, label="post")


def next_slot(now: dt.datetime, schedules: dict[dt.date, list[Slot]]) -> Slot:
    today = now.date()
    if today not in schedules:
        schedules[today] = build_daily_schedule(today)

    for slot in schedules[today]:
        if slot.scheduled_for > now:
            return slot

    tomorrow = today + dt.timedelta(days=1)
    if tomorrow not in schedules:
        schedules[tomorrow] = build_daily_schedule(tomorrow)
    return schedules[tomorrow][0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduler for Pinterest phone automation")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print/log today's schedule and the next slot, then exit.",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Execute one synthetic slot immediately, then exit.",
    )
    parser.add_argument(
        "--force-no-phone",
        action="store_true",
        help="Pretend that no phone is connected (useful for local verification).",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    schedules: dict[dt.date, list[Slot]] = {}

    now = dt.datetime.now()
    schedules[now.date()] = build_daily_schedule(now.date())
    next_planned = next_slot(now, schedules)

    if args.dry_run:
        logger.info(
            "Dry run: next slot is %s at %s",
            next_planned.name,
            next_planned.scheduled_for.strftime("%Y-%m-%d %H:%M:%S"),
        )
        print(f"Next slot: {next_planned.name} at {next_planned.scheduled_for:%Y-%m-%d %H:%M:%S}")
        return 0

    if args.run_now:
        immediate_slot = Slot(
            name="manual-now",
            scheduled_for=now,
            jitter_minutes=0,
        )
        execute_slot(immediate_slot, force_no_phone=args.force_no_phone)
        return 0

    logger.info("Scheduler started. First upcoming slot: %s at %s",
                next_planned.name, next_planned.scheduled_for.strftime("%Y-%m-%d %H:%M:%S"))

    while True:
        now = dt.datetime.now()
        slot = next_slot(now, schedules)
        logger.info("Waiting for next slot %s at %s", slot.name, slot.scheduled_for.strftime("%Y-%m-%d %H:%M:%S"))
        sleep_until(slot.scheduled_for)
        execute_slot(slot)


if __name__ == "__main__":
    raise SystemExit(main())
