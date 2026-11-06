from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"


class BusyBlock(BaseModel):
    start: datetime
    end: datetime
    all_day: bool = False


class FreeBusyResponse(BaseModel):
    mailbox: str
    start: datetime
    end: datetime
    busy: list[BusyBlock]


class CalendarEvent(BaseModel):
    start: datetime
    end: datetime
    all_day: bool = False
    subject: str | None = Field(default=None, description="Only populated when explicitly enabled.")


class UpcomingResponse(BaseModel):
    mailbox: str
    start: datetime
    end: datetime
    events: list[CalendarEvent]


class MailMessageSummary(BaseModel):
    id: str
    changekey: str | None = None
    source_key: str | None = None
    conversation_key: str | None = None
    message_ref: str | None = None
    folder: str = "inbox"
    subject: str | None = None
    sender_name: str | None = None
    sender_email: str | None = None
    to_recipients: list[str] = Field(default_factory=list)
    cc_recipients: list[str] = Field(default_factory=list)
    received_at: datetime | None = None
    sent_at: datetime | None = None
    is_read: bool | None = None
    has_attachments: bool | None = None
    conversation_id: str | None = None
    conversation_topic: str | None = None
    importance: str | None = None
    categories: list[str] = Field(default_factory=list)


class InboxResponse(BaseModel):
    mailbox: str
    folder: str = "inbox"
    messages: list[MailMessageSummary]


class MailMessageDetail(MailMessageSummary):
    body_text: str | None = None
    body_truncated: bool = False


class MailIntakeMessage(MailMessageDetail):
    needs_attention_hint: bool = False


class MailIntakeResponse(BaseModel):
    mailbox: str
    folder: str = "inbox"
    start: datetime | None = None
    end: datetime
    scanned: int
    body_enabled: bool
    body_max_chars: int
    page_offset: int = 0
    has_more: bool = False
    next_cursor: str | None = None
    saturated: bool = False
    messages: list[MailIntakeMessage]


class GoogleMirrorResponse(BaseModel):
    status: str
    start: datetime
    end: datetime
    scanned: int
    has_more: bool = False
    snapshot_complete: bool = True
    created: int
    updated: int
    unchanged: int
    deleted: int = 0
    dry_run: bool = False


class GoogleMirrorEvent(BaseModel):
    source_key: str
    fingerprint: str
    google_event: dict[str, Any]
    cancelled: bool = False
    cancellation_reason: Literal["source_cancelled", "missing_from_complete_snapshot"] | None = None


class GoogleMirrorEventsResponse(BaseModel):
    status: str
    mailbox: str
    start: datetime
    end: datetime
    scanned: int
    returned: int
    has_more: bool = False
    snapshot_complete: bool = True
    reconciliation_enabled: bool = False
    reconciliation_key: str | None = None
    tombstones: int = 0
    events: list[GoogleMirrorEvent]
