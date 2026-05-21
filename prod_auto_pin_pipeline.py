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


@dataclass
class ProdAutoPinResult:
    niche: str
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


def run_prod_auto_pin(
    niche: str = "fonts",
    limit: int = 20,
    output_root: str | Path = "output/prod/fonts",
    pages: int = 1,
    upload_vds: bool = False,
) -> ProdAutoPinResult:
    if niche not in CATEGORIES:
        raise ValueError(f"Unknown niche: {niche}")

    out_root = Path(output_root)
    input_dir = out_root / "_input"
    input_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Starting prod auto-pin | niche=%s | pages=%d | limit=%d | output=%s | upload_vds=%s",
        niche,
        pages,
        limit,
        out_root,
        upload_vds,
    )

    products = parse_category(CATEGORIES[niche], niche, pages=pages)
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
        "Parsed %d products | existing skipped=%d | selected new=%d | limit=%d",
        len(products),
        skipped_existing,
        len(selected),
        hard_limit,
    )
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

    return ProdAutoPinResult(
        niche=niche,
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
