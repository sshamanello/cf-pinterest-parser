"""
Posts a Pinterest pin via the Pinterest Android app using uiautomator2.

Verified UI flow (Russian locale, Pinterest on Android 8.1):
  1. menu_creation → action_button_label("Пин")
  2. Permissions dialog (РАЗРЕШИТЬ x2) → lands on camera
  3. back → MediaGalleryActivity (gallery_title = album name)
  4. Select album PinterestBot → items[2] = our photo (0=camera, 1=website)
  5. Tap Далее → CreationActivity
  6. editor_title / editor_description → Далее (toolbar) → board picker → Опубликовать
"""
import datetime
import logging
import subprocess
import time
from pathlib import Path

import uiautomator2 as u2

from phone_manager import download_image
from sheets import get_sheet_client

logger = logging.getLogger(__name__)

PINTEREST_PKG = "com.pinterest"
TEMP_DIR = Path("/tmp/pinterest_pins")
REMOTE_ALBUM_DIR = "/sdcard/DCIM/PinterestBot"


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _get_next_ready_pin(tab: str = "fonts") -> dict | None:
    spreadsheet = get_sheet_client()
    ws = spreadsheet.worksheet(tab)
    headers = ws.row_values(1)
    rows = ws.get_all_values()
    col = {h: i for i, h in enumerate(headers)}

    for idx, row in enumerate(rows[1:], start=2):
        def val(name):
            return row[col[name]] if name in col and col[name] < len(row) else ""

        if (val("publish_status") == "ready"
                and val("vds_upload_status") == "uploaded"
                and val("public_image_url")
                and val("slug")):
            logger.info("Found ready pin in sheet | tab=%s | row=%d | slug=%s", tab, idx, val("slug"))
            return {
                "row_idx": idx, "slug": val("slug"),
                "title": val("title"), "hook_text": val("hook_text"),
                "hook_enabled": val("hook_enabled"),
                "public_image_url": val("public_image_url"),
                "affiliate_url": val("affiliate_url"),
                "tab": tab, "ws": ws, "col": col,
            }
    return None


def _mark_published(pin: dict, pin_id: str = "") -> None:
    ws, col, idx = pin["ws"], pin["col"], pin["row_idx"]
    updates = []
    for name, value in [
        ("publish_status", "published"), ("posted", "TRUE"),
        ("pin_id", pin_id),
        ("published_at", datetime.datetime.utcnow().isoformat() + "+00:00"),
    ]:
        if name in col:
            updates.append({"range": f"{_col_letter(col[name]+1)}{idx}", "values": [[value]]})
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    logger.info("Marked published: %s", pin["slug"])


