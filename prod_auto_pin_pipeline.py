import json
import logging
import os
import mimetypes
import shlex
import shutil
import ssl
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from auto_pin_pipeline import run_auto_pin_batch
from config import CATEGORIES
from parser import parse_category
from sheets import ensure_tabs, get_existing_slugs, get_sheet_client

logger = logging.getLogger(__name__)

VDS_SSH_HOST = os.environ.get("VDS_SSH_HOST", "")
VDS_SSH_USER = os.environ.get("VDS_SSH_USER", "")
VDS_SSH_PORT = int(os.environ.get("VDS_SSH_PORT", "22"))
VDS_SSH_PASSWORD = os.environ.get("VDS_SSH_PASSWORD", "")
VDS_REMOTE_DIR = os.environ.get("VDS_REMOTE_DIR", "/var/www/pins/ready")
VDS_PUBLIC_BASE_URL = os.environ.get("VDS_PUBLIC_BASE_URL", "")
SKIP_EXISTING_SHEET = os.environ.get("CF_SKIP_EXISTING_SHEET", "true").strip().lower() not in {"0", "false", "no"}
PAGE_CURSOR_FILE = Path(os.environ.get("CF_PAGE_CURSOR_FILE", "output/_state/page_cursor.json"))
PAGE_CURSOR_MAX = max(1, int(os.environ.get("CF_PAGE_CURSOR_MAX", "100")))
ADAPTIVE_ENABLED = os.environ.get("CF_ADAPTIVE_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
ADAPTIVE_TARGET_NEW = max(1, int(os.environ.get("CF_ADAPTIVE_TARGET_NEW", "50")))
ADAPTIVE_MAX_PAGES_PER_RUN = max(1, int(os.environ.get("CF_ADAPTIVE_MAX_PAGES_PER_RUN", "300")))


@dataclass
class ProdAutoPinResult:
    niche: str
    start_page: int
    end_page: int
    parsed_count: int
    selected_count: int
    downloaded_count: int
    generated_count: int
    rejected_count: int
    uploaded_count: int
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
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=30, context=context) as resp:
            content = resp.read()
            suffix = _guess_suffix(url, resp.headers.get("Content-Type", ""))
    except Exception as exc:
        logger.warning("[%s] preview download failed: %s", slug, exc)
        return None

    path = input_dir / f"{slug}{suffix}"
    path.write_bytes(content)
    logger.info("[%s] preview downloaded -> %s", slug, path)
    return path


def _prepare_local_preview(product: dict, input_dir: Path) -> Path | None:
    slug = str(product.get("slug", "")).strip()
    local_image_path = str(product.get("local_image_path", "")).strip()
    if not slug or not local_image_path:
        return None

    src = Path(local_image_path)
    if not src.exists() or not src.is_file():
        logger.warning("[%s] local preview file not found: %s", slug, src)
        return None

    suffix = src.suffix.lower() if src.suffix else ".jpg"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        suffix = ".jpg"
    if suffix == ".jpeg":
        suffix = ".jpg"

    dst = input_dir / f"{slug}{suffix}"
    shutil.copy2(src, dst)
    logger.info("[%s] local preview copied -> %s", slug, dst)
    return dst


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


def _vds_ready() -> bool:
    return bool(VDS_SSH_HOST and VDS_SSH_USER and VDS_PUBLIC_BASE_URL and VDS_REMOTE_DIR)


def _ssh_base_cmd() -> list[str]:
    cmd = ["ssh", "-p", str(VDS_SSH_PORT), "-o", "StrictHostKeyChecking=accept-new"]
    return cmd


def _scp_base_cmd() -> list[str]:
    cmd = ["scp", "-P", str(VDS_SSH_PORT), "-o", "StrictHostKeyChecking=accept-new"]
    return cmd


