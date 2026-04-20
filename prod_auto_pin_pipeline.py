import json
import logging
import mimetypes
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from auto_pin_pipeline import run_auto_pin_batch
from config import CATEGORIES
from parser import parse_category

logger = logging.getLogger(__name__)


@dataclass
class ProdAutoPinResult:
    niche: str
    parsed_count: int
    selected_count: int
    downloaded_count: int
    generated_count: int
    rejected_count: int
    report_path: str
    input_dir: str
    output_root: str


def _guess_suffix(url: str, content_type: str = "") -> str:
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    if guessed in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    lower = url.lower().split("?")[0]
    for suffix in [".jpg", ".jpeg", ".png", ".webp"]:
        if lower.endswith(suffix):
            return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _download_preview(product: dict, input_dir: Path) -> Path | None:
    slug = product.get("slug", "").strip()
    url = product.get("image_url", "").strip()
    if not slug or not url:
        return None

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
            suffix = _guess_suffix(url, resp.headers.get("Content-Type", ""))
    except Exception as exc:
        logger.warning("[%s] preview download failed: %s", slug, exc)
        return None

    path = input_dir / f"{slug}{suffix}"
    path.write_bytes(content)
    return path


def _enrich_auto_pin_report(report_path: Path, products: list[dict]) -> None:
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    by_slug = {p.get("slug", ""): p for p in products if p.get("slug")}

    for item in raw.get("items", []):
        slug = Path(item.get("source_file", "")).stem
        product = by_slug.get(slug, {})
        item["title"] = product.get("title", "")
        item["image_url"] = product.get("image_url", "")
        item["cf_url"] = product.get("cf_url", "")
        item["affiliate_url"] = product.get("affiliate_url", "")
        item["slug"] = slug

    raw["products"] = products
    report_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def run_prod_auto_pin(
    niche: str = "fonts",
    limit: int = 20,
    output_root: str | Path = "output/prod/fonts",
    pages: int = 1,
) -> ProdAutoPinResult:
    if niche not in CATEGORIES:
        raise ValueError(f"Unknown niche: {niche}")

    out_root = Path(output_root)
    input_dir = out_root / "_input"
    input_dir.mkdir(parents=True, exist_ok=True)

    products = parse_category(CATEGORIES[niche], niche, pages=pages)
    selected = products[: max(0, int(limit))]
    downloaded_products: list[dict] = []

    for product in selected:
        path = _download_preview(product, input_dir)
        if path is None:
            continue
        product = dict(product)
        product["local_image_path"] = str(path)
        downloaded_products.append(product)

    results = run_auto_pin_batch(input_path=input_dir, output_root=out_root)
    report_path = out_root / "_reports" / "auto_pin_batch_report.json"
    _enrich_auto_pin_report(report_path, downloaded_products)

    generated = sum(1 for r in results if r.status == "generated")
    rejected = sum(1 for r in results if r.status == "rejected")

    return ProdAutoPinResult(
        niche=niche,
        parsed_count=len(products),
        selected_count=len(selected),
        downloaded_count=len(downloaded_products),
        generated_count=generated,
        rejected_count=rejected,
        report_path=str(report_path),
        input_dir=str(input_dir),
        output_root=str(out_root),
    )
