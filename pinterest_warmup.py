"""
Pinterest account warmup using uiautomator2 with randomised human-like behaviour.
No LLM or DroidRun needed — pure scripted gestures with random timing/targets.

Session picks one of several warmup scenarios:
  - scroll home feed + save a pin
  - search for a topic + browse results
  - browse notifications + go home
"""
import logging
import random
import time

import uiautomator2 as u2

logger = logging.getLogger(__name__)

PINTEREST_PKG = "com.pinterest"

# --- helpers ---

def _human_sleep(lo: float = 1.0, hi: float = 3.5) -> None:
    time.sleep(random.uniform(lo, hi))


def _human_swipe(d: u2.Device, direction: str = "up") -> None:
    info = d.info
    w, h = info["displayWidth"], info["displayHeight"]
    cx = w // 2 + random.randint(-40, 40)
    if direction == "up":
        y0, y1 = int(h * 0.75), int(h * 0.25)
    else:
        y0, y1 = int(h * 0.25), int(h * 0.75)
    duration = random.uniform(0.25, 0.6)
    d.swipe(cx, y0, cx, y1, duration=duration)


def _open_app(d: u2.Device) -> bool:
    d.screen_on()
    d.shell("input keyevent 82")
    time.sleep(0.5)
    info = d.info
    d.swipe(info["displayWidth"] // 2, info["displayHeight"] * 3 // 4,
            info["displayWidth"] // 2, info["displayHeight"] // 4, duration=0.3)
    time.sleep(0.5)
    d.press("home")
    time.sleep(1)
    d.app_start(PINTEREST_PKG, stop=True)
    time.sleep(5)
    for text in ("Пропустить", "Skip", "Не сейчас", "Not now"):
        if d(text=text).exists(timeout=1):
            d(text=text).click()
    return d.app_current().get("package") == PINTEREST_PKG


def _save_visible_pin(d: u2.Device) -> bool:
    """Try to long-press a random pin card to save it."""
    pins = d(resourceId="com.pinterest:id/lego_pin_grid_cell_id")
    count = pins.count
    if count == 0:
        return False
    pick = random.randint(0, min(count - 1, 5))
    try:
        pins[pick].long_click()
        _human_sleep(1.5, 2.5)
        # Save sheet appears — tap first board option
        if d(resourceId="com.pinterest:id/board_section_picker_board_cell").exists(timeout=4):
            d(resourceId="com.pinterest:id/board_section_picker_board_cell").click()
            _human_sleep(1, 2)
            logger.info("Saved a pin to board")
            return True
        # Dismiss if no board picker appeared
        d.press("back")
    except Exception:
        pass
    return False


# --- warmup scenarios ---

def _scenario_scroll_and_save(d: u2.Device) -> None:
    """Scroll home feed for a while, save 1-2 pins."""
    logger.info("Scenario: scroll home feed + save pin(s)")
    scrolls = random.randint(4, 9)
    saved = 0
    for i in range(scrolls):
        _human_swipe(d, "up")
        _human_sleep(1.2, 3.8)
        # Occasionally try to save
        if saved < 2 and random.random() < 0.35:
            if _save_visible_pin(d):
                saved += 1
    logger.info("Scrolled %d times, saved %d pin(s)", scrolls, saved)


def _scenario_search(d: u2.Device) -> None:
    """Open search, query a random topic, scroll results."""
    topics = ["modern fonts", "typography design", "cricut fonts",
              "handwritten fonts", "calligraphy fonts", "script fonts"]
    topic = random.choice(topics)
    logger.info("Scenario: search for '%s'", topic)

    if not d(resourceId="com.pinterest:id/menu_search").exists(timeout=5):
        return
    d(resourceId="com.pinterest:id/menu_search").click()
    _human_sleep(1, 2)

    search_box = d(resourceId="com.pinterest:id/search_bar_text") or \
                 d(hint="Search") or d(hintText="Поиск")
    if not search_box.exists(timeout=4):
        d.press("back")
        return
    search_box.click()
    _human_sleep(0.5, 1)
    search_box.set_text(topic)
    d.press("enter")
    _human_sleep(2, 4)

    # Scroll results
    for _ in range(random.randint(3, 6)):
        _human_swipe(d, "up")
        _human_sleep(1.5, 3.5)

    d.press("back")
    _human_sleep(1, 2)


def _scenario_browse_notifications(d: u2.Device) -> None:
    """Open notifications tab, glance, go back."""
    logger.info("Scenario: browse notifications")
    notif = d(resourceId="com.pinterest:id/menu_notifications")
    if not notif.exists(timeout=5):
        return
    notif.click()
    _human_sleep(2, 4)
    for _ in range(random.randint(2, 4)):
        _human_swipe(d, "up")
        _human_sleep(1.5, 3)
    # Go back to home
    home = d(resourceId="com.pinterest:id/bottom_nav_home_icon")
    if home.exists(timeout=3):
        home.click()
    _human_sleep(1, 2)


# --- public entry point ---

SCENARIOS = [
    (_scenario_scroll_and_save, 0.6),   # 60% chance — most common
    (_scenario_search,          0.25),  # 25%
    (_scenario_browse_notifications, 0.15),  # 15%
]


def warmup_device_sync(serial: str) -> bool:
    """Run one warmup session on the given device. Returns True on success."""
    d = u2.connect(serial)

    if not _open_app(d):
        logger.error("[%s] Could not open Pinterest", serial)
        return False

    # Pick scenario by weight
    rand = random.random()
    cumulative = 0.0
    scenario_fn = SCENARIOS[0][0]
    for fn, weight in SCENARIOS:
        cumulative += weight
        if rand < cumulative:
            scenario_fn = fn
            break

    try:
        scenario_fn(d)
        logger.info("[%s] ✓ Warmup complete", serial)
        return True
    except Exception as e:
        logger.error("[%s] Warmup failed: %s", serial, e)
        return False
    finally:
        d.app_stop(PINTEREST_PKG)
