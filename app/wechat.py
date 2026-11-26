"""WeChat Official Account draft-only uploader.

HARD CONSTRAINT:
- STRICTLY draft-only (`draft/add`).
- NEVER calls any auto-publish or broadcast endpoint.
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
import time
from pathlib import Path
from typing import Any

import httpx

from .html_utils import (
    MAX_AUTHOR_CHARS,
    MAX_DIGEST_CHARS,
    MAX_TITLE_CHARS,
    sanitize_wechat_html,
    truncate_utf8_chars,
)

PUBLISH_MODE = "draft_only"
WECHAT_API_BASE = "https://api.weixin.qq.com/cgi-bin"
DEFAULT_AUTHOR = "资讯后台管理系统"

# Error codes that require refreshing the cached access_token
TOKEN_EXPIRED_ERRCODES = {40001, 40014, 42001}
INVALID_MEDIA_ID_ERRCODE = 40007

SUPPORTED_COVER_MIMES = {"image/png", "image/jpeg", "image/gif", "image/bmp"}

# In-memory access_token cache keyed by (appid, sha256(appsecret))
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_TOKEN_LOCK = asyncio.Lock()


def _mock_id(prefix: str, seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


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


def _parse_wechat_json(resp: httpx.Response, step: str) -> tuple[dict[str, Any] | None, str | None]:
    """Guard HTTP status and non-JSON responses from WeChat API."""
    if resp.status_code < 200 or resp.status_code >= 300:
        snippet = (resp.text or "")[:200].strip()
        return None, f"{step} HTTP 异常 ({resp.status_code}): {snippet}"
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        snippet = (resp.text or "")[:200].strip()
        return None, f"{step} 返回非 JSON 响应 ({exc}): {snippet}"
    if not isinstance(data, dict):
        return None, f"{step} 返回数据格式异常: {type(data).__name__}"
    return data, None


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
        token_data, parse_err = _parse_wechat_json(token_resp, "获取 access_token")
        if parse_err or token_data is None:
            _TOKEN_CACHE.pop(key, None)
            return None, {"errmsg": parse_err or "解析 access_token 响应失败"}

        access_token = token_data.get("access_token")
        if not access_token:
            _TOKEN_CACHE.pop(key, None)
            return None, token_data

        expires_in = int(token_data.get("expires_in") or 7200)
        ttl = max(60, expires_in - 300)
        _TOKEN_CACHE[key] = (str(access_token), time.monotonic() + ttl)
        return str(access_token), {"cached": False, "expires_in": expires_in}


def _guess_image_mime(cover_path: Path) -> str:
    suffix = cover_path.suffix.lower()
    if suffix == ".webp":
        raise ValueError("微信公众号封面素材不支持 WebP 格式，请使用 PNG/JPEG/GIF")
    mime, _ = mimetypes.guess_type(str(cover_path))
    if mime == "image/webp":
        raise ValueError("微信公众号封面素材不支持 WebP 格式，请使用 PNG/JPEG/GIF")
    if mime in SUPPORTED_COVER_MIMES:
        return mime
    return "image/png"


async def _upload_cover_material(
    client: httpx.AsyncClient,
    *,
    access_token: str,
    appid: str,
    appsecret: str,
    cover_path: Path,
    cover_mime: str,
) -> tuple[str | None, str, dict[str, Any], str | None]:
    """Upload permanent cover image material, refreshing token once if expired.

    Returns (thumb_media_id, active_access_token, raw_data, error_message).
    """
    cover_bytes = cover_path.read_bytes()
    media_data: dict[str, Any] = {}
    token = access_token
    for attempt in range(2):
        media_resp = await client.post(
            f"{WECHAT_API_BASE}/material/add_material",
            params={"access_token": token, "type": "image"},
            files={"media": (cover_path.name, cover_bytes, cover_mime)},
        )
        parsed, parse_err = _parse_wechat_json(media_resp, "上传封面素材")
        if parse_err or parsed is None:
            return None, token, {"errmsg": parse_err}, parse_err or "上传封面素材响应异常"
        media_data = parsed
        if media_data.get("errcode") in TOKEN_EXPIRED_ERRCODES and attempt == 0:
            invalidate_access_token_cache(appid)
            new_token, token_info = await _get_access_token(client, appid, appsecret, force_refresh=True)
            if not new_token:
                err = f"刷新 access_token 失败: {token_info.get('errmsg', token_info)}"
                return None, token, token_info, err
            token = new_token
            continue
        break

    thumb_media_id = media_data.get("media_id")
    if not thumb_media_id:
        err = f"上传封面素材失败: {media_data.get('errmsg', media_data)}"
        return None, token, media_data, err
    return str(thumb_media_id), token, media_data, None


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
    existing_thumb_media_id: str = "",
) -> dict[str, Any]:
    """Upload cover image and sanitized article HTML into WeChat Official Account draft box ONLY.

    Never publishes or sends to followers.
    """
    use_mock = mock_mode or not appid.strip() or not appsecret.strip()
    mode_label = "mock" if use_mock else "real"
    reusable_thumb = (existing_thumb_media_id or "").strip()
    if reusable_thumb.startswith("mock_"):
        reusable_thumb = ""

    if not reusable_thumb or use_mock:
        if not cover_path.exists():
            return {
                "ok": False,
                "mode": mode_label,
                "publish_mode": PUBLISH_MODE,
                "error": f"封面图片不存在: {cover_path.name}",
            }
        try:
            cover_mime = _guess_image_mime(cover_path)
        except ValueError as exc:
            return {
                "ok": False,
                "mode": mode_label,
                "publish_mode": PUBLISH_MODE,
                "error": str(exc),
            }
    else:
        cover_mime = "image/png"
        if cover_path.exists():
            try:
                cover_mime = _guess_image_mime(cover_path)
            except ValueError as exc:
                return {
                    "ok": False,
                    "mode": mode_label,
                    "publish_mode": PUBLISH_MODE,
                    "error": str(exc),
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
    safe_author = truncate_utf8_chars(author or DEFAULT_AUTHOR, MAX_AUTHOR_CHARS)
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

            # 1. Reuse existing thumb_media_id on retry when provided, otherwise upload cover
            reused_thumb = bool(reusable_thumb)
            if reusable_thumb:
                thumb_media_id = reusable_thumb
            else:
                thumb_media_id, access_token, media_data, upload_err = await _upload_cover_material(
                    client,
                    access_token=access_token,
                    appid=appid,
                    appsecret=appsecret,
                    cover_path=cover_path,
                    cover_mime=cover_mime,
                )
                if upload_err or not thumb_media_id:
                    return {
                        "ok": False,
                        "mode": "real",
                        "publish_mode": PUBLISH_MODE,
                        "error": upload_err or "上传封面素材失败",
                        "response": media_data,
                    }

            # 2. Create draft in WeChat Official Account draft box (draft/add ONLY)
            async def _submit_draft(current_token: str, current_thumb: str) -> tuple[dict[str, Any] | None, str, str | None]:
                draft_payload = {
                    "articles": [
                        {
                            "title": safe_title,
                            "author": safe_author,
                            "digest": safe_digest,
                            "content": sanitized_html,
                            "thumb_media_id": current_thumb,
                            "need_open_comment": 1,
                            "only_fans_can_comment": 0,
                        }
                    ]
                }
                payload_bytes = json.dumps(draft_payload, ensure_ascii=False).encode("utf-8")
                tok = current_token
                parsed_data: dict[str, Any] = {}
                for attempt in range(2):
                    draft_resp = await client.post(
                        f"{WECHAT_API_BASE}/draft/add",
                        params={"access_token": tok},
                        content=payload_bytes,
                        headers={"Content-Type": "application/json; charset=utf-8"},
                    )
                    parsed, parse_err = _parse_wechat_json(draft_resp, "创建公众号草稿")
                    if parse_err or parsed is None:
                        return None, tok, parse_err or "创建公众号草稿响应异常"
                    parsed_data = parsed
                    if parsed_data.get("errcode") in TOKEN_EXPIRED_ERRCODES and attempt == 0:
                        invalidate_access_token_cache(appid)
                        refreshed, tok_info = await _get_access_token(
                            client, appid, appsecret, force_refresh=True
                        )
                        if not refreshed:
                            return tok_info, tok, f"刷新 access_token 失败: {tok_info.get('errmsg', tok_info)}"
                        tok = refreshed
                        continue
                    break
                return parsed_data, tok, None

            draft_data, access_token, draft_err = await _submit_draft(access_token, thumb_media_id)
            if draft_err:
                return {
                    "ok": False,
                    "mode": "real",
                    "publish_mode": PUBLISH_MODE,
                    "thumb_media_id": thumb_media_id,
                    "error": draft_err,
                    "response": draft_data or {},
                }

            # If reused thumb_media_id was rejected by WeChat (40007), re-upload cover once
            if (
                reused_thumb
                and isinstance(draft_data, dict)
                and draft_data.get("errcode") == INVALID_MEDIA_ID_ERRCODE
                and cover_path.exists()
            ):
                fresh_thumb, access_token, media_data, upload_err = await _upload_cover_material(
                    client,
                    access_token=access_token,
                    appid=appid,
                    appsecret=appsecret,
                    cover_path=cover_path,
                    cover_mime=cover_mime,
                )
                if upload_err or not fresh_thumb:
                    return {
                        "ok": False,
                        "mode": "real",
                        "publish_mode": PUBLISH_MODE,
                        "error": upload_err or "重新上传封面素材失败",
                        "response": media_data,
                    }
                thumb_media_id = fresh_thumb
                draft_data, access_token, draft_err = await _submit_draft(access_token, thumb_media_id)
                if draft_err:
                    return {
                        "ok": False,
                        "mode": "real",
                        "publish_mode": PUBLISH_MODE,
                        "thumb_media_id": thumb_media_id,
                        "error": draft_err,
                        "response": draft_data or {},
                    }

            draft_media_id = (draft_data or {}).get("media_id")
            if not draft_media_id:
                return {
                    "ok": False,
                    "mode": "real",
                    "publish_mode": PUBLISH_MODE,
                    "thumb_media_id": thumb_media_id,
                    "error": f"创建公众号草稿失败: {(draft_data or {}).get('errmsg', draft_data)}",
                    "response": draft_data or {},
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
