"""Unit tests for AI Media Docker server (db, 7-day cleanup, wechat draft_only guards)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app import db, wechat


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

    def test_backup_creation_and_pruning(self) -> None:
        bk = db.create_sqlite_backup(trigger="manual")
        self.assertTrue(bk["ok"])
        self.assertTrue((self.tmp / bk["rel_path"]).exists())

    def test_wechat_draft_only_and_html_sanitization(self) -> None:
        self.assertEqual(wechat.PUBLISH_MODE, "draft_only")
        dirty = '<section><script>alert(1)</script><p onclick="evil()">正文</p><iframe src="x"></iframe></section>'
        clean = wechat.sanitize_wechat_html(dirty)
        self.assertNotIn("<script", clean.lower())
        self.assertNotIn("<iframe", clean.lower())
        self.assertNotIn("onclick", clean.lower())
        self.assertIn("正文", clean)


if __name__ == "__main__":
    unittest.main()
