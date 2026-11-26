"""News management and one-click translate/publish routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..db import get_conn
from ..schemas import NewsUpdate
from ..services.drafts import publish_news_to_draft

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("")
async def list_news(
    date: str | None = None,
    status: str | None = None,
    category: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if date:
        clauses.append("date = ?")
        params.append(date.strip())
    if status:
        clauses.append("status = ?")
        params.append(status.strip())
    if category:
        clauses.append("category = ?")
        params.append(category.strip())
    if q and q.strip():
        clauses.append("(title LIKE ? OR summary LIKE ? OR source_name LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) AS c FROM news_items {where}", params).fetchone()["c"])
        rows = conn.execute(
            f"SELECT * FROM news_items {where} ORDER BY date DESC, score DESC, published_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = [dict(r) for r in rows]
    return {"items": items, "count": len(items), "total": total}


@router.patch("/{news_id}")
async def update_news_status(news_id: str, payload: NewsUpdate) -> dict[str, Any]:
    with get_conn() as conn:
        cur = conn.execute("UPDATE news_items SET status = ? WHERE id = ?", (payload.status, news_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="资讯不存在")
        row = conn.execute("SELECT * FROM news_items WHERE id = ?", (news_id,)).fetchone()
    return {"ok": True, "item": dict(row)}


@router.delete("/{news_id}")
async def delete_news_item(news_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM news_items WHERE id = ?", (news_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="资讯不存在")
    return {"ok": True, "id": news_id}


@router.post("/{news_id}/publish")
async def publish_news(news_id: str) -> dict[str, Any]:
    return await publish_news_to_draft(news_id)
