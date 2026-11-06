from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ews_endpoint: str = "https://mail.example.com/EWS/Exchange.asmx"
    ews_server: str = "mail.example.com"
    ews_email: str = "user@example.com"
    ews_username: str = "DOMAIN\\username"
    ews_password: SecretStr
    ews_auth_type: Literal["ntlm", "basic"] = "ntlm"
    ews_max_window_days: int = Field(default=31, ge=1, le=90)
    ews_max_items: int = Field(default=100, ge=1, le=500)
    ews_allow_event_subjects: bool = False
    ews_mail_body_enabled: bool = False
    ews_mail_body_max_chars: int = Field(default=4000, ge=500, le=20000)
    ews_mail_attachments_enabled: bool = False
    ews_mail_attachment_max_bytes: int = Field(default=10_485_760, ge=1024, le=20_971_520)
    ews_shared_mailbox: str = ""
    ews_mail_read_roots: str = "inbox,sent"
    ews_calendar_context_enabled: bool = False
    ews_extended_reads_enabled: bool = False
    ews_write_api_token: SecretStr | None = None
    ews_enabled_write_groups: str = ""

    @field_validator("ews_write_api_token", mode="before")
    @classmethod
    def validate_write_token(cls, value: str | SecretStr | None) -> str | SecretStr | None:
        if value == "" or value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if len(raw) < 32:
            raise ValueError("write token must contain at least 32 characters")
        return value

    @field_validator("ews_enabled_write_groups")
    @classmethod
    def validate_write_groups(cls, value: str) -> str:
        allowed = {"calendar", "mail", "send", "folders", "contacts", "tasks", "settings", "subscriptions"}
        groups = value.split(",") if value else []
        if len(groups) != len(set(groups)) or set(groups) - allowed:
            raise ValueError("invalid write groups")
        return value

    @field_validator("ews_mail_read_roots")
    @classmethod
    def validate_mail_roots(cls, value: str) -> str:
        roots = value.split(",")
        if (
            not roots
            or len(set(roots)) != len(roots)
            or set(roots) - {"inbox", "sent", "drafts", "deleted", "junk", "archive", "mail"}
        ):
            raise ValueError("invalid configured mail roots")
        return value

    bridge_api_token: SecretStr = Field(min_length=16)
    bridge_log_level: str = "info"

    data_dir: Path = Path("/data")
    google_calendar_enabled: bool = False
    google_service_account_file: Path = Path("/run/secrets/exchange-ews-bridge/google-service-account.json")
    google_calendar_id: str = ""
    google_mirror_days: int = Field(default=14, ge=1, le=90)
    google_mirror_limit: int = Field(default=200, ge=1, le=500)
    google_mirror_summary_mode: Literal["busy", "generic", "subject"] = "generic"
    google_mirror_generic_summary: str = "Work meeting"
    google_mirror_include_location: bool = False
    google_mirror_include_description: bool = False
    google_mirror_description_max_chars: int = Field(default=4000, ge=500, le=20000)
    google_mirror_include_attendees: bool = False
    google_mirror_delete_missing: bool = False

    @field_validator("ews_endpoint")
    @classmethod
    def validate_https_endpoint(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("EWS endpoint must use HTTPS")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
