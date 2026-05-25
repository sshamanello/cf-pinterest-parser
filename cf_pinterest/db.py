import sqlite3
from pathlib import Path

from cf_pinterest.models import QueueItem, QueueSyncResult


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS queue_items (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    niche TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    pin_jpg TEXT NOT NULL DEFAULT '',
    cf_url TEXT NOT NULL DEFAULT '',
    affiliate_url TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(status);
CREATE INDEX IF NOT EXISTS idx_queue_niche ON queue_items(niche);

CREATE TABLE IF NOT EXISTS publish_items (
    slug TEXT PRIMARY KEY,
    niche TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    target_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_publish_status ON publish_items(status);
CREATE INDEX IF NOT EXISTS idx_publish_niche ON publish_items(niche);

CREATE TABLE IF NOT EXISTS queue_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_path TEXT NOT NULL,
    niche TEXT NOT NULL,
    parsed_items INTEGER NOT NULL,
    upserted_items INTEGER NOT NULL,
    skipped_items INTEGER NOT NULL,
    generated_items INTEGER NOT NULL,
    uploaded_items INTEGER NOT NULL,
    rejected_items INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect_db(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def upsert_queue_items(conn: sqlite3.Connection, items: list[QueueItem]) -> int:
    if not items:
        return 0
    conn.executemany(
        """
        INSERT INTO queue_items (
            slug, title, niche, status, pin_jpg, cf_url, affiliate_url, image_url, source_file
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            title=excluded.title,
            niche=excluded.niche,
            status=excluded.status,
            pin_jpg=excluded.pin_jpg,
            cf_url=excluded.cf_url,
            affiliate_url=excluded.affiliate_url,
            image_url=excluded.image_url,
            source_file=excluded.source_file,
            updated_at=CURRENT_TIMESTAMP
        """,
        [
            (
                x.slug,
                x.title,
                x.niche,
                x.status,
                x.pin_jpg,
                x.cf_url,
                x.affiliate_url,
                x.image_url,
                x.source_file,
            )
            for x in items
        ],
    )
    conn.commit()
    return len(items)


def insert_sync_run(
    conn: sqlite3.Connection,
    report_path: str,
    niche: str,
    result: QueueSyncResult,
) -> None:
    conn.execute(
        """
        INSERT INTO queue_sync_runs (
            report_path,
            niche,
            parsed_items,
            upserted_items,
            skipped_items,
            generated_items,
            uploaded_items,
            rejected_items
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_path,
            niche,
            result.parsed_items,
            result.upserted_items,
            result.skipped_items,
            result.generated_items,
            result.uploaded_items,
            result.rejected_items,
        ),
    )
    conn.commit()


def load_queue_summary(conn: sqlite3.Connection, niche: str) -> dict:
    totals = conn.execute(
        """
        SELECT
            COUNT(*) AS total_items,
            SUM(CASE WHEN status = 'generated' THEN 1 ELSE 0 END) AS generated_items,
            SUM(CASE WHEN status = 'uploaded' THEN 1 ELSE 0 END) AS uploaded_items,
            SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_items
        FROM queue_items
        WHERE niche = ?
        """,
        (niche,),
    ).fetchone()

    last_run = conn.execute(
        """
        SELECT
            report_path,
            parsed_items,
            upserted_items,
            skipped_items,
            generated_items,
            uploaded_items,
            rejected_items,
            created_at
        FROM queue_sync_runs
        WHERE niche = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (niche,),
    ).fetchone()

    return {
        "niche": niche,
        "total_items": int((totals["total_items"] or 0) if totals else 0),
        "generated_items": int((totals["generated_items"] or 0) if totals else 0),
        "uploaded_items": int((totals["uploaded_items"] or 0) if totals else 0),
        "rejected_items": int((totals["rejected_items"] or 0) if totals else 0),
        "last_sync": dict(last_run) if last_run else None,
    }


def rebuild_publish_items(conn: sqlite3.Connection, niche: str) -> int:
    rows = conn.execute(
        """
        SELECT slug, niche, title, status, pin_jpg, affiliate_url
        FROM queue_items
        WHERE niche = ?
        """,
        (niche,),
    ).fetchall()
    if not rows:
        return 0

    payload = []
    for row in rows:
        title = row["title"] or row["slug"]
        payload.append(
            (
                row["slug"],
                row["niche"],
                title,
                f"{title} - ready for Pinterest publishing",
                row["pin_jpg"] or "",
                row["affiliate_url"] or "",
                row["status"] or "",
            )
        )

    conn.executemany(
        """
        INSERT INTO publish_items (
            slug, niche, title, description, image_url, target_url, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            niche=excluded.niche,
            title=excluded.title,
            description=excluded.description,
            image_url=excluded.image_url,
            target_url=excluded.target_url,
            status=excluded.status,
            updated_at=CURRENT_TIMESTAMP
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def list_publish_items_for_n8n(conn: sqlite3.Connection, niche: str, limit: int = 500) -> list[dict]:
    rows = conn.execute(
        """
        SELECT slug, title, description, image_url, target_url, status, updated_at
        FROM publish_items
        WHERE niche = ?
          AND status IN ('generated', 'uploaded')
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (niche, max(1, int(limit))),
    ).fetchall()
    return [dict(x) for x in rows]
