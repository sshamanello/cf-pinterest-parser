"""
Manages connected Android devices via ADB.
"""
import json
import logging
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Serial → Pinterest account name mapping (from PHONE_ACCOUNTS env var)
# Format: '{"SERIAL1": "account1", "SERIAL2": "account2"}'
_PHONE_ACCOUNTS: dict[str, str] = json.loads(os.environ.get("PHONE_ACCOUNTS", "{}"))


def get_connected_devices() -> list[str]:
    """Return list of connected ADB device serial numbers."""
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    serials = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if line and "\t" in line:
            serial, state = line.split("\t", 1)
            if state.strip() == "device":
                serials.append(serial.strip())
    logger.info("ADB scan complete | devices=%d", len(serials))
    return serials


def get_device_account(serial: str) -> str:
    """Return Pinterest account name for a device, or serial if not mapped."""
    return _PHONE_ACCOUNTS.get(serial, serial)


def push_image(serial: str, local_path: str | Path) -> str:
    """
    Push image to phone gallery folder. Returns remote path on device.
    """
    local_path = Path(local_path)
    remote_path = f"/sdcard/Pictures/pinterest_{local_path.name}"
    result = subprocess.run(
        ["adb", "-s", serial, "push", str(local_path), remote_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"adb push failed: {result.stderr}")

    # Trigger media scanner so gallery picks up the new file
    subprocess.run(
        ["adb", "-s", serial, "shell",
         f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{remote_path}"],
        capture_output=True,
    )
    logger.info("Pushed %s → %s on %s", local_path.name, remote_path, serial)
    return remote_path


def download_image(url: str, dest_dir: str | Path = "/tmp") -> Path:
    """Download image from URL to local temp file. Returns local path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = url.split("/")[-1].split("?")[0] or "pin_image.jpg"
    local_path = dest_dir / filename

    if local_path.exists():
        logger.info("Using cached image %s for %s", local_path, url)
        return local_path

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        local_path.write_bytes(resp.read())

    logger.info("Downloaded %s → %s", url, local_path)
    return local_path


def delete_remote_image(serial: str, remote_path: str) -> None:
    """Remove image from device after posting."""
    subprocess.run(
        ["adb", "-s", serial, "shell", f"rm -f {remote_path}"],
        capture_output=True,
    )
