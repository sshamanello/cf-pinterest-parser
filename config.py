import os
from dotenv import load_dotenv

load_dotenv()

GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CF_AFFILIATE_ID = os.environ["CF_AFFILIATE_ID"]
GOOGLE_CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials.json")

AFFILIATE_URL_TEMPLATE = (
    "https://www.creativefabrica.com/product/{slug}/ref/{affiliate_id}/?sharedfrom=pdp"
)

CATEGORIES = {
    "fonts": "https://www.creativefabrica.com/fonts/",
    "graphics": "https://www.creativefabrica.com/graphics/",
    "3d-svg": "https://www.creativefabrica.com/3d-svg/",
    "3d-printing": "https://www.creativefabrica.com/3d-printing/",
    "embroidery": "https://www.creativefabrica.com/embroidery/",
    "laser-cutting": "https://www.creativefabrica.com/laser-cutting/",
    "bundles": "https://www.creativefabrica.com/bundles/",
}

SHEET_TABS = list(CATEGORIES.keys())

# Columns in each tab (0-indexed order)
COLUMNS = ["title", "image_url", "cf_url", "affiliate_url", "slug", "posted", "pin_id", "created_at"]

PAGES_PER_RUN = int(os.environ.get("PAGES_PER_RUN", 3))
MIN_DELAY = 2  # seconds
MAX_DELAY = 5  # seconds
