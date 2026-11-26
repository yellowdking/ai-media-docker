"""Article ingestion service (manifest SHA-256 verification + deduped filtered/state processing)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from ..db import (
    add_task_log,
    ensure_data_dirs,
    get_conn,
    invalidate_disk_usage_cache,
    now_iso,
)
from ..utils import (
    HTTPException,
    OPTIONAL_INGEST_FILES,
    REQUIRED_ARTICLE_FILES,
    SAFE_FILENAME_RE,
    sha256_file,
    validate_date,
)

PUBLISH_MODE = "draft_only"


def parse_manifest_files(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Parse manifest.json files mapping flexibly."""
    raw_files = manifest.get("files")
    parsed: dict[str, dict[str, Any]] = {}
    if isinstance(raw_files, dict):
        for fname, info in raw_files.items():
            if isinstance(info, dict):
                parsed[str(fname)] = {
                    "sha256": str(info.get("sha256", "")).lower().strip(),
                    "size": info.get("size"),
                    "required": bool(info.get("required", fname in REQUIRED_ARTICLE_FILES)),
                }
            elif isinstance(info, str):
                parsed[str(fname)] = {
                    "sha256": info.lower().strip(),
                    "size": None,
                    "required": fname in REQUIRED_ARTICLE_FILES,
                }
    elif isinstance(raw_files, list):
        for item in raw_files:
            if isinstance(item, dict) and item.get("name"):
                fname = str(item["name"])
                parsed[fname] = {
                    "sha256": str(item.get("sha256", "")).lower().strip(),
                    "size": item.get("size"),
                    "required": bool(item.get("required", fname in REQUIRED_ARTICLE_FILES)),
                }
    return parsed


