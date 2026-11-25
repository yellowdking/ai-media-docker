"""Unit tests for AI Media Docker server (db, auth cookies, cleanup, config, wechat draft_only)."""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import db

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


if __name__ == "__main__":
    unittest.main()
