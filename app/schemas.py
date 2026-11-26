"""Pydantic request schemas for AI Media Debian backend."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)
    remember: bool = True


class NewsUpdate(BaseModel):
    status: str = Field(..., pattern="^(candidate|selected|ignored)$")


class DraftUploadRequest(BaseModel):
    simulate_failure: bool = False


class PublishRecordUpdate(BaseModel):
    status: str = Field(..., pattern="^(draft_ready|manual_published)$")
    published_url: str = ""
    notes: str = ""


class ArticleBatchDeleteRequest(BaseModel):
    dates: list[str] = Field(default_factory=list)
    all: bool = False


class PublishRecordBatchDeleteRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    all: bool = False


class SettingsUpdate(BaseModel):
    wechat_appid: str | None = None
    wechat_appsecret: str | None = None
    wechat_mock_mode: bool | None = None
    auto_backup_enabled: bool | None = None
    backup_retain_count: int | None = Field(default=None, ge=3, le=365)
    data_retain_days: int | None = Field(default=None, ge=1, le=365)
    draft_retry_enabled: bool | None = None
    draft_retry_max_attempts: int | None = Field(default=None, ge=1, le=20)
    site_name: str | None = None
