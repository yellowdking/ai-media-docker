"""Authentication routes (login with rate limiting, logout, session check)."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ..auth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_DEFAULT,
    SESSION_TTL_REMEMBER,
    check_login_rate_limit,
    clear_login_failures,
    create_session_token,
    get_admin_credentials,
    get_client_ip,
    is_https_request,
    is_request_authenticated,
    record_login_failure,
)
from ..db import add_task_log, get_conn, today_str
from ..schemas import LoginRequest
from ..utils import is_wechat_configured

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def auth_login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    admin_user, admin_password = get_admin_credentials()
    if not admin_password:
        return {
            "ok": True,
            "username": admin_user,
            "wechat_configured": is_wechat_configured(),
        }

    client_ip = get_client_ip(request)
    allowed, retry_after = check_login_rate_limit(client_ip)
    if not allowed:
        with get_conn() as conn:
            add_task_log(
                conn,
                date_str=today_str(),
                stage="auth",
                status="warning",
                level="WARN",
                message=f"登录失败次数过多，已触发临时限流（IP: {client_ip}，{retry_after} 秒后重试）",
                details={"ip": client_ip, "retry_after": retry_after},
            )
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )

    user_ok = secrets.compare_digest(payload.username.strip(), admin_user)
    pwd_ok = secrets.compare_digest(payload.password, admin_password)
    if not (user_ok and pwd_ok):
        fail_count = record_login_failure(client_ip)
        with get_conn() as conn:
            add_task_log(
                conn,
                date_str=today_str(),
                stage="auth",
                status="warning",
                level="WARN",
                message=f"后台登录校验失败（IP: {client_ip}，5 分钟内第 {fail_count} 次）",
                details={"ip": client_ip, "failures": fail_count},
            )
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    clear_login_failures(client_ip)
    ttl = SESSION_TTL_REMEMBER if payload.remember else SESSION_TTL_DEFAULT
    token = create_session_token(admin_user, ttl)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
        path="/",
    )
    return {
        "ok": True,
        "username": admin_user,
        "wechat_configured": is_wechat_configured(),
    }


@router.post("/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
    )
    return {"ok": True}


@router.get("/me")
async def auth_me(request: Request) -> dict[str, Any]:
    admin_user, admin_password = get_admin_credentials()
    auth_enabled = bool(admin_password)
    authenticated = is_request_authenticated(request)
    return {
        "authenticated": authenticated,
        "auth_enabled": auth_enabled,
        "username": admin_user if authenticated else "",
        "wechat_configured": is_wechat_configured() if authenticated else False,
    }
