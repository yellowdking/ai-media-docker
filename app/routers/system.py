"""System configuration, backups, and storage cleanup routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query

from ..db import (
    add_task_log,
    cleanup_old_data,
    cleanup_pushed_article_media,
    create_sqlite_backup,
    format_bytes,
    get_conn,
    get_data_dir,
    get_data_disk_usage,
    get_db_health,
    get_settings_dict,
    now_iso,
    prune_backups,
    today_str,
)
from ..schemas import SettingsUpdate
from ..utils import is_masked_secret, mask_secret
from ..wechat import PUBLISH_MODE, invalidate_access_token_cache

router = APIRouter(prefix="/api", tags=["system"])


def _build_public_config() -> dict[str, Any]:
    with get_conn() as conn:
        cfg = get_settings_dict(conn)
        backups_count = conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()["c"]
        queued_drafts = conn.execute(
            "SELECT COUNT(*) AS c FROM draft_tasks WHERE status = 'queued'"
        ).fetchone()["c"]

    raw_secret = cfg.get("wechat_appsecret", "").strip()
    has_secret = bool(raw_secret)
    masked_secret = mask_secret(raw_secret)
    appid = cfg.get("wechat_appid", "").strip()
    disk_info = get_data_disk_usage()
    return {
        "wechat_appid": appid,
        "has_wechat_appsecret": has_secret,
        "wechat_appsecret_masked": masked_secret,
        "wechat_configured": bool(appid and has_secret),
        "wechat_mock_mode": cfg.get("wechat_mock_mode", "true").lower() == "true",
        "wechat_publish_mode": PUBLISH_MODE,
        "auto_backup_enabled": cfg.get("auto_backup_enabled", "true").lower() == "true",
        "backup_retain_count": int(cfg.get("backup_retain_count", "7") or "7"),
        "data_retain_days": int(cfg.get("data_retain_days", "7") or "7"),
        "backups_count": backups_count,
        "draft_retry_enabled": cfg.get("draft_retry_enabled", "true").lower() == "true",
        "draft_retry_max_attempts": int(cfg.get("draft_retry_max_attempts", "5") or "5"),
        "queued_drafts": queued_drafts,
        "site_name": cfg.get("site_name", "资讯后台管理系统"),
        "data_dir": str(get_data_dir()),
        "disk_usage_bytes": disk_info["disk_usage_bytes"],
        "disk_usage_human": disk_info["disk_usage_human"],
        "sqlite": get_db_health(),
    }


def _apply_config_update(payload: SettingsUpdate) -> dict[str, Any]:
    ts = now_iso()
    with get_conn() as conn:
        current = get_settings_dict(conn)
        current_secret = current.get("wechat_appsecret", "").strip()
        updates: dict[str, str] = {"wechat_publish_mode": PUBLISH_MODE}
        creds_changed = False

        if payload.wechat_appid is not None:
            new_appid = payload.wechat_appid.strip()
            if new_appid != current.get("wechat_appid", ""):
                creds_changed = True
            updates["wechat_appid"] = new_appid

        if payload.wechat_appsecret is not None and not is_masked_secret(payload.wechat_appsecret, current_secret):
            updates["wechat_appsecret"] = payload.wechat_appsecret.strip()
            creds_changed = True

        if payload.wechat_mock_mode is not None:
            updates["wechat_mock_mode"] = "true" if payload.wechat_mock_mode else "false"
        if payload.auto_backup_enabled is not None:
            updates["auto_backup_enabled"] = "true" if payload.auto_backup_enabled else "false"
        if payload.backup_retain_count is not None:
            updates["backup_retain_count"] = str(payload.backup_retain_count)
        if payload.data_retain_days is not None:
            updates["data_retain_days"] = str(payload.data_retain_days)
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


@router.get("/config")
async def get_config() -> dict[str, Any]:
    return await asyncio.to_thread(_build_public_config)


@router.put("/config")
async def update_config(payload: SettingsUpdate) -> dict[str, Any]:
    return await asyncio.to_thread(_apply_config_update, payload)


@router.get("/backups")
async def list_backups() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM backups ORDER BY id DESC LIMIT 100").fetchall()
        cfg = get_settings_dict(conn)
    return {
        "items": [dict(r) for r in rows],
        "count": len(rows),
        "retain_count": int(cfg.get("backup_retain_count", "7") or "7"),
    }


@router.post("/backups/run")
async def run_backup() -> dict[str, Any]:
    return await asyncio.to_thread(create_sqlite_backup, trigger="manual")


@router.post("/cleanup/run")
async def run_cleanup(retain_days: int | None = Query(default=None, ge=1, le=365)) -> dict[str, Any]:
    return await asyncio.to_thread(cleanup_old_data, retain_days=retain_days, trigger="manual")


def _cleanup_pushed_media_sync() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date FROM articles WHERE status IN ('draft_uploaded', 'published_manual') ORDER BY date DESC"
        ).fetchall()
    dates = [r["date"] for r in rows]
    cleaned_dates: list[str] = []
    total_removed_files = 0
    total_freed_bytes = 0
    for d in dates:
        res = cleanup_pushed_article_media(d)
        if res["removed_files"] > 0:
            cleaned_dates.append(d)
        total_removed_files += res["removed_files"]
        total_freed_bytes += res["freed_bytes"]

    freed_kb = round(total_freed_bytes / 1024, 1)
    with get_conn() as conn:
        add_task_log(
            conn,
            date_str=today_str(),
            stage="cleanup",
            status="completed",
            message=(
                f"手动清理已推文章图片与缓存：检查 {len(dates)} 篇，"
                f"删除 {total_removed_files} 个文件，释放 {freed_kb} KB"
            ),
            details={
                "articles_checked": len(dates),
                "cleaned_dates": cleaned_dates,
                "removed_files": total_removed_files,
                "freed_bytes": total_freed_bytes,
            },
        )
    return {
        "ok": True,
        "articles_checked": len(dates),
        "cleaned_dates": cleaned_dates,
        "removed_files": total_removed_files,
        "freed_bytes": total_freed_bytes,
        "freed_human": format_bytes(total_freed_bytes),
    }


@router.post("/cleanup/pushed-media")
async def run_cleanup_pushed_media() -> dict[str, Any]:
    return await asyncio.to_thread(_cleanup_pushed_media_sync)
