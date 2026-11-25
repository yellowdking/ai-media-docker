"""SQLite storage, schema management, and online backup utilities for AI Media Debian server.

All persistent data lives under DATA_DIR (default /data, bind-mounted from /opt/ai-media/data).
Zero paid AI APIs or model SDKs are used anywhere in this server.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    SHANGHAI_TZ = timezone(timedelta(hours=8))
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

DEFAULT_SETTINGS: dict[str, str] = {
    "wechat_appid": "",
    "wechat_appsecret": "",
    "wechat_mock_mode": "true",
    "wechat_publish_mode": "draft_only",
    "auto_backup_enabled": "true",
    "backup_retain_count": "7",
    "data_retain_days": "7",
    "draft_retry_enabled": "true",
    "draft_retry_max_attempts": "5",
    "site_name": "AI Media 自媒体管理后台",
}


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def now_iso() -> str:
    return now_shanghai().isoformat(timespec="seconds")


def today_str() -> str:
    return now_shanghai().strftime("%Y-%m-%d")


def get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def get_db_path() -> Path:
    return get_data_dir() / "ai_media.db"


def ensure_data_dirs() -> dict[str, Path]:
    base = get_data_dir()
    dirs = {
        "root": base,
        "articles": base / "articles",
        "incoming": base / "incoming",
        "backups": base / "backups",
        "logs": base / "logs",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    ensure_data_dirs()
    conn = sqlite3.connect(str(get_db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_id TEXT DEFAULT '',
    source_name TEXT DEFAULT '',
    source_type TEXT DEFAULT 'rss',
    category TEXT DEFAULT 'AI新闻',
    priority INTEGER DEFAULT 5,
    score REAL DEFAULT 0.0,
    published_at TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    status TEXT DEFAULT 'candidate',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_date ON news_items(date);
CREATE INDEX IF NOT EXISTS idx_news_status ON news_items(status);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT DEFAULT 'AI新闻',
    source_url TEXT DEFAULT '',
    source_name TEXT DEFAULT '',
    score REAL DEFAULT 0.0,
    status TEXT DEFAULT 'candidate',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_date ON topics(date);

CREATE TABLE IF NOT EXISTS articles (
    date TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    subtitle TEXT DEFAULT '',
    category TEXT DEFAULT 'AI新闻',
    template TEXT DEFAULT 'news',
    author TEXT DEFAULT 'AI Media',
    publish_mode TEXT DEFAULT 'draft_only',
    review_passed INTEGER DEFAULT 0,
    needs_manual_review INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ready',
    word_count INTEGER DEFAULT 0,
    sources_count INTEGER DEFAULT 0,
    cover_path TEXT DEFAULT '',
    html_path TEXT DEFAULT '',
    md_path TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    sources_json TEXT DEFAULT '{}',
    review_json TEXT DEFAULT '{}',
    ingested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    filename TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size_bytes INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    kind TEXT DEFAULT 'cover',
    created_at TEXT NOT NULL,
    UNIQUE(date, filename)
);

CREATE TABLE IF NOT EXISTS draft_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_date TEXT NOT NULL,
    title TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'mock',
    status TEXT NOT NULL DEFAULT 'queued',
    media_id TEXT DEFAULT '',
    thumb_media_id TEXT DEFAULT '',
    attempts INTEGER DEFAULT 1,
    max_attempts INTEGER DEFAULT 5,
    next_retry_at TEXT DEFAULT '',
    last_error_at TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    response_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_draft_article_date ON draft_tasks(article_date);

CREATE TABLE IF NOT EXISTS publish_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_date TEXT NOT NULL,
    title TEXT NOT NULL,
    channel TEXT DEFAULT 'wechat_official_account',
    publish_mode TEXT DEFAULT 'draft_only',
    draft_media_id TEXT DEFAULT '',
    status TEXT DEFAULT 'draft_ready',
    published_url TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publish_article_date ON publish_records(article_date);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_date TEXT NOT NULL,
    recorded_date TEXT NOT NULL,
    reads INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    collections INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_article_date ON metrics(article_date);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    stage TEXT NOT NULL,
    level TEXT DEFAULT 'INFO',
    status TEXT DEFAULT 'ok',
    message TEXT NOT NULL,
    details_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_logs_date ON task_logs(date);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL UNIQUE,
    rel_path TEXT NOT NULL,
    size_bytes INTEGER DEFAULT 0,
    trigger TEXT DEFAULT 'manual',
    created_at TEXT NOT NULL
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def init_db() -> None:
    ensure_data_dirs()
    ts = now_iso()
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
        _ensure_column(conn, "draft_tasks", "max_attempts", "INTEGER DEFAULT 5")
        _ensure_column(conn, "draft_tasks", "next_retry_at", "TEXT DEFAULT ''")
        _ensure_column(conn, "draft_tasks", "last_error_at", "TEXT DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_draft_status_retry ON draft_tasks(status, next_retry_at)"
        )

        for key, val in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, val, ts),
            )
        conn.execute(
            "UPDATE settings SET value = 'draft_only', updated_at = ? WHERE key = 'wechat_publish_mode'",
            (ts,),
        )


def get_db_health() -> dict[str, Any]:
    with get_conn() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    return {
        "journal_mode": str(journal_mode).lower(),
        "busy_timeout_ms": int(busy_timeout),
        "db_path": str(get_db_path()),
    }


def get_settings_dict(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    result = dict(DEFAULT_SETTINGS)
    for r in rows:
        result[r["key"]] = r["value"]
    result["wechat_publish_mode"] = "draft_only"
    return result


def add_task_log(
    conn: sqlite3.Connection,
    date_str: str,
    stage: str,
    status: str,
    message: str,
    level: str = "INFO",
    details: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO task_logs (date, stage, level, status, message, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date_str,
            stage,
            level,
            status,
            message,
            json.dumps(details or {}, ensure_ascii=False),
            now_iso(),
        ),
    )
    return int(cur.lastrowid or 0)


def prune_backups(conn: sqlite3.Connection, retain: int) -> int:
    """Keep only the latest `retain` backups in DB and on disk under /data/backups/."""
    dirs = ensure_data_dirs()
    backup_dir = dirs["backups"]
    retain = max(3, int(retain))
    removed = 0

    old_rows = conn.execute(
        "SELECT id, filename FROM backups ORDER BY id DESC LIMIT -1 OFFSET ?",
        (retain,),
    ).fetchall()
    for row in old_rows:
        old_file = backup_dir / row["filename"]
        if old_file.exists():
            try:
                old_file.unlink()
            except OSError:
                pass
        conn.execute("DELETE FROM backups WHERE id = ?", (row["id"],))
        removed += 1

    kept_rows = conn.execute("SELECT id, filename FROM backups ORDER BY id DESC").fetchall()
    kept_names: set[str] = set()
    for row in kept_rows:
        fpath = backup_dir / row["filename"]
        if not fpath.exists():
            conn.execute("DELETE FROM backups WHERE id = ?", (row["id"],))
        else:
            kept_names.add(row["filename"])

    disk_files = sorted(
        (p for p in backup_dir.glob("ai_media_*.db") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for idx, fpath in enumerate(disk_files):
        if idx >= retain and fpath.name not in kept_names:
            try:
                fpath.unlink()
                removed += 1
            except OSError:
                pass

    return removed


def create_sqlite_backup(trigger: str = "manual") -> dict[str, Any]:
    """Perform an atomic online SQLite backup into /data/backups/ with retention cleanup."""
    dirs = ensure_data_dirs()
    ts_dt = now_shanghai()
    ts_iso = ts_dt.isoformat(timespec="seconds")
    stamp = ts_dt.strftime("%Y%m%d_%H%M%S")
    filename = f"ai_media_{stamp}_{trigger}.db"
    backup_path = dirs["backups"] / filename
    counter = 1
    while backup_path.exists():
        filename = f"ai_media_{stamp}_{trigger}_{counter}.db"
        backup_path = dirs["backups"] / filename
        counter += 1

    src_conn = sqlite3.connect(str(get_db_path()), timeout=30.0)
    src_conn.execute("PRAGMA busy_timeout=5000;")
    dst_conn = sqlite3.connect(str(backup_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    size_bytes = backup_path.stat().st_size
    rel_path = f"backups/{filename}"

    with get_conn() as conn:
        cfg = get_settings_dict(conn)
        retain = max(3, int(cfg.get("backup_retain_count", "7") or "7"))
        conn.execute(
            """
            INSERT INTO backups (filename, rel_path, size_bytes, trigger, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (filename, rel_path, size_bytes, trigger, ts_iso),
        )
        pruned = prune_backups(conn, retain)
        add_task_log(
            conn,
            date_str=today_str(),
            stage="backup",
            status="completed",
            message=(
                f"SQLite 备份完成 ({trigger}): {filename} ({size_bytes} bytes)"
                + (f"，清理过期备份 {pruned} 份" if pruned else "")
            ),
            details={
                "filename": filename,
                "size_bytes": size_bytes,
                "trigger": trigger,
                "retain_count": retain,
                "pruned_count": pruned,
            },
        )

    return {
        "ok": True,
        "filename": filename,
        "rel_path": rel_path,
        "size_bytes": size_bytes,
        "trigger": trigger,
        "retain_count": retain,
        "pruned_count": pruned,
        "created_at": ts_iso,
    }


