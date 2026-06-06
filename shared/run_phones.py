#!/usr/bin/env python3
"""
CLI for Pinterest phone automation.

Usage:
    python run_phones.py warmup                        # warmup all connected phones
    python run_phones.py warmup --device SERIAL        # warmup one phone
    python run_phones.py post                          # post 1 pin from all phones
    python run_phones.py post --device SERIAL          # post 1 pin from one phone
    python run_phones.py post --tab graphics           # post from specific sheet tab
    python run_phones.py devices                       # list connected devices
"""
import argparse
import logging
import sys

from dotenv import load_dotenv
from shared.logging_utils import configure_logging

load_dotenv()

logger = configure_logging("run_phones", default_log_name="phones.log")


def cmd_devices():
    from publish.phone_manager import get_connected_devices, get_device_account
    devices = get_connected_devices()
    if not devices:
        print("No devices connected. Check USB cable and ADB debug mode.")
        return
    print(f"Connected devices ({len(devices)}):")
    for serial in devices:
        account = get_device_account(serial)
        print(f"  {serial}  →  {account}")


def cmd_warmup(serial: str | None):
    from publish.phone_manager import get_connected_devices
    from publish.pinterest_warmup import warmup_device_sync

    devices = [serial] if serial else get_connected_devices()
    if not devices:
        logger.error("No devices connected.")
        sys.exit(1)

    for dev in devices:
        logger.info("=== Warmup: %s ===", dev)
        ok = warmup_device_sync(dev)
        logger.info("[%s] %s", dev, "✓ done" if ok else "✗ failed")


def cmd_post(serial: str | None, tab: str = "fonts"):
    from publish.phone_manager import get_connected_devices
    from publish.pinterest_post import post_pin

    devices = [serial] if serial else get_connected_devices()
    if not devices:
        logger.error("No devices connected.")
        sys.exit(1)

    for dev in devices:
        logger.info("=== Post: %s (tab: %s) ===", dev, tab)
        ok = post_pin(serial=dev, tab=tab)
        logger.info("[%s] %s", dev, "✓ posted" if ok else "✗ no ready pins or failed")


def main():
    parser = argparse.ArgumentParser(description="Pinterest phone automation")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("devices", help="List connected ADB devices")

    p_warmup = sub.add_parser("warmup", help="Run warmup session on phone(s)")
    p_warmup.add_argument("--device", "-d", help="Device serial (default: all)")

    p_post = sub.add_parser("post", help="Post next ready pin from Google Sheet")
    p_post.add_argument("--device", "-d", help="Device serial (default: all)")
    p_post.add_argument("--tab", "-t", default="fonts", help="Sheet tab (default: fonts)")

    args = parser.parse_args()

    if args.cmd == "devices":
        cmd_devices()
    elif args.cmd == "warmup":
        cmd_warmup(serial=args.device)
    elif args.cmd == "post":
        cmd_post(serial=args.device, tab=args.tab)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
