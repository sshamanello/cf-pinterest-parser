import logging
import random
import re
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from config import (
    AFFILIATE_URL_TEMPLATE,
    CF_AFFILIATE_ID,
    MIN_DELAY,
    MAX_DELAY,
    PAGES_PER_RUN,
)

logger = logging.getLogger(__name__)

# JS snippet that extracts products from the rendered page.
# Works on both the category overview (page 1) and paginated pages (page 2+).
_EXTRACT_JS = """
(() => {
    const results = [];
    const seen = new Set();
    const allLinks = [...document.querySelectorAll('a[href*="/product/"]')];

    for (const link of allLinks) {
        const href = link.href;
        const m = href.match(/\\/product\\/([^/]+)\\/?/);
        if (!m) continue;
        const slug = m[1];

        const img = link.querySelector('img');
        // data-src = lazy-loaded (graphics/other), src = already loaded (fonts)
        const imgSrc = img ? (img.dataset.src || img.src || '') : '';

        // textContent on image links can include <noscript> inner HTML as a text node.
        // Filter it out: a real title never starts with "<".
        const rawText = link.textContent.trim();
        const title = rawText.startsWith('<') ? '' : rawText;

        if (!seen.has(slug)) {
            seen.add(slug);
            results.push({ slug, href, imgSrc, title });
        } else if (title) {
            const entry = results.find(r => r.slug === slug);
            if (entry && !entry.title) entry.title = title;
            // also pick up imgSrc if missing from first pass
            if (entry && !entry.imgSrc && imgSrc) entry.imgSrc = imgSrc;
        }
    }
    return results;
})()
"""


def _slug_from_url(url: str) -> str | None:
    match = re.search(r"/product/([^/]+)/?", url)
    return match.group(1) if match else None


def _build_page_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url
    return base_url.rstrip("/") + f"/page/{page}/"


def parse_category(url: str, niche: str, pages: int = PAGES_PER_RUN) -> list[dict]:
    """
    Scrape `pages` pages of a CF category using a real Chromium browser.
    Returns a list of product dicts with keys:
        title, image_url, cf_url, affiliate_url, slug, niche
    """
    all_products: list[dict] = []
    seen_slugs: set[str] = set()  # dedup across pages within one run

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page_obj = context.new_page()

        # Block fonts/videos to speed up page loads
        page_obj.route(
            "**/*.{mp4,webm,woff,woff2,ttf,otf}",
            lambda route: route.abort()
        )

        for page_num in range(1, pages + 1):
            page_url = _build_page_url(url, page_num)
            logger.info("[%s] Fetching page %d: %s", niche, page_num, page_url)

            try:
                page_obj.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
                # Wait until at least one product link is present
                page_obj.wait_for_selector('a[href*="/product/"]', timeout=15_000)
            except PlaywrightTimeoutError:
                logger.warning("[%s] Timeout on page %d — no products appeared.", niche, page_num)
                break
            except Exception as exc:
                logger.error("[%s] Failed to load page %d: %s", niche, page_num, exc)
                break

            raw: list[dict] = page_obj.evaluate(_EXTRACT_JS)

            if not raw:
                logger.info("[%s] No products on page %d, stopping.", niche, page_num)
                break

            page_products = []
            for item in raw:
                slug = item.get("slug", "")
                cf_url = item.get("href", "")
                image_url = item.get("imgSrc", "")
                if not slug or not cf_url or not image_url or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                title = item.get("title") or slug.replace("-", " ").title()
                page_products.append({
                    "title": title,
                    "image_url": image_url,
                    "cf_url": cf_url,
                    "affiliate_url": AFFILIATE_URL_TEMPLATE.format(slug=slug, affiliate_id=CF_AFFILIATE_ID),
                    "slug": slug,
                    "niche": niche,
                })

            logger.info("[%s] Page %d: %d new unique products.", niche, page_num, len(page_products))
            all_products.extend(page_products)

            if page_num < pages:
                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                logger.debug("[%s] Sleeping %.1f s before next page.", niche, delay)
                time.sleep(delay)

        browser.close()

    return all_products
