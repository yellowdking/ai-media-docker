"""WeChat draft upload queue, retry orchestration, and one-click news publish service."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from datetime import timedelta
from typing import Any

from ..db import (
    add_task_log,
    cleanup_pushed_article_media,
    ensure_data_dirs,
    get_conn,
    get_data_dir,
    get_settings_dict,
    invalidate_disk_usage_cache,
    now_iso,
    now_shanghai,
    today_str,
)
from ..translator import (
    build_translated_article_bundle,
    generate_cover_png,
)
from ..utils import HTTPException, validate_date

try:
    from ..wechat import PUBLISH_MODE, upload_wechat_draft
except ImportError:  # pragma: no cover
    PUBLISH_MODE = "draft_only"

    async def upload_wechat_draft(**_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "mode": "mock",
            "publish_mode": PUBLISH_MODE,
            "error": "httpx 未安装，无法执行微信草稿上传",
        }


logger = logging.getLogger("ai_media.drafts")

_DRAFT_UPLOAD_LOCK = asyncio.Lock()
_SLOT_ALLOCATION_LOCK = asyncio.Lock()
_ORIG_UPLOAD_WECHAT_DRAFT = upload_wechat_draft


def _resolve_upload_wechat_draft():
    """Resolve upload_wechat_draft while supporting mocks patched on either app.services.drafts or app.main."""
    if upload_wechat_draft is not _ORIG_UPLOAD_WECHAT_DRAFT:
        return upload_wechat_draft
    main_mod = sys.modules.get("app.main")
    if main_mod is not None:
        main_fn = getattr(main_mod, "upload_wechat_draft", None)
        if main_fn is not None and main_fn is not _ORIG_UPLOAD_WECHAT_DRAFT:
            return main_fn
    return upload_wechat_draft


def compute_next_retry_at(attempts: int) -> str:
    """Exponential backoff for queued WeChat draft uploads (2m, 4m, 8m, ..., capped at 60m)."""
    delay_seconds = min(3600, 120 * (2 ** max(0, attempts - 1)))
    return (now_shanghai() + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")


def allocate_daily_article_slot(base_date: str | None = None, *, reserve_dir: bool = False) -> str:
    """Allocate the next unused YYYY-MM-DD-NN slot (01..99) across SQLite articles and /data/articles/."""
    day = (base_date or today_str())[:10]
    dirs = ensure_data_dirs()
    articles_root = dirs["articles"]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date FROM articles WHERE date LIKE ?",
            (f"{day}-%",),
        ).fetchall()
    used_slots = {str(r["date"]) for r in rows}
    if articles_root.exists():
        for child in articles_root.iterdir():
            if child.name.startswith(f"{day}-"):
                used_slots.add(child.name)
    for idx in range(1, 100):
        candidate = f"{day}-{idx:02d}"
        if candidate in used_slots:
            continue
        candidate_dir = articles_root / candidate
        if candidate_dir.exists():
            continue
        if reserve_dir:
            try:
                candidate_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
        return candidate
    raise HTTPException(status_code=409, detail=f"当日文章槽位已满（{day} 最多 99 篇）")


async def execute_draft_upload(
    article_date: str,
    retry_task_id: int | None = None,
    simulate_failure: bool = False,
    trigger: str = "manual",
) -> dict[str, Any]:
    date_str = validate_date(article_date)
    async with _DRAFT_UPLOAD_LOCK:
        with get_conn() as conn:
            art = conn.execute("SELECT * FROM articles WHERE date = ?", (date_str,)).fetchone()
            if not art:
                raise HTTPException(status_code=404, detail=f"未找到文章: {date_str}")
            cfg = get_settings_dict(conn)
            max_attempts = max(1, int(cfg.get("draft_retry_max_attempts", "5") or "5"))
            prev_attempts = 0
            prev_thumb_media_id = ""
            if retry_task_id is not None:
                prev_row = conn.execute(
                    "SELECT attempts, max_attempts, thumb_media_id FROM draft_tasks WHERE id = ?",
                    (retry_task_id,),
                ).fetchone()
                if not prev_row:
                    raise HTTPException(status_code=404, detail="草稿任务不存在")
                prev_attempts = int(prev_row["attempts"] or 0)
                if prev_row["max_attempts"]:
                    max_attempts = max(max_attempts, int(prev_row["max_attempts"]))
                raw_thumb = str(prev_row["thumb_media_id"] or "").strip()
                if raw_thumb and not raw_thumb.startswith("mock_"):
                    prev_thumb_media_id = raw_thumb

        art_dir = get_data_dir() / "articles" / date_str
        html_path = art_dir / "article.html"
        cover_path = art_dir / "cover.png"
        html_content = html_path.read_text(encoding="utf-8") if html_path.exists() else ""

        # Re-pushing a media-cleaned article: regenerate cover.png cleanly instead of failing into retries
        cover_regenerated = False
        if not cover_path.exists() and (
            art["status"] in ("draft_uploaded", "published_manual")
            or not str(art["cover_path"] or "").strip()
        ):
            await asyncio.to_thread(generate_cover_png, cover_path, f"{date_str}:{art['title']}")
            cover_regenerated = True
            with get_conn() as conn:
                conn.execute(
                    "UPDATE articles SET cover_path = ?, updated_at = ? WHERE date = ?",
                    (f"articles/{date_str}/cover.png", now_iso(), date_str),
                )
            invalidate_disk_usage_cache()

        appid = cfg.get("wechat_appid", "")
        appsecret = cfg.get("wechat_appsecret", "")
        mock_mode = cfg.get("wechat_mock_mode", "true").lower() == "true"

        upload_fn = _resolve_upload_wechat_draft()
        upload_kwargs: dict[str, Any] = {
            "article_date": date_str,
            "title": art["title"],
            "author": art["author"] or "资讯后台管理系统",
            "digest": art["subtitle"] or art["title"],
            "html_content": html_content,
            "cover_path": cover_path,
            "appid": appid,
            "appsecret": appsecret,
            "mock_mode": mock_mode,
            "simulate_failure": simulate_failure,
        }
        if prev_thumb_media_id:
            upload_kwargs["existing_thumb_media_id"] = prev_thumb_media_id

        result = await upload_fn(**upload_kwargs)

        ts = now_iso()
        ok = bool(result.get("ok"))
        new_attempts = prev_attempts + 1 if retry_task_id is not None else 1
        mode = result.get("mode", "mock")
        media_id = str(result.get("media_id") or "")
        thumb_media_id = str(result.get("thumb_media_id") or prev_thumb_media_id or "")
        err_msg = "" if ok else str(result.get("error") or "未知上传错误")
        resp_json = json.dumps(result.get("response") or result, ensure_ascii=False)

        if ok:
            status = "success"
            next_retry_at = ""
            last_error_at = ""
        elif new_attempts < max_attempts:
            status = "queued"
            next_retry_at = compute_next_retry_at(new_attempts)
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
                    "SELECT id FROM publish_records WHERE article_date = ? ORDER BY id DESC LIMIT 1",
                    (date_str,),
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
                        "cover_regenerated": cover_regenerated,
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

        media_cleanup: dict[str, Any] | None = None
        if ok and mode == "real":
            try:
                media_cleanup = await asyncio.to_thread(cleanup_pushed_article_media, date_str)
                freed_kb = round(media_cleanup["freed_bytes"] / 1024, 1)
                with get_conn() as conn:
                    add_task_log(
                        conn,
                        date_str=date_str,
                        stage="cleanup",
                        status="completed",
                        message=(
                            f"真实草稿上传成功，已自动清理文章图片与缓存："
                            f"删除 {media_cleanup['removed_files']} 个文件，释放 {freed_kb} KB"
                        ),
                        details=media_cleanup,
                    )
            except Exception as exc:
                logger.warning("Auto-clean pushed media failed for %s: %s", date_str, exc)

        return {
            "ok": ok,
            "publish_mode": PUBLISH_MODE,
            "mode": mode,
            "status": status,
            "next_retry_at": next_retry_at,
            "cover_regenerated": cover_regenerated,
            "message": (
                result.get("message")
                if ok
                else f"{err_msg}" + (f"（已排队，下次自动重试: {next_retry_at}）" if next_retry_at else "")
            ),
            "task": dict(task_row) if task_row else None,
            "media_cleanup": media_cleanup,
        }


async def process_due_draft_retries(*, ignore_schedule: bool = False) -> dict[str, Any]:
    """Retry queued WeChat draft tasks that are due (safe to run while Windows is offline)."""
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
            res = await execute_draft_upload(
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


async def publish_news_to_draft(news_id: str) -> dict[str, Any]:
    """Translate a news item, atomically allocate an unused daily slot, persist article, and push draft."""
    with get_conn() as conn:
        news = conn.execute("SELECT * FROM news_items WHERE id = ?", (news_id,)).fetchone()
        if not news:
            raise HTTPException(status_code=404, detail="资讯不存在")
        news_dict = dict(news)
        cfg = get_settings_dict(conn)

    default_author = (cfg.get("site_name") or "资讯后台管理系统").strip() or "资讯后台管理系统"

    # Translate & fetch concurrently BEFORE acquiring the slot lock
    bundle = await build_translated_article_bundle(
        article_date=today_str(),
        title=str(news_dict.get("title") or ""),
        url=str(news_dict.get("url") or ""),
        source_name=str(news_dict.get("source_name") or ""),
        category=str(news_dict.get("category") or "AI新闻"),
        summary=str(news_dict.get("summary") or ""),
        published_at=str(news_dict.get("published_at") or ""),
        author=default_author,
    )

    dirs = ensure_data_dirs()
    # Atomically allocate & reserve slot directory under lock to prevent TOCTOU collisions
    async with _SLOT_ALLOCATION_LOCK:
        slot_date = allocate_daily_article_slot(reserve_dir=True)
        target_dir = dirs["articles"] / slot_date

    bundle["date"] = slot_date
    bundle["metadata"]["date"] = slot_date
    bundle["sources"]["date"] = slot_date
    bundle["review"]["date"] = slot_date

    zh_title = bundle["title"]
    orig_title = bundle["original_title"]
    translation_mode = bundle["translation_mode"]
    ts = now_iso()

    try:
        (target_dir / "article.md").write_text(bundle["article_md"], encoding="utf-8")
        (target_dir / "article.html").write_text(bundle["article_html"], encoding="utf-8")
        (target_dir / "sources.json").write_text(
            json.dumps(bundle["sources"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (target_dir / "review.json").write_text(
            json.dumps(bundle["review"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (target_dir / "metadata.json").write_text(
            json.dumps(bundle["metadata"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cover_file = target_dir / "cover.png"
        await asyncio.to_thread(generate_cover_png, cover_file, f"{slot_date}:{news_id}:{zh_title}")

        with get_conn() as conn:
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
                    status='ready',
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
                    slot_date,
                    zh_title,
                    bundle["subtitle"],
                    bundle["category"],
                    "news_translated",
                    bundle["author"],
                    PUBLISH_MODE,
                    1,
                    0,
                    "ready",
                    bundle["word_count"],
                    bundle["sources_count"],
                    f"articles/{slot_date}/cover.png",
                    f"articles/{slot_date}/article.html",
                    f"articles/{slot_date}/article.md",
                    json.dumps(bundle["metadata"], ensure_ascii=False),
                    json.dumps(bundle["sources"], ensure_ascii=False),
                    json.dumps(bundle["review"], ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
            conn.execute("UPDATE news_items SET status = 'selected' WHERE id = ?", (news_id,))
            add_task_log(
                conn,
                date_str=slot_date,
                stage="news_publish",
                status="completed",
                message=f"资讯一键翻译成稿：《{zh_title}》（模式: {translation_mode}，槽位: {slot_date}）",
                details={
                    "news_id": news_id,
                    "article_date": slot_date,
                    "title": zh_title,
                    "original_title": orig_title,
                    "translation_mode": translation_mode,
                    "publish_mode": PUBLISH_MODE,
                },
            )
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    invalidate_disk_usage_cache()
    draft_res = await execute_draft_upload(slot_date, trigger="news_publish")
    task_info = draft_res.get("task") or {}
    return {
        "ok": bool(draft_res.get("ok")),
        "news_id": news_id,
        "article_date": slot_date,
        "title": zh_title,
        "original_title": orig_title,
        "translation_mode": translation_mode,
        "publish_mode": PUBLISH_MODE,
        "mode": draft_res.get("mode", "mock"),
        "media_id": str(task_info.get("media_id") or ""),
        "message": draft_res.get("message") or f"已翻译并推送草稿：《{zh_title}》",
        "draft": draft_res,
        "media_cleanup": draft_res.get("media_cleanup"),
    }
