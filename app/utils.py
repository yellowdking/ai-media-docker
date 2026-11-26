"""Shared validation, hashing, and secret-masking helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

try:
    from fastapi import HTTPException
except ImportError:  # pragma: no cover
    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

from .db import get_conn, get_settings_dict

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-\d{2})?$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
REQUIRED_ARTICLE_FILES = (
    "article.md",
    "article.html",
    "cover.png",
    "sources.json",
    "review.json",
    "metadata.json",
)
OPTIONAL_INGEST_FILES = ("filtered.json", "state.json", "manifest.json")


def validate_date(date_str: str) -> str:
    if not DATE_RE.match(date_str):
        raise HTTPException(
            status_code=400,
            detail=f"无效的日期格式（需要 YYYY-MM-DD 或 YYYY-MM-DD-NN）: {date_str}",
        )
    try:
        datetime.strptime(date_str[:10], "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"无效的日历日期: {date_str}") from exc
    if len(date_str) == 13 and not (1 <= int(date_str[11:]) <= 99):
        raise HTTPException(status_code=400, detail=f"无效的文章序号（需要 01-99）: {date_str}")
    return date_str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def mask_secret(secret: str) -> str:
    clean = (secret or "").strip()
    if not clean:
        return ""
    if len(clean) < 6:
        return "****"
    return f"****{clean[-4:]}"


def is_masked_secret(value: str, current_secret: str = "") -> bool:
    clean = (value or "").strip()
    if not clean:
        return True
    if clean.startswith("****") or set(clean) == {"*"}:
        return True
    if current_secret and clean == mask_secret(current_secret):
        return True
    return False


def is_wechat_configured_from_cfg(cfg: dict[str, str]) -> bool:
    return bool(cfg.get("wechat_appid", "").strip() and cfg.get("wechat_appsecret", "").strip())


def is_wechat_configured() -> bool:
    with get_conn() as conn:
        cfg = get_settings_dict(conn)
    return is_wechat_configured_from_cfg(cfg)
