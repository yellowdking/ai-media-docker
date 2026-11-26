"""Internal localhost-only ingest route."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from ..auth import require_localhost
from ..services.ingest import ingest_article_from_incoming

router = APIRouter(prefix="/api/internal", tags=["ingest"])


@router.post("/ingest/{date}")
async def ingest_article(date: str, request: Request) -> dict[str, Any]:
    require_localhost(request)
    return await asyncio.to_thread(ingest_article_from_incoming, date)
