from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _resolve_level(raw_level: str | None) -> int:
    level_name = (raw_level or os.environ.get("CF_LOG_LEVEL") or "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def _resolve_log_dir() -> Path:
    return Path(os.environ.get("CF_LOG_DIR", "logs")).resolve()


def configure_logging(
    app_name: str,
    *,
    default_log_name: str | None = None,
    level: str | None = None,
) -> logging.Logger:
    """Configure a shared console + rotating-file logger for CLI entrypoints."""
    root = logging.getLogger()
    configured_marker = f"_cf_logging_{app_name}"
    if getattr(root, configured_marker, False):
        return logging.getLogger(app_name)

    log_level = _resolve_level(level)
    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATEFMT)

    root.handlers.clear()
    root.setLevel(log_level)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    log_name = default_log_name or f"{app_name}.log"
    log_dir = _resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / log_name

    max_bytes = int(os.environ.get("CF_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
    backups = int(os.environ.get("CF_LOG_BACKUP_COUNT", "5"))

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.captureWarnings(True)
    setattr(root, configured_marker, True)

    logger = logging.getLogger(app_name)
    logger.info(
        "Logging configured | level=%s | file=%s",
        logging.getLevelName(log_level),
        log_file,
    )
    return logger
