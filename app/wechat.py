"""WeChat Official Account draft-only uploader.

HARD CONSTRAINT:
- STRICTLY draft-only (`draft/add`).
- NEVER calls any auto-publish, freepublish, or mass-send endpoint.
- Enforces official WeChat draft field limits (title <= 64, author <= 8, digest <= 120 chars).
- Caches access_token with expiry and auto-refreshes on WeChat error codes 40001 / 42001 / 40014.
- Sanitizes article HTML before uploading to WeChat draft box.
- Supports realistic dry-run / mock mode when AppID/AppSecret are not configured or mock_mode is enabled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

import httpx

PUBLISH_MODE = "draft_only"
WECHAT_API_BASE = "https://api.weixin.qq.com/cgi-bin"

# Official WeChat draft field character limits
MAX_TITLE_CHARS = 64
MAX_AUTHOR_CHARS = 8
MAX_DIGEST_CHARS = 120

# Error codes that require refreshing the cached access_token
TOKEN_EXPIRED_ERRCODES = {40001, 40014, 42001}

# In-memory access_token cache keyed by (appid, sha256(appsecret))
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_TOKEN_LOCK = asyncio.Lock()

# HTML sanitization patterns (strip unsafe tags, inline handlers, javascript: URLs)
_DANGEROUS_BLOCK_RE = re.compile(
    r"<\s*(script|iframe|object|embed|form|style|svg|base|meta|link)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DANGEROUS_SINGLE_RE = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|form|style|svg|base|meta|link)\b[^>]*>",
    re.IGNORECASE,
)
_EVENT_HANDLER_RE = re.compile(
    r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)
_JS_URI_ATTR_RE = re.compile(
    r"""(\b(?:href|src|action|xlink:href)\s*=\s*)(["']?)\s*(?:javascript|vbscript|data\s*:\s*text/html)\s*:[^"'\s>]*\2""",
    re.IGNORECASE,
)


def _mock_id(prefix: str, seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def truncate_utf8_chars(text: str, max_chars: int) -> str:
    """Safely normalize whitespace and truncate to `max_chars` Unicode characters (UTF-8 clean)."""
    cleaned = re.sub(r"[\r\n\t]+", " ", (text or "")).strip()
    if max_chars <= 0:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars == 1:
        return cleaned[:1]
    return cleaned[: max_chars - 1].rstrip() + "…"


def sanitize_wechat_html(html_content: str) -> str:
    """Remove unsafe HTML tags/attributes while preserving WeChat-friendly inline formatting."""
    if not html_content:
        return ""
    out = _DANGEROUS_BLOCK_RE.sub("", html_content)
    out = _DANGEROUS_SINGLE_RE.sub("", out)
    out = _EVENT_HANDLER_RE.sub("", out)
    out = _JS_URI_ATTR_RE.sub(r'\1"#blocked"', out)
    return out.strip()


def _cache_key(appid: str, appsecret: str) -> tuple[str, str]:
    secret_hash = hashlib.sha256(appsecret.encode("utf-8")).hexdigest()
    return (appid.strip(), secret_hash)


def invalidate_access_token_cache(appid: str | None = None) -> None:
    """Clear cached access_token(s), e.g., when credentials are updated or token expires."""
    if appid is None:
        _TOKEN_CACHE.clear()
        return
    clean = appid.strip()
    for key in list(_TOKEN_CACHE.keys()):
        if key[0] == clean:
            _TOKEN_CACHE.pop(key, None)


def get_token_cache_status(appid: str = "") -> dict[str, Any]:
    now = time.monotonic()
    clean = appid.strip()
    active = []
    for (k_appid, _), (_, exp) in _TOKEN_CACHE.items():
        if exp > now and (not clean or k_appid == clean):
            active.append({"appid": k_appid, "ttl_seconds": int(exp - now)})
    return {"cached_tokens": len(active), "entries": active}


async def _get_access_token(
    client: httpx.AsyncClient,
    appid: str,
    appsecret: str,
    *,
    force_refresh: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    """Return cached access_token if valid, otherwise fetch a fresh token from WeChat API."""
    key = _cache_key(appid, appsecret)
    now = time.monotonic()
    if not force_refresh:
        cached = _TOKEN_CACHE.get(key)
        if cached and cached[1] > now:
            return cached[0], {"cached": True, "ttl_seconds": int(cached[1] - now)}

    async with _TOKEN_LOCK:
        now = time.monotonic()
        if not force_refresh:
            cached = _TOKEN_CACHE.get(key)
            if cached and cached[1] > now:
                return cached[0], {"cached": True, "ttl_seconds": int(cached[1] - now)}

        token_resp = await client.get(
            f"{WECHAT_API_BASE}/token",
            params={
                "grant_type": "client_credential",
                "appid": appid.strip(),
                "secret": appsecret.strip(),
            },
        )
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            _TOKEN_CACHE.pop(key, None)
            return None, token_data

        expires_in = int(token_data.get("expires_in") or 7200)
        # Safety margin of 300s (minimum 60s TTL) to avoid edge-of-expiry failures
        ttl = max(60, expires_in - 300)
        _TOKEN_CACHE[key] = (str(access_token), time.monotonic() + ttl)
        return str(access_token), {"cached": False, "expires_in": expires_in}


def _guess_image_mime(cover_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(cover_path))
    if mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        return mime
    return "image/png"


async def upload_wechat_draft(
    *,
    article_date: str,
    title: str,
    author: str,
    digest: str,
    html_content: str,
    cover_path: Path,
    appid: str,
    appsecret: str,
    mock_mode: bool,
    simulate_failure: bool = False,
) -> dict[str, Any]:
    """Upload cover image and sanitized article HTML into WeChat Official Account draft box ONLY.

    Never publishes or sends to followers.
    """
    use_mock = mock_mode or not appid.strip() or not appsecret.strip()
    mode_label = "mock" if use_mock else "real"

    if not cover_path.exists():
        return {
            "ok": False,
            "mode": mode_label,
            "publish_mode": PUBLISH_MODE,
            "error": f"封面图片不存在: {cover_path.name}",
        }

    sanitized_html = sanitize_wechat_html(html_content)
    if not sanitized_html:
        return {
            "ok": False,
            "mode": mode_label,
            "publish_mode": PUBLISH_MODE,
            "error": "文章 HTML 内容为空（或清洗后无有效内容），无法上传草稿箱",
        }

    safe_title = truncate_utf8_chars(title, MAX_TITLE_CHARS)
    safe_author = truncate_utf8_chars(author or "AI Media", MAX_AUTHOR_CHARS)
    safe_digest = truncate_utf8_chars(digest or title, MAX_DIGEST_CHARS)

    if use_mock:
        if simulate_failure:
            return {
                "ok": False,
                "mode": "mock",
                "publish_mode": PUBLISH_MODE,
                "error": "模拟上传失败（任务已保留在自动重试队列中）",
                "response": {"mock": True, "simulated_failure": True},
            }

        cover_bytes = cover_path.read_bytes()
        thumb_id = _mock_id("mock_thumb", f"{article_date}:{len(cover_bytes)}:{safe_title}")
        draft_id = _mock_id("mock_draft", f"{article_date}:{safe_title}:{len(sanitized_html)}")
        reason = "已开启模拟模式" if mock_mode else "尚未配置公众号 AppID/AppSecret，自动降级为模拟草稿上传"
        return {
            "ok": True,
            "mode": "mock",
            "publish_mode": PUBLISH_MODE,
            "thumb_media_id": thumb_id,
            "media_id": draft_id,
            "message": f"模拟上传至微信公众号草稿箱成功（{reason}，严格仅草稿箱模式，不会自动群发）",
            "response": {
                "mock": True,
                "publish_mode": PUBLISH_MODE,
                "thumb_media_id": thumb_id,
                "media_id": draft_id,
                "title": safe_title,
                "author": safe_author,
                "digest": safe_digest,
                "cover_bytes": len(cover_bytes),
                "html_bytes": len(sanitized_html.encode("utf-8")),
                "sanitized": True,
            },
        }

    # Real WeChat Official Account API (strictly draft-only: token -> add_material -> draft/add)
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            access_token, token_info = await _get_access_token(client, appid, appsecret, force_refresh=False)
            if not access_token:
                return {
                    "ok": False,
                    "mode": "real",
                    "publish_mode": PUBLISH_MODE,
                    "error": f"获取 access_token 失败: {token_info.get('errmsg', token_info)}",
                    "response": token_info,
                }

            # 1. Upload permanent cover image material (retry once if token expired: 40001/42001/40014)
            cover_bytes = cover_path.read_bytes()
            cover_mime = _guess_image_mime(cover_path)
            media_data: dict[str, Any] = {}
            for attempt in range(2):
                media_resp = await client.post(
                    f"{WECHAT_API_BASE}/material/add_material",
                    params={"access_token": access_token, "type": "image"},
                    files={"media": (cover_path.name, cover_bytes, cover_mime)},
                )
                media_data = media_resp.json()
                if media_data.get("errcode") in TOKEN_EXPIRED_ERRCODES and attempt == 0:
                    invalidate_access_token_cache(appid)
                    access_token, token_info = await _get_access_token(
                        client, appid, appsecret, force_refresh=True
                    )
                    if not access_token:
                        return {
                            "ok": False,
                            "mode": "real",
                            "publish_mode": PUBLISH_MODE,
                            "error": f"刷新 access_token 失败: {token_info.get('errmsg', token_info)}",
                            "response": token_info,
                        }
                    continue
                break

            thumb_media_id = media_data.get("media_id")
            if not thumb_media_id:
                return {
                    "ok": False,
                    "mode": "real",
                    "publish_mode": PUBLISH_MODE,
                    "error": f"上传封面素材失败: {media_data.get('errmsg', media_data)}",
                    "response": media_data,
                }

            # 2. Create draft in WeChat Official Account draft box (draft/add ONLY)
            draft_payload = {
                "articles": [
                    {
                        "title": safe_title,
                        "author": safe_author,
                        "digest": safe_digest,
                        "content": sanitized_html,
                        "thumb_media_id": thumb_media_id,
                        "need_open_comment": 1,
                        "only_fans_can_comment": 0,
                    }
                ]
            }
            payload_bytes = json.dumps(draft_payload, ensure_ascii=False).encode("utf-8")
            draft_data: dict[str, Any] = {}
            for attempt in range(2):
                draft_resp = await client.post(
                    f"{WECHAT_API_BASE}/draft/add",
                    params={"access_token": access_token},
                    content=payload_bytes,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                draft_data = draft_resp.json()
                if draft_data.get("errcode") in TOKEN_EXPIRED_ERRCODES and attempt == 0:
                    invalidate_access_token_cache(appid)
                    access_token, token_info = await _get_access_token(
                        client, appid, appsecret, force_refresh=True
                    )
                    if not access_token:
                        return {
                            "ok": False,
                            "mode": "real",
                            "publish_mode": PUBLISH_MODE,
                            "thumb_media_id": thumb_media_id,
                            "error": f"刷新 access_token 失败: {token_info.get('errmsg', token_info)}",
                            "response": token_info,
                        }
                    continue
                break

            draft_media_id = draft_data.get("media_id")
            if not draft_media_id:
                return {
                    "ok": False,
                    "mode": "real",
                    "publish_mode": PUBLISH_MODE,
                    "thumb_media_id": thumb_media_id,
                    "error": f"创建公众号草稿失败: {draft_data.get('errmsg', draft_data)}",
                    "response": draft_data,
                }

            return {
                "ok": True,
                "mode": "real",
                "publish_mode": PUBLISH_MODE,
                "thumb_media_id": thumb_media_id,
                "media_id": draft_media_id,
                "message": "已成功上传至微信公众号草稿箱（严格仅草稿箱模式，请登录公众号后台人工审核发布）",
                "response": draft_data,
            }
    except Exception as exc:
        return {
            "ok": False,
            "mode": "real",
            "publish_mode": PUBLISH_MODE,
            "error": f"调用微信草稿接口异常: {exc}",
            "response": {"exception": str(exc)},
        }