def _unlock_and_home(d: u2.Device) -> None:
    d.screen_on()
    d.shell("input keyevent 82")
    time.sleep(0.5)
    info = d.info
    d.swipe(info["displayWidth"]//2, info["displayHeight"]*3//4,
            info["displayWidth"]//2, info["displayHeight"]//4, duration=0.3)
    time.sleep(0.5)
    d.press("home")
    time.sleep(1)


def _open_pinterest(d: u2.Device) -> None:
    _unlock_and_home(d)
    logger.info("Opening Pinterest app")
    d.app_start(PINTEREST_PKG, stop=True)
    time.sleep(6)
    # Dismiss any onboarding popups
    for text in ("Пропустить", "Skip", "Не сейчас", "Not now"):
        if d(text=text).exists(timeout=1):
            d(text=text).click()
            time.sleep(0.5)


def _navigate_to_gallery(d: u2.Device) -> None:
    """
    Create → Пин → grant permissions (first time) → arrives at camera → back → gallery.
    On subsequent runs lands directly in gallery.
    """
    # Tap Create in bottom nav
    assert d(resourceId="com.pinterest:id/menu_creation").exists(timeout=8), \
        "Create button not found"
    logger.info("Pinterest opened, entering Create -> Pin flow")
    d(resourceId="com.pinterest:id/menu_creation").click()
    time.sleep(2)

    # Choose Пин (not Пин-идея or Доска)
    assert d(resourceId="com.pinterest:id/action_button_label", text="Пин").exists(timeout=5), \
        "'Пин' not found in action sheet"
    d(resourceId="com.pinterest:id/action_button_label", text="Пин").click()
    time.sleep(3)

    # Grant storage/camera/audio permissions if asked (first time only)
    for _ in range(3):
        if d(text="РАЗРЕШИТЬ").exists(timeout=2):
            d(text="РАЗРЕШИТЬ").click()
            time.sleep(1.5)

    # After permissions Pinterest may land on camera — press back to get to gallery
    if d(resourceId="com.pinterest:id/camera_capture").exists(timeout=3):
        logger.info("Camera opened after permissions — pressing back to gallery")
        d.press("back")
        time.sleep(2)

    # Should now be in MediaGalleryActivity
    assert d(resourceId="com.pinterest:id/gallery_title").exists(timeout=8), \
        "Gallery not opened"


def _select_image_from_album(d: u2.Device) -> None:
    """
    Navigate to PinterestBot album and select the first photo.
    Album item layout: [0]=camera  [1]=website  [2..]=photos
    """
    # Open album picker by tapping the title
    current_title = d(resourceId="com.pinterest:id/gallery_title").get_text(timeout=3)
    if current_title != "PinterestBot":
        logger.info("Switching gallery album from '%s' to PinterestBot", current_title)
        d(resourceId="com.pinterest:id/gallery_title").click()
        time.sleep(2)
        assert d(text="PinterestBot").exists(timeout=5), \
            "PinterestBot album not found — did the image push succeed?"
        d(text="PinterestBot").click()
        time.sleep(2)

    # Select the first actual photo (index 2, after camera and website buttons)
    items = d(resourceId="com.pinterest:id/media_gallery_recycler").child(
        className="android.widget.FrameLayout", clickable=True
    )
    count = items.count
    assert count > 2, f"Expected >2 items in PinterestBot album, got {count}"
    logger.info("Selecting first uploaded media item from PinterestBot album | gallery_count=%d", count)
    items[2].click()
    time.sleep(2)

    # Tap Далее
    if d(text="Далее").exists(timeout=5):
        d(text="Далее").click()
    else:
        assert d(resourceId="com.pinterest:id/gallery_next_gestalt_button").exists(timeout=5), \
            "Далее button not found after selecting image"
        d(resourceId="com.pinterest:id/gallery_next_gestalt_button").click()
    time.sleep(5)

    assert "CreationActivity" in d.app_current().get("activity", ""), \
        f"Expected CreationActivity, got: {d.app_current()}"


def _fill_and_publish(d: u2.Device, title: str, description: str) -> None:
    """Fill title + description in CreationActivity, then publish."""
    logger.info("Filling pin editor | title_len=%d | description_len=%d", len(title), len(description))
    # Title
    title_field = d(resourceId="com.pinterest:id/editor_title")
    assert title_field.exists(timeout=5), "Title field not found"
    title_field.click()
    title_field.clear_text()
    title_field.set_text(title[:100])
    d.press("back")  # close keyboard
    time.sleep(1)

    # Scroll down so description field comes into view
    cx = d.info["displayWidth"] // 2
    d.swipe(cx, 700, cx, 400, duration=0.3)
    time.sleep(0.5)

    # Description
    desc_field = d(resourceId="com.pinterest:id/editor_description")
    if desc_field.exists(timeout=3):
        desc_field.click()
        time.sleep(0.5)
        desc_field.clear_text()
        desc_field.set_text(description[:500])
        d.press("back")  # close keyboard
        time.sleep(1)
    else:
        logger.warning("Description field not found — skipping")

    # Tap Далее in toolbar to go to board picker
    assert d(text="Далее").exists(timeout=5), "Далее not found in editor toolbar"
    d(text="Далее").click()
    time.sleep(4)

    # Board picker bottom sheet appears — select first available board
    if d(resourceId="com.pinterest:id/board_section_picker_board_cell").exists(timeout=5):
        logger.info("Board picker opened, selecting first board")
        d(resourceId="com.pinterest:id/board_section_picker_board_cell").click()
        time.sleep(5)
    else:
        import re as _re
        xml2 = d.dump_hierarchy()
        logger.warning("Board picker not found — texts: %s",
                       [t for t in _re.findall(r'text="([^"]+)"', xml2) if t.strip()][:10])

    # Dismiss success toast if present ("Отличный пин!")
    if d(text="Готово").exists(timeout=5):
        d(text="Готово").click()
        time.sleep(1)


def post_pin(serial: str, tab: str = "fonts") -> bool:
    """Post the next ready pin from the sheet. Returns True on success."""
    pin = _get_next_ready_pin(tab=tab)
    if pin is None:
        logger.info("[%s] No ready pins in tab '%s'", serial, tab)
        return False

    slug = pin["slug"]
    title = pin["hook_text"] if pin["hook_enabled"] == "TRUE" and pin["hook_text"] else pin["title"]
    description = pin["affiliate_url"]

    logger.info("[%s] Posting: %s", serial, slug)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    local_img = download_image(pin["public_image_url"], TEMP_DIR)
    logger.info("[%s] Local pin asset ready: %s", serial, local_img)

    # Push image to dedicated album
    d_raw = u2.connect(serial)
    d_raw.shell(f"mkdir -p {REMOTE_ALBUM_DIR}")
    remote_path = f"{REMOTE_ALBUM_DIR}/{local_img.name}"
    logger.info("[%s] Uploading asset to device album: %s", serial, remote_path)
    r = subprocess.run(["adb", "-s", serial, "push", str(local_img), remote_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"adb push failed: {r.stderr}")
    d_raw.shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
                f"-d file://{remote_path}")
    time.sleep(2)

    try:
        _open_pinterest(d_raw)
        _navigate_to_gallery(d_raw)
        _select_image_from_album(d_raw)
        _fill_and_publish(d_raw, title=title, description=description)
        _mark_published(pin)
        logger.info("[%s] ✓ Published: %s", serial, slug)
        return True
    except Exception as e:
        logger.error("[%s] Failed to post %s: %s", serial, slug, e)
        return False
    finally:
        logger.info("[%s] Cleaning up uploaded device asset: %s", serial, remote_path)
        d_raw.shell(f"rm -f {remote_path}")
        d_raw.app_stop(PINTEREST_PKG)
