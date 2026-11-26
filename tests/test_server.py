"""Unit tests for AI Media Docker server (v1.3.0: modular backend, security, dedup, pagination)."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import importlib
import importlib.util
import json
import logging
import logging.config
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app import auth, db, html_utils, translator, uvicorn_log
from app.services import drafts as drafts_service
from app.services import ingest as ingest_service
from app.utils import HTTPException as AppHTTPException

try:
    import httpx
    from app import wechat

    HAS_WECHAT = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    wechat = None  # type: ignore[assignment]
    HAS_WECHAT = False

try:
    from fastapi.testclient import TestClient
    from app import main as server_main

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _create_incoming_slot(
    incoming_root: Path,
    slot_key: str,
    *,
    title: str = "测试入库文章",
    filtered_payload: dict | None = None,
    state_payload: dict | None = None,
    corrupt_sha_file: str | None = None,
) -> Path:
    slot_dir = incoming_root / slot_key
    slot_dir.mkdir(parents=True, exist_ok=True)

    files_data: dict[str, bytes] = {
        "article.md": f"# {title}\n\n正文内容测试段落。\n".encode("utf-8"),
        "article.html": f"<section><p>{title}</p></section>".encode("utf-8"),
        "cover.png": b"\x89PNG\r\n\x1a\n" + b"0" * 512,
        "sources.json": json.dumps({"date": slot_key, "sources": [{"title": title, "url": "https://example.com"}]}, ensure_ascii=False).encode("utf-8"),
        "review.json": json.dumps({"passed": True, "needs_manual_review": False}, ensure_ascii=False).encode("utf-8"),
        "metadata.json": json.dumps({"title": title, "category": "AI新闻", "author": "资讯后台管理系统"}, ensure_ascii=False).encode("utf-8"),
    }
    if filtered_payload is not None:
        files_data["filtered.json"] = json.dumps(filtered_payload, ensure_ascii=False).encode("utf-8")
    if state_payload is not None:
        files_data["state.json"] = json.dumps(state_payload, ensure_ascii=False).encode("utf-8")

    manifest_files: dict[str, dict] = {}
    for fname, raw in files_data.items():
        (slot_dir / fname).write_bytes(raw)
        sha = "0" * 64 if corrupt_sha_file == fname else _sha256_bytes(raw)
        manifest_files[fname] = {
            "sha256": sha,
            "size": len(raw),
            "required": fname not in ("filtered.json", "state.json"),
        }

    manifest = {"date": slot_key, "files": manifest_files}
    (slot_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return slot_dir


class ServerCoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ai_media_srv_test_"))
        self._old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = str(self.tmp)
        auth.reset_login_rate_limiter()
        db.init_db()

    def tearDown(self) -> None:
        auth.reset_login_rate_limiter()
        db.invalidate_disk_usage_cache()
        if self._old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_db_init_wal_defaults_and_indexes(self) -> None:
        health = db.get_db_health()
        self.assertEqual(health["journal_mode"], "wal")
        self.assertEqual(health["busy_timeout_ms"], 5000)
        with db.get_conn() as conn:
            cfg = db.get_settings_dict(conn)
            idx_names = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
            }
        self.assertEqual(cfg["wechat_publish_mode"], "draft_only")
        self.assertEqual(cfg["data_retain_days"], "7")
        self.assertEqual(cfg["backup_retain_count"], "7")
        self.assertEqual(cfg["site_name"], "资讯后台管理系统")
        self.assertIn("idx_news_category", idx_names)
        self.assertIn("idx_task_logs_stage_status", idx_names)
        self.assertIn("idx_draft_status_retry", idx_names)

    def test_legacy_site_name_migration_preserves_custom(self) -> None:
        ts = db.now_iso()
        for legacy in ("AI Media 自媒体管理后台", "AI Media · 公众号草稿中心", "AI Media", "咨询管理后台系统"):
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE settings SET value = ?, updated_at = ? WHERE key = 'site_name'",
                    (legacy, ts),
                )
            db.init_db()
            with db.get_conn() as conn:
                cfg = db.get_settings_dict(conn)
            self.assertEqual(cfg["site_name"], "资讯后台管理系统")

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE settings SET value = '我的专属资讯台', updated_at = ? WHERE key = 'site_name'",
                (ts,),
            )
        db.init_db()
        with db.get_conn() as conn:
            cfg = db.get_settings_dict(conn)
        self.assertEqual(cfg["site_name"], "我的专属资讯台")

    def test_html_utils_sanitize_xss_bypasses(self) -> None:
        dirty = (
            '<section><script>alert("xss")</script>'
            '<iframe src="https://evil.test">bad</iframe>'
            '<img/onerror=alert(1) src=x>'
            '<a href="&#106;avascript:alert(1)">EntityJS</a>'
            '<a href="&#x6A;ava&#x09;script:alert(2)">TabJS</a>'
            '<a href="https://example.com/ok">SafeLink</a>'
            '<img src="/api/articles/2026-11-26-01/files/cover.png" alt="封面">'
            '<p onclick="evil()" onmouseover="evil2()" style="color:#111;background:url(javascript:alert(1))">正文内容</p>'
            "</section>"
        )
        clean = html_utils.sanitize_wechat_html(dirty)
        lower = clean.lower()
        self.assertNotIn("<script", lower)
        self.assertNotIn("alert(", lower)
        self.assertNotIn("<iframe", lower)
        self.assertNotIn("onerror", lower)
        self.assertNotIn("onclick", lower)
        self.assertNotIn("onmouseover", lower)
        self.assertNotIn("javascript:", lower)
        self.assertNotIn('src="x"', lower)
        self.assertIn('href="https://example.com/ok"', clean)
        self.assertIn('src="/api/articles/2026-11-26-01/files/cover.png"', clean)
        self.assertIn("color:#111", clean)
        self.assertIn("正文内容", clean)

    def test_ssrf_guard_blocks_private_loopback_and_redirect_and_caps_2mb(self) -> None:
        blocked_urls = [
            "http://127.0.0.1:8000/health",
            "http://localhost/admin",
            "http://10.0.0.5/internal",
            "http://192.168.1.10/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]:8000/",
            "file:///etc/passwd",
            "ftp://example.com/file",
        ]
        for u in blocked_urls:
            self.assertFalse(asyncio.run(translator.is_safe_fetch_url(u)), f"Expected blocked: {u}")

        with patch.object(translator, "_resolve_host_ips", return_value=["172.16.0.8"]):
            self.assertFalse(asyncio.run(translator.is_safe_fetch_url("https://internal.example.test/page")))

        with patch.object(translator, "_resolve_host_ips", return_value=["93.184.216.34", "127.0.0.1"]):
            self.assertFalse(asyncio.run(translator.is_safe_fetch_url("https://dual.example.test/page")))

        with patch.object(translator, "_resolve_host_ips", return_value=["93.184.216.34"]):
            self.assertTrue(asyncio.run(translator.is_safe_fetch_url("https://public.example.test/article")))

        # Redirect to loopback must be blocked
        class _FakeStreamResp:
            def __init__(self, status_code: int, headers: dict[str, str], chunks: list[bytes]) -> None:
                self.status_code = status_code
                self.headers = headers
                self.encoding = "utf-8"
                self._chunks = chunks

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aiter_bytes(self):
                for ch in self._chunks:
                    yield ch

        class _RedirectClient:
            def __init__(self) -> None:
                self.requested: list[str] = []

            def stream(self, _method: str, url: str, **_kwargs):
                self.requested.append(url)
                return _FakeStreamResp(302, {"location": "http://127.0.0.1:8088/secret"}, [])

        rc = _RedirectClient()
        with patch.object(translator, "_resolve_host_ips", return_value=["93.184.216.34"]):
            paras = asyncio.run(translator.fetch_source_paragraphs("https://public.example.test/go", client=rc))
        self.assertEqual(paras, [])
        self.assertEqual(rc.requested, ["https://public.example.test/go"])

        # Streaming cap stops reading beyond 2 MiB
        class _LargeClient:
            def __init__(self) -> None:
                self.chunks_yielded = 0

            def stream(self, _method: str, _url: str, **_kwargs):
                outer = self

                class _LargeResp(_FakeStreamResp):
                    async def aiter_bytes(self):
                        yield b"<html><body><p>Valid introductory paragraph exceeding thirty characters easily.</p>"
                        outer.chunks_yielded += 1
                        big = b"x" * (512 * 1024)
                        for _ in range(10):
                            outer.chunks_yielded += 1
                            yield big

                return _LargeResp(200, {"content-type": "text/html"}, [])

        lc = _LargeClient()
        with patch.object(translator, "_resolve_host_ips", return_value=["93.184.216.34"]):
            paras = asyncio.run(translator.fetch_source_paragraphs("https://public.example.test/large", client=lc))
        self.assertLessEqual(lc.chunks_yielded, 6)
        self.assertTrue(any("Valid introductory paragraph" in p for p in paras))

    def test_mymemory_chunks_over_480_chars_without_truncation(self) -> None:
        long_english = "\n\n".join(
            [
                f"Section {i}: Engineers released an open-source distributed inference engine "
                f"supporting deterministic scheduling and quantized kernels across edge devices."
                for i in range(6)
            ]
        )
        self.assertGreater(len(long_english), 700)

        seen_queries: list[str] = []

        class _FakeResp:
            status_code = 200

            def __init__(self, text: str) -> None:
                self._text = text

            def json(self):
                return {"responseStatus": 200, "responseData": {"translatedText": f"译文:{self._text}"}}

        class _FakeMyMemoryClient:
            async def get(self, _url: str, *, params: dict, **_kwargs):
                q = str(params["q"])
                seen_queries.append(q)
                return _FakeResp(q)

        result = asyncio.run(translator._translate_chunk_mymemory(_FakeMyMemoryClient(), long_english))
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(seen_queries), 2)
        for q in seen_queries:
            self.assertLessEqual(len(q), translator.MYMEMORY_MAX_CHUNK_CHARS)
        self.assertIn("Section 0:", result or "")
        self.assertIn("Section 5:", result or "")

    def test_generate_cover_png_valid_1280x720(self) -> None:
        p1 = self.tmp / "covers" / "cover_a.png"
        p2 = self.tmp / "covers" / "cover_b.png"
        translator.generate_cover_png(p1, "2026-11-25-01:New open source model release")
        translator.generate_cover_png(p2, "2026-11-25-02:Different seed headline")

        data = p1.read_bytes()
        self.assertGreater(len(data), 5120)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertNotEqual(data, p2.read_bytes())

        pos = 8
        chunks: list[bytes] = []
        width = height = bit_depth = color_type = 0
        idat_parts: list[bytes] = []
        while pos < len(data):
            length = struct.unpack(">I", data[pos : pos + 4])[0]
            ctype = data[pos + 4 : pos + 8]
            cdata = data[pos + 8 : pos + 8 + length]
            crc_stored = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
            crc_calc = zlib.crc32(ctype + cdata) & 0xFFFFFFFF
            self.assertEqual(crc_stored, crc_calc)
            chunks.append(ctype)
            if ctype == b"IHDR":
                width, height, bit_depth, color_type, _, _, _ = struct.unpack(">IIBBBBB", cdata)
            elif ctype == b"IDAT":
                idat_parts.append(cdata)
            pos += 12 + length

        self.assertEqual(chunks[0], b"IHDR")
        self.assertIn(b"IDAT", chunks)
        self.assertEqual(chunks[-1], b"IEND")
        self.assertEqual((width, height, bit_depth, color_type), (1280, 720, 8, 2))
        raw = zlib.decompress(b"".join(idat_parts))
        self.assertEqual(len(raw), 720 * (1 + 1280 * 3))

    def test_translator_cjk_chunking_html_and_bundle(self) -> None:
        zh_text, zh_mode = asyncio.run(translator.translate_to_zh("这是一篇关于人工智能最新进展的中文资讯文章"))
        self.assertEqual(zh_mode, "already_zh")
        self.assertIn("人工智能", zh_text)

        long_text = "\n\n".join([f"Paragraph {i}: " + ("AI engineering insight. " * 25) for i in range(6)])
        chunks = translator.split_text_chunks(long_text, max_chars=1400)
        self.assertGreaterEqual(len(chunks), 2)
        for ch in chunks:
            self.assertLessEqual(len(ch), 1400)

        sample_html = """
        <html>
          <head><style>.x{color:red;}</style><script>console.log('ignored script text that is very long indeed');</script></head>
          <body>
            <header><p>Header navigation banner text that should definitely be skipped completely.</p></header>
            <nav><p>Menu item link navigation bar text that must not appear in output.</p></nav>
            <main>
              <h2>Major Breakthrough in Open Source Robotics Engineering Today</h2>
              <p>Researchers published a comprehensive open-source benchmark for autonomous mobile manipulation across warehouses.</p>
              <p>Short</p>
              <li>The evaluation suite covers twelve real-world tasks with reproducible hardware configurations and baselines.</li>
            </main>
            <aside><p>Sidebar promotional text that should be stripped out by the HTML parser.</p></aside>
            <footer><p>Copyright notice footer text that should never be extracted into article body.</p></footer>
            <noscript><p>Please enable JavaScript in your browser to view this interactive website.</p></noscript>
          </body>
        </html>
        """
        paras = translator.extract_paragraphs_from_html(sample_html)
        self.assertEqual(len(paras), 3)
        self.assertTrue(any("Major Breakthrough" in p for p in paras))
        self.assertFalse(any("Copyright" in p or "Sidebar" in p or "ignored script" in p for p in paras))

        async def _fake_translate(text: str) -> tuple[str, str]:
            if "Open Source Robotics" in text:
                return ("开源机器人工程迎来重大突破", "google_gtx")
            return ("这是一段自动翻译后的中文资讯正文内容，包含核心技术要点与行业影响分析。", "google_gtx")

        with (
            patch.object(translator, "translate_to_zh", side_effect=_fake_translate),
            patch.object(
                translator,
                "fetch_source_paragraphs",
                new=AsyncMock(return_value=["First detailed English paragraph about robotics breakthroughs."]),
            ),
        ):
            bundle = asyncio.run(
                translator.build_translated_article_bundle(
                    article_date="2026-11-25-01",
                    title="Open Source Robotics Breakthrough",
                    url="https://example.com/robotics",
                    source_name="Example Tech",
                    category="AI新闻",
                    summary="A concise English summary of the robotics release.",
                    published_at="2026-11-25",
                )
            )
        self.assertEqual(bundle["title"], "开源机器人工程迎来重大突破")
        self.assertEqual(bundle["translation_mode"], "google_gtx")
        self.assertIn("## 核心要点", bundle["article_md"])
        self.assertIn("## 正文编译", bundle["article_md"])
        self.assertIn("## 来源说明", bundle["article_md"])
        self.assertIn("核心要点", bundle["article_html"])
        self.assertTrue(bundle["review"]["passed"])
        self.assertFalse(bundle["review"]["needs_manual_review"])
        self.assertEqual(bundle["metadata"]["publish_mode"], "draft_only")

    def test_ingest_sha256_validation_and_5_slot_dedup(self) -> None:
        dirs = db.ensure_data_dirs()
        # SHA-256 mismatch must raise 400
        _create_incoming_slot(dirs["incoming"], "2026-11-26-09", corrupt_sha_file="article.md")
        with self.assertRaises(AppHTTPException) as ctx:
            ingest_service.ingest_article_from_incoming("2026-11-26-09")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("SHA-256", str(ctx.exception.detail))

        # 5 slots sharing identical filtered.json and state.json
        filtered = {
            "candidates": [
                {"id": "n-01", "title": "News 1", "url": "https://example.com/1", "score": 95.0, "category": "AI新闻"},
                {"id": "n-02", "title": "News 2", "url": "https://example.com/2", "score": 88.0, "category": "工程实践"},
            ]
        }
        state = {
            "stages": {
                "collect": {"status": "completed", "message": "采集完成"},
                "produce": {"status": "completed", "message": "生产完成"},
            }
        }
        for idx in range(1, 6):
            slot = f"2026-11-26-0{idx}"
            _create_incoming_slot(
                dirs["incoming"],
                slot,
                title=f"多槽位文章 {idx}",
                filtered_payload=filtered,
                state_payload=state,
            )
            res = ingest_service.ingest_article_from_incoming(slot)
            self.assertTrue(res["ok"])
            if idx == 1:
                self.assertFalse(res["filtered_skipped"])
                self.assertEqual(res["news_ingested"], 2)
            else:
                self.assertTrue(res["filtered_skipped"])
                self.assertEqual(res["news_ingested"], 0)

        with db.get_conn() as conn:
            collect_logs = conn.execute(
                "SELECT COUNT(*) AS c FROM task_logs WHERE stage = 'win_collect' AND status = 'completed'"
            ).fetchone()["c"]
            produce_logs = conn.execute(
                "SELECT COUNT(*) AS c FROM task_logs WHERE stage = 'win_produce' AND status = 'completed'"
            ).fetchone()["c"]
            topics_cnt = conn.execute("SELECT COUNT(*) AS c FROM topics").fetchone()["c"]
            images_cnt = conn.execute("SELECT COUNT(*) AS c FROM images").fetchone()["c"]
            news_cnt = conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()["c"]

        self.assertEqual(collect_logs, 1)
        self.assertEqual(produce_logs, 1)
        self.assertEqual(topics_cnt, 0)
        self.assertEqual(images_cnt, 0)
        self.assertEqual(news_cnt, 2)

    def test_toctou_slot_allocation_lock_concurrent_publish(self) -> None:
        ts = db.now_iso()
        today = db.today_str()
        with db.get_conn() as conn:
            for idx in range(1, 4):
                conn.execute(
                    """
                    INSERT INTO news_items (id, date, title, url, source_name, category, score, summary, status, created_at)
                    VALUES (?, ?, ?, ?, 'Tech', 'AI新闻', 90.0, 'Summary', 'candidate', ?)
                    """,
                    (f"conc-{idx}", today, f"Concurrent Headline {idx}", f"https://example.com/{idx}", ts),
                )

        async def _slow_bundle(**kwargs):
            await asyncio.sleep(0.03)
            title = kwargs["title"]
            return {
                "date": kwargs["article_date"],
                "title": f"中文：{title}",
                "original_title": title,
                "subtitle": "摘要",
                "category": "AI新闻",
                "author": "资讯后台管理系统",
                "translation_mode": "google_gtx",
                "article_md": f"# {title}\n",
                "article_html": f"<p>{title}</p>",
                "metadata": {"date": kwargs["article_date"], "title": title},
                "sources": {"date": kwargs["article_date"], "sources": []},
                "review": {"date": kwargs["article_date"], "passed": True},
                "word_count": 100,
                "sources_count": 1,
            }

        fake_draft = {
            "ok": True,
            "mode": "mock",
            "media_id": "mock_draft_conc",
            "thumb_media_id": "mock_thumb_conc",
            "message": "模拟草稿成功",
            "response": {"mock": True},
        }

        async def _run_concurrent():
            with (
                patch.object(drafts_service, "build_translated_article_bundle", side_effect=_slow_bundle),
                patch.object(drafts_service, "upload_wechat_draft", new=AsyncMock(return_value=fake_draft)),
            ):
                return await asyncio.gather(
                    drafts_service.publish_news_to_draft("conc-1"),
                    drafts_service.publish_news_to_draft("conc-2"),
                    drafts_service.publish_news_to_draft("conc-3"),
                )

        results = asyncio.run(_run_concurrent())
        allocated_slots = sorted(r["article_date"] for r in results)
        self.assertEqual(allocated_slots, [f"{today}-01", f"{today}-02", f"{today}-03"])
        with db.get_conn() as conn:
            art_cnt = conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
            topics_cnt = conn.execute("SELECT COUNT(*) AS c FROM topics").fetchone()["c"]
            images_cnt = conn.execute("SELECT COUNT(*) AS c FROM images").fetchone()["c"]
        self.assertEqual(art_cnt, 3)
        self.assertEqual(topics_cnt, 0)
        self.assertEqual(images_cnt, 0)

    def test_repush_media_cleaned_article_regenerates_cover(self) -> None:
        date_str = "2026-11-26-01"
        dirs = db.ensure_data_dirs()
        art_dir = dirs["articles"] / date_str
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 512)
        (art_dir / "article.html").write_text("<p>已清理封面的文章正文</p>", encoding="utf-8")
        ts = db.now_iso()
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at)
                VALUES (?, '清图后重推测试', ?, 'draft_uploaded', ?, ?)
                """,
                (date_str, f"articles/{date_str}/cover.png", ts, ts),
            )

        db.cleanup_pushed_article_media(date_str)
        self.assertFalse((art_dir / "cover.png").exists())

        seen_cover_exists = False

        async def _check_upload(**kwargs):
            nonlocal seen_cover_exists
            seen_cover_exists = Path(kwargs["cover_path"]).exists()
            return {
                "ok": True,
                "mode": "real",
                "media_id": "repush_draft_123",
                "thumb_media_id": "repush_thumb_456",
                "message": "重推成功",
                "response": {"media_id": "repush_draft_123"},
            }

        with patch.object(drafts_service, "upload_wechat_draft", side_effect=_check_upload):
            res = asyncio.run(drafts_service.execute_draft_upload(date_str, trigger="manual"))

        self.assertTrue(res["ok"])
        self.assertTrue(res["cover_regenerated"])
        self.assertTrue(seen_cover_exists)
        with db.get_conn() as conn:
            art = conn.execute("SELECT status FROM articles WHERE date = ?", (date_str,)).fetchone()
            queued = conn.execute("SELECT COUNT(*) AS c FROM draft_tasks WHERE status = 'queued'").fetchone()["c"]
        self.assertEqual(art["status"], "draft_uploaded")
        self.assertEqual(queued, 0)

    def test_login_rate_limiter_sliding_window(self) -> None:
        ip = "198.51.100.25"
        base_t = 1000.0
        for i in range(auth.LOGIN_RATE_LIMIT_MAX_ATTEMPTS):
            allowed, _ = auth.check_login_rate_limit(ip, now_ts=base_t + i)
            self.assertTrue(allowed)
            auth.record_login_failure(ip, now_ts=base_t + i)

        allowed, retry_after = auth.check_login_rate_limit(ip, now_ts=base_t + 20)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

        # Expires after window passes
        allowed_later, _ = auth.check_login_rate_limit(ip, now_ts=base_t + auth.LOGIN_RATE_LIMIT_WINDOW_SECONDS + 20)
        self.assertTrue(allowed_later)

    def test_uvicorn_health_access_log_filter(self) -> None:
        flt = uvicorn_log.HealthCheckAccessFilter()
        rec_health_200 = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1234", "GET", "/health", "1.1", 200), None
        )
        rec_health_500 = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1234", "GET", "/health", "1.1", 500), None
        )
        rec_api_200 = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1234", "GET", "/api/articles", "1.1", 200), None
        )
        self.assertFalse(flt.filter(rec_health_200))
        self.assertTrue(flt.filter(rec_health_500))
        self.assertTrue(flt.filter(rec_api_200))

        # Validate app/logging.json structure, factory path, and dictConfig loading
        log_cfg_path = SERVER_ROOT / "app" / "logging.json"
        self.assertTrue(log_cfg_path.is_file())
        log_cfg = json.loads(log_cfg_path.read_text(encoding="utf-8"))
        factory_path = log_cfg["filters"]["health_filter"]["()"]
        self.assertEqual(factory_path, "app.uvicorn_log.HealthCheckAccessFilter")
        mod_name, cls_name = factory_path.rsplit(".", 1)
        resolved_cls = getattr(importlib.import_module(mod_name), cls_name)
        self.assertIs(resolved_cls, uvicorn_log.HealthCheckAccessFilter)

        if importlib.util.find_spec("uvicorn") is not None:
            logging.config.dictConfig(copy.deepcopy(log_cfg))
        else:
            # Verify dictConfig resolves our filter factory cleanly even without uvicorn formatters
            stdlib_cfg = copy.deepcopy(log_cfg)
            stdlib_cfg["formatters"]["default"] = {"format": "%(levelname)s %(message)s"}
            stdlib_cfg["formatters"]["access"] = {"format": "%(levelname)s %(message)s"}
            logging.config.dictConfig(stdlib_cfg)

    def test_delete_article_and_storage_removes_dirs_and_linked_rows(self) -> None:
        date_str = "2026-11-25-03"
        dirs = db.ensure_data_dirs()
        art_dir = dirs["articles"] / date_str
        inc_dir = dirs["incoming"] / date_str
        art_dir.mkdir(parents=True, exist_ok=True)
        inc_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "cover.png").write_bytes(b"\x89PNG" + b"0" * 1024)
        (art_dir / "article.html").write_text("<p>正文</p>", encoding="utf-8")
        (inc_dir / "manifest.json").write_text("{}", encoding="utf-8")

        ts = db.now_iso()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at) VALUES (?, '待清理文章', ?, 'draft_uploaded', ?, ?)",
                (date_str, f"articles/{date_str}/cover.png", ts, ts),
            )
            conn.execute(
                "INSERT INTO images (date, filename, rel_path, size_bytes, sha256, kind, created_at) VALUES (?, 'cover.png', ?, 1028, 'abc', 'cover', ?)",
                (date_str, f"articles/{date_str}/cover.png", ts),
            )
            conn.execute(
                "INSERT INTO draft_tasks (article_date, title, mode, status, created_at, updated_at) VALUES (?, '待清理文章', 'real', 'success', ?, ?)",
                (date_str, ts, ts),
            )
            conn.execute(
                "INSERT INTO publish_records (article_date, title, status, created_at, updated_at) VALUES (?, '待清理文章', 'draft_ready', ?, ?)",
                (date_str, ts, ts),
            )
            conn.execute(
                "INSERT INTO metrics (article_date, recorded_date, reads, created_at, updated_at) VALUES (?, '2026-11-25', 100, ?, ?)",
                (date_str, ts, ts),
            )
            conn.execute(
                "INSERT INTO topics (date, title, status, created_at, updated_at) VALUES (?, '待清理文章', 'produced', ?, ?)",
                (date_str, ts, ts),
            )

        res = db.delete_article_and_storage(date_str, reason="unit_test")
        self.assertTrue(res["ok"])
        self.assertTrue(res["existed"])
        self.assertEqual(res["title"], "待清理文章")
        self.assertEqual(res["deleted_articles"], 1)
        self.assertEqual(res["deleted_publish_records"], 1)
        self.assertEqual(res["deleted_draft_tasks"], 1)
        self.assertEqual(res["deleted_images"], 1)
        self.assertEqual(res["removed_files"], 3)
        self.assertGreater(res["freed_bytes"], 1024)
        self.assertTrue(res["freed_human"])
        self.assertFalse(art_dir.exists())
        self.assertFalse(inc_dir.exists())

        with db.get_conn() as conn:
            for tbl, col in (
                ("articles", "date"),
                ("images", "date"),
                ("draft_tasks", "article_date"),
                ("publish_records", "article_date"),
                ("metrics", "article_date"),
                ("topics", "date"),
            ):
                cnt = conn.execute(f"SELECT COUNT(*) AS c FROM {tbl} WHERE {col} = ?", (date_str,)).fetchone()["c"]
                self.assertEqual(cnt, 0, f"Expected 0 rows in {tbl}")

        res2 = db.delete_article_and_storage(date_str, reason="unit_test")
        self.assertFalse(res2["existed"])

    def test_cleanup_old_data_removes_expired_incoming_and_articles(self) -> None:
        dirs = db.ensure_data_dirs()
        old_inc = dirs["incoming"] / "2020-01-01"
        old_art = dirs["articles"] / "2020-01-01-02"
        keep_inc = dirs["incoming"] / db.today_str()
        keep_art = dirs["articles"] / db.today_str()
        for p in (old_inc, old_art, keep_inc, keep_art):
            p.mkdir(parents=True, exist_ok=True)
            (p / "sample.txt").write_text("hello", encoding="utf-8")

        ts = db.now_iso()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO articles (date, title, status, ingested_at, updated_at) VALUES ('2020-01-01-02', 'Old', 'ready', ?, ?)",
                (ts, ts),
            )

        res = db.cleanup_old_data(retain_days=7, trigger="manual")
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed_incoming"], ["2020-01-01"])
        self.assertEqual(res["removed_articles"], ["2020-01-01-02"])
        self.assertEqual(res["db_deleted"].get("articles"), 1)
        self.assertFalse(old_inc.exists())
        self.assertFalse(old_art.exists())
        self.assertTrue(keep_inc.exists())
        self.assertTrue(keep_art.exists())

    def test_cleanup_pushed_article_media_removes_images_and_incoming(self) -> None:
        date_str = "2026-11-25"
        dirs = db.ensure_data_dirs()
        art_dir = dirs["articles"] / date_str
        inc_dir = dirs["incoming"] / date_str
        art_dir.mkdir(parents=True, exist_ok=True)
        inc_dir.mkdir(parents=True, exist_ok=True)

        (art_dir / "cover.png").write_bytes(b"\x89PNG" + b"0" * 1024)
        (art_dir / "chart.webp").write_bytes(b"RIFF" + b"1" * 512)
        (art_dir / "article.md").write_text("# Title\nBody", encoding="utf-8")
        (art_dir / "article.html").write_text("<p>Body</p>", encoding="utf-8")
        (inc_dir / "cover.png").write_bytes(b"\x89PNG" + b"2" * 2048)
        (inc_dir / "manifest.json").write_text("{}", encoding="utf-8")

        ts = db.now_iso()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at) VALUES (?, 'Test Article', ?, 'draft_uploaded', ?, ?)",
                (date_str, f"articles/{date_str}/cover.png", ts, ts),
            )
            conn.execute(
                "INSERT INTO images (date, filename, rel_path, size_bytes, sha256, kind, created_at) VALUES (?, 'cover.png', ?, 1028, 'abc', 'cover', ?)",
                (date_str, f"articles/{date_str}/cover.png", ts),
            )

        res = db.cleanup_pushed_article_media(date_str)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed_files"], 4)
        self.assertGreaterEqual(res["freed_bytes"], 3584)
        self.assertFalse((art_dir / "cover.png").exists())
        self.assertFalse((art_dir / "chart.webp").exists())
        self.assertTrue((art_dir / "article.md").exists())
        self.assertTrue((art_dir / "article.html").exists())
        self.assertFalse(inc_dir.exists())

        with db.get_conn() as conn:
            art = conn.execute("SELECT cover_path FROM articles WHERE date = ?", (date_str,)).fetchone()
            img_cnt = conn.execute("SELECT COUNT(*) AS c FROM images WHERE date = ?", (date_str,)).fetchone()["c"]
        self.assertEqual(art["cover_path"], "")
        self.assertEqual(img_cnt, 0)

    def test_disk_usage_helper_and_cache(self) -> None:
        usage1 = db.get_data_disk_usage()
        self.assertGreater(usage1["disk_usage_bytes"], 0)
        self.assertTrue(isinstance(usage1["disk_usage_human"], str) and len(usage1["disk_usage_human"]) > 0)
        extra_file = self.tmp / "extra_blob.bin"
        extra_file.write_bytes(b"1" * 4096)
        # Cached within 60s TTL
        usage_cached = db.get_data_disk_usage()
        self.assertEqual(usage_cached["disk_usage_bytes"], usage1["disk_usage_bytes"])
        # Invalidated cache reflects updated size
        db.invalidate_disk_usage_cache()
        usage_fresh = db.get_data_disk_usage()
        self.assertGreaterEqual(usage_fresh["disk_usage_bytes"], usage1["disk_usage_bytes"] + 4096)

    def test_backup_creation_and_pruning(self) -> None:
        bk = db.create_sqlite_backup(trigger="manual")
        self.assertTrue(bk["ok"])
        self.assertTrue((self.tmp / bk["rel_path"]).exists())

    @unittest.skipUnless(HAS_WECHAT, "httpx not installed in this environment")
    def test_wechat_draft_only_webp_reject_and_html_sanitization(self) -> None:
        self.assertEqual(wechat.PUBLISH_MODE, "draft_only")
        self.assertEqual(wechat.DEFAULT_AUTHOR, "资讯后台管理系统")
        self.assertFalse(hasattr(wechat, "get_token_cache_status"))

        dirty = '<section><script>alert(1)</script><p onclick="evil()">正文</p><iframe src="x"></iframe></section>'
        clean = wechat.sanitize_wechat_html(dirty)
        self.assertNotIn("<script", clean.lower())
        self.assertNotIn("<iframe", clean.lower())
        self.assertNotIn("onclick", clean.lower())
        self.assertIn("正文", clean)

        webp_cover = self.tmp / "cover.webp"
        webp_cover.write_bytes(b"RIFF0000WEBP")
        res_webp = asyncio.run(
            wechat.upload_wechat_draft(
                article_date="2026-11-26-01",
                title="WebP Test",
                author="资讯后台管理系统",
                digest="摘要",
                html_content="<p>Hello</p>",
                cover_path=webp_cover,
                appid="dummy_appid",
                appsecret="dummy_secret",
                mock_mode=True,
            )
        )
        self.assertFalse(res_webp["ok"])
        self.assertIn("WebP", res_webp["error"])

        # Real-mode retry reuses existing_thumb_media_id without re-uploading material
        png_cover = self.tmp / "cover.png"
        png_cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 128)
        called_urls: list[str] = []

        class _FakeWeChatClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url: str, **_kwargs):
                called_urls.append(url)
                return httpx.Response(200, json={"access_token": "tok_123", "expires_in": 7200})

            async def post(self, url: str, **_kwargs):
                called_urls.append(url)
                return httpx.Response(200, json={"media_id": "draft_ok_999"})

        wechat.invalidate_access_token_cache()
        with patch.object(wechat.httpx, "AsyncClient", _FakeWeChatClient):
            res_reuse = asyncio.run(
                wechat.upload_wechat_draft(
                    article_date="2026-11-26-01",
                    title="Retry Test",
                    author="资讯后台管理系统",
                    digest="摘要",
                    html_content="<p>Hello</p>",
                    cover_path=png_cover,
                    appid="dummy_appid_reuse",
                    appsecret="dummy_secret_reuse",
                    mock_mode=False,
                    existing_thumb_media_id="persisted_thumb_777",
                )
            )
        self.assertTrue(res_reuse["ok"])
        self.assertEqual(res_reuse["thumb_media_id"], "persisted_thumb_777")
        self.assertEqual(res_reuse["media_id"], "draft_ok_999")
        self.assertFalse(any("add_material" in u for u in called_urls))
        self.assertTrue(any("draft/add" in u for u in called_urls))


