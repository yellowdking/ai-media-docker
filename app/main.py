"""AI Media Debian FastAPI backend & Chinese Admin UI server.

Hard constraints:
- All persistent data under DATA_DIR (/data -> /opt/ai-media/data).
- Zero paid AI APIs or model SDKs.
- WeChat Official Account integration is STRICTLY draft-only (never auto-publish).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import struct
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from .db import (
    add_task_log,
    cleanup_old_data,
    create_sqlite_backup,
    ensure_data_dirs,
    get_conn,
    get_data_dir,
    get_db_health,
    get_settings_dict,
    has_scheduled_backup_today,
    init_db,
    now_iso,
    now_shanghai,
    prune_backups,
    today_str,
)
from .wechat import (
    PUBLISH_MODE,
    invalidate_access_token_cache,
    sanitize_wechat_html,
    upload_wechat_draft,
)

logger = logging.getLogger("ai_media.server")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
REQUIRED_ARTICLE_FILES = (
    "article.md",
    "article.html",
    "cover.png",
    "sources.json",
    "review.json",
    "metadata.json",
)
OPTIONAL_INGEST_FILES = ("filtered.json", "state.json", "manifest.json")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_DRAFT_UPLOAD_LOCK = asyncio.Lock()
_CACHED_DOCKER_GATEWAY: str | None = None


def _validate_date(date_str: str) -> str:
    if not DATE_RE.match(date_str):
        raise HTTPException(status_code=400, detail=f"无效的日期格式（需要 YYYY-MM-DD）: {date_str}")
    return date_str


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _mask_secret(secret: str) -> str:
    clean = (secret or "").strip()
    if not clean:
        return ""
    if len(clean) < 6:
        return "****"
    return f"****{clean[-4:]}"


def _is_masked_secret(value: str, current_secret: str = "") -> bool:
    clean = (value or "").strip()
    if not clean:
        return True
    if clean.startswith("****") or set(clean) == {"*"}:
        return True
    if current_secret and clean == _mask_secret(current_secret):
        return True
    return False


def _get_docker_bridge_gateway() -> str | None:
    """Detect the container's default Docker bridge gateway from /proc/net/route."""
    global _CACHED_DOCKER_GATEWAY
    if _CACHED_DOCKER_GATEWAY is not None:
        return _CACHED_DOCKER_GATEWAY or None

    route_file = Path("/proc/net/route")
    if not route_file.exists():
        _CACHED_DOCKER_GATEWAY = ""
        return None
    try:
        for line in route_file.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            dest, gateway, flags = parts[1], parts[2], int(parts[3], 16)
            if dest == "00000000" and (flags & 2):
                gw_ip = socket.inet_ntoa(struct.pack("<L", int(gateway, 16)))
                _CACHED_DOCKER_GATEWAY = gw_ip
                return gw_ip
    except Exception:
        pass
    _CACHED_DOCKER_GATEWAY = ""
    return None


def _is_loopback_host_header(host_header: str) -> bool:
    raw = (host_header or "").strip().lower()
    if not raw:
        return False
    if raw.startswith("[::1]"):
        rest = raw[5:]
        return rest == "" or (rest.startswith(":") and rest[1:].isdigit())
    hostname = raw.split(":", 1)[0]
    return hostname in ("127.0.0.1", "localhost")


def _require_localhost(request: Request) -> None:
    """Allow internal ingest ONLY from container loopback or host loopback (127.0.0.1 / ::1)."""
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        raise HTTPException(
            status_code=403,
            detail="禁止通过代理访问内部入库接口（仅允许本机 localhost / 127.0.0.1）",
        )

    client_ip = (request.client.host if request.client else "").strip()
    if client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return

    gw_ip = _get_docker_bridge_gateway()
    host_hdr = request.headers.get("host", "")
    if gw_ip and client_ip == gw_ip and _is_loopback_host_header(host_hdr):
        return

    logger.warning("Rejected non-localhost internal ingest attempt from %s (Host=%s)", client_ip, host_hdr)
    raise HTTPException(
        status_code=403,
        detail="禁止非本机访问内部入库接口（仅允许 localhost / 127.0.0.1）",
    )


