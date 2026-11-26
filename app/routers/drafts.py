"""WeChat Official Account draft-only upload & retry queue routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..db import get_conn, get_settings_dict
from ..schemas import DraftUploadRequest
from ..services.drafts import (
    PUBLISH_MODE,
    execute_draft_upload,
    process_due_draft_retries,
)

router = APIRouter(prefix="/api/wechat", tags=["drafts"])


@router.get("/drafts")
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


@router.post("/draft/retry/{task_id}")
async def retry_draft_upload(task_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM draft_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="草稿任务不存在")
    return await execute_draft_upload(
        row["article_date"], retry_task_id=task_id, simulate_failure=False, trigger="manual_retry"
    )


@router.post("/draft/{date}")
async def trigger_draft_upload(date: str, body: DraftUploadRequest | None = None) -> dict[str, Any]:
    sim_fail = bool(body.simulate_failure) if body else False
    return await execute_draft_upload(date, retry_task_id=None, simulate_failure=sim_fail, trigger="manual")


@router.post("/drafts/process-queue")
async def process_draft_retry_queue() -> dict[str, Any]:
    result = await process_due_draft_retries(ignore_schedule=True)
    return {"ok": True, "publish_mode": PUBLISH_MODE, **result}
