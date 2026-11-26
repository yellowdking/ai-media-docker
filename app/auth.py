"""Authentication, session cookies, localhost checks, and login rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import struct
import time
from collections import deque
from pathlib import Path
from typing import Any

try:
    from fastapi import HTTPException, Request
except ImportError:  # pragma: no cover
    HTTPException = None  # type: ignore[assignment]
    Request = Any  # type: ignore[assignment,misc]

logger = logging.getLogger("ai_media.auth")

SESSION_COOKIE_NAME = "ai_media_session"
SESSION_TTL_REMEMBER = 7 * 86400
SESSION_TTL_DEFAULT = 24 * 3600

AUTH_EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
    }
)
AUTH_EXEMPT_PREFIXES = ("/static/",)

# Sliding-window login brute-force protection: max 10 failures per 5 minutes per IP
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 10
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300

_CACHED_DOCKER_GATEWAY: str | None = None
_LOGIN_FAILURES: dict[str, deque[float]] = {}


def get_docker_bridge_gateway() -> str | None:
    """Detect the container's default Docker bridge gateway from /proc/net/route."""
    global _CACHED_DOCKER_GATEWAY
    if _CACHED_DOCKER_GATEWAY is not None:
        return _CACHED_DOCKER_GATEWAY or None

    route_file = Path("/proc/net/route")
    if not route_file.exists():
        _CACHED_DOCKER_GATEWAY = ""
        return None
    try:
        for line in route_file.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            dest, gateway, flags = parts[1], parts[2], int(parts[3], 16)
            if dest == "00000000" and (flags & 2):
                gw_ip = socket.inet_ntoa(struct.pack("<L", int(gateway, 16)))
                _CACHED_DOCKER_GATEWAY = gw_ip
                return gw_ip
    except Exception:
        pass
    _CACHED_DOCKER_GATEWAY = ""
    return None


def is_loopback_host_header(host_header: str) -> bool:
    raw = (host_header or "").strip().lower()
    if not raw:
        return False
    if raw.startswith("[::1]"):
        rest = raw[5:]
        return rest == "" or (rest.startswith(":") and rest[1:].isdigit())
    hostname = raw.split(":", 1)[0]
    return hostname in ("127.0.0.1", "localhost")


def is_localhost_request(request: Request) -> bool:
    """Return True only for direct container/host loopback requests (never proxied)."""
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return False
    client_ip = (request.client.host if request.client else "").strip()
    if client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return True
    gw_ip = get_docker_bridge_gateway()
    host_hdr = request.headers.get("host", "")
    return bool(gw_ip and client_ip == gw_ip and is_loopback_host_header(host_hdr))


def require_localhost(request: Request) -> None:
    """Enforce localhost-only access for internal ingest endpoint."""
    if is_localhost_request(request):
        return
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        raise HTTPException(
            status_code=403,
            detail="禁止通过代理访问内部入库接口（仅允许本机 localhost / 127.0.0.1）",
        )
    client_ip = (request.client.host if request.client else "").strip()
    host_hdr = request.headers.get("host", "")
    logger.warning("Rejected non-localhost internal ingest attempt from %s (Host=%s)", client_ip, host_hdr)
    raise HTTPException(
        status_code=403,
        detail="禁止非本机访问内部入库接口（仅允许 localhost / 127.0.0.1）",
    )


def get_admin_credentials() -> tuple[str, str]:
    admin_user = os.environ.get("ADMIN_USER", "admin").strip() or "admin"
    admin_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    return admin_user, admin_password


def get_session_signing_key() -> bytes:
    _, admin_password = get_admin_credentials()
    session_secret = os.environ.get("SESSION_SECRET", "").strip()
    material = f"ai-media-session-v1:{session_secret}:{admin_password}".encode("utf-8")
    return hashlib.sha256(material).digest()


def create_session_token(username: str, ttl_seconds: int) -> str:
    payload = {
        "u": username,
        "exp": int(time.time()) + max(60, int(ttl_seconds)),
        "n": secrets.token_hex(8),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    sig = hmac.new(get_session_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_session_token(token: str, expected_username: str) -> bool:
    if not token or "." not in token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    payload_b64, sig = parts
    expected_sig = hmac.new(
        get_session_signing_key(), payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not secrets.compare_digest(sig, expected_sig):
        return False
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict):
            return False
        user = str(payload.get("u") or "")
        exp = int(payload.get("exp") or 0)
        if exp <= int(time.time()):
            return False
        return secrets.compare_digest(user, expected_username)
    except Exception:
        return False


def verify_basic_auth(auth_header: str, admin_user: str, admin_password: str) -> bool:
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
        user, sep, pwd = decoded.partition(":")
        if not sep:
            return False
        user_ok = secrets.compare_digest(user, admin_user)
        pwd_ok = secrets.compare_digest(pwd, admin_password)
        return user_ok and pwd_ok
    except Exception:
        return False


def is_https_request(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def is_request_authenticated(request: Request) -> bool:
    admin_user, admin_password = get_admin_credentials()
    if not admin_password:
        return True
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if verify_session_token(cookie_token, admin_user):
        return True
    auth_header = request.headers.get("authorization", "")
    if verify_basic_auth(auth_header, admin_user, admin_password):
        return True
    return False


def get_client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    xri = (request.headers.get("x-real-ip") or "").strip()
    if xri:
        return xri
    return (request.client.host if request.client else "").strip() or "unknown"


def _prune_ip_bucket(ip: str, now_ts: float) -> deque[float]:
    cutoff = now_ts - LOGIN_RATE_LIMIT_WINDOW_SECONDS
    bucket = _LOGIN_FAILURES.get(ip)
    if bucket is None:
        return deque()
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if not bucket:
        _LOGIN_FAILURES.pop(ip, None)
    return bucket


def _evict_stale_rate_limit_entries(now_ts: float) -> None:
    if len(_LOGIN_FAILURES) <= 512:
        return
    cutoff = now_ts - LOGIN_RATE_LIMIT_WINDOW_SECONDS
    for ip_key in list(_LOGIN_FAILURES.keys()):
        bucket = _LOGIN_FAILURES.get(ip_key)
        if not bucket:
            _LOGIN_FAILURES.pop(ip_key, None)
            continue
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            _LOGIN_FAILURES.pop(ip_key, None)


def check_login_rate_limit(ip: str, now_ts: float | None = None) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds) for login attempts from `ip`."""
    ts = time.monotonic() if now_ts is None else now_ts
    bucket = _prune_ip_bucket(ip, ts)
    if len(bucket) >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS:
        retry_after = max(1, int(LOGIN_RATE_LIMIT_WINDOW_SECONDS - (ts - bucket[0])) + 1)
        return False, retry_after
    return True, 0


def record_login_failure(ip: str, now_ts: float | None = None) -> int:
    ts = time.monotonic() if now_ts is None else now_ts
    _evict_stale_rate_limit_entries(ts)
    bucket = _prune_ip_bucket(ip, ts)
    bucket.append(ts)
    _LOGIN_FAILURES[ip] = bucket
    return len(bucket)


def clear_login_failures(ip: str) -> None:
    _LOGIN_FAILURES.pop(ip, None)


def reset_login_rate_limiter() -> None:
    _LOGIN_FAILURES.clear()
