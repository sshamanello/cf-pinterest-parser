"""
Posts a Pinterest pin via the Pinterest Android app using uiautomator2.
No LLM needed — scripted UI flow.
"""
import logging
import time
from pathlib import Path

import uiautomator2 as u2

from phone_manager import delete_remote_image, download_image, push_image
from sheets import get_sheet_client

logger = logging.getLogger(__name__)

PINTEREST_PKG = "com.pinterest"
TEMP_DIR = Path("/tmp/pinterest_pins")


def _get_next_ready_pin(tab: str = "fonts") -> dict | None:
    """Fetch the next row with publish_status=ready and vds_upload_status=uploaded."""
    spreadsheet = get_sheet_client()
    ws = spreadsheet.worksheet(tab)
    headers = ws.row_values(1)
    rows = ws.get_all_values()

    col = {h: i for i, h in enumerate(headers)}
    required = {"publish_status", "public_image_url", "affiliate_url", "slug"}
    if not required.issubset(col.keys()):
        raise RuntimeError(f"Sheet missing columns: {required - set(col.keys())}")

    for idx, row in enumerate(rows[1:], start=2):
        def val(name):
            return row[col[name]] if col[name] < len(row) else ""

        if (val("publish_status") == "ready"
                and val("vds_upload_status") == "uploaded"
                and val("public_image_url")
                and val("slug")):
            return {
                "row_idx": idx,
                "slug": val("slug"),
                "title": val("title"),
                "hook_text": val("hook_text"),
                "hook_enabled": val("hook_enabled"),
                "public_image_url": val("public_image_url"),
                "affiliate_url": val("affiliate_url"),
                "tab": tab,
                "ws": ws,
                "col": col,
            }
    return None


def _mark_published(pin: dict, pin_id: str = "") -> None:
    ws = pin["ws"]
    col = pin["col"]
    idx = pin["row_idx"]

    updates = []
    def set_col(name, value):
        if name in col:
            # col index is 0-based, gspread range is 1-based
            col_letter = _col_letter(col[name] + 1)
            updates.append({
                "range": f"{col_letter}{idx}",
                "values": [[value]],
            })

    set_col("publish_status", "published")
    set_col("posted", "TRUE")
    set_col("pin_id", pin_id)
    import datetime
    set_col("published_at", datetime.datetime.utcnow().isoformat() + "+00:00")

    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    logger.info("Marked published: %s", pin["slug"])


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _open_pinterest(d: u2.Device) -> None:
    d.app_start(PINTEREST_PKG, stop=True)
    time.sleep(4)
    # Dismiss any popups
    for text in ("Skip", "Not now", "Later", "Не сейчас", "Пропустить"):
        if d(text=text).exists(timeout=2):
            d(text=text).click()
            time.sleep(1)


def _navigate_to_create_pin(d: u2.Device) -> None:
    # Tap the + / Create button in bottom nav
    for res_id in (
        "com.pinterest:id/create_pin_menu_item",
        "com.pinterest:id/fab",
        "com.pinterest:id/creation_button",
    ):
        if d(resourceId=res_id).exists(timeout=3):
            d(resourceId=res_id).click()
            time.sleep(2)
            return

    # Fallback: look by description
    for desc in ("Create", "Создать", "+"):
        if d(description=desc).exists(timeout=2):
            d(description=desc).click()
            time.sleep(2)
            return

    raise RuntimeError("Could not find Create button in Pinterest")


def _select_image_from_gallery(d: u2.Device, remote_path: str) -> None:
    # Tap "from gallery" / camera roll option
    for text in ("Gallery", "Галерея", "Camera Roll", "Photo library"):
        if d(textContains=text).exists(timeout=4):
            d(textContains=text).click()
            time.sleep(2)
            break

    # Select the most recent image (our pushed image should be first)
    # Try selecting by filename or just tap the first image in grid
    time.sleep(2)
    d.xpath('//*[@resource-id="com.pinterest:id/image_grid"]//*[1]').click()
    time.sleep(1)
    # Some versions show a checkmark / "Next" button
    for text in ("Next", "Далее", "Done", "Готово"):
        if d(text=text).exists(timeout=3):
            d(text=text).click()
            time.sleep(2)
            break


def _fill_pin_details(d: u2.Device, title: str, description: str) -> None:
    # Title field
    title_field = None
    for res_id in ("com.pinterest:id/pin_title", "com.pinterest:id/title_input"):
        if d(resourceId=res_id).exists(timeout=3):
            title_field = d(resourceId=res_id)
            break
    if title_field is None:
        title_field = d(hint="Add a title") or d(hintText="Add a title")

    if title_field and title_field.exists(timeout=2):
        title_field.click()
        title_field.clear_text()
        title_field.set_text(title[:100])
        time.sleep(1)

    # Description / note field
    for res_id in ("com.pinterest:id/pin_description", "com.pinterest:id/description_input"):
        if d(resourceId=res_id).exists(timeout=3):
            desc_field = d(resourceId=res_id)
            desc_field.click()
            desc_field.clear_text()
            desc_field.set_text(description[:500])
            time.sleep(1)
            break


def _publish_pin(d: u2.Device) -> str:
    """Tap Publish and return pin_id if visible in URL or toast."""
    for text in ("Publish", "Опубликовать", "Post", "Save"):
        if d(text=text).exists(timeout=5):
            d(text=text).click()
            time.sleep(5)
            return ""
    raise RuntimeError("Publish button not found")


def post_pin(serial: str, tab: str = "fonts") -> bool:
    """
    Post the next ready pin from the sheet on the given device.
    Returns True on success.
    """
    pin = _get_next_ready_pin(tab=tab)
    if pin is None:
        logger.info("[%s] No ready pins in tab '%s'", serial, tab)
        return False

    slug = pin["slug"]
    title = pin["hook_text"] if pin["hook_enabled"] == "TRUE" and pin["hook_text"] else pin["title"]
    description = pin["affiliate_url"]
    image_url = pin["public_image_url"]

    logger.info("[%s] Posting pin: %s", serial, slug)

    # Download image locally, push to phone
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    local_img = download_image(image_url, TEMP_DIR)
    remote_path = push_image(serial, local_img)

    d = u2.connect(serial)
    try:
        _open_pinterest(d)
        _navigate_to_create_pin(d)
        _select_image_from_gallery(d, remote_path)
        _fill_pin_details(d, title=title, description=description)
        pin_id = _publish_pin(d)
        _mark_published(pin, pin_id=pin_id)
        logger.info("[%s] ✓ Published: %s", serial, slug)
        return True
    except Exception as e:
        logger.error("[%s] Failed to post %s: %s", serial, slug, e)
        return False
    finally:
        delete_remote_image(serial, remote_path)
        d.app_stop(PINTEREST_PKG)