@unittest.skipUnless(HAS_FASTAPI, "FastAPI TestClient not installed in this environment")
class ServerApiAuthAndCleanupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ai_media_api_test_"))
        self._env_backup = {
            k: os.environ.get(k) for k in ("DATA_DIR", "ADMIN_USER", "ADMIN_PASSWORD", "SESSION_SECRET")
        }
        os.environ["DATA_DIR"] = str(self.tmp)
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "dummy-unit-pass"
        os.environ["SESSION_SECRET"] = "dummy-unit-session-key"
        auth.reset_login_rate_limiter()
        db.init_db()
        self.client = TestClient(server_main.app)

    def tearDown(self) -> None:
        self.client.close()
        auth.reset_login_rate_limiter()
        db.invalidate_disk_usage_cache()
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, password: str = "dummy-unit-pass", remember: bool = True, headers: dict | None = None):
        return self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": password, "remember": remember},
            headers=headers or {},
        )

    def test_unauthenticated_root_etag_health_and_401_without_www_authenticate(self) -> None:
        r_root = self.client.get("/")
        self.assertEqual(r_root.status_code, 200)
        self.assertNotIn("www-authenticate", {k.lower() for k in r_root.headers})
        etag = r_root.headers.get("etag", "")
        self.assertTrue(etag)
        self.assertEqual(r_root.headers.get("cache-control"), "no-cache")

        r_304 = self.client.get("/", headers={"If-None-Match": etag})
        self.assertEqual(r_304.status_code, 304)

        # Static assets MUST load unauthenticated so the login screen renders cleanly
        r_css = self.client.get("/static/app.css?v=1.3.0")
        self.assertEqual(r_css.status_code, 200)
        r_js = self.client.get("/static/app.js?v=1.3.0")
        self.assertEqual(r_js.status_code, 200)

        r_health = self.client.get("/health")
        self.assertEqual(r_health.status_code, 200)
        health_data = r_health.json()
        self.assertEqual(health_data["version"], "1.3.0")
        self.assertFalse(health_data["wechat_configured"])
        self.assertGreater(health_data["disk_usage_bytes"], 0)
        self.assertIn("disk_usage_human", health_data)

        r_me = self.client.get("/api/auth/me")
        self.assertEqual(r_me.status_code, 200)
        self.assertEqual(
            r_me.json(),
            {
                "authenticated": False,
                "auth_enabled": True,
                "username": "",
                "wechat_configured": False,
            },
        )

        r_api = self.client.get("/api/articles")
        self.assertEqual(r_api.status_code, 401)
        self.assertEqual(r_api.json().get("detail"), "未登录或登录已过期")
        self.assertNotIn("www-authenticate", {k.lower() for k in r_api.headers})

    def test_cookie_login_logout_secure_flag_rate_limit_and_basic_auth(self) -> None:
        bad = self._login(password="wrong-pass")
        self.assertEqual(bad.status_code, 401)
        self.assertNotIn("www-authenticate", {k.lower() for k in bad.headers})

        ok = self._login(remember=True)
        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["username"], "admin")
        self.assertFalse(body["wechat_configured"])

        set_cookie = ok.headers.get("set-cookie", "")
        self.assertIn("ai_media_session=", set_cookie)
        self.assertIn("httponly", set_cookie.lower())
        self.assertIn("samesite=lax", set_cookie.lower())

        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()["authenticated"])
        self.assertEqual(me.json()["username"], "admin")

        r_articles = self.client.get("/api/articles")
        self.assertEqual(r_articles.status_code, 200)

        # Logout over HTTPS sets Secure attribute on delete_cookie
        out_https = self.client.post("/api/auth/logout", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(out_https.status_code, 200)
        self.assertTrue(out_https.json()["ok"])
        self.assertIn("secure", out_https.headers.get("set-cookie", "").lower())

        r_after = self.client.get("/api/articles")
        self.assertEqual(r_after.status_code, 401)

        # Rate limit: 10 consecutive failed logins from same IP -> 11th returns 429
        rate_hdr = {"X-Forwarded-For": "203.0.113.77"}
        for _ in range(10):
            resp_fail = self._login(password="bad-password", headers=rate_hdr)
            self.assertEqual(resp_fail.status_code, 401)
        resp_429 = self._login(password="bad-password", headers=rate_hdr)
        self.assertEqual(resp_429.status_code, 429)
        self.assertIn("retry-after", {k.lower() for k in resp_429.headers})

        # HTTP Basic Auth header remains compatible for scripts
        basic_token = base64.b64encode(b"admin:dummy-unit-pass").decode("ascii")
        r_basic = self.client.get("/api/articles", headers={"Authorization": f"Basic {basic_token}"})
        self.assertEqual(r_basic.status_code, 200)

    def test_removed_dead_endpoints_and_ingest_localhost_403(self) -> None:
        self.assertEqual(self._login().status_code, 200)
        for dead_path in ("/api/analytics/summary", "/api/topics", "/api/images", "/api/metrics", "/api/settings"):
            self.assertEqual(self.client.get(dead_path).status_code, 404, f"Expected 404 for {dead_path}")
        self.assertEqual(self.client.post("/api/news/any-id/promote").status_code, 404)

        # Proxied ingest request must return 403
        r_proxy = self.client.post(
            "/api/internal/ingest/2026-11-26-01",
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        self.assertEqual(r_proxy.status_code, 403)

        # Localhost ASGI client: analytics bypass is gone (401 without auth), internal ingest allowed
        async def _localhost_asgi(scope, receive, send):
            if scope.get("type") == "http":
                scope = dict(scope)
                scope["client"] = ("127.0.0.1", 50000)
            await server_main.app(scope, receive, send)

        local_client = TestClient(_localhost_asgi)
        try:
            self.assertEqual(local_client.get("/api/analytics/summary").status_code, 401)

            dirs = db.ensure_data_dirs()
            filtered = {"candidates": [{"id": "loc-1", "title": "Local News", "url": "https://example.com/l1"}]}
            state = {"stages": {"collect": {"status": "completed", "message": "done"}}}
            _create_incoming_slot(dirs["incoming"], "2026-11-26-01", filtered_payload=filtered, state_payload=state)
            _create_incoming_slot(dirs["incoming"], "2026-11-26-02", filtered_payload=filtered, state_payload=state)

            r1 = local_client.post("/api/internal/ingest/2026-11-26-01")
            self.assertEqual(r1.status_code, 200)
            self.assertFalse(r1.json()["filtered_skipped"])
            r2 = local_client.post("/api/internal/ingest/2026-11-26-02")
            self.assertEqual(r2.status_code, 200)
            self.assertTrue(r2.json()["filtered_skipped"])
        finally:
            local_client.close()

    def test_config_canonical_fields_and_data_retain_days(self) -> None:
        self.assertEqual(self._login().status_code, 200)
        cfg0 = self.client.get("/api/config").json()
        self.assertFalse(cfg0["wechat_configured"])
        self.assertEqual(cfg0["data_retain_days"], 7)
        self.assertIn("disk_usage_human", cfg0)
        for removed_alias in ("app_id", "has_app_secret", "app_secret"):
            self.assertNotIn(removed_alias, cfg0)

        updated = self.client.put(
            "/api/config",
            json={
                "wechat_appid": "dummy_unit_appid",
                "wechat_appsecret": "dummy_unit_val_9876",
                "wechat_mock_mode": False,
                "data_retain_days": 14,
            },
        )
        self.assertEqual(updated.status_code, 200)
        u = updated.json()
        self.assertTrue(u["wechat_configured"])
        self.assertEqual(u["data_retain_days"], 14)
        self.assertEqual(u["wechat_appsecret_masked"], "****9876")
        self.assertEqual(u["wechat_publish_mode"], "draft_only")

        self.assertTrue(self.client.get("/health").json()["wechat_configured"])
        self.assertTrue(self.client.get("/api/auth/me").json()["wechat_configured"])

    def test_pagination_and_filters_across_list_endpoints(self) -> None:
        self.assertEqual(self._login().status_code, 200)
        ts = db.now_iso()
        with db.get_conn() as conn:
            for i in range(1, 6):
                cat = "AI新闻" if i <= 3 else "开源工具"
                st = "selected" if i % 2 == 0 else "candidate"
                conn.execute(
                    """
                    INSERT INTO news_items (id, date, title, url, source_name, category, score, summary, status, created_at)
                    VALUES (?, '2026-11-26', ?, ?, 'Src', ?, ?, 'summary', ?, ?)
                    """,
                    (f"pg-news-{i}", f"Alpha Item {i}", f"https://example.com/{i}", cat, float(100 - i), st, ts),
                )
                art_status = "draft_uploaded" if i <= 2 else "ready"
                conn.execute(
                    """
                    INSERT INTO articles (date, title, category, status, ingested_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (f"2026-11-26-0{i}", f"Beta Article {i}", cat, art_status, ts, ts),
                )
                pub_status = "manual_published" if i == 1 else "draft_ready"
                conn.execute(
                    """
                    INSERT INTO publish_records (article_date, title, draft_media_id, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (f"2026-11-26-0{i}", f"Gamma Record {i}", f"mid_{i}", pub_status, ts, ts),
                )
                db.add_task_log(conn, "2026-11-26", "ingest" if i <= 3 else "cleanup", "completed", f"Delta log {i}")

        # /api/news
        n_res = self.client.get("/api/news", params={"category": "AI新闻", "limit": 2, "offset": 1}).json()
        self.assertEqual(n_res["total"], 3)
        self.assertEqual(n_res["count"], 2)
        self.assertEqual(len(n_res["items"]), 2)

        # /api/articles
        a_res = self.client.get("/api/articles", params={"status": "ready", "q": "Beta", "limit": 2, "offset": 0}).json()
        self.assertEqual(a_res["total"], 3)
        self.assertEqual(a_res["count"], 2)

        # /api/publish-records
        p_res = self.client.get("/api/publish-records", params={"status": "draft_ready", "q": "Gamma", "limit": 2, "offset": 2}).json()
        self.assertEqual(p_res["total"], 4)
        self.assertEqual(p_res["count"], 2)

        # /api/logs
        l_res = self.client.get("/api/logs", params={"stage": "ingest", "q": "Delta", "date": "2026-11-26", "limit": 2, "offset": 0}).json()
        self.assertEqual(l_res["total"], 3)
        self.assertEqual(l_res["count"], 2)

    def test_real_draft_upload_auto_cleans_media_repush_and_manual_endpoints(self) -> None:
        self.assertEqual(self._login().status_code, 200)
        date_str = "2026-11-25"
        dirs = db.ensure_data_dirs()
        art_dir = dirs["articles"] / date_str
        inc_dir = dirs["incoming"] / date_str
        art_dir.mkdir(parents=True, exist_ok=True)
        inc_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "cover.png").write_bytes(b"\x89PNG" + b"x" * 2048)
        (art_dir / "article.html").write_text("<p>Hello</p>", encoding="utf-8")
        (inc_dir / "cover.png").write_bytes(b"\x89PNG" + b"y" * 2048)

        ts = db.now_iso()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at) VALUES (?, 'Auto Clean Test', ?, 'ready', ?, ?)",
                (date_str, f"articles/{date_str}/cover.png", ts, ts),
            )
            conn.execute(
                "INSERT INTO news_items (id, date, title, url, status, created_at) VALUES ('news-del-1', ?, 'Delete Me', 'https://example.com', 'candidate', ?)",
                (date_str, ts),
            )

        fake_real_result = {
            "ok": True,
            "mode": "real",
            "media_id": "real_draft_media_123",
            "thumb_media_id": "real_thumb_456",
            "message": "真实草稿上传成功",
            "response": {"media_id": "real_draft_media_123"},
        }
        with patch.object(drafts_service, "upload_wechat_draft", new=AsyncMock(return_value=fake_real_result)):
            res = self.client.post(f"/api/wechat/draft/{date_str}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data["media_cleanup"])
        self.assertEqual(data["media_cleanup"]["removed_files"], 2)
        self.assertFalse((art_dir / "cover.png").exists())
        self.assertFalse(inc_dir.exists())

        # Re-pushing after media cleanup regenerates cover and succeeds cleanly
        with patch.object(drafts_service, "upload_wechat_draft", new=AsyncMock(return_value=fake_real_result)):
            repush = self.client.post(f"/api/wechat/draft/{date_str}")
        self.assertEqual(repush.status_code, 200)
        self.assertTrue(repush.json()["ok"])
        self.assertTrue(repush.json()["cover_regenerated"])

        arts = self.client.get("/api/articles").json()["items"]
        self.assertEqual(arts[0]["cover_path"], "")
        self.assertEqual(arts[0]["cover_url"], "")

        clean_res = self.client.post("/api/cleanup/pushed-media")
        self.assertEqual(clean_res.status_code, 200)
        self.assertTrue(clean_res.json()["ok"])
        self.assertEqual(clean_res.json()["articles_checked"], 1)

        del_res = self.client.delete("/api/news/news-del-1")
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.json()["ok"])
        self.assertEqual(self.client.delete("/api/news/news-del-1").status_code, 404)

    def test_multi_slot_articles_draft_and_cleanup(self) -> None:
        self._login()
        self.client.put(
            "/api/config",
            json={"wechat_appid": "dummy_slot_appid", "wechat_appsecret": "dummy_slot_val", "wechat_mock_mode": False},
        )
        dirs = db.ensure_data_dirs()
        ts = db.now_iso()
        slots = ["2026-11-26-01", "2026-11-26-02"]
        for key in slots:
            art_dir = dirs["articles"] / key
            inc_dir = dirs["incoming"] / key
            art_dir.mkdir(parents=True, exist_ok=True)
            inc_dir.mkdir(parents=True, exist_ok=True)
            (art_dir / "cover.png").write_bytes(b"\x89PNG" + b"a" * 512)
            (art_dir / "article.html").write_text(f"<p>{key}</p>", encoding="utf-8")
            (inc_dir / "cover.png").write_bytes(b"\x89PNG" + b"b" * 512)
            with db.get_conn() as conn:
                conn.execute(
                    "INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at) VALUES (?, ?, ?, 'ready', ?, ?)",
                    (key, f"Multi Article {key}", f"articles/{key}/cover.png", ts, ts),
                )

        bad_res = self.client.post("/api/wechat/draft/2026-11-26-00")
        self.assertEqual(bad_res.status_code, 400)

        fake_real = {
            "ok": True,
            "mode": "real",
            "media_id": "slot_draft_id",
            "thumb_media_id": "slot_thumb_id",
            "message": "真实草稿上传成功",
            "response": {"media_id": "slot_draft_id"},
        }
        with patch.object(server_main, "upload_wechat_draft", new=AsyncMock(return_value=fake_real)):
            for key in slots:
                r = self.client.post(f"/api/wechat/draft/{key}")
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()["ok"])
                self.assertFalse((dirs["articles"] / key / "cover.png").exists())
                self.assertFalse((dirs["incoming"] / key).exists())

        old_key = "2020-01-01-03"
        (dirs["articles"] / old_key).mkdir(parents=True, exist_ok=True)
        (dirs["articles"] / old_key / "article.md").write_text("old", encoding="utf-8")
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO articles (date, title, status, ingested_at, updated_at) VALUES (?, 'Old Slot', 'ready', ?, ?)",
                (old_key, ts, ts),
            )
        clean = db.cleanup_old_data(retain_days=7, trigger="manual")
        self.assertIn(old_key, clean["removed_articles"])
        self.assertFalse((dirs["articles"] / old_key).exists())

    def test_news_one_click_translate_and_publish(self) -> None:
        self.assertEqual(self._login().status_code, 200)
        ts = db.now_iso()
        today = db.today_str()
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO news_items (
                    id, date, title, url, source_name, category, score, summary, status, created_at
                ) VALUES (
                    'news-en-01', ?, 'New Open Source Agent Framework Released',
                    'https://example.com/agent-framework', 'Tech Blog', 'AI新闻', 92.5,
                    'An open-source framework simplifies multi-step tool execution.', 'candidate', ?
                )
                """,
                (today, ts),
            )

        async def _mock_translate(text: str) -> tuple[str, str]:
            if "Framework Released" in text:
                return ("全新开源智能体框架正式发布", "google_gtx")
            return ("这款开源框架大幅简化了多步工具编排与执行流程，提升了开发者生产效率。", "google_gtx")

        fake_real = {
            "ok": True,
            "mode": "real",
            "media_id": "news_draft_media_999",
            "thumb_media_id": "news_thumb_888",
            "message": "真实草稿上传成功",
            "response": {"media_id": "news_draft_media_999"},
        }

        with (
            patch.object(translator, "translate_to_zh", side_effect=_mock_translate),
            patch.object(
                translator,
                "fetch_source_paragraphs",
                new=AsyncMock(return_value=["The framework introduces deterministic workflow orchestration for developers."]),
            ),
            patch.object(drafts_service, "upload_wechat_draft", new=AsyncMock(return_value=fake_real)),
        ):
            r = self.client.post("/api/news/news-en-01/publish")

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["news_id"], "news-en-01")
        self.assertEqual(body["article_date"], f"{today}-01")
        self.assertEqual(body["title"], "全新开源智能体框架正式发布")
        self.assertEqual(body["original_title"], "New Open Source Agent Framework Released")
        self.assertEqual(body["translation_mode"], "google_gtx")
        self.assertEqual(body["publish_mode"], "draft_only")
        self.assertEqual(body["mode"], "real")
        self.assertEqual(body["media_id"], "news_draft_media_999")
        self.assertIsNotNone(body["media_cleanup"])

        art_dir = db.get_data_dir() / "articles" / f"{today}-01"
        self.assertTrue((art_dir / "article.md").exists())
        self.assertTrue((art_dir / "article.html").exists())
        self.assertTrue((art_dir / "metadata.json").exists())
        self.assertFalse((art_dir / "cover.png").exists())

        with db.get_conn() as conn:
            news_row = conn.execute("SELECT status FROM news_items WHERE id = 'news-en-01'").fetchone()
            art_row = conn.execute("SELECT title, status, publish_mode FROM articles WHERE date = ?", (f"{today}-01",)).fetchone()
            pub_row = conn.execute("SELECT draft_media_id FROM publish_records WHERE article_date = ?", (f"{today}-01",)).fetchone()
        self.assertEqual(news_row["status"], "selected")
        self.assertEqual(art_row["title"], "全新开源智能体框架正式发布")
        self.assertEqual(art_row["status"], "draft_uploaded")
        self.assertEqual(art_row["publish_mode"], "draft_only")
        self.assertEqual(pub_row["draft_media_id"], "news_draft_media_999")

        self.assertEqual(self.client.post("/api/news/non-existent-id/publish").status_code, 404)

    def _seed_article_and_publish_record(self, date_str: str, title: str) -> int:
        dirs = db.ensure_data_dirs()
        art_dir = dirs["articles"] / date_str
        inc_dir = dirs["incoming"] / date_str
        art_dir.mkdir(parents=True, exist_ok=True)
        inc_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "cover.png").write_bytes(b"\x89PNG" + b"c" * 1024)
        (art_dir / "article.html").write_text(f"<p>{title}</p>", encoding="utf-8")
        (inc_dir / "cover.png").write_bytes(b"\x89PNG" + b"d" * 1024)
        ts = db.now_iso()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at) VALUES (?, ?, ?, 'draft_uploaded', ?, ?)",
                (date_str, title, f"articles/{date_str}/cover.png", ts, ts),
            )
            conn.execute(
                "INSERT INTO images (date, filename, rel_path, size_bytes, sha256, kind, created_at) VALUES (?, 'cover.png', ?, 1028, 'sha', 'cover', ?)",
                (date_str, f"articles/{date_str}/cover.png", ts),
            )
            conn.execute(
                "INSERT INTO draft_tasks (article_date, title, mode, status, created_at, updated_at) VALUES (?, ?, 'mock', 'success', ?, ?)",
                (date_str, title, ts, ts),
            )
            cur = conn.execute(
                "INSERT INTO publish_records (article_date, title, draft_media_id, status, created_at, updated_at) VALUES (?, ?, 'mid_1', 'draft_ready', ?, ?)",
                (date_str, title, ts, ts),
            )
            return int(cur.lastrowid or 0)

    def test_article_detail_and_delete_endpoints(self) -> None:
        self.assertEqual(self._login().status_code, 200)
        self._seed_article_and_publish_record("2026-11-25-01", "单删测试文章")
        self._seed_article_and_publish_record("2026-11-25-02", "批删测试文章一")
        self._seed_article_and_publish_record("2026-11-25-03", "批删测试文章二")

        detail = self.client.get("/api/articles/2026-11-25-01")
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("metrics", detail.json())

        r_del = self.client.delete("/api/articles/2026-11-25-01")
        self.assertEqual(r_del.status_code, 200)
        d = r_del.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["deleted_articles"], 1)
        self.assertEqual(d["removed_files"], 3)
        self.assertFalse((db.get_data_dir() / "articles" / "2026-11-25-01").exists())
        self.assertFalse((db.get_data_dir() / "incoming" / "2026-11-25-01").exists())
        self.assertEqual(self.client.delete("/api/articles/2026-11-25-01").status_code, 404)

        r_batch = self.client.post("/api/articles/batch-delete", json={"dates": ["2026-11-25-02"]})
        self.assertEqual(r_batch.status_code, 200)
        b = r_batch.json()
        self.assertEqual(b["deleted_count"], 1)
        self.assertEqual(b["deleted_dates"], ["2026-11-25-02"])
        self.assertFalse((db.get_data_dir() / "articles" / "2026-11-25-02").exists())

        r_all = self.client.post("/api/articles/batch-delete", json={"all": True})
        self.assertEqual(r_all.status_code, 200)
        a = r_all.json()
        self.assertEqual(a["deleted_count"], 1)
        self.assertEqual(a["deleted_dates"], ["2026-11-25-03"])
        self.assertEqual(self.client.get("/api/articles").json()["count"], 0)

    def test_publish_record_delete_and_batch_delete_cascade_articles(self) -> None:
        self.assertEqual(self._login().status_code, 200)
        id1 = self._seed_article_and_publish_record("2026-11-25-04", "发布记录联动单删")
        id2 = self._seed_article_and_publish_record("2026-11-25-05", "发布记录联动批删一")
        id3 = self._seed_article_and_publish_record("2026-11-25-06", "发布记录联动批删二")

        r_single = self.client.delete(f"/api/publish-records/{id1}")
        self.assertEqual(r_single.status_code, 200)
        s = r_single.json()
        self.assertTrue(s["ok"])
        self.assertEqual(s["article_date"], "2026-11-25-04")
        self.assertEqual(s["deleted_articles"], 1)
        self.assertEqual(s["deleted_publish_records"], 1)
        self.assertEqual(s["removed_files"], 3)
        self.assertFalse((db.get_data_dir() / "articles" / "2026-11-25-04").exists())
        self.assertFalse((db.get_data_dir() / "incoming" / "2026-11-25-04").exists())
        self.assertEqual(self.client.get("/api/articles/2026-11-25-04").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/publish-records/{id1}").status_code, 404)

        r_b1 = self.client.post("/api/publish-records/batch-delete", json={"ids": [id2]})
        self.assertEqual(r_b1.status_code, 200)
        b1 = r_b1.json()
        self.assertEqual(b1["deleted_records"], 1)
        self.assertEqual(b1["deleted_articles"], 1)
        self.assertFalse((db.get_data_dir() / "articles" / "2026-11-25-05").exists())
        self.assertEqual(self.client.get("/api/articles/2026-11-25-05").status_code, 404)

        r_b2 = self.client.post("/api/publish-records/batch-delete", json={"all": True})
        self.assertEqual(r_b2.status_code, 200)
        b2 = r_b2.json()
        self.assertEqual(b2["deleted_records"], 1)
        self.assertEqual(b2["deleted_articles"], 1)
        self.assertFalse((db.get_data_dir() / "articles" / "2026-11-25-06").exists())
        self.assertEqual(self.client.get("/api/publish-records").json()["count"], 0)
        self.assertEqual(self.client.get("/api/articles").json()["count"], 0)


if __name__ == "__main__":
    unittest.main()