def _compute_next_retry_at(attempts: int) -> str:
    """Exponential backoff for queued WeChat draft uploads (2m, 4m, 8m, ..., capped at 60m)."""
    delay_seconds = min(3600, 120 * (2 ** max(0, attempts - 1)))
    return (now_shanghai() + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")


def _parse_manifest_files(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_files = manifest.get("files")
    parsed: dict[str, dict[str, Any]] = {}
    if isinstance(raw_files, dict):
        for fname, info in raw_files.items():
            if isinstance(info, dict):
                parsed[str(fname)] = {
                    "sha256": str(info.get("sha256", "")).lower().strip(),
                    "size": info.get("size"),
                    "required": bool(info.get("required", fname in REQUIRED_ARTICLE_FILES)),
                }
            elif isinstance(info, str):
                parsed[str(fname)] = {
                    "sha256": info.lower().strip(),
                    "size": None,
                    "required": fname in REQUIRED_ARTICLE_FILES,
                }
    elif isinstance(raw_files, list):
        for item in raw_files:
            if isinstance(item, dict) and item.get("name"):
                fname = str(item["name"])
                parsed[fname] = {
                    "sha256": str(item.get("sha256", "")).lower().strip(),
                    "size": item.get("size"),
                    "required": bool(item.get("required", fname in REQUIRED_ARTICLE_FILES)),
                }
    return parsed


async def _daily_backup_loop() -> None:
    """Background loop for daily SQLite backup and >7d disk cleanup (incoming/ + articles/)."""
    await asyncio.sleep(15)
    while True:
        try:
            with get_conn() as conn:
                cfg = get_settings_dict(conn)
            cleanup_old_data(trigger="scheduled")
            if cfg.get("auto_backup_enabled", "true").lower() == "true":
                if not has_scheduled_backup_today():
                    create_sqlite_backup(trigger="scheduled")
        except Exception as exc:
            logger.warning("Scheduled backup/cleanup loop error: %s", exc)
        await asyncio.sleep(3600)


async def _process_due_draft_retries(*, ignore_schedule: bool = False) -> dict[str, Any]:
    with get_conn() as conn:
        cfg = get_settings_dict(conn)
        if not ignore_schedule and cfg.get("draft_retry_enabled", "true").lower() != "true":
            return {"processed": 0, "succeeded": 0, "still_queued": 0, "failed": 0, "skipped": "disabled"}

        now_ts = now_iso()
        if ignore_schedule:
            due_rows = conn.execute(
                """
                SELECT id, article_date FROM draft_tasks
                WHERE status = 'queued' AND attempts < COALESCE(max_attempts, 5)
                ORDER BY id ASC
                LIMIT 5
                """
            ).fetchall()
        else:
            due_rows = conn.execute(
                """
                SELECT id, article_date FROM draft_tasks
                WHERE status = 'queued'
                  AND next_retry_at != ''
                  AND next_retry_at <= ?
                  AND attempts < COALESCE(max_attempts, 5)
                ORDER BY next_retry_at ASC, id ASC
                LIMIT 3
                """,
                (now_ts,),
            ).fetchall()

    succeeded = 0
    still_queued = 0
    failed = 0
    for row in due_rows:
        try:
            res = await _execute_draft_upload(
                row["article_date"],
                retry_task_id=int(row["id"]),
                simulate_failure=False,
                trigger="auto_retry",
            )
            if res.get("ok"):
                succeeded += 1
            elif res.get("task") and res["task"].get("status") == "queued":
                still_queued += 1
            else:
                failed += 1
        except Exception as exc:
            logger.warning("Background draft retry failed for task #%s: %s", row["id"], exc)
            failed += 1

    return {
        "processed": len(due_rows),
        "succeeded": succeeded,
        "still_queued": still_queued,
        "failed": failed,
    }


async def _draft_retry_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await _process_due_draft_retries(ignore_schedule=False)
        except Exception as exc:
            logger.warning("Draft retry background loop error: %s", exc)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    backup_task = asyncio.create_task(_daily_backup_loop())
    retry_task = asyncio.create_task(_draft_retry_loop())
    try:
        yield
    finally:
        for task in (backup_task, retry_task):
            task.cancel()
        for task in (backup_task, retry_task):
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="AI Media Debian Backend",
    version="1.1.0",
    lifespan=lifespan,
)


def _is_localhost_request(request: Request) -> bool:
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return False
    client_ip = (request.client.host if request.client else "").strip()
    if client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return True
    gw_ip = _get_docker_bridge_gateway()
    host_hdr = request.headers.get("host", "")
    return bool(gw_ip and client_ip == gw_ip and _is_loopback_host_header(host_hdr))


@app.middleware("http")
async def admin_basic_auth_middleware(request: Request, call_next):
    admin_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not admin_password:
        return await call_next(request)

    path = request.url.path
    if path == "/health":
        return await call_next(request)
    if _is_localhost_request(request) and (
        path.startswith("/api/internal/")
        or path == "/api/analytics/summary"
        or path.startswith("/api/wechat/draft/")
    ):
        return await call_next(request)

    admin_user = os.environ.get("ADMIN_USER", "admin").strip() or "admin"
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
            user, _, pwd = decoded.partition(":")
            if secrets.compare_digest(user, admin_user) and secrets.compare_digest(pwd, admin_password):
                return await call_next(request)
        except Exception:
            pass

    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="AI Media Admin"'},
    )


# ---------------------------- Pydantic Models ----------------------------


class NewsUpdate(BaseModel):
    status: str = Field(..., pattern="^(candidate|selected|ignored)$")


class TopicCreate(BaseModel):
    date: str = Field(default_factory=today_str)
    title: str = Field(..., min_length=1, max_length=200)
    category: str = "AI新闻"
    source_url: str = ""
    source_name: str = ""
    score: float = 0.0
    status: str = Field(default="candidate", pattern="^(candidate|approved|produced|rejected)$")
    notes: str = ""


class TopicUpdate(BaseModel):
    title: str | None = None
    category: str | None = None
    status: str | None = Field(default=None, pattern="^(candidate|approved|produced|rejected)$")
    notes: str | None = None


class DraftUploadRequest(BaseModel):
    simulate_failure: bool = False


class PublishRecordUpdate(BaseModel):
    status: str = Field(..., pattern="^(draft_ready|manual_published)$")
    published_url: str = ""
    notes: str = ""


class MetricsCreate(BaseModel):
    article_date: str
    recorded_date: str | None = None
    reads: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    shares: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    collections: int = Field(default=0, ge=0)
    notes: str = ""


class SettingsUpdate(BaseModel):
    wechat_appid: str | None = None
    app_id: str | None = None
    wechat_appsecret: str | None = None
    app_secret: str | None = None
    wechat_mock_mode: bool | None = None
    auto_backup_enabled: bool | None = None
    backup_retain_count: int | None = Field(default=None, ge=3, le=365)
    draft_retry_enabled: bool | None = None
    draft_retry_max_attempts: int | None = Field(default=None, ge=1, le=20)
    site_name: str | None = None


# ---------------------------- Core & UI Routes ----------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    ensure_data_dirs()
    db_info = get_db_health()
    with get_conn() as conn:
        queued_drafts = conn.execute(
            "SELECT COUNT(*) AS c FROM draft_tasks WHERE status = 'queued'"
        ).fetchone()["c"]
        backups_count = conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()["c"]
        latest_bk = conn.execute(
            "SELECT filename, created_at FROM backups ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cfg = get_settings_dict(conn)
    return {
        "status": "ok",
        "service": "ai-media-server",
        "publish_mode": PUBLISH_MODE,
        "data_dir": str(get_data_dir()),
        "sqlite": db_info,
        "queued_drafts": queued_drafts,
        "backups_count": backups_count,
        "latest_backup": dict(latest_bk) if latest_bk else None,
        "background_loops": {
            "daily_backup": True,
            "daily_cleanup": True,
            "draft_retry": True,
        },
        "data_retain_days": int(cfg.get("data_retain_days", "7") or "7"),
        "backup_retain_count": int(cfg.get("backup_retain_count", "7") or "7"),
        "time": now_iso(),
    }


@app.get("/", response_class=HTMLResponse)
async def admin_ui() -> HTMLResponse:
    index_path = TEMPLATES_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="Admin UI template missing")
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


# ---------------------------- Internal Ingest API (Localhost ONLY) ----------------------------


