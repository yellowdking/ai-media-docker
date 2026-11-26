"""Task logs query route with search, stage/status/date filtering, and pagination."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..db import get_conn

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def list_logs(
    q: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    date: str | None = None,
    limit: int = Query(default=120, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if stage and stage.strip():
        clauses.append("stage = ?")
        params.append(stage.strip())
    if status and status.strip():
        clauses.append("status = ?")
        params.append(status.strip())
    if date and date.strip():
        clean_date = date.strip()
        if len(clean_date) == 10:
            clauses.append("(date = ? OR date LIKE ?)")
            params.extend([clean_date, f"{clean_date}-%"])
        else:
            clauses.append("date = ?")
            params.append(clean_date)
    if q and q.strip():
        clauses.append("(message LIKE ? OR stage LIKE ? OR date LIKE ? OR details_json LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) AS c FROM task_logs {where}", params).fetchone()["c"])
        rows = conn.execute(
            f"SELECT * FROM task_logs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = [dict(r) for r in rows]
    return {"items": items, "count": len(items), "total": total}