def ingest_article_from_incoming(date: str) -> dict[str, Any]:
    """Verify incoming manifest & SHA-256 checksums, copy files, and upsert DB records."""
    date_str = validate_date(date)
    dirs = ensure_data_dirs()
    incoming_dir = dirs["incoming"] / date_str
    if not incoming_dir.exists() or not incoming_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"未找到待入库目录: {incoming_dir}")

    manifest_path = incoming_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=400, detail="缺少 manifest.json，拒绝入库")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"manifest.json 解析失败: {exc}")

    manifest_files = parse_manifest_files(manifest)
    if not manifest_files:
        raise HTTPException(status_code=400, detail="manifest.json 未包含有效文件清单")

    # 1. Verify all 6 required files are present in manifest and on disk, and verify SHA-256
    missing_required = [f for f in REQUIRED_ARTICLE_FILES if f not in manifest_files]
    if missing_required:
        raise HTTPException(
            status_code=400,
            detail=f"manifest.json 缺少必需文件: {', '.join(missing_required)}",
        )

    verified_files: list[str] = []
    file_hashes: dict[str, tuple[str, int]] = {}
    for fname, meta in manifest_files.items():
        if not SAFE_FILENAME_RE.match(fname):
            raise HTTPException(status_code=400, detail=f"非法文件名: {fname}")
        fpath = incoming_dir / fname
        if not fpath.exists():
            if meta.get("required") or fname in REQUIRED_ARTICLE_FILES:
                raise HTTPException(status_code=400, detail=f"缺少必需文件: {fname}")
            continue
        actual_sha = sha256_file(fpath)
        actual_size = fpath.stat().st_size
        expected_sha = meta.get("sha256", "")
        if not expected_sha or actual_sha != expected_sha:
            raise HTTPException(
                status_code=400,
                detail=f"文件 SHA-256 校验失败: {fname} (expected={expected_sha}, actual={actual_sha})",
            )
        if meta.get("size") is not None and int(meta["size"]) != actual_size:
            raise HTTPException(
                status_code=400,
                detail=f"文件大小校验失败: {fname} (expected={meta['size']}, actual={actual_size})",
            )
        verified_files.append(fname)
        file_hashes[fname] = (actual_sha, actual_size)

    # 2. Copy verified files from /data/incoming/{date}/ to /data/articles/{date}/
    target_dir = dirs["articles"] / date_str
    target_dir.mkdir(parents=True, exist_ok=True)
    files_to_copy = set(verified_files) | {"manifest.json"}
    for opt in OPTIONAL_INGEST_FILES:
        if (incoming_dir / opt).exists():
            files_to_copy.add(opt)

    for fname in sorted(files_to_copy):
        src = incoming_dir / fname
        if src.exists() and src.is_file():
            shutil.copy2(src, target_dir / fname)

    # 3. Parse article metadata, sources, review, markdown
    try:
        metadata = json.loads((target_dir / "metadata.json").read_text(encoding="utf-8"))
        sources_data = json.loads((target_dir / "sources.json").read_text(encoding="utf-8"))
        review_data = json.loads((target_dir / "review.json").read_text(encoding="utf-8"))
        md_text = (target_dir / "article.md").read_text(encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"文章 JSON/Markdown 解析失败: {exc}")

    title = str(metadata.get("title") or f"{date_str} 资讯文章").strip()
    subtitle = str(metadata.get("subtitle") or "").strip()
    category = str(metadata.get("category") or "AI新闻").strip()
    template = str(metadata.get("template") or "news").strip()
    author = str(metadata.get("author") or "资讯后台管理系统").strip()
    review_passed = 1 if review_data.get("passed") else 0
    needs_manual = 1 if review_data.get("needs_manual_review") else 0
    sources_list = sources_data.get("sources", []) if isinstance(sources_data, dict) else []
    word_count = len(re.sub(r"\s+", "", md_text))

    ts = now_iso()
    base_date = date_str[:10]
    news_ingested = 0
    filtered_skipped = False

    with get_conn() as conn:
        existing = conn.execute("SELECT status FROM articles WHERE date = ?", (date_str,)).fetchone()
        article_status = existing["status"] if existing else "ready"

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
                date_str,
                title,
                subtitle,
                category,
                template,
                author,
                PUBLISH_MODE,
                review_passed,
                needs_manual,
                article_status,
                word_count,
                len(sources_list),
                f"articles/{date_str}/cover.png",
                f"articles/{date_str}/article.html",
                f"articles/{date_str}/article.md",
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(sources_data, ensure_ascii=False),
                json.dumps(review_data, ensure_ascii=False),
                ts,
                ts,
            ),
        )

        # Optional filtered.json -> news_items (deduped across slots 01..05 by SHA-256 per base_date)
        filtered_path = target_dir / "filtered.json"
        if filtered_path.exists():
            try:
                filtered_sha = file_hashes["filtered.json"][0] if "filtered.json" in file_hashes else sha256_file(filtered_path)
                hash_key = f"filtered_hash:{base_date}"
                prev_hash_row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (hash_key,)
                ).fetchone()
                if prev_hash_row and prev_hash_row["value"] == filtered_sha:
                    filtered_skipped = True
                else:
                    filtered_data = json.loads(filtered_path.read_text(encoding="utf-8"))
                    candidates = filtered_data.get("candidates", []) if isinstance(filtered_data, dict) else []
                    for idx, item in enumerate(candidates):
                        if not isinstance(item, dict):
                            continue
                        item_id = str(
                            item.get("id")
                            or hashlib.sha256(str(item.get("url", idx)).encode("utf-8")).hexdigest()[:16]
                        )
                        conn.execute(
                            """
                            INSERT INTO news_items (
                                id, date, title, url, source_id, source_name, source_type,
                                category, priority, score, published_at, summary, status, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
                            ON CONFLICT(id) DO UPDATE SET
                                title=excluded.title,
                                score=excluded.score,
                                summary=excluded.summary
                            """,
                            (
                                item_id,
                                base_date,
                                str(item.get("title") or ""),
                                str(item.get("url") or ""),
                                str(item.get("source_id") or ""),
                                str(item.get("source_name") or ""),
                                str(item.get("source_type") or "rss"),
                                str(item.get("category") or "AI新闻"),
                                int(item.get("priority") or 5),
                                float(item.get("score") or 0.0),
                                str(item.get("published_at") or ""),
                                str(item.get("summary") or ""),
                                ts,
                            ),
                        )
                        news_ingested += 1
                    conn.execute(
                        """
                        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (hash_key, filtered_sha, ts),
                    )
            except Exception as exc:
                add_task_log(conn, date_str, "ingest", "warning", f"filtered.json 解析跳过: {exc}", level="WARN")

        # Optional state.json -> task_logs (deduped per (base_date, stage, status) across slots)
        state_path = target_dir / "state.json"
        if state_path.exists():
            try:
                state_data = json.loads(state_path.read_text(encoding="utf-8"))
                stages = state_data.get("stages", {}) if isinstance(state_data, dict) else {}
                for stage_name, stage_info in stages.items():
                    if isinstance(stage_info, dict) and stage_info.get("status") not in (None, "pending"):
                        stage_key = f"win_{stage_name}"
                        stage_status = str(stage_info.get("status"))
                        dup = conn.execute(
                            """
                            SELECT 1 FROM task_logs
                            WHERE (date = ? OR date LIKE ?) AND stage = ? AND status = ?
                            LIMIT 1
                            """,
                            (base_date, f"{base_date}-%", stage_key, stage_status),
                        ).fetchone()
                        if dup:
                            continue
                        msg = stage_info.get("message") or f"Windows 阶段 {stage_name}: {stage_status}"
                        add_task_log(
                            conn,
                            date_str=base_date,
                            stage=stage_key,
                            status=stage_status,
                            message=str(msg),
                            details=stage_info.get("outputs") or {},
                        )
            except Exception as exc:
                add_task_log(conn, date_str, "ingest", "warning", f"state.json 解析跳过: {exc}", level="WARN")

        add_task_log(
            conn,
            date_str=date_str,
            stage="ingest",
            status="completed",
            message=(
                f"文章《{title}》入库完成，校验 {len(verified_files)} 个文件"
                + ("（资讯列表无变化已跳过重复写入）" if filtered_skipped else f"，入库资讯 {news_ingested} 条")
            ),
            details={
                "verified_files": verified_files,
                "news_ingested": news_ingested,
                "filtered_skipped": filtered_skipped,
            },
        )

    invalidate_disk_usage_cache()
    return {
        "ok": True,
        "date": date_str,
        "title": title,
        "category": category,
        "publish_mode": PUBLISH_MODE,
        "review_passed": bool(review_passed),
        "files_verified": len(verified_files),
        "verified_files": verified_files,
        "news_ingested": news_ingested,
        "filtered_skipped": filtered_skipped,
        "ingested_at": ts,
    }
