"""Articles listing, preview, file serving, and deletion routes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..db import (
    add_task_log,
    delete_article_and_storage,
    ensure_data_dirs,
    format_bytes,
    get_conn,
    get_data_dir,
    today_str,
)
from ..html_utils import sanitize_wechat_html
from ..schemas import ArticleBatchDeleteRequest
from ..utils import DATE_RE, SAFE_FILENAME_RE, validate_date

router = APIRouter(prefix="/api/articles", tags=["articles"])


@router.get("")
async def list_articles(
    q: str | None = None,
    status: str | None = None,
    category: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if status and status.strip():
        clauses.append("status = ?")
        params.append(status.strip())
    if category and category.strip():
        clauses.append("category = ?")
        params.append(category.strip())
    if q and q.strip():
        clauses.append("(title LIKE ? OR subtitle LIKE ? OR date LIKE ? OR category LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) AS c FROM articles {where}", params).fetchone()["c"])
        rows = conn.execute(
            f"""
            SELECT date, title, subtitle, category, template, author, publish_mode,
                   review_passed, needs_manual_review, status, word_count, sources_count,
                   cover_path, ingested_at, updated_at
            FROM articles
            {where}
            ORDER BY date DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["cover_url"] = f"/api/articles/{d['date']}/files/cover.png" if d.get("cover_path") else ""
        items.append(d)
    return {"items": items, "count": len(items), "total": total}


def _batch_delete_articles_sync(payload: ArticleBatchDeleteRequest) -> dict[str, Any]:
    target_dates: list[str] = []
    seen: set[str] = set()
    if payload.all:
        with get_conn() as conn:
            rows = conn.execute("SELECT date FROM articles ORDER BY date DESC").fetchall()
        for r in rows:
            d = str(r["date"])
            if DATE_RE.match(d) and d not in seen:
                seen.add(d)
                target_dates.append(d)
        dirs = ensure_data_dirs()
        for bucket in (dirs["articles"], dirs["incoming"]):
            if bucket.exists():
                for child in sorted(bucket.iterdir(), reverse=True):
                    if DATE_RE.match(child.name) and child.name not in seen:
                        seen.add(child.name)
                        target_dates.append(child.name)
    else:
        for raw in payload.dates:
            d = validate_date(str(raw).strip())
            if d not in seen:
                seen.add(d)
                target_dates.append(d)

    deleted_dates: list[str] = []
    total_removed_files = 0
    total_freed_bytes = 0
    for d in target_dates:
        res = delete_article_and_storage(d, reason="article_batch_delete", record_log=False)
        if res["existed"]:
            deleted_dates.append(d)
        total_removed_files += res["removed_files"]
        total_freed_bytes += res["freed_bytes"]

    freed_human = format_bytes(total_freed_bytes)
    with get_conn() as conn:
        add_task_log(
            conn,
            date_str=today_str(),
            stage="cleanup",
            status="completed",
            message=(
                f"批量清理文章与缓存（{'全部清空' if payload.all else '按勾选'}）："
                f"删除 {len(deleted_dates)} 篇、本地文件 {total_removed_files} 个，释放 {freed_human}"
            ),
            details={
                "all": payload.all,
                "deleted_count": len(deleted_dates),
                "deleted_dates": deleted_dates,
                "removed_files": total_removed_files,
                "freed_bytes": total_freed_bytes,
                "freed_human": freed_human,
            },
        )

    return {
        "ok": True,
        "deleted_count": len(deleted_dates),
        "deleted_dates": deleted_dates,
        "removed_files": total_removed_files,
        "freed_bytes": total_freed_bytes,
        "freed_human": freed_human,
    }


@router.post("/batch-delete")
async def batch_delete_articles(payload: ArticleBatchDeleteRequest) -> dict[str, Any]:
    return await asyncio.to_thread(_batch_delete_articles_sync, payload)


@router.delete("/{date}")
async def delete_article(date: str) -> dict[str, Any]:
    date_str = validate_date(date)
    res = await asyncio.to_thread(delete_article_and_storage, date_str, reason="article_delete", record_log=True)
    if not res["existed"]:
        raise HTTPException(status_code=404, detail="文章不存在")
    return res


@router.get("/{date}")
async def get_article_detail(date: str) -> dict[str, Any]:
    date_str = validate_date(date)
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
    item["cover_url"] = f"/api/articles/{date_str}/files/cover.png" if item.get("cover_path") else ""
    item["draft_tasks"] = drafts
    return item


@router.get("/{date}/files/{filename}")
async def get_article_file(date: str, filename: str) -> FileResponse:
    date_str = validate_date(date)
    if not SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="非法文件名")
    target = (get_data_dir() / "articles" / date_str / filename).resolve()
    root = (get_data_dir() / "articles" / date_str).resolve()
    if root not in target.parents or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path=str(target))