def has_scheduled_backup_today() -> bool:
    prefix = f"ai_media_{now_shanghai().strftime('%Y%m%d')}_"
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM backups WHERE trigger = 'scheduled' AND filename LIKE ? LIMIT 1",
            (f"{prefix}%",),
        ).fetchone()
        return row is not None


def cleanup_old_data(retain_days: int | None = None, trigger: str = "scheduled") -> dict[str, Any]:
    """Delete incoming/ and articles/ folders and SQLite records older than `retain_days` (default 7)."""
    dirs = ensure_data_dirs()
    with get_conn() as conn:
        cfg = get_settings_dict(conn)
        days = max(1, int(retain_days if retain_days is not None else (cfg.get("data_retain_days", "7") or "7")))
        backup_retain = max(3, int(cfg.get("backup_retain_count", "7") or "7"))

    cutoff_date = (now_shanghai() - timedelta(days=days)).strftime("%Y-%m-%d")
    removed_incoming: list[str] = []
    removed_articles: list[str] = []
    freed_bytes = 0

    def _dir_size(path: Path) -> int:
        total = 0
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    for kind, bucket_dir, removed_list in (
        ("incoming", dirs["incoming"], removed_incoming),
        ("articles", dirs["articles"], removed_articles),
    ):
        if not bucket_dir.exists():
            continue
        for child in sorted(bucket_dir.iterdir()):
            if child.is_dir() and DATE_DIR_RE.match(child.name) and child.name < cutoff_date:
                freed_bytes += _dir_size(child)
                shutil.rmtree(child, ignore_errors=True)
                removed_list.append(child.name)

    db_deleted: dict[str, int] = {}
    with get_conn() as conn:
        table_date_cols = (
            ("images", "date"),
            ("news_items", "date"),
            ("topics", "date"),
            ("draft_tasks", "article_date"),
            ("publish_records", "article_date"),
            ("metrics", "article_date"),
            ("task_logs", "date"),
            ("articles", "date"),
        )
        for table, col in table_date_cols:
            cur = conn.execute(f"DELETE FROM {table} WHERE {col} != '' AND {col} < ?", (cutoff_date,))
            if cur.rowcount:
                db_deleted[table] = cur.rowcount

        pruned_backups = prune_backups(conn, backup_retain)
        total_removed = len(removed_incoming) + len(removed_articles) + sum(db_deleted.values()) + pruned_backups
        if total_removed > 0 or trigger == "manual":
            add_task_log(
                conn,
                date_str=today_str(),
                stage="cleanup",
                status="completed",
                message=(
                    f"自动空间清理 ({trigger}, 保留 {days} 天, 截止 < {cutoff_date}): "
                    f"incoming={len(removed_incoming)}, articles={len(removed_articles)}, "
                    f"backups={pruned_backups}, 释放约 {freed_bytes // 1024} KB"
                ),
                details={
                    "cutoff_date": cutoff_date,
                    "retain_days": days,
                    "removed_incoming": removed_incoming,
                    "removed_articles": removed_articles,
                    "db_deleted": db_deleted,
                    "pruned_backups": pruned_backups,
                    "freed_bytes": freed_bytes,
                },
            )

    try:
        with get_conn() as conn:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except sqlite3.OperationalError:
        pass

    return {
        "ok": True,
        "trigger": trigger,
        "retain_days": days,
        "cutoff_date": cutoff_date,
        "removed_incoming": removed_incoming,
        "removed_articles": removed_articles,
        "db_deleted": db_deleted,
        "pruned_backups": pruned_backups,
        "freed_bytes": freed_bytes,
    }


