"""
Pinterest account warmup using uiautomator2 with randomised human-like behaviour.
No LLM or DroidRun needed — pure scripted gestures with random timing/targets.

Session picks one of several warmup scenarios:
  - scroll home feed + save a pin
  - search for a topic + browse results
  - browse notifications + go home
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import uiautomator2 as u2

logger = logging.getLogger(__name__)

PINTEREST_PKG = "com.pinterest"
PIN_CELL_ID = "com.pinterest:id/lego_pin_grid_cell_id"
BOARD_PICKER_ID = "com.pinterest:id/board_section_picker_board_cell"
MIN_SESSION_SECONDS = 90.0
MAX_SESSION_SECONDS = 180.0


@dataclass
class WarmupSession:
    follow_enabled: bool
    follow_done: bool = False


# --- helpers ---

def _clamped_gauss(mu: float, sigma: float, min_value: float, max_value: float) -> float:
    """Gaussian random value clipped to a sane range for human-like timing."""
    value = random.gauss(mu, sigma)
    return max(min_value, min(max_value, value))


def _human_sleep(lo: float = 1.0, hi: float = 3.5, mu: float | None = None, sigma: float | None = None) -> None:
    """Sleep using a clipped Gaussian distribution instead of flat uniform randomness."""
    if mu is None:
        mu = (lo + hi) / 2
    if sigma is None:
        sigma = max((hi - lo) / 6, 0.08)
    time.sleep(_clamped_gauss(mu=mu, sigma=sigma, min_value=lo, max_value=hi))


def _pick_scroll_style() -> tuple[str, int]:
    roll = random.random()
    if roll < 0.22:
        return "fast", random.randint(2, 4)
    if roll < 0.57:
        return "slow", 1
    return "normal", random.randint(1, 2)


def _human_swipe(d: u2.Device, direction: str = "up", style: str = "normal") -> None:
    info = d.info
    w, h = info["displayWidth"], info["displayHeight"]
    cx = w // 2 + random.randint(-55, 55)

    if direction == "up":
        y0, y1 = int(h * 0.78), int(h * 0.23)
    else:
        y0, y1 = int(h * 0.24), int(h * 0.77)

    if style == "fast":
        duration = _clamped_gauss(0.18, 0.05, 0.10, 0.28)
    elif style == "slow":
        duration = _clamped_gauss(0.62, 0.11, 0.40, 0.90)
    else:
        duration = _clamped_gauss(0.36, 0.08, 0.22, 0.58)

    d.swipe(cx, y0, cx, y1, duration=duration)


def _maybe_mis_tap(d: u2.Device) -> None:
    """Occasionally tap a slightly wrong area and back out immediately."""
    if random.random() >= 0.15:
        return

    info = d.info
    w, h = info["displayWidth"], info["displayHeight"]
    x = random.randint(int(w * 0.08), int(w * 0.92))
    y = random.randint(int(h * 0.18), int(h * 0.48))
    logger.info("Human-like mis-tap at (%d, %d)", x, y)
    d.click(x, y)
    _human_sleep(0.6, 1.4, mu=0.95)
    d.press("back")
    _human_sleep(0.8, 1.6, mu=1.1)


def _click_first_available(
    d: u2.Device,
    *,
    resource_ids: tuple[str, ...] = (),
    texts: tuple[str, ...] = (),
    descriptions: tuple[str, ...] = (),
    timeout: float = 1.2,
) -> bool:
    for rid in resource_ids:
        node = d(resourceId=rid)
        if node.exists(timeout=timeout):
            node.click()
            return True
    for text in texts:
        node = d(text=text)
        if node.exists(timeout=timeout):
            node.click()
            return True
    for desc in descriptions:
        node = d(description=desc)
        if node.exists(timeout=timeout):
            node.click()
            return True
    return False


def _first_existing(
    *nodes,
    timeout: float = 1.0,
):
    for node in nodes:
        try:
            if node.exists(timeout=timeout):
                return node
        except Exception:
            continue
    return None


def _tap_random_visible_pin(d: u2.Device) -> bool:
    pins = d(resourceId=PIN_CELL_ID)
    count = pins.count
    if count == 0:
        return False

    pick = random.randint(0, min(count - 1, 5))
    try:
        pins[pick].click()
        return True
    except Exception:
        return False


def _maybe_follow_author(d: u2.Device, session: WarmupSession) -> bool:
    """Sometimes open author profile and follow. Runs at most once per session."""
    if session.follow_done or not session.follow_enabled:
        return False

    profile_opened = _click_first_available(
        d,
        resource_ids=(
            "com.pinterest:id/pin_closeup_attribution_click_target",
            "com.pinterest:id/pin_closeup_creator_button",
            "com.pinterest:id/pin_closeup_header_avatar",
            "com.pinterest:id/pin_closeup_profile_image",
        ),
        descriptions=("Профиль автора", "Автор", "Profile", "Creator"),
        timeout=1.1,
    )
    if not profile_opened:
        return False

    _human_sleep(1.2, 2.8, mu=1.9)
    followed = _click_first_available(
        d,
        resource_ids=(
            "com.pinterest:id/profile_header_follow_button",
            "com.pinterest:id/follow_button",
        ),
        texts=("Подписаться", "Follow"),
        timeout=1.1,
    )
    if followed:
        session.follow_done = True
        logger.info("Followed pin author")
        _human_sleep(1.0, 2.2, mu=1.5)

    d.press("back")
    _human_sleep(0.9, 1.8, mu=1.3)
    return followed


def _browse_open_pin(d: u2.Device, session: WarmupSession) -> bool:
    """Open a pin, pause to read, maybe scroll inside, then go back."""
    if not _tap_random_visible_pin(d):
        return False

    logger.info("Opened a pin for reading")
    _human_sleep(3.0, 8.0, mu=4.9, sigma=1.1)

    if random.random() < 0.45:
        _maybe_follow_author(d, session)

    style, repeats = _pick_scroll_style()
    for idx in range(repeats):
        _human_swipe(d, "up", style=style)
        if style == "fast":
            _human_sleep(0.35, 1.0, mu=0.55)
        elif style == "slow":
            _human_sleep(2.2, 4.8, mu=3.4)
        else:
            _human_sleep(1.1, 2.4, mu=1.7)

        # Sometimes look back a little like a human re-checking the pin.
        if idx == repeats - 1 and random.random() < 0.25:
            _human_swipe(d, "down", style="normal")
            _human_sleep(0.9, 1.8, mu=1.2)

    d.press("back")
    _human_sleep(1.0, 2.0, mu=1.45)
    return True


def _feed_browse_step(d: u2.Device, session: WarmupSession) -> None:
    """One browsing step with variable speed, optional reading, and occasional mistakes."""
    _maybe_mis_tap(d)

    if random.random() < 0.28:
        _browse_open_pin(d, session)
        return

    style, repeats = _pick_scroll_style()
    for idx in range(repeats):
        _human_swipe(d, "up", style=style)
        if style == "fast":
            _human_sleep(0.45, 1.1, mu=0.65)
        elif style == "slow":
            _human_sleep(2.0, 4.4, mu=3.0)
        else:
            _human_sleep(1.0, 2.9, mu=1.8)

        if style == "fast" and idx < repeats - 1 and random.random() < 0.55:
            continue


def _ensure_home_feed(d: u2.Device) -> None:
    home = d(resourceId="com.pinterest:id/bottom_nav_home_icon")
    if home.exists(timeout=2):
        home.click()
        _human_sleep(0.8, 1.8, mu=1.2)


def _extend_session_if_needed(d: u2.Device, session: WarmupSession, started_at: float) -> None:
    """Guarantee that warmup is not visually too short on the phone."""
    target_duration = _clamped_gauss(
        mu=125.0,
        sigma=22.0,
        min_value=MIN_SESSION_SECONDS,
        max_value=MAX_SESSION_SECONDS,
    )
    elapsed = time.time() - started_at
    if elapsed >= target_duration:
        return

    logger.info("Extending session to %.1fs total (elapsed %.1fs)", target_duration, elapsed)
    _ensure_home_feed(d)

    while (time.time() - started_at) < target_duration:
        _feed_browse_step(d, session)


def _open_app(d: u2.Device) -> bool:
    d.screen_on()
    d.shell("input keyevent 82")
    time.sleep(0.5)
    info = d.info
    d.swipe(
        info["displayWidth"] // 2,
        info["displayHeight"] * 3 // 4,
        info["displayWidth"] // 2,
        info["displayHeight"] // 4,
        duration=0.3,
    )
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
    pins = d(resourceId=PIN_CELL_ID)
    count = pins.count
    if count == 0:
        return False
    pick = random.randint(0, min(count - 1, 5))
    try:
        pins[pick].long_click()
        _human_sleep(1.5, 2.7, mu=2.0)
        if d(resourceId=BOARD_PICKER_ID).exists(timeout=4):
            d(resourceId=BOARD_PICKER_ID).click()
            _human_sleep(1.0, 2.0, mu=1.4)
            logger.info("Saved a pin to board")
            return True
        d.press("back")
    except Exception:
        pass
    return False


# --- warmup scenarios ---

def _scenario_scroll_and_save(d: u2.Device, session: WarmupSession) -> None:
    """Scroll home feed for a while, save 1-2 pins."""
    logger.info("Scenario: scroll home feed + save pin(s)")
    scrolls = random.randint(6, 12)
    saved = 0
    opened = 0
    for _ in range(scrolls):
        _feed_browse_step(d, session)
        if saved < 2 and random.random() < 0.35:
            if _save_visible_pin(d):
                saved += 1
        elif opened < 2 and random.random() < 0.18:
            if _browse_open_pin(d, session):
                opened += 1
    logger.info("Scrolled %d times, saved %d pin(s), opened %d pin(s)", scrolls, saved, opened)


def _scenario_search(d: u2.Device, session: WarmupSession) -> None:
    """Open search, query a random topic, scroll results."""
    topics = [
        "modern fonts",
        "typography design",
        "cricut fonts",
        "handwritten fonts",
        "calligraphy fonts",
        "script fonts",
    ]
    topic = random.choice(topics)
    logger.info("Scenario: search for '%s'", topic)

    if not d(resourceId="com.pinterest:id/menu_search").exists(timeout=5):
        return
    d(resourceId="com.pinterest:id/menu_search").click()
    _human_sleep(1.0, 2.0, mu=1.4)

    search_box = _first_existing(
        d(resourceId="com.pinterest:id/search_bar_text"),
        d(resourceId="com.pinterest:id/search_src_text"),
        d(className="android.widget.EditText"),
        timeout=4,
    )
    if search_box is None:
        d.press("back")
        return
    search_box.click()
    _human_sleep(0.5, 1.1, mu=0.75)
    search_box.set_text(topic)
    d.press("enter")
    _human_sleep(2.0, 4.0, mu=2.8)

    for _ in range(random.randint(5, 8)):
        _feed_browse_step(d, session)

    d.press("back")
    _human_sleep(1.0, 2.0, mu=1.4)


def _scenario_browse_notifications(d: u2.Device, session: WarmupSession) -> None:
    """Open notifications tab, glance, go back."""
    logger.info("Scenario: browse notifications")
    notif = d(resourceId="com.pinterest:id/menu_notifications")
    if not notif.exists(timeout=5):
        return
    notif.click()
    _human_sleep(2.0, 4.0, mu=2.9)
    for _ in range(random.randint(3, 6)):
        _feed_browse_step(d, session)

    home = d(resourceId="com.pinterest:id/bottom_nav_home_icon")
    if home.exists(timeout=3):
        home.click()
    _human_sleep(1.0, 2.0, mu=1.4)


# --- public entry point ---

SCENARIOS = [
    (_scenario_scroll_and_save, 0.6),   # 60% chance — most common
    (_scenario_search, 0.25),           # 25%
    (_scenario_browse_notifications, 0.15),  # 15%
]


def warmup_device_sync(serial: str) -> bool:
    """Run one warmup session on the given device. Returns True on success."""
    d = u2.connect(serial)
    session = WarmupSession(follow_enabled=(random.random() < 0.13))
    started_at = time.time()

    if not _open_app(d):
        logger.error("[%s] Could not open Pinterest", serial)
        return False

    rand = random.random()
    cumulative = 0.0
    scenario_fn = SCENARIOS[0][0]
    for fn, weight in SCENARIOS:
        cumulative += weight
        if rand < cumulative:
            scenario_fn = fn
            break

    try:
        scenario_fn(d, session)
        _extend_session_if_needed(d, session, started_at)
        logger.info("[%s] ✓ Warmup complete | follow_enabled=%s follow_done=%s",
                    serial, session.follow_enabled, session.follow_done)
        return True
    except Exception as e:
        logger.error("[%s] Warmup failed: %s", serial, e)
        return False
    finally:
        try:
            d.press("home")
        except Exception:
            pass
