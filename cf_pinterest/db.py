import sqlite3
from datetime import datetime, timedelta, timezone
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


def rebuild_publish_items_all(conn: sqlite3.Connection) -> dict[str, int]:
    niche_rows = conn.execute(
        """
        SELECT DISTINCT niche
        FROM queue_items
        WHERE TRIM(niche) != ''
        ORDER BY niche ASC
        """
    ).fetchall()
    result: dict[str, int] = {}
    for row in niche_rows:
        niche = str(row["niche"])
        result[niche] = rebuild_publish_items(conn, niche)
    return result


def list_publish_items_for_n8n(
    conn: sqlite3.Connection,
    niche: str,
    limit: int = 500,
    statuses: tuple[str, ...] = ("generated", "uploaded"),
) -> list[dict]:
    query = """
        SELECT slug, title, description, image_url, target_url, status, updated_at
        FROM publish_items
        WHERE niche = ?
    """
    params: list[object] = [niche]
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        query += f" AND status IN ({placeholders})"
        params.extend(statuses)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(x) for x in rows]


def check_db_health(conn: sqlite3.Connection) -> dict:
    required_tables = {"queue_items", "publish_items", "queue_sync_runs"}
    required_indexes = {"idx_queue_status", "idx_queue_niche", "idx_publish_status", "idx_publish_niche"}

    table_rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    index_rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    tables = {str(row["name"]) for row in table_rows}
    indexes = {str(row["name"]) for row in index_rows}

    missing_tables = sorted(required_tables - tables)
    missing_indexes = sorted(required_indexes - indexes)

    queue_total = int(conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0])
    publish_total = int(conn.execute("SELECT COUNT(*) FROM publish_items").fetchone()[0])

    orphans = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM publish_items p
            LEFT JOIN queue_items q ON q.slug = p.slug
            WHERE q.slug IS NULL
            """
        ).fetchone()[0]
    )
    empty_slug_queue = int(conn.execute("SELECT COUNT(*) FROM queue_items WHERE TRIM(slug) = ''").fetchone()[0])
    empty_slug_publish = int(conn.execute("SELECT COUNT(*) FROM publish_items WHERE TRIM(slug) = ''").fetchone()[0])

    issues: list[str] = []
    if missing_tables:
        issues.append(f"missing_tables={','.join(missing_tables)}")
    if missing_indexes:
        issues.append(f"missing_indexes={','.join(missing_indexes)}")
    if orphans:
        issues.append(f"publish_orphans={orphans}")
    if empty_slug_queue:
        issues.append(f"empty_slug_queue={empty_slug_queue}")
    if empty_slug_publish:
        issues.append(f"empty_slug_publish={empty_slug_publish}")

    return {
        "ok": not issues,
        "issues": issues,
        "stats": {
            "queue_items_total": queue_total,
            "publish_items_total": publish_total,
            "publish_orphans": orphans,
            "missing_tables": missing_tables,
            "missing_indexes": missing_indexes,
        },
    }


def load_queue_stats(conn: sqlite3.Connection, niche: str | None = None, runs_limit: int = 10) -> dict:
    status_query = """
        SELECT status, COUNT(*) AS cnt
        FROM queue_items
        {where_clause}
        GROUP BY status
        ORDER BY cnt DESC, status ASC
    """
    niche_query = """
        SELECT niche, COUNT(*) AS cnt
        FROM queue_items
        GROUP BY niche
        ORDER BY cnt DESC, niche ASC
    """
    runs_query = """
        SELECT id, niche, report_path, parsed_items, upserted_items, skipped_items, generated_items, uploaded_items, rejected_items, created_at
        FROM queue_sync_runs
        {where_clause}
        ORDER BY id DESC
        LIMIT ?
    """

    if niche:
        where_clause = "WHERE niche = ?"
        status_rows = conn.execute(status_query.format(where_clause=where_clause), (niche,)).fetchall()
        runs_rows = conn.execute(runs_query.format(where_clause=where_clause), (niche, max(1, int(runs_limit)))).fetchall()
    else:
        where_clause = ""
        status_rows = conn.execute(status_query.format(where_clause=where_clause)).fetchall()
        runs_rows = conn.execute(runs_query.format(where_clause=where_clause), (max(1, int(runs_limit)),)).fetchall()

    niche_rows = conn.execute(niche_query).fetchall()

    total_query = "SELECT COUNT(*) FROM queue_items" + (" WHERE niche = ?" if niche else "")
    total = int(conn.execute(total_query, (niche,) if niche else ()).fetchone()[0])

    return {
        "niche": niche,
        "total_items": total,
        "status_counts": {str(r["status"]): int(r["cnt"]) for r in status_rows},
        "niche_counts": {str(r["niche"]): int(r["cnt"]) for r in niche_rows},
        "recent_runs": [dict(r) for r in runs_rows],
    }


def prune_queue_data(
    conn: sqlite3.Connection,
    keep_sync_runs: int = 200,
    prune_rejected_older_than_days: int | None = None,
    apply_changes: bool = False,
) -> dict:
    keep_sync_runs = max(1, int(keep_sync_runs))
    max_sync_id_row = conn.execute("SELECT MAX(id) FROM queue_sync_runs").fetchone()
    max_sync_id = int(max_sync_id_row[0] or 0)
    cutoff_sync_id = max(0, max_sync_id - keep_sync_runs)

    sync_runs_to_delete = int(
        conn.execute(
            "SELECT COUNT(*) FROM queue_sync_runs WHERE id <= ?",
            (cutoff_sync_id,),
        ).fetchone()[0]
    )

    rejected_to_delete = 0
    rejected_before_ts = None
    if prune_rejected_older_than_days is not None:
        days = max(1, int(prune_rejected_older_than_days))
        rejected_before_ts = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rejected_to_delete = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM queue_items
                WHERE status = 'rejected' AND updated_at < ?
                """,
                (rejected_before_ts,),
            ).fetchone()[0]
        )

    if apply_changes:
        if sync_runs_to_delete > 0:
            conn.execute("DELETE FROM queue_sync_runs WHERE id <= ?", (cutoff_sync_id,))
        if rejected_before_ts and rejected_to_delete > 0:
            conn.execute(
                """
                DELETE FROM publish_items
                WHERE slug IN (
                    SELECT slug
                    FROM queue_items
                    WHERE status = 'rejected' AND updated_at < ?
                )
                """,
                (rejected_before_ts,),
            )
            conn.execute(
                """
                DELETE FROM queue_items
                WHERE status = 'rejected' AND updated_at < ?
                """,
                (rejected_before_ts,),
            )
        conn.commit()

    return {
        "apply_changes": apply_changes,
        "keep_sync_runs": keep_sync_runs,
        "sync_runs_to_delete": sync_runs_to_delete,
        "prune_rejected_older_than_days": prune_rejected_older_than_days,
        "rejected_to_delete": rejected_to_delete,
    }