@app.post("/api/internal/ingest/{date}")
async def ingest_article(date: str, request: Request) -> dict[str, Any]:
    _require_localhost(request)

    date_str = _validate_date(date)
    dirs = ensure_data_dirs()
    incoming_dir = dirs["incoming"] / date_str
    if not incoming_dir.exists() or not incoming_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"未找到待入库目录: {incoming_dir}")

    manifest_path = incoming_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=400, detail="缺少 manifest.json，拒绝入库")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"manifest.json 解析失败: {exc}")

    manifest_files = _parse_manifest_files(manifest)
    if not manifest_files:
        raise HTTPException(status_code=400, detail="manifest.json 未包含有效文件清单")

    missing_required = [f for f in REQUIRED_ARTICLE_FILES if f not in manifest_files]
    if missing_required:
        raise HTTPException(
            status_code=400,
            detail=f"manifest.json 缺少必需文件: {', '.join(missing_required)}",
        )

    verified_files: list[str] = []
    file_hashes: dict[str, tuple[str, int]] = {}
    for fname, meta in manifest_files.items():
        if not SAFE_FILENAME_RE.match(fname):
            raise HTTPException(status_code=400, detail=f"非法文件名: {fname}")
        fpath = incoming_dir / fname
        if not fpath.exists():
            if meta.get("required") or fname in REQUIRED_ARTICLE_FILES:
                raise HTTPException(status_code=400, detail=f"缺少必需文件: {fname}")
            continue
        actual_sha = _sha256_file(fpath)
        actual_size = fpath.stat().st_size
        expected_sha = meta.get("sha256", "")
        if not expected_sha or actual_sha != expected_sha:
            raise HTTPException(
                status_code=400,
                detail=f"文件 SHA-256 校验失败: {fname} (expected={expected_sha}, actual={actual_sha})",
            )
        if meta.get("size") is not None and int(meta["size"]) != actual_size:
            raise HTTPException(
                status_code=400,
                detail=f"文件大小校验失败: {fname} (expected={meta['size']}, actual={actual_size})",
            )
        verified_files.append(fname)
        file_hashes[fname] = (actual_sha, actual_size)

    target_dir = dirs["articles"] / date_str
    target_dir.mkdir(parents=True, exist_ok=True)
    files_to_copy = set(verified_files) | {"manifest.json"}
    for opt in OPTIONAL_INGEST_FILES:
        if (incoming_dir / opt).exists():
            files_to_copy.add(opt)

    for fname in sorted(files_to_copy):
        src = incoming_dir / fname
        if src.exists() and src.is_file():
            shutil.copy2(src, target_dir / fname)

    try:
        metadata = json.loads((target_dir / "metadata.json").read_text(encoding="utf-8"))
        sources_data = json.loads((target_dir / "sources.json").read_text(encoding="utf-8"))
        review_data = json.loads((target_dir / "review.json").read_text(encoding="utf-8"))
        md_text = (target_dir / "article.md").read_text(encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"文章 JSON/Markdown 解析失败: {exc}")

    title = str(metadata.get("title") or f"{date_str} AI 文章").strip()
    subtitle = str(metadata.get("subtitle") or "").strip()
    category = str(metadata.get("category") or "AI新闻").strip()
    template = str(metadata.get("template") or "news").strip()
    author = str(metadata.get("author") or "AI Media").strip()
    review_passed = 1 if review_data.get("passed") else 0
    needs_manual = 1 if review_data.get("needs_manual_review") else 0
    sources_list = sources_data.get("sources", []) if isinstance(sources_data, dict) else []
    word_count = len(re.sub(r"\s+", "", md_text))

    ts = now_iso()
    news_ingested = 0
    topics_ingested = 0
    images_ingested = 0

    with get_conn() as conn:
        existing = conn.execute("SELECT status FROM articles WHERE date = ?", (date_str,)).fetchone()
        article_status = existing["status"] if existing else "ready"

        conn.execute(
            """
            INSERT INTO articles (
                date, title, subtitle, category, template, author, publish_mode,
                review_passed, needs_manual_review, status, word_count, sources_count,
                cover_path, html_path, md_path, metadata_json, sources_json, review_json,
                ingested_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                title=excluded.title,
                subtitle=excluded.subtitle,
                category=excluded.category,
                template=excluded.template,
                author=excluded.author,
                publish_mode='draft_only',
                review_passed=excluded.review_passed,
                needs_manual_review=excluded.needs_manual_review,
                word_count=excluded.word_count,
                sources_count=excluded.sources_count,
                cover_path=excluded.cover_path,
                html_path=excluded.html_path,
                md_path=excluded.md_path,
                metadata_json=excluded.metadata_json,
                sources_json=excluded.sources_json,
                review_json=excluded.review_json,
                updated_at=excluded.updated_at
            """,
            (
                date_str,
                title,
                subtitle,
                category,
                template,
                author,
                PUBLISH_MODE,
                review_passed,
                needs_manual,
                article_status,
                word_count,
                len(sources_list),
                f"articles/{date_str}/cover.png",
                f"articles/{date_str}/article.html",
                f"articles/{date_str}/article.md",
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(sources_data, ensure_ascii=False),
                json.dumps(review_data, ensure_ascii=False),
                ts,
                ts,
            ),
        )

        for fpath in sorted(target_dir.iterdir()):
            if fpath.is_file() and fpath.suffix.lower() in IMAGE_EXTS:
                sha_hex, size_b = file_hashes.get(fpath.name, (_sha256_file(fpath), fpath.stat().st_size))
                kind = "cover" if fpath.name == "cover.png" else "asset"
                conn.execute(
                    """
                    INSERT INTO images (date, filename, rel_path, size_bytes, sha256, kind, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, filename) DO UPDATE SET
                        size_bytes=excluded.size_bytes,
                        sha256=excluded.sha256,
                        kind=excluded.kind
                    """,
                    (date_str, fpath.name, f"articles/{date_str}/{fpath.name}", size_b, sha_hex, kind, ts),
                )
                images_ingested += 1

        filtered_path = target_dir / "filtered.json"
        if filtered_path.exists():
            try:
                filtered_data = json.loads(filtered_path.read_text(encoding="utf-8"))
                candidates = filtered_data.get("candidates", []) if isinstance(filtered_data, dict) else []
                for idx, item in enumerate(candidates):
                    if not isinstance(item, dict):
                        continue
                    item_id = str(item.get("id") or hashlib.sha256(str(item.get("url", idx)).encode()).hexdigest()[:16])
                    conn.execute(
                        """
                        INSERT INTO news_items (
                            id, date, title, url, source_id, source_name, source_type,
                            category, priority, score, published_at, summary, status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
                        ON CONFLICT(id) DO UPDATE SET
                            title=excluded.title,
                            score=excluded.score,
                            summary=excluded.summary
                        """,
                        (
                            item_id,
                            date_str,
                            str(item.get("title") or ""),
                            str(item.get("url") or ""),
                            str(item.get("source_id") or ""),
                            str(item.get("source_name") or ""),
                            str(item.get("source_type") or "rss"),
                            str(item.get("category") or "AI新闻"),
                            int(item.get("priority") or 5),
                            float(item.get("score") or 0.0),
                            str(item.get("published_at") or ""),
                            str(item.get("summary") or ""),
                            ts,
                        ),
                    )
                    news_ingested += 1

                existing_topics = conn.execute(
                    "SELECT COUNT(*) AS c FROM topics WHERE date = ?", (date_str,)
                ).fetchone()["c"]
                if existing_topics == 0:
                    for item in candidates[:5]:
                        if not isinstance(item, dict):
                            continue
                        conn.execute(
                            """
                            INSERT INTO topics (date, title, category, source_url, source_name, score, status, notes, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, 'candidate', '', ?, ?)
                            """,
                            (
                                date_str,
                                str(item.get("title") or ""),
                                str(item.get("category") or "AI新闻"),
                                str(item.get("url") or ""),
                                str(item.get("source_name") or ""),
                                float(item.get("score") or 0.0),
                                ts,
                                ts,
                            ),
                        )
                        topics_ingested += 1
            except Exception as exc:
                add_task_log(conn, date_str, "ingest", "warning", f"filtered.json 解析跳过: {exc}", level="WARN")

        prod_topic = conn.execute(
            "SELECT id FROM topics WHERE date = ? AND title = ? LIMIT 1", (date_str, title)
        ).fetchone()
        if not prod_topic:
            conn.execute(
                """
                INSERT INTO topics (date, title, category, source_url, source_name, score, status, notes, created_at, updated_at)
                VALUES (?, ?, ?, '', 'AI Media 当日成稿', 100.0, 'produced', '已自动生成并入库', ?, ?)
                """,
                (date_str, title, category, ts, ts),
            )
            topics_ingested += 1

        state_path = target_dir / "state.json"
        if state_path.exists():
            try:
                state_data = json.loads(state_path.read_text(encoding="utf-8"))
                stages = state_data.get("stages", {}) if isinstance(state_data, dict) else {}
                for stage_name, stage_info in stages.items():
                    if isinstance(stage_info, dict) and stage_info.get("status") not in (None, "pending"):
                        msg = stage_info.get("message") or f"Windows 阶段 {stage_name}: {stage_info.get('status')}"
                        add_task_log(
                            conn,
                            date_str=date_str,
                            stage=f"win_{stage_name}",
                            status=str(stage_info.get("status")),
                            message=str(msg),
                            details=stage_info.get("outputs") or {},
                        )
            except Exception as exc:
                add_task_log(conn, date_str, "ingest", "warning", f"state.json 解析跳过: {exc}", level="WARN")

        add_task_log(
            conn,
            date_str=date_str,
            stage="ingest",
            status="completed",
            message=f"文章《{title}》入库完成，校验 {len(verified_files)} 个文件，入库资讯 {news_ingested} 条",
            details={
                "verified_files": verified_files,
                "news_ingested": news_ingested,
                "images_ingested": images_ingested,
            },
        )

    return {
        "ok": True,
        "date": date_str,
        "title": title,
        "category": category,
        "publish_mode": PUBLISH_MODE,
        "review_passed": bool(review_passed),
        "files_verified": len(verified_files),
        "verified_files": verified_files,
        "news_ingested": news_ingested,
        "topics_ingested": topics_ingested,
        "images_ingested": images_ingested,
        "ingested_at": ts,
    }