def _run_password_aware(cmd: list[str]) -> subprocess.CompletedProcess:
    if not VDS_SSH_PASSWORD:
        return subprocess.run(cmd, check=True, capture_output=True, text=True)

    if shutil.which("sshpass"):
        return subprocess.run(
            ["sshpass", "-p", VDS_SSH_PASSWORD] + cmd,
            check=True,
            capture_output=True,
            text=True,
        )

    if not shutil.which("expect"):
        raise RuntimeError("password auth requires sshpass or expect")

    spawn_cmd = " ".join(shlex.quote(part) for part in cmd)
    script = f"""
set timeout -1
spawn {spawn_cmd}
expect {{
    -re ".*yes/no.*" {{ send "yes\\r"; exp_continue }}
    -re ".*password:.*" {{ send "$env(VDS_SSH_PASSWORD)\\r"; exp_continue }}
    eof
}}
catch wait result
exit [lindex $result 3]
"""
    env = dict(os.environ)
    env["VDS_SSH_PASSWORD"] = VDS_SSH_PASSWORD
    return subprocess.run(
        ["expect", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_remote_mkdir() -> None:
    target = f"{VDS_SSH_USER}@{VDS_SSH_HOST}"
    cmd = _ssh_base_cmd() + [target, f"mkdir -p {VDS_REMOTE_DIR}"]
    logger.info("Ensuring remote VDS directory exists: %s", VDS_REMOTE_DIR)
    _run_password_aware(cmd)


def _upload_file_to_vds(local_path: Path, slug: str) -> dict:
    if not _vds_ready():
        return {
            "vds_upload_status": "skipped",
            "upload_error": "missing_vds_env",
        }
    digest = local_path.read_bytes()
    import hashlib

    suffix = hashlib.sha1(digest).hexdigest()[:10]
    remote_name = f"{slug}-{suffix}.jpg"
    remote_path = f"{VDS_REMOTE_DIR.rstrip('/')}/{remote_name}"
    public_url = f"{VDS_PUBLIC_BASE_URL.rstrip('/')}/{remote_name}"
    target = f"{VDS_SSH_USER}@{VDS_SSH_HOST}:{remote_path}"

    try:
        _run_remote_mkdir()
        cmd = _scp_base_cmd() + [str(local_path), target]
        _run_password_aware(cmd)
    except Exception as exc:
        logger.error("[%s] VDS upload failed: %s", slug, exc)
        return {
            "vds_upload_status": "failed",
            "upload_error": str(exc),
            "remote_image_path": remote_path,
            "public_image_url": public_url,
        }

    logger.info("[%s] VDS upload complete -> %s", slug, public_url)
    return {
        "vds_upload_status": "uploaded",
        "upload_error": "",
        "remote_image_path": remote_path,
        "public_image_url": public_url,
        "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cleanup_status": "pending",
    }


def upload_report_pins_to_vds(report_path: str | Path) -> int:
    report = Path(report_path)
    logger.info("Uploading generated pins from report: %s", report)
    raw = json.loads(report.read_text(encoding="utf-8"))
    uploaded = 0

    for item in raw.get("items", []):
        if item.get("status") != "generated":
            continue
        slug = item.get("slug") or Path(item.get("source_file", "")).stem
        local = Path(item.get("pin_jpg") or "")
        if not slug or not local.exists():
            item["vds_upload_status"] = "failed"
            item["upload_error"] = "pin_jpg_missing"
            continue
        upload_meta = _upload_file_to_vds(local, slug)
        item.update(upload_meta)
        if upload_meta.get("vds_upload_status") == "uploaded":
            uploaded += 1

    raw["vds_upload"] = {
        "uploaded_count": uploaded,
        "remote_dir": VDS_REMOTE_DIR,
        "public_base_url": VDS_PUBLIC_BASE_URL,
    }
    report.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("VDS upload summary | uploaded=%d | report=%s", uploaded, report)
    return uploaded


def _get_existing_sheet_slugs(niche: str) -> set[str]:
    if not SKIP_EXISTING_SHEET:
        return set()
    try:
        spreadsheet = get_sheet_client()
        ensure_tabs(spreadsheet)
        slugs = get_existing_slugs(spreadsheet, niche)
        logger.info("Loaded %d existing slugs from sheet tab '%s'", len(slugs), niche)
        return slugs
    except Exception as exc:
        logger.warning("Could not load existing slugs from sheet for niche '%s': %s", niche, exc)
        return set()


def _load_products_file(products_file: str | Path, niche: str) -> list[dict]:
    path = Path(products_file)
    if not path.exists():
        raise FileNotFoundError(f"Products file not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("items", [])
    if not isinstance(items, list):
        raise ValueError("Products file must contain a list or {'items': [...]} structure")

    products: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug", "")).strip()
        image_url = str(item.get("image_url", "")).strip()
        local_image_path = str(item.get("local_image_path", "")).strip()
        if not slug or (not image_url and not local_image_path):
            continue
        product = dict(item)
        product.setdefault("niche", niche)
        products.append(product)
    return products


def _load_page_cursor_state() -> dict:
    if not PAGE_CURSOR_FILE.exists():
        return {}

    try:
        raw = json.loads(PAGE_CURSOR_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read page cursor state from %s: %s", PAGE_CURSOR_FILE, exc)
        return {}

    return raw if isinstance(raw, dict) else {}


def _save_page_cursor_state(state: dict) -> None:
    PAGE_CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAGE_CURSOR_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_window_size(niche_state: dict, pages: int) -> int:
    base_pages = max(1, int(pages))
    if not ADAPTIVE_ENABLED:
        return base_pages

    last_window_pages = max(1, int(niche_state.get("last_window_pages", base_pages)))
    last_selected = max(0, int(niche_state.get("last_selected_count", 0)))
    target = ADAPTIVE_TARGET_NEW
    max_pages = max(base_pages, ADAPTIVE_MAX_PAGES_PER_RUN)

    if last_selected < target and last_window_pages < max_pages:
        next_pages = min(max_pages, last_window_pages * 2)
        logger.info(
            "Adaptive pages upshift | last_selected=%d < target=%d | pages %d -> %d",
            last_selected,
            target,
            last_window_pages,
            next_pages,
        )
        return next_pages

    if last_selected >= target and last_window_pages > base_pages:
        logger.info(
            "Adaptive pages hold | last_selected=%d >= target=%d | pages=%d",
            last_selected,
            target,
            last_window_pages,
        )
        return last_window_pages

    return base_pages


def _resolve_page_window(niche: str, pages: int) -> tuple[int, int, int]:
    state = _load_page_cursor_state()
    niche_state = state.get(niche, {}) if isinstance(state.get(niche), dict) else {}
    window_size = _resolve_window_size(niche_state, pages)
    start_page = max(1, int(niche_state.get("next_start_page", 1)))
    end_page = start_page + window_size - 1
    logger.info(
        "Resolved page window | niche=%s | current=%d-%d | window_size=%d | cursor_file=%s | max_page=%d",
        niche,
        start_page,
        end_page,
        window_size,
        PAGE_CURSOR_FILE,
        PAGE_CURSOR_MAX,
    )
    return start_page, end_page, window_size


def _advance_page_window(
    niche: str,
    start_page: int,
    end_page: int,
    window_size: int,
    selected_count: int,
    downloaded_count: int,
    parsed_count: int,
) -> int:
    next_start_page = end_page + 1
    if next_start_page > PAGE_CURSOR_MAX:
        next_start_page = 1

    state = _load_page_cursor_state()
    state[niche] = {
        "next_start_page": next_start_page,
        "last_start_page": start_page,
        "last_end_page": end_page,
        "last_window_pages": window_size,
        "last_selected_count": selected_count,
        "last_downloaded_count": downloaded_count,
        "last_parsed_count": parsed_count,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_page_cursor_state(state)
    logger.info(
        "Advanced page cursor | niche=%s | completed=%d-%d | next_start=%d | pages=%d | selected=%d | downloaded=%d | cursor_file=%s",
        niche,
        start_page,
        end_page,
        next_start_page,
        window_size,
        selected_count,
        downloaded_count,
        PAGE_CURSOR_FILE,
    )
    return next_start_page


def run_prod_auto_pin(
    niche: str = "fonts",
    limit: int = 20,
    output_root: str | Path = "output/prod/fonts",
    pages: int = 1,
    upload_vds: bool = False,
    products_file: str | Path | None = None,
) -> ProdAutoPinResult:
    if niche not in CATEGORIES:
        raise ValueError(f"Unknown niche: {niche}")

    out_root = Path(output_root)
    input_dir = out_root / "_input"
    input_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Starting prod auto-pin | niche=%s | pages=%d | limit=%d | output=%s | upload_vds=%s | products_file=%s",
        niche,
        pages,
        limit,
        out_root,
        upload_vds,
        products_file or "",
    )

    if products_file:
        start_page, end_page, window_size = 0, 0, 0
        products = _load_products_file(products_file, niche=niche)
    else:
        start_page, end_page, window_size = _resolve_page_window(niche, pages)
        products = parse_category(CATEGORIES[niche], niche, pages=window_size, start_page=start_page)
    existing_slugs = _get_existing_sheet_slugs(niche)
    selected: list[dict] = []
    skipped_existing = 0
    hard_limit = max(0, int(limit))

    for product in products:
        slug = str(product.get("slug", "")).strip()
        if not slug:
            continue
        if slug in existing_slugs:
            skipped_existing += 1
            continue
        selected.append(product)
        if len(selected) >= hard_limit:
            break

    logger.info(
        "Parsed %d products | page_window=%d-%d | existing skipped=%d | selected new=%d | limit=%d",
        len(products),
        start_page,
        end_page,
        skipped_existing,
        len(selected),
        hard_limit,
    )
    downloaded_products: list[dict] = []

    for product in selected:
        path = _prepare_local_preview(product, input_dir)
        if path is None:
            path = _download_preview(product, input_dir)
        if path is None:
            continue
        product = dict(product)
        product["local_image_path"] = str(path)
        downloaded_products.append(product)

    results = run_auto_pin_batch(input_path=input_dir, output_root=out_root)
    report_path = out_root / "_reports" / "auto_pin_batch_report.json"
    _enrich_auto_pin_report(report_path, downloaded_products)
    uploaded = upload_report_pins_to_vds(report_path) if upload_vds else 0

    generated = sum(1 for r in results if r.status == "generated")
    rejected = sum(1 for r in results if r.status == "rejected")
    logger.info(
        "Prod auto-pin finished | downloaded=%d | generated=%d | rejected=%d | uploaded=%d | report=%s",
        len(downloaded_products),
        generated,
        rejected,
        uploaded,
        report_path,
    )
    if not products_file:
        _advance_page_window(
            niche=niche,
            start_page=start_page,
            end_page=end_page,
            window_size=window_size,
            selected_count=len(selected),
            downloaded_count=len(downloaded_products),
            parsed_count=len(products),
        )

    return ProdAutoPinResult(
        niche=niche,
        start_page=start_page,
        end_page=end_page,
        parsed_count=len(products),
        selected_count=len(selected),
        downloaded_count=len(downloaded_products),
        generated_count=generated,
        rejected_count=rejected,
        uploaded_count=uploaded,
        report_path=str(report_path),
        input_dir=str(input_dir),
        output_root=str(out_root),
    )
