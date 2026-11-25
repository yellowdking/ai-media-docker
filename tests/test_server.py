"""Unit tests for AI Media Docker server (db, auth cookies, cleanup, config, wechat draft_only)."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import db, translator

try:
    from app import wechat

    HAS_WECHAT = True
except ImportError:
    wechat = None  # type: ignore[assignment]
    HAS_WECHAT = False

try:
    from fastapi.testclient import TestClient
    from app import main as server_main

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


class ServerCoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ai_media_srv_test_"))
        self._old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = str(self.tmp)
        db.init_db()

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_db_init_wal_and_defaults(self) -> None:
        health = db.get_db_health()
        self.assertEqual(health["journal_mode"], "wal")
        self.assertEqual(health["busy_timeout_ms"], 5000)
        with db.get_conn() as conn:
            cfg = db.get_settings_dict(conn)
        self.assertEqual(cfg["wechat_publish_mode"], "draft_only")
        self.assertEqual(cfg["data_retain_days"], "7")
        self.assertEqual(cfg["backup_retain_count"], "7")
        self.assertEqual(cfg["site_name"], "资讯后台管理系统")

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

        # Custom user-specified site_name must NOT be overwritten
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE settings SET value = '我的专属资讯台', updated_at = ? WHERE key = 'site_name'",
                (ts,),
            )
        db.init_db()
        with db.get_conn() as conn:
            cfg = db.get_settings_dict(conn)
        self.assertEqual(cfg["site_name"], "我的专属资讯台")

    def test_generate_cover_png_valid_1280x720(self) -> None:
        p1 = self.tmp / "covers" / "cover_a.png"
        p2 = self.tmp / "covers" / "cover_b.png"
        translator.generate_cover_png(p1, "2026-11-25-01:New open source model release")
        translator.generate_cover_png(p2, "2026-11-25-02:Different seed headline")

        data = p1.read_bytes()
        self.assertGreater(len(data), 5120)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertNotEqual(data, p2.read_bytes())

        # Parse PNG chunks and validate CRC + IHDR dimensions + IDAT/IEND presence
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
        # >=30% CJK returns already_zh without network calls
        zh_text, zh_mode = asyncio.run(translator.translate_to_zh("这是一篇关于人工智能最新进展的中文资讯文章"))
        self.assertEqual(zh_mode, "already_zh")
        self.assertIn("人工智能", zh_text)

        # Chunking preserves paragraphs and respects <=1400 limit
        long_text = "\n\n".join([f"Paragraph {i}: " + ("AI engineering insight. " * 25) for i in range(6)])
        chunks = translator.split_text_chunks(long_text, max_chars=1400)
        self.assertGreaterEqual(len(chunks), 2)
        for ch in chunks:
            self.assertLessEqual(len(ch), 1400)

        # HTML extraction strips nav/script/style/footer/header/aside/noscript
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

        # Bundle builder with mocked translator & fetcher
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
            log_row = conn.execute(
                "SELECT stage, status FROM task_logs WHERE date = ? AND stage = 'cleanup' ORDER BY id DESC LIMIT 1",
                (date_str,),
            ).fetchone()
            self.assertIsNotNone(log_row)

        # Calling again on missing date returns existed=False
        res2 = db.delete_article_and_storage(date_str, reason="unit_test")
        self.assertFalse(res2["existed"])

    def test_cleanup_old_data_removes_expired_incoming_and_articles(self) -> None:
        dirs = db.ensure_data_dirs()
        old_inc = dirs["incoming"] / "2020-01-01"
        old_art = dirs["articles"] / "2020-01-01"
        keep_inc = dirs["incoming"] / db.today_str()
        keep_art = dirs["articles"] / db.today_str()
        for p in (old_inc, old_art, keep_inc, keep_art):
            p.mkdir(parents=True, exist_ok=True)
            (p / "sample.txt").write_text("hello", encoding="utf-8")

        res = db.cleanup_old_data(retain_days=7, trigger="manual")
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed_incoming"], ["2020-01-01"])
        self.assertEqual(res["removed_articles"], ["2020-01-01"])
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
                """
                INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at)
                VALUES (?, 'Test Article', ?, 'draft_uploaded', ?, ?)
                """,
                (date_str, f"articles/{date_str}/cover.png", ts, ts),
            )
            conn.execute(
                """
                INSERT INTO images (date, filename, rel_path, size_bytes, sha256, kind, created_at)
                VALUES (?, 'cover.png', ?, 1028, 'abc', 'cover', ?)
                """,
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

    def test_disk_usage_helper(self) -> None:
        usage = db.get_data_disk_usage()
        self.assertGreater(usage["disk_usage_bytes"], 0)
        self.assertTrue(isinstance(usage["disk_usage_human"], str) and len(usage["disk_usage_human"]) > 0)

    def test_backup_creation_and_pruning(self) -> None:
        bk = db.create_sqlite_backup(trigger="manual")
        self.assertTrue(bk["ok"])
        self.assertTrue((self.tmp / bk["rel_path"]).exists())

    @unittest.skipUnless(HAS_WECHAT, "httpx not installed in this environment")
    def test_wechat_draft_only_and_html_sanitization(self) -> None:
        self.assertEqual(wechat.PUBLISH_MODE, "draft_only")
        dirty = '<section><script>alert(1)</script><p onclick="evil()">正文</p><iframe src="x"></iframe></section>'
        clean = wechat.sanitize_wechat_html(dirty)
        self.assertNotIn("<script", clean.lower())
        self.assertNotIn("<iframe", clean.lower())
        self.assertNotIn("onclick", clean.lower())
        self.assertIn("正文", clean)


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
        db.init_db()
        self.client = TestClient(server_main.app)

    def tearDown(self) -> None:
        self.client.close()
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, password: str = "dummy-unit-pass", remember: bool = True):
        return self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": password, "remember": remember},
        )

    def test_unauthenticated_root_health_and_401_without_www_authenticate(self) -> None:
        r_root = self.client.get("/")
        self.assertEqual(r_root.status_code, 200)
        self.assertNotIn("www-authenticate", {k.lower() for k in r_root.headers})

        r_health = self.client.get("/health")
        self.assertEqual(r_health.status_code, 200)
        health_data = r_health.json()
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

    def test_cookie_login_logout_and_basic_auth_compat(self) -> None:
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

        out = self.client.post("/api/auth/logout")
        self.assertEqual(out.status_code, 200)
        self.assertTrue(out.json()["ok"])

        r_after = self.client.get("/api/articles")
        self.assertEqual(r_after.status_code, 401)

        # HTTP Basic Auth header remains compatible for scripts
        basic_token = base64.b64encode(b"admin:dummy-unit-pass").decode("ascii")
        r_basic = self.client.get("/api/articles", headers={"Authorization": f"Basic {basic_token}"})
        self.assertEqual(r_basic.status_code, 200)

    def test_localhost_bypass_for_ssh_endpoints(self) -> None:
        async def _localhost_asgi(scope, receive, send):
            if scope.get("type") == "http":
                scope = dict(scope)
                scope["client"] = ("127.0.0.1", 50000)
            await server_main.app(scope, receive, send)

        local_client = TestClient(_localhost_asgi)
        try:
            r = local_client.get("/api/analytics/summary")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])

            r_proxied = local_client.get(
                "/api/analytics/summary", headers={"X-Forwarded-For": "203.0.113.10"}
            )
            self.assertEqual(r_proxied.status_code, 401)
            self.assertNotIn("www-authenticate", {k.lower() for k in r_proxied.headers})
        finally:
            local_client.close()

    def test_config_wechat_configured_and_data_retain_days(self) -> None:
        self.assertEqual(self._login().status_code, 200)
        cfg0 = self.client.get("/api/config").json()
        self.assertFalse(cfg0["wechat_configured"])
        self.assertEqual(cfg0["data_retain_days"], 7)
        self.assertIn("disk_usage_human", cfg0)

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

    def test_real_draft_upload_auto_cleans_media_and_manual_endpoints(self) -> None:
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
                """
                INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at)
                VALUES (?, 'Auto Clean Test', ?, 'ready', ?, ?)
                """,
                (date_str, f"articles/{date_str}/cover.png", ts, ts),
            )
            conn.execute(
                """
                INSERT INTO news_items (id, date, title, url, status, created_at)
                VALUES ('news-del-1', ?, 'Delete Me', 'https://example.com', 'candidate', ?)
                """,
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
        with patch.object(server_main, "upload_wechat_draft", new=AsyncMock(return_value=fake_real_result)):
            res = self.client.post(f"/api/wechat/draft/{date_str}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data["media_cleanup"])
        self.assertEqual(data["media_cleanup"]["removed_files"], 2)
        self.assertFalse((art_dir / "cover.png").exists())
        self.assertFalse(inc_dir.exists())

        arts = self.client.get("/api/articles").json()["items"]
        self.assertEqual(arts[0]["cover_path"], "")
        self.assertEqual(arts[0]["cover_url"], "")

        # Manual cleanup pushed-media endpoint
        clean_res = self.client.post("/api/cleanup/pushed-media")
        self.assertEqual(clean_res.status_code, 200)
        self.assertTrue(clean_res.json()["ok"])
        self.assertEqual(clean_res.json()["articles_checked"], 1)

        # Delete news item endpoint
        del_res = self.client.delete("/api/news/news-del-1")
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.json()["ok"])
        self.assertEqual(self.client.delete("/api/news/news-del-1").status_code, 404)

    def test_multi_slot_articles_draft_and_cleanup(self) -> None:
        self._login()
        self.client.put(
            "/api/settings",
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
                    """
                    INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at)
                    VALUES (?, ?, ?, 'ready', ?, ?)
                    """,
                    (key, f"Multi Article {key}", f"articles/{key}/cover.png", ts, ts),
                )

        # Invalid slot 00 rejected
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

        # Expired slotted directory & row cleaned up by cleanup_old_data
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
            patch.object(server_main, "upload_wechat_draft", new=AsyncMock(return_value=fake_real)),
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

        # Cover auto-cleaned on real push, while markdown/html/metadata remain
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

        # 404 on unknown news_id
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
                """
                INSERT INTO articles (date, title, cover_path, status, ingested_at, updated_at)
                VALUES (?, ?, ?, 'draft_uploaded', ?, ?)
                """,
                (date_str, title, f"articles/{date_str}/cover.png", ts, ts),
            )
            conn.execute(
                """
                INSERT INTO images (date, filename, rel_path, size_bytes, sha256, kind, created_at)
                VALUES (?, 'cover.png', ?, 1028, 'sha', 'cover', ?)
                """,
                (date_str, f"articles/{date_str}/cover.png", ts),
            )
            conn.execute(
                """
                INSERT INTO draft_tasks (article_date, title, mode, status, created_at, updated_at)
                VALUES (?, ?, 'mock', 'success', ?, ?)
                """,
                (date_str, title, ts, ts),
            )
            cur = conn.execute(
                """
                INSERT INTO publish_records (article_date, title, draft_media_id, status, created_at, updated_at)
                VALUES (?, ?, 'mid_1', 'draft_ready', ?, ?)
                """,
                (date_str, title, ts, ts),
            )
            return int(cur.lastrowid or 0)

    def test_article_delete_and_batch_delete_endpoints(self) -> None:
        self.assertEqual(self._login().status_code, 200)
        self._seed_article_and_publish_record("2026-11-25-01", "单删测试文章")
        self._seed_article_and_publish_record("2026-11-25-02", "批删测试文章一")
        self._seed_article_and_publish_record("2026-11-25-03", "批删测试文章二")

        # Single delete
        r_del = self.client.delete("/api/articles/2026-11-25-01")
        self.assertEqual(r_del.status_code, 200)
        d = r_del.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["deleted_articles"], 1)
        self.assertEqual(d["removed_files"], 3)
        self.assertFalse((db.get_data_dir() / "articles" / "2026-11-25-01").exists())
        self.assertFalse((db.get_data_dir() / "incoming" / "2026-11-25-01").exists())
        self.assertEqual(self.client.delete("/api/articles/2026-11-25-01").status_code, 404)

        # Batch delete selected dates
        r_batch = self.client.post("/api/articles/batch-delete", json={"dates": ["2026-11-25-02"]})
        self.assertEqual(r_batch.status_code, 200)
        b = r_batch.json()
        self.assertEqual(b["deleted_count"], 1)
        self.assertEqual(b["deleted_dates"], ["2026-11-25-02"])
        self.assertFalse((db.get_data_dir() / "articles" / "2026-11-25-02").exists())

        # Batch delete all remaining
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

        # Single delete publish record MUST cascade-delete article + disk images/cache
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

        # Batch delete selected IDs
        r_b1 = self.client.post("/api/publish-records/batch-delete", json={"ids": [id2]})
        self.assertEqual(r_b1.status_code, 200)
        b1 = r_b1.json()
        self.assertEqual(b1["deleted_records"], 1)
        self.assertEqual(b1["deleted_articles"], 1)
        self.assertFalse((db.get_data_dir() / "articles" / "2026-11-25-05").exists())
        self.assertEqual(self.client.get("/api/articles/2026-11-25-05").status_code, 404)

        # Batch delete all
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