# ---------------------------- Analytics Summary API ----------------------------


@app.get("/api/analytics/summary")
async def analytics_summary() -> dict[str, Any]:
    with get_conn() as conn:
        art_total = conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
        art_passed = conn.execute("SELECT COUNT(*) AS c FROM articles WHERE review_passed = 1").fetchone()["c"]
        art_manual = conn.execute("SELECT COUNT(*) AS c FROM articles WHERE needs_manual_review = 1").fetchone()["c"]
        art_drafted = conn.execute(
            "SELECT COUNT(*) AS c FROM articles WHERE status IN ('draft_uploaded', 'published_manual')"
        ).fetchone()["c"]
        art_published = conn.execute(
            "SELECT COUNT(*) AS c FROM articles WHERE status = 'published_manual'"
        ).fetchone()["c"]

        cat_rows = conn.execute(
            "SELECT category, COUNT(*) AS count FROM articles GROUP BY category ORDER BY count DESC"
        ).fetchall()

        draft_rows = conn.execute("SELECT status, mode, COUNT(*) AS count FROM draft_tasks GROUP BY status, mode").fetchall()
        draft_stats = {"total": 0, "success": 0, "failed": 0, "queued": 0, "mock": 0, "real": 0}
        for r in draft_rows:
            c = r["count"]
            draft_stats["total"] += c
            if r["status"] in draft_stats:
                draft_stats[r["status"]] += c
            if r["mode"] in draft_stats:
                draft_stats[r["mode"]] += c

        m_agg = conn.execute(
            """
            SELECT
                COUNT(*) AS records,
                COALESCE(SUM(reads), 0) AS total_reads,
                COALESCE(SUM(likes), 0) AS total_likes,
                COALESCE(SUM(shares), 0) AS total_shares,
                COALESCE(SUM(comments), 0) AS total_comments,
                COALESCE(SUM(collections), 0) AS total_collections
            FROM metrics
            """
        ).fetchone()

        recent_metrics = [
            dict(r)
            for r in conn.execute(
                """
                SELECT m.id, m.article_date, a.title, m.recorded_date,
                       m.reads, m.likes, m.shares, m.comments, m.collections, m.notes
                FROM metrics m
                LEFT JOIN articles a ON a.date = m.article_date
                ORDER BY m.recorded_date DESC, m.id DESC
                LIMIT 14
                """
            ).fetchall()
        ]

        news_total = conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()["c"]
        news_by_status = {
            r["status"]: r["count"]
            for r in conn.execute("SELECT status, COUNT(*) AS count FROM news_items GROUP BY status").fetchall()
        }

        log_total = conn.execute("SELECT COUNT(*) AS c FROM task_logs").fetchone()["c"]
        log_by_status = {
            r["status"]: r["count"]
            for r in conn.execute("SELECT status, COUNT(*) AS count FROM task_logs GROUP BY status").fetchall()
        }
        recent_logs = [
            dict(r)
            for r in conn.execute(
                "SELECT id, date, stage, level, status, message, created_at FROM task_logs ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]

        recent_articles = [
            dict(r)
            for r in conn.execute(
                """
                SELECT a.date, a.title, a.category, a.template, a.status,
                       a.review_passed, a.word_count, a.sources_count,
                       COALESCE((SELECT MAX(reads) FROM metrics WHERE article_date = a.date), 0) AS reads,
                       COALESCE((SELECT MAX(likes) FROM metrics WHERE article_date = a.date), 0) AS likes
                FROM articles a
                ORDER BY a.date DESC
                LIMIT 14
                """
            ).fetchall()
        ]

        backups_count = conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()["c"]
        latest_backup_row = conn.execute(
            "SELECT filename, size_bytes, trigger, created_at FROM backups ORDER BY id DESC LIMIT 1"
        ).fetchone()

    avg_reads = round(m_agg["total_reads"] / m_agg["records"], 1) if m_agg["records"] else 0.0

    return {
        "ok": True,
        "generated_at": now_iso(),
        "publish_mode": PUBLISH_MODE,
        "article_counts": {
            "total": art_total,
            "review_passed": art_passed,
            "needs_manual_review": art_manual,
            "draft_uploaded": art_drafted,
            "manual_published": art_published,
        },
        "category_breakdown": [dict(r) for r in cat_rows],
        "draft_stats": draft_stats,
        "metrics_summary": {
            "records": m_agg["records"],
            "total_reads": m_agg["total_reads"],
            "total_likes": m_agg["total_likes"],
            "total_shares": m_agg["total_shares"],
            "total_comments": m_agg["total_comments"],
            "total_collections": m_agg["total_collections"],
            "avg_reads": avg_reads,
            "recent_metrics": recent_metrics,
        },
        "news_summary": {
            "total": news_total,
            "by_status": news_by_status,
        },
        "task_logs_summary": {
            "total": log_total,
            "by_status": log_by_status,
            "recent_logs": recent_logs,
        },
        "recent_articles": recent_articles,
        "backups_count": backups_count,
        "latest_backup": dict(latest_backup_row) if latest_backup_row else None,
    }


# ---------------------------- Module 1: News ----------------------------


@app.get("/api/news")
async def list_news(
    date: str | None = None,
    status: str | None = None,
    category: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if date:
        clauses.append("date = ?")
        params.append(date)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if q:
        clauses.append("(title LIKE ? OR summary LIKE ? OR source_name LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM news_items {where} ORDER BY date DESC, score DESC, published_at DESC LIMIT ?",
            params,
        ).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@app.patch("/api/news/{news_id}")
async def update_news_status(news_id: str, payload: NewsUpdate) -> dict[str, Any]:
    with get_conn() as conn:
        cur = conn.execute("UPDATE news_items SET status = ? WHERE id = ?", (payload.status, news_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="资讯不存在")
        row = conn.execute("SELECT * FROM news_items WHERE id = ?", (news_id,)).fetchone()
    return {"ok": True, "item": dict(row)}


@app.post("/api/news/{news_id}/promote")
async def promote_news_to_topic(news_id: str) -> dict[str, Any]:
    ts = now_iso()
    with get_conn() as conn:
        news = conn.execute("SELECT * FROM news_items WHERE id = ?", (news_id,)).fetchone()
        if not news:
            raise HTTPException(status_code=404, detail="资讯不存在")
        conn.execute("UPDATE news_items SET status = 'selected' WHERE id = ?", (news_id,))
        cur = conn.execute(
            """
            INSERT INTO topics (date, title, category, source_url, source_name, score, status, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'approved', '由资讯管理一键加入选题', ?, ?)
            """,
            (
                news["date"],
                news["title"],
                news["category"],
                news["url"],
                news["source_name"],
                news["score"],
                ts,
                ts,
            ),
        )
        topic = conn.execute("SELECT * FROM topics WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "topic": dict(topic)}


# ---------------------------- Module 2: Topics ----------------------------


@app.get("/api/topics")
async def list_topics(status: str | None = None) -> dict[str, Any]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM topics WHERE status = ? ORDER BY date DESC, score DESC, id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM topics ORDER BY date DESC, score DESC, id DESC").fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@app.post("/api/topics")
async def create_topic(payload: TopicCreate) -> dict[str, Any]:
    date_str = _validate_date(payload.date.strip())
    ts = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO topics (date, title, category, source_url, source_name, score, status, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date_str,
                payload.title.strip(),
                payload.category.strip(),
                payload.source_url.strip(),
                payload.source_name.strip(),
                payload.score,
                payload.status,
                payload.notes.strip(),
                ts,
                ts,
            ),
        )
        row = conn.execute("SELECT * FROM topics WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "item": dict(row)}


@app.patch("/api/topics/{topic_id}")
async def update_topic(topic_id: int, payload: TopicUpdate) -> dict[str, Any]:
    updates: list[str] = []
    params: list[Any] = []
    for field in ("title", "category", "status", "notes"):
        val = getattr(payload, field)
        if val is not None:
            updates.append(f"{field} = ?")
            params.append(val)
    if not updates:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    updates.append("updated_at = ?")
    params.append(now_iso())
    params.append(topic_id)
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE topics SET {', '.join(updates)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="选题不存在")
        row = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    return {"ok": True, "item": dict(row)}


@app.delete("/api/topics/{topic_id}")
async def delete_topic(topic_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="选题不存在")
    return {"ok": True}


# ---------------------------- Module 3 & 4: Articles & Preview ----------------------------


@app.get("/api/articles")
async def list_articles() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT date, title, subtitle, category, template, author, publish_mode,
                   review_passed, needs_manual_review, status, word_count, sources_count,
                   ingested_at, updated_at
            FROM articles
            ORDER BY date DESC
            """
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["cover_url"] = f"/api/articles/{d['date']}/files/cover.png"
        items.append(d)
    return {"items": items, "count": len(items)}


@app.get("/api/articles/{date}")
async def get_article_detail(date: str) -> dict[str, Any]:
    date_str = _validate_date(date)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM articles WHERE date = ?", (date_str,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"未找到文章: {date_str}")
        drafts = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM draft_tasks WHERE article_date = ? ORDER BY id DESC", (date_str,)
            ).fetchall()
        ]
        metrics = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM metrics WHERE article_date = ? ORDER BY recorded_date DESC, id DESC", (date_str,)
            ).fetchall()
        ]

    base = get_data_dir()
    art_dir = base / "articles" / date_str
    raw_html = (art_dir / "article.html").read_text(encoding="utf-8") if (art_dir / "article.html").exists() else ""
    md_content = (art_dir / "article.md").read_text(encoding="utf-8") if (art_dir / "article.md").exists() else ""

    item = dict(row)
    item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    item["sources"] = json.loads(item.pop("sources_json") or "{}")
    item["review"] = json.loads(item.pop("review_json") or "{}")
    item["article_html"] = sanitize_wechat_html(raw_html)
    item["article_md"] = md_content
    item["cover_url"] = f"/api/articles/{date_str}/files/cover.png"
    item["draft_tasks"] = drafts
    item["metrics"] = metrics
    return item


@app.get("/api/articles/{date}/files/{filename}")
async def get_article_file(date: str, filename: str) -> FileResponse:
    date_str = _validate_date(date)
    if not SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="非法文件名")
    target = (get_data_dir() / "articles" / date_str / filename).resolve()
    root = (get_data_dir() / "articles" / date_str).resolve()
    if root not in target.parents or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path=str(target))


# ---------------------------- Module 5: Images ----------------------------


@app.get("/api/images")
async def list_images() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.*, a.title AS article_title
            FROM images i
            LEFT JOIN articles a ON a.date = i.date
            ORDER BY i.date DESC, i.id DESC
            """
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["url"] = f"/api/articles/{d['date']}/files/{d['filename']}"
        items.append(d)
    return {"items": items, "count": len(items)}


# ---------------------------- Module 6: WeChat Draft Upload (Draft-Only!) ----------------------------


async def _execute_draft_upload(
    article_date: str,
    retry_task_id: int | None = None,
    simulate_failure: bool = False,
    trigger: str = "manual",
) -> dict[str, Any]:
    date_str = _validate_date(article_date)
    async with _DRAFT_UPLOAD_LOCK:
        with get_conn() as conn:
            art = conn.execute("SELECT * FROM articles WHERE date = ?", (date_str,)).fetchone()
            if not art:
                raise HTTPException(status_code=404, detail=f"未找到文章: {date_str}")
            cfg = get_settings_dict(conn)
            max_attempts = max(1, int(cfg.get("draft_retry_max_attempts", "5") or "5"))
            prev_attempts = 0
            if retry_task_id is not None:
                prev_row = conn.execute(
                    "SELECT attempts, max_attempts FROM draft_tasks WHERE id = ?",
                    (retry_task_id,),
                ).fetchone()
                if not prev_row:
                    raise HTTPException(status_code=404, detail="草稿任务不存在")
                prev_attempts = int(prev_row["attempts"] or 0)
                if prev_row["max_attempts"]:
                    max_attempts = max(max_attempts, int(prev_row["max_attempts"]))

        art_dir = get_data_dir() / "articles" / date_str
        html_path = art_dir / "article.html"
        cover_path = art_dir / "cover.png"
        html_content = html_path.read_text(encoding="utf-8") if html_path.exists() else ""

        appid = cfg.get("wechat_appid", "")
        appsecret = cfg.get("wechat_appsecret", "")
        mock_mode = cfg.get("wechat_mock_mode", "true").lower() == "true"

        result = await upload_wechat_draft(
            article_date=date_str,
            title=art["title"],
            author=art["author"] or "AI Media",
            digest=art["subtitle"] or art["title"],
            html_content=html_content,
            cover_path=cover_path,
            appid=appid,
            appsecret=appsecret,
            mock_mode=mock_mode,
            simulate_failure=simulate_failure,
        )

        ts = now_iso()
        ok = bool(result.get("ok"))
        new_attempts = prev_attempts + 1 if retry_task_id is not None else 1
        mode = result.get("mode", "mock")
        media_id = str(result.get("media_id") or "")
        thumb_media_id = str(result.get("thumb_media_id") or "")
        err_msg = "" if ok else str(result.get("error") or "未知上传错误")
        resp_json = json.dumps(result.get("response") or result, ensure_ascii=False)

        if ok:
            status = "success"
            next_retry_at = ""
            last_error_at = ""
        elif new_attempts < max_attempts:
            status = "queued"
            next_retry_at = _compute_next_retry_at(new_attempts)
            last_error_at = ts
        else:
            status = "failed"
            next_retry_at = ""
            last_error_at = ts

        with get_conn() as conn:
            if retry_task_id is not None:
                conn.execute(
                    """
                    UPDATE draft_tasks
                    SET mode = ?, status = ?, media_id = ?, thumb_media_id = ?,
                        attempts = ?, max_attempts = ?, next_retry_at = ?, last_error_at = ?,
                        error_message = ?, response_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        mode,
                        status,
                        media_id,
                        thumb_media_id,
                        new_attempts,
                        max_attempts,
                        next_retry_at,
                        last_error_at,
                        err_msg,
                        resp_json,
                        ts,
                        retry_task_id,
                    ),
                )
                task_id = retry_task_id
            else:
                cur = conn.execute(
                    """
                    INSERT INTO draft_tasks (
                        article_date, title, mode, status, media_id, thumb_media_id,
                        attempts, max_attempts, next_retry_at, last_error_at,
                        error_message, response_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        date_str,
                        art["title"],
                        mode,
                        status,
                        media_id,
                        thumb_media_id,
                        new_attempts,
                        max_attempts,
                        next_retry_at,
                        last_error_at,
                        err_msg,
                        resp_json,
                        ts,
                        ts,
                    ),
                )
                task_id = int(cur.lastrowid or 0)

            stage_name = "draft_retry" if trigger == "auto_retry" else "wechat_draft"
            if ok:
                conn.execute(
                    "UPDATE articles SET status = 'draft_uploaded', updated_at = ? WHERE date = ?",
                    (ts, date_str),
                )
                existing_pub = conn.execute(
                    "SELECT id FROM publish_records WHERE article_date = ? ORDER BY id DESC LIMIT 1", (date_str,)
                ).fetchone()
                if existing_pub:
                    conn.execute(
                        """
                        UPDATE publish_records
                        SET draft_media_id = ?, status = 'draft_ready', updated_at = ?
                        WHERE id = ?
                        """,
                        (media_id, ts, existing_pub["id"]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO publish_records (
                            article_date, title, channel, publish_mode, draft_media_id,
                            status, published_url, notes, created_at, updated_at
                        ) VALUES (?, ?, 'wechat_official_account', 'draft_only', ?, 'draft_ready', '', ?, ?, ?)
                        """,
                        (
                            date_str,
                            art["title"],
                            media_id,
                            f"{'模拟' if mode == 'mock' else '真实'}草稿已上传，待人工在公众号后台审核发布",
                            ts,
                            ts,
                        ),
                    )
                add_task_log(
                    conn,
                    date_str=date_str,
                    stage=stage_name,
                    status="completed",
                    message=result.get("message") or f"草稿上传成功 ({mode}): {media_id}",
                    details={
                        "task_id": task_id,
                        "mode": mode,
                        "media_id": media_id,
                        "publish_mode": PUBLISH_MODE,
                        "trigger": trigger,
                        "attempts": new_attempts,
                    },
                )
            else:
                conn.execute(
                    "UPDATE articles SET status = 'draft_failed', updated_at = ? WHERE date = ?",
                    (ts, date_str),
                )
                retry_hint = (
                    f"已进入自动重试队列（第 {new_attempts}/{max_attempts} 次，下次重试 {next_retry_at}）"
                    if status == "queued"
                    else f"已达自动重试上限 ({new_attempts}/{max_attempts})，请排查后手动重试"
                )
                add_task_log(
                    conn,
                    date_str=date_str,
                    stage=stage_name,
                    status="warning" if status == "queued" else "failed",
                    level="WARN" if status == "queued" else "ERROR",
                    message=f"草稿上传失败 ({mode}): {err_msg}；{retry_hint}",
                    details={
                        "task_id": task_id,
                        "mode": mode,
                        "error": err_msg,
                        "attempts": new_attempts,
                        "max_attempts": max_attempts,
                        "next_retry_at": next_retry_at,
                        "trigger": trigger,
                    },
                )

            task_row = conn.execute("SELECT * FROM draft_tasks WHERE id = ?", (task_id,)).fetchone()

        return {
            "ok": ok,
            "publish_mode": PUBLISH_MODE,
            "mode": mode,
            "status": status,
            "next_retry_at": next_retry_at,
            "message": (
                result.get("message")
                if ok
                else f"{err_msg}" + (f"（已排队，下次自动重试: {next_retry_at}）" if next_retry_at else "")
            ),
            "task": dict(task_row) if task_row else None,
        }


@app.get("/api/wechat/drafts")
async def list_draft_tasks() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM draft_tasks ORDER BY id DESC LIMIT 100").fetchall()
        cfg = get_settings_dict(conn)
    items = [dict(r) for r in rows]
    queued_count = sum(1 for r in items if r.get("status") == "queued")
    return {
        "items": items,
        "count": len(items),
        "queued_count": queued_count,
        "retry_loop_enabled": cfg.get("draft_retry_enabled", "true").lower() == "true",
        "publish_mode": PUBLISH_MODE,
    }


@app.post("/api/wechat/draft/{date}")
async def trigger_draft_upload(date: str, body: DraftUploadRequest | None = None) -> dict[str, Any]:
    sim_fail = bool(body.simulate_failure) if body else False
    return await _execute_draft_upload(date, retry_task_id=None, simulate_failure=sim_fail, trigger="manual")


@app.post("/api/wechat/draft/retry/{task_id}")
async def retry_draft_upload(task_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM draft_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="草稿任务不存在")
    return await _execute_draft_upload(
        row["article_date"], retry_task_id=task_id, simulate_failure=False, trigger="manual_retry"
    )


@app.post("/api/wechat/drafts/process-queue")
async def process_draft_retry_queue() -> dict[str, Any]:
    result = await _process_due_draft_retries(ignore_schedule=True)
    return {"ok": True, "publish_mode": PUBLISH_MODE, **result}


# ---------------------------- Module 7: Publish Records ----------------------------


@app.get("/api/publish-records")
async def list_publish_records() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM publish_records ORDER BY id DESC LIMIT 100").fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@app.patch("/api/publish-records/{record_id}")
async def update_publish_record(record_id: int, payload: PublishRecordUpdate) -> dict[str, Any]:
    ts = now_iso()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM publish_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="发布记录不存在")
        conn.execute(
            """
            UPDATE publish_records
            SET status = ?, published_url = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (payload.status, payload.published_url.strip(), payload.notes.strip(), ts, record_id),
        )
        if payload.status == "manual_published":
            conn.execute(
                "UPDATE articles SET status = 'published_manual', updated_at = ? WHERE date = ?",
                (ts, row["article_date"]),
            )
            add_task_log(
                conn,
                date_str=row["article_date"],
                stage="publish_record",
                status="completed",
                message=f"已人工确认发布《{row['title']}》",
                details={"published_url": payload.published_url.strip()},
            )
        updated = conn.execute("SELECT * FROM publish_records WHERE id = ?", (record_id,)).fetchone()
    return {"ok": True, "item": dict(updated)}


# ---------------------------- Module 8: Operational Metrics ----------------------------


@app.get("/api/metrics")
async def list_metrics() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.*, a.title AS article_title, a.category AS article_category
            FROM metrics m
            LEFT JOIN articles a ON a.date = m.article_date
            ORDER BY m.recorded_date DESC, m.id DESC
            LIMIT 200
            """
        ).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@app.post("/api/metrics")
async def record_metrics(payload: MetricsCreate) -> dict[str, Any]:
    article_date = _validate_date(payload.article_date)
    recorded_date = _validate_date(payload.recorded_date) if payload.recorded_date else today_str()
    ts = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO metrics (
                article_date, recorded_date, reads, likes, shares, comments, collections,
                notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_date,
                recorded_date,
                payload.reads,
                payload.likes,
                payload.shares,
                payload.comments,
                payload.collections,
                payload.notes.strip(),
                ts,
                ts,
            ),
        )
        add_task_log(
            conn,
            date_str=article_date,
            stage="metrics",
            status="completed",
            message=f"录入运营数据 ({article_date}): 阅读 {payload.reads} / 点赞 {payload.likes} / 转发 {payload.shares}",
        )
        row = conn.execute("SELECT * FROM metrics WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "item": dict(row)}


# ---------------------------- Module 9: Task Logs ----------------------------


@app.get("/api/logs")
async def list_logs(
    stage: str | None = None,
    status: str | None = None,
    date: str | None = None,
    limit: int = Query(default=150, ge=1, le=500),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if stage:
        clauses.append("stage = ?")
        params.append(stage)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if date:
        clauses.append("date = ?")
        params.append(date)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM task_logs {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


# ---------------------------- Module 10: System Config, Settings & Backups ----------------------------


def _build_public_config() -> dict[str, Any]:
    with get_conn() as conn:
        cfg = get_settings_dict(conn)
        backups_count = conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()["c"]
        queued_drafts = conn.execute(
            "SELECT COUNT(*) AS c FROM draft_tasks WHERE status = 'queued'"
        ).fetchone()["c"]

    raw_secret = cfg.get("wechat_appsecret", "").strip()
    has_secret = bool(raw_secret)
    masked_secret = _mask_secret(raw_secret)
    appid = cfg.get("wechat_appid", "")
    return {
        "wechat_appid": appid,
        "app_id": appid,
        "has_wechat_appsecret": has_secret,
        "has_app_secret": has_secret,
        "wechat_appsecret_masked": masked_secret,
        "app_secret": masked_secret,
        "wechat_mock_mode": cfg.get("wechat_mock_mode", "true").lower() == "true",
        "wechat_publish_mode": PUBLISH_MODE,
        "auto_backup_enabled": cfg.get("auto_backup_enabled", "true").lower() == "true",
        "backup_retain_count": int(cfg.get("backup_retain_count", "7") or "7"),
        "data_retain_days": int(cfg.get("data_retain_days", "7") or "7"),
        "backups_count": backups_count,
        "draft_retry_enabled": cfg.get("draft_retry_enabled", "true").lower() == "true",
        "draft_retry_max_attempts": int(cfg.get("draft_retry_max_attempts", "5") or "5"),
        "queued_drafts": queued_drafts,
        "site_name": cfg.get("site_name", "AI Media 自媒体管理后台"),
        "data_dir": str(get_data_dir()),
        "sqlite": get_db_health(),
    }


def _apply_config_update(payload: SettingsUpdate) -> dict[str, Any]:
    ts = now_iso()
    with get_conn() as conn:
        current = get_settings_dict(conn)
        current_secret = current.get("wechat_appsecret", "").strip()
        updates: dict[str, str] = {"wechat_publish_mode": PUBLISH_MODE}
        creds_changed = False

        submitted_appid = payload.wechat_appid if payload.wechat_appid is not None else payload.app_id
        if submitted_appid is not None:
            new_appid = submitted_appid.strip()
            if new_appid != current.get("wechat_appid", ""):
                creds_changed = True
            updates["wechat_appid"] = new_appid

        submitted_secret = payload.wechat_appsecret if payload.wechat_appsecret is not None else payload.app_secret
        if submitted_secret is not None and not _is_masked_secret(submitted_secret, current_secret):
            updates["wechat_appsecret"] = submitted_secret.strip()
            creds_changed = True

        if payload.wechat_mock_mode is not None:
            updates["wechat_mock_mode"] = "true" if payload.wechat_mock_mode else "false"
        if payload.auto_backup_enabled is not None:
            updates["auto_backup_enabled"] = "true" if payload.auto_backup_enabled else "false"
        if payload.backup_retain_count is not None:
            updates["backup_retain_count"] = str(payload.backup_retain_count)
        if payload.draft_retry_enabled is not None:
            updates["draft_retry_enabled"] = "true" if payload.draft_retry_enabled else "false"
        if payload.draft_retry_max_attempts is not None:
            updates["draft_retry_max_attempts"] = str(payload.draft_retry_max_attempts)
        if payload.site_name is not None and payload.site_name.strip():
            updates["site_name"] = payload.site_name.strip()

        for k, v in updates.items():
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (k, v, ts),
            )

        if payload.backup_retain_count is not None:
            prune_backups(conn, payload.backup_retain_count)

        add_task_log(
            conn,
            date_str=today_str(),
            stage="settings",
            status="completed",
            message="已更新系统配置（微信公众号始终锁定仅草稿箱模式）",
        )

    if creds_changed:
        invalidate_access_token_cache()

    return _build_public_config()


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return _build_public_config()


@app.put("/api/config")
async def update_config(payload: SettingsUpdate) -> dict[str, Any]:
    return _apply_config_update(payload)


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return _build_public_config()


@app.put("/api/settings")
async def update_settings(payload: SettingsUpdate) -> dict[str, Any]:
    return _apply_config_update(payload)


@app.get("/api/backups")
async def list_backups() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM backups ORDER BY id DESC LIMIT 100").fetchall()
        cfg = get_settings_dict(conn)
    return {
        "items": [dict(r) for r in rows],
        "count": len(rows),
        "retain_count": int(cfg.get("backup_retain_count", "7") or "7"),
    }


@app.post("/api/backups/run")
async def run_backup() -> dict[str, Any]:
    return create_sqlite_backup(trigger="manual")


@app.post("/api/cleanup/run")
async def run_cleanup(retain_days: int | None = Query(default=None, ge=1, le=365)) -> dict[str, Any]:
    return cleanup_old_data(retain_days=retain_days, trigger="manual")
