"""AI Media Debian FastAPI backend & Chinese Admin UI server (v1.3.0).

Hard constraints:
- All persistent data under DATA_DIR (/data -> /opt/ai-media/data).
- Zero paid AI APIs or model SDKs.
- WeChat Official Account integration is STRICTLY draft-only (never auto-publish).
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import (
    AUTH_EXEMPT_PATHS,
    AUTH_EXEMPT_PREFIXES,
    get_admin_credentials,
    is_localhost_request,
    is_request_authenticated,
)
from .db import (
    ensure_data_dirs,
    get_conn,
    get_data_dir,
    get_data_disk_usage,
    get_db_health,
    get_settings_dict,
    init_db,
    now_iso,
)
from .routers import (
    articles,
    auth,
    drafts,
    ingest,
    logs,
    news,
    publish_records,
    system,
)
from .services.background import daily_backup_loop, draft_retry_loop
from .utils import is_wechat_configured_from_cfg
from .wechat import PUBLISH_MODE, upload_wechat_draft

APP_VERSION = "1.3.0"
APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

# Lightweight ETag cache for templates/index.html keyed by (mtime_ns, size)
_INDEX_HTML_CACHE: tuple[int, int, str, str] | None = None


def _load_index_html_with_etag() -> tuple[str, str]:
    global _INDEX_HTML_CACHE
    index_path = TEMPLATES_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="Admin UI template missing")
    stat = index_path.stat()
    if (
        _INDEX_HTML_CACHE is not None
        and _INDEX_HTML_CACHE[0] == stat.st_mtime_ns
        and _INDEX_HTML_CACHE[1] == stat.st_size
    ):
        return _INDEX_HTML_CACHE[2], _INDEX_HTML_CACHE[3]
    content = index_path.read_text(encoding="utf-8")
    etag = f'"{hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]}"'
    _INDEX_HTML_CACHE = (stat.st_mtime_ns, stat.st_size, content, etag)
    return content, etag


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    backup_task = asyncio.create_task(daily_backup_loop())
    retry_task = asyncio.create_task(draft_retry_loop())
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
    title="资讯后台管理系统",
    version=APP_VERSION,
    lifespan=lifespan,
)


@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    _, admin_password = get_admin_credentials()
    if not admin_password:
        return await call_next(request)

    path = request.url.path
    if path in AUTH_EXEMPT_PATHS or path.startswith(AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    if is_localhost_request(request) and (
        path.startswith("/api/internal/")
        or path.startswith("/api/wechat/draft/")
    ):
        return await call_next(request)

    if is_request_authenticated(request):
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"detail": "未登录或登录已过期"},
    )


app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")

app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(news.router)
app.include_router(articles.router)
app.include_router(drafts.router)
app.include_router(publish_records.router)
app.include_router(logs.router)
app.include_router(system.router)


@app.get("/health")
async def health() -> dict[str, Any]:
    ensure_data_dirs()
    db_info = get_db_health()
    disk_info = get_data_disk_usage()
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
        "version": APP_VERSION,
        "publish_mode": PUBLISH_MODE,
        "wechat_configured": is_wechat_configured_from_cfg(cfg),
        "data_dir": str(get_data_dir()),
        "disk_usage_bytes": disk_info["disk_usage_bytes"],
        "disk_usage_human": disk_info["disk_usage_human"],
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
async def admin_ui(request: Request) -> Response:
    content, etag = _load_index_html_with_etag()
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if_none_match = (request.headers.get("if-none-match") or "").strip()
    if if_none_match and (if_none_match == etag or if_none_match == f"W/{etag}"):
        return Response(status_code=304, headers=headers)
    return HTMLResponse(content=content, headers=headers)


__all__ = ["app", "upload_wechat_draft"]