def format_bytes(num_bytes: int) -> str:
    size = float(max(0, int(num_bytes)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return "0 B"


def get_data_disk_usage() -> dict[str, Any]:
    """Compute total disk usage (bytes + human string) under DATA_DIR (/data)."""
    base = get_data_dir()
    total_bytes = 0
    if base.exists():
        for p in base.rglob("*"):
            if p.is_file():
                try:
                    total_bytes += p.stat().st_size
                except OSError:
                    pass
    return {
        "disk_usage_bytes": total_bytes,
        "disk_usage_human": format_bytes(total_bytes),
    }


def cleanup_pushed_article_media(date_str: str) -> dict[str, Any]:
    """Delete article images and incoming cache for a date after successful real WeChat draft upload."""
    if not DATE_DIR_RE.match(date_str):
        raise ValueError(f"Invalid date format: {date_str}")
    dirs = ensure_data_dirs()
    removed_files = 0
    freed_bytes = 0

    art_dir = dirs["articles"] / date_str
    if art_dir.exists() and art_dir.is_dir():
        for fpath in sorted(art_dir.iterdir()):
            if fpath.is_file() and fpath.suffix.lower() in IMAGE_EXTS:
                try:
                    size = fpath.stat().st_size
                except OSError:
                    size = 0
                try:
                    fpath.unlink()
                    removed_files += 1
                    freed_bytes += size
                except OSError:
                    pass

    inc_dir = dirs["incoming"] / date_str
    if inc_dir.exists() and inc_dir.is_dir():
        for p in inc_dir.rglob("*"):
            if p.is_file():
                removed_files += 1
                try:
                    freed_bytes += p.stat().st_size
                except OSError:
                    pass
        shutil.rmtree(inc_dir, ignore_errors=True)

    with get_conn() as conn:
        conn.execute("DELETE FROM images WHERE date = ?", (date_str,))
        conn.execute(
            "UPDATE articles SET cover_path = '', updated_at = ? WHERE date = ?",
            (now_iso(), date_str),
        )

    return {
        "ok": True,
        "date": date_str,
        "removed_files": removed_files,
        "freed_bytes": freed_bytes,
    }
