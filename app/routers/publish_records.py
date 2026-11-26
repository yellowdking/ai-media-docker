"""Publish records listing, status updates, and cascade-deletion routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..db import (
    add_task_log,
    delete_article_and_storage,
    format_bytes,
    get_conn,
    now_iso,
    today_str,
)
from ..schemas import PublishRecordBatchDeleteRequest, PublishRecordUpdate
from ..utils import DATE_RE

router = APIRouter(prefix="/api/publish-records", tags=["publish-records"])


@router.get("")
async def list_publish_records(
    q: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if status and status.strip():
        clauses.append("status = ?")
        params.append(status.strip())
    if q and q.strip():
        clauses.append("(title LIKE ? OR article_date LIKE ? OR draft_media_id LIKE ? OR notes LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) AS c FROM publish_records {where}", params).fetchone()["c"])
        rows = conn.execute(
            f"SELECT * FROM publish_records {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = [dict(r) for r in rows]
    return {"items": items, "count": len(items), "total": total}


@router.patch("/{record_id}")
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


def _batch_delete_publish_records_sync(payload: PublishRecordBatchDeleteRequest) -> dict[str, Any]:
    with get_conn() as conn:
        if payload.all:
            rows = conn.execute(
                "SELECT id, article_date, title FROM publish_records ORDER BY id DESC"
            ).fetchall()
        else:
            clean_ids = sorted({int(i) for i in payload.ids if int(i) > 0})
            if not clean_ids:
                rows = []
            else:
                placeholders = ",".join("?" for _ in clean_ids)
                rows = conn.execute(
                    f"SELECT id, article_date, title FROM publish_records WHERE id IN ({placeholders})",
                    clean_ids,
                ).fetchall()

    matched_ids = [int(r["id"]) for r in rows]
    distinct_dates: list[str] = []
    seen_dates: set[str] = set()
    for r in rows:
        adate = str(r["article_date"] or "").strip()
        if adate and DATE_RE.match(adate) and adate not in seen_dates:
            seen_dates.add(adate)
            distinct_dates.append(adate)

    total_deleted_records = 0
    total_deleted_articles = 0
    total_removed_files = 0
    total_freed_bytes = 0

    for d in distinct_dates:
        res = delete_article_and_storage(d, reason="publish_record_batch_delete", record_log=False)
        total_deleted_records += res["deleted_publish_records"]
        total_deleted_articles += res["deleted_articles"]
        total_removed_files += res["removed_files"]
        total_freed_bytes += res["freed_bytes"]

    with get_conn() as conn:
        if matched_ids:
            placeholders = ",".join("?" for _ in matched_ids)
            extra = conn.execute(
                f"DELETE FROM publish_records WHERE id IN ({placeholders})",
                matched_ids,
            ).rowcount or 0
            total_deleted_records += extra

        freed_human = format_bytes(total_freed_bytes)
        add_task_log(
            conn,
            date_str=today_str(),
            stage="cleanup",
            status="completed",
            message=(
                f"批量清理发布记录（{'全部清空' if payload.all else '按勾选'}，联动删除文章与缓存）："
                f"发布记录 {total_deleted_records} 条、文章 {total_deleted_articles} 篇、"
                f"本地文件 {total_removed_files} 个，释放 {freed_human}"
            ),
            details={
                "all": payload.all,
                "deleted_records": total_deleted_records,
                "deleted_articles": total_deleted_articles,
                "removed_files": total_removed_files,
                "freed_bytes": total_freed_bytes,
                "freed_human": freed_human,
            },
        )

    return {
        "ok": True,
        "deleted_records": total_deleted_records,
        "deleted_articles": total_deleted_articles,
        "removed_files": total_removed_files,
        "freed_bytes": total_freed_bytes,
        "freed_human": freed_human,
    }


@router.post("/batch-delete")
async def batch_delete_publish_records(payload: PublishRecordBatchDeleteRequest) -> dict[str, Any]:
    return await asyncio.to_thread(_batch_delete_publish_records_sync, payload)


def _delete_publish_record_sync(record_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM publish_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="发布记录不存在")
        article_date = str(row["article_date"] or "").strip()
        title = str(row["title"] or "")

    deleted_articles = 0
    deleted_publish_records = 0
    removed_files = 0
    freed_bytes = 0

    if article_date and DATE_RE.match(article_date):
        res = delete_article_and_storage(
            article_date,
            reason="publish_record_delete",
            record_log=True,
        )
        deleted_articles += res["deleted_articles"]
        deleted_publish_records += res["deleted_publish_records"]
        removed_files += res["removed_files"]
        freed_bytes += res["freed_bytes"]

    with get_conn() as conn:
        extra = conn.execute("DELETE FROM publish_records WHERE id = ?", (record_id,)).rowcount or 0
        deleted_publish_records += extra
        if extra > 0 and not (article_date and DATE_RE.match(article_date)):
            add_task_log(
                conn,
                date_str=today_str(),
                stage="cleanup",
                status="completed",
                message=f"清理发布记录 #{record_id}（《{title}》）",
                details={"record_id": record_id, "article_date": article_date, "title": title},
            )

    return {
        "ok": True,
        "id": record_id,
        "article_date": article_date,
        "title": title,
        "deleted_articles": deleted_articles,
        "deleted_publish_records": deleted_publish_records,
        "removed_files": removed_files,
        "freed_bytes": freed_bytes,
        "freed_human": format_bytes(freed_bytes),
    }


@router.delete("/{record_id}")
async def delete_publish_record(record_id: int) -> dict[str, Any]:
    return await asyncio.to_thread(_delete_publish_record_sync, record_id)
