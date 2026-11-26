"""Background maintenance loops (daily backup/cleanup + draft retry)."""

from __future__ import annotations

import asyncio
import logging

from ..db import (
    cleanup_old_data,
    create_sqlite_backup,
    get_conn,
    get_settings_dict,
    has_scheduled_backup_today,
)
from .drafts import process_due_draft_retries

logger = logging.getLogger("ai_media.background")


async def daily_backup_loop() -> None:
    """Background loop for daily SQLite backup and expired disk cleanup (incoming/ + articles/)."""
    await asyncio.sleep(15)
    while True:
        try:
            with get_conn() as conn:
                cfg = get_settings_dict(conn)
            await asyncio.to_thread(cleanup_old_data, trigger="scheduled")
            if cfg.get("auto_backup_enabled", "true").lower() == "true":
                already_done = await asyncio.to_thread(has_scheduled_backup_today)
                if not already_done:
                    await asyncio.to_thread(create_sqlite_backup, trigger="scheduled")
        except Exception as exc:
            logger.warning("Scheduled backup/cleanup loop error: %s", exc)
        await asyncio.sleep(3600)


async def draft_retry_loop() -> None:
    """Lightweight background retry loop for queued WeChat draft uploads."""
    await asyncio.sleep(20)
    while True:
        try:
            await process_due_draft_retries(ignore_schedule=False)
        except Exception as exc:
            logger.warning("Draft retry background loop error: %s", exc)
        await asyncio.sleep(60)
