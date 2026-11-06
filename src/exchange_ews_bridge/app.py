from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from exchangelib.errors import UnauthorizedError
from exchangelib.items import CalendarItem
from fastapi import Depends, FastAPI, HTTPException, Query

from exchange_ews_bridge.config import Settings, get_settings
from exchange_ews_bridge.ews import calendar_items, inbox_message, inbox_messages, sent_messages
from exchange_ews_bridge.google_calendar import (
    google_event_body,
    mirror_events_to_google,
    source_fingerprint,
    stable_source_key,
)
from exchange_ews_bridge.mail_reads import reference_for_message
from exchange_ews_bridge.mail_reads import router as mail_reads_router
from exchange_ews_bridge.schemas import (
    BusyBlock,
    CalendarEvent,
    FreeBusyResponse,
    GoogleMirrorEvent,
    GoogleMirrorEventsResponse,
    GoogleMirrorResponse,
    HealthResponse,
    InboxResponse,
    MailIntakeMessage,
    MailIntakeResponse,
    MailMessageDetail,
    MailMessageSummary,
    UpcomingResponse,
)
from exchange_ews_bridge.security import require_bearer_token
from exchange_ews_bridge.state import CalendarSnapshotState

app = FastAPI(
    title="Exchange EWS Bridge",
    version="0.2.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(mail_reads_router)


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise HTTPException(status_code=422, detail="Datetime must include timezone")
    return value.astimezone(UTC)


def enforce_window(start: datetime, end: datetime, settings: Settings) -> tuple[datetime, datetime]:
    start_utc = normalize_datetime(start)
    end_utc = normalize_datetime(end)
    if end_utc <= start_utc:
        raise HTTPException(status_code=422, detail="end must be after start")
    max_window = timedelta(days=settings.ews_max_window_days)
    if end_utc - start_utc > max_window:
        raise HTTPException(status_code=422, detail=f"window must be <= {settings.ews_max_window_days} days")
    return start_utc, end_utc


def safe_calendar_items(settings: Settings, start: datetime, end: datetime, limit: int) -> list[CalendarItem]:
    try:
        return list(calendar_items(settings, start, end, limit))
    except UnauthorizedError as exc:
        raise HTTPException(status_code=502, detail="ews_auth_failed") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="ews_request_failed") from exc


def safe_inbox_messages(
    settings: Settings,
    limit: int,
    unread_only: bool,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
    include_body: bool = False,
) -> list[object]:
    try:
        kwargs: dict[str, object] = {
            "since": since,
            "include_body": include_body,
        }
        if until is not None:
            kwargs["until"] = until
        if offset:
            kwargs["offset"] = offset
        return list(
            cast(Any, inbox_messages)(
                settings,
                limit,
                unread_only,
                **kwargs,
            )
        )
    except UnauthorizedError as exc:
        raise HTTPException(status_code=502, detail="ews_auth_failed") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="ews_request_failed") from exc


def safe_inbox_message(settings: Settings, item_id: str, changekey: str | None) -> object:
    try:
        return inbox_message(settings, item_id, changekey)
    except UnauthorizedError as exc:
        raise HTTPException(status_code=502, detail="ews_auth_failed") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="ews_request_failed") from exc


def safe_sent_messages(
    settings: Settings,
    limit: int,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
    include_body: bool = False,
) -> list[object]:
    try:
        kwargs: dict[str, object] = {
            "since": since,
            "until": until,
            "offset": offset,
        }
        if include_body:
            kwargs["include_body"] = True
        return list(
            cast(Any, sent_messages)(
                settings,
                limit,
                **kwargs,
            )
        )
    except UnauthorizedError as exc:
        raise HTTPException(status_code=502, detail="ews_auth_failed") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="ews_request_failed") from exc


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def encode_mail_cursor(
    settings: Settings,
    folder: str,
    start: datetime,
    end: datetime,
    offset: int,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "folder": folder,
            "start": start.astimezone(UTC).isoformat(),
            "end": end.astimezone(UTC).isoformat(),
            "offset": offset,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = _base64url_encode(payload)
    signature = hmac.new(
        settings.bridge_api_token.get_secret_value().encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_base64url_encode(signature)}"


def decode_mail_cursor(
    settings: Settings,
    cursor: str,
    expected_folder: str,
) -> tuple[datetime, datetime, int]:
    try:
        encoded, supplied_signature = cursor.split(".", 1)
        expected_signature = hmac.new(
            settings.bridge_api_token.get_secret_value().encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_base64url_decode(supplied_signature), expected_signature):
            raise ValueError("signature mismatch")
        payload = json.loads(_base64url_decode(encoded))
        if payload.get("v") != 1 or payload.get("folder") != expected_folder:
            raise ValueError("unsupported cursor")
        start = normalize_datetime(datetime.fromisoformat(str(payload["start"])))
        end = normalize_datetime(datetime.fromisoformat(str(payload["end"])))
        offset = int(payload["offset"])
        if end <= start or end - start > timedelta(days=90) or offset < 1 or offset > 100_000:
            raise ValueError("cursor bounds invalid")
        return start, end, offset
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="invalid_cursor") from exc


def encode_sent_cursor(settings: Settings, start: datetime, end: datetime, offset: int) -> str:
    return encode_mail_cursor(settings, "sent", start, end, offset)


def decode_sent_cursor(settings: Settings, cursor: str) -> tuple[datetime, datetime, int]:
    return decode_mail_cursor(settings, cursor, "sent")


def encode_inbox_cursor(settings: Settings, start: datetime, end: datetime, offset: int) -> str:
    return encode_mail_cursor(settings, "inbox", start, end, offset)


def decode_inbox_cursor(settings: Settings, cursor: str) -> tuple[datetime, datetime, int]:
    return decode_mail_cursor(settings, cursor, "inbox")


def cancellation_fingerprint(google_event: dict[str, object], reason: str) -> str:
    return source_fingerprint({"google_event": google_event, "cancellation_reason": reason})


def event_boundary(value: dict[str, object]) -> datetime:
    raw = str(value.get("dateTime") or value.get("date") or "")
    if not raw:
        raise ValueError("calendar event boundary is missing")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def minimal_cancelled_google_event(row: dict[str, object]) -> dict[str, object]:
    source_key = str(row["source_key"])
    return {
        "summary": "Cancelled Exchange event",
        "transparency": "opaque",
        "start": json.loads(str(row["start_json"])),
        "end": json.loads(str(row["end_json"])),
        "extendedProperties": {
            "private": {
                "exchangeEwsBridge": "true",
                "ewsSourceKey": source_key,
            }
        },
    }


def sender_value(item: object, attr: str) -> str | None:
    sender = getattr(item, "sender", None)
    # EWS Sender can be absent while the native From mailbox is populated.
    # Select one whole mailbox; never combine two people's name and address.
    author = getattr(item, "author", None)
    if not getattr(sender, "email_address", None) and getattr(author, "email_address", None):
        sender = author
    value = getattr(sender, attr, None) if sender else None
    return str(value) if value else None


def stable_mail_source_key(item: object) -> str:
    item_id = str(getattr(item, "id", ""))
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return f"ews_mail_{digest[:32]}"


def stable_conversation_key(item: object) -> str:
    conversation_id = str(getattr(item, "conversation_id", "") or "")
    source = conversation_id or str(getattr(item, "id", ""))
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"ews_conversation_{digest[:32]}"


def recipient_values(item: object, attr: str) -> list[str]:
    recipients = getattr(item, attr, None) or []
    values: list[str] = []
    for recipient in recipients:
        email = getattr(recipient, "email_address", None)
        name = getattr(recipient, "name", None)
        if email and name and str(name) != str(email):
            values.append(f"{name} <{email}>")
        elif email:
            values.append(str(email))
        elif name:
            values.append(str(name))
    return values


def category_values(item: object) -> list[str]:
    categories = getattr(item, "categories", None) or []
    return [str(category) for category in categories if category]


def mail_summary(item: object, folder: str = "inbox") -> MailMessageSummary:
    return MailMessageSummary(
        id=str(getattr(item, "id", "")),
        changekey=getattr(item, "changekey", None),
        source_key=stable_mail_source_key(item),
        conversation_key=stable_conversation_key(item),
        folder=folder,
        subject=getattr(item, "subject", None),
        sender_name=sender_value(item, "name"),
        sender_email=sender_value(item, "email_address"),
        to_recipients=recipient_values(item, "to_recipients"),
        cc_recipients=recipient_values(item, "cc_recipients"),
        received_at=getattr(item, "datetime_received", None),
        sent_at=getattr(item, "datetime_sent", None),
        is_read=getattr(item, "is_read", None),
        has_attachments=getattr(item, "has_attachments", None),
        conversation_id=str(getattr(item, "conversation_id", "") or "") or None,
        conversation_topic=getattr(item, "conversation_topic", None),
        importance=str(getattr(item, "importance", "") or "") or None,
        categories=category_values(item),
    )


def body_text(item: object, max_chars: int) -> tuple[str | None, bool]:
    value = getattr(item, "text_body", None) or getattr(item, "body", None)
    if value is None:
        return None, False
    text = str(value)
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def mail_detail(item: object, max_chars: int) -> MailMessageDetail:
    summary = mail_summary(item)
    text, truncated = body_text(item, max_chars)
    return MailMessageDetail(**summary.model_dump(), body_text=text, body_truncated=truncated)


def needs_attention_hint(item: object) -> bool:
    if getattr(item, "is_read", None) is False:
        return True
    importance = str(getattr(item, "importance", "") or "").lower()
    return importance == "high"


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse()


@app.get(
    "/v1/calendar/freebusy",
    response_model=FreeBusyResponse,
    dependencies=[Depends(require_bearer_token)],
)
def freebusy(
    start: datetime = Query(..., description="ISO datetime with timezone"),
    end: datetime = Query(..., description="ISO datetime with timezone"),
    settings: Settings = Depends(get_settings),
) -> FreeBusyResponse:
    start_utc, end_utc = enforce_window(start, end, settings)
    items = safe_calendar_items(settings, start_utc, end_utc, settings.ews_max_items)
    busy = [
        BusyBlock(
            start=cast(datetime, item.start),
            end=cast(datetime, item.end),
            all_day=bool(getattr(item, "is_all_day", False)),
        )
        for item in items
        if not bool(getattr(item, "is_cancelled", False))
    ]
    return FreeBusyResponse(mailbox=settings.ews_email, start=start_utc, end=end_utc, busy=busy)


@app.get(
    "/v1/calendar/upcoming",
    response_model=UpcomingResponse,
    dependencies=[Depends(require_bearer_token)],
)
def upcoming(
    days: int = Query(default=7, ge=1, le=31),
    limit: int = Query(default=20, ge=1, le=100),
    settings: Settings = Depends(get_settings),
) -> UpcomingResponse:
    now = datetime.now(UTC)
    end = now + timedelta(days=min(days, settings.ews_max_window_days))
    item_limit = min(limit, settings.ews_max_items)
    items = safe_calendar_items(settings, now, end, item_limit)
    events = [
        CalendarEvent(
            start=cast(datetime, item.start),
            end=cast(datetime, item.end),
            all_day=bool(getattr(item, "is_all_day", False)),
            subject=getattr(item, "subject", None) if settings.ews_allow_event_subjects else None,
        )
        for item in items
        if not bool(getattr(item, "is_cancelled", False))
    ]
    return UpcomingResponse(mailbox=settings.ews_email, start=now, end=end, events=events)


@app.post(
    "/v1/calendar/google/mirror",
    response_model=GoogleMirrorResponse,
    dependencies=[Depends(require_bearer_token)],
)
def mirror_google_calendar(
    days: int = Query(default=14, ge=1, le=90),
    limit: int = Query(default=200, ge=1, le=500),
    dry_run: bool = Query(default=False),
    settings: Settings = Depends(get_settings),
) -> GoogleMirrorResponse:
    now = datetime.now(UTC)
    end = now + timedelta(days=min(days, settings.google_mirror_days, settings.ews_max_window_days))
    item_limit = min(limit, settings.google_mirror_limit, settings.ews_max_items)
    fetched_items = safe_calendar_items(settings, now, end, item_limit + 1)
    has_more = len(fetched_items) > item_limit
    items = fetched_items[:item_limit]
    stats = mirror_events_to_google(settings, items, dry_run=dry_run, snapshot_complete=not has_more)
    return GoogleMirrorResponse(
        status="ok",
        start=now,
        end=end,
        scanned=len(items),
        has_more=has_more,
        snapshot_complete=not has_more,
        dry_run=dry_run,
        **stats,
    )


@app.get(
    "/v1/calendar/google/events",
    response_model=GoogleMirrorEventsResponse,
    dependencies=[Depends(require_bearer_token)],
)
def google_calendar_events(
    days: int = Query(default=14, ge=1, le=90),
    past_hours: int = Query(default=0, ge=0, le=168),
    limit: int = Query(default=200, ge=1, le=500),
    reconcile: bool = Query(default=False),
    reconciliation_key: str = Query(
        default="calendar-mirror",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    ),
    settings: Settings = Depends(get_settings),
) -> GoogleMirrorEventsResponse:
    now = datetime.now(UTC)
    start = now - timedelta(hours=past_hours)
    end = now + timedelta(days=min(days, settings.google_mirror_days, settings.ews_max_window_days))
    item_limit = min(limit, settings.google_mirror_limit, settings.ews_max_items)
    fetched_items = safe_calendar_items(settings, start, end, item_limit + 1)
    has_more = len(fetched_items) > item_limit
    items = fetched_items[:item_limit]
    snapshot_complete = not has_more
    events: list[GoogleMirrorEvent] = []
    observed: list[dict[str, object]] = []
    for item in items:
        source_key = stable_source_key(item)
        google_event = google_event_body(item, settings, source_key)
        cancelled = bool(getattr(item, "is_cancelled", False))
        cancellation_reason = "source_cancelled" if cancelled else None
        fingerprint = (
            cancellation_fingerprint(google_event, cancellation_reason)
            if cancellation_reason
            else source_fingerprint(google_event)
        )
        events.append(
            GoogleMirrorEvent(
                source_key=source_key,
                fingerprint=fingerprint,
                google_event=google_event,
                cancelled=cancelled,
                cancellation_reason=cancellation_reason,
            )
        )
        start_json = google_event["start"]
        end_json = google_event["end"]
        observed.append(
            {
                "source_key": source_key,
                "source_hash": fingerprint,
                "start_json": json.dumps(start_json, sort_keys=True, separators=(",", ":")),
                "end_json": json.dumps(end_json, sort_keys=True, separators=(",", ":")),
                "event_start": event_boundary(start_json).isoformat(),
                "event_end": event_boundary(end_json).isoformat(),
                "is_cancelled": cancelled,
            }
        )

    missing: list[dict[str, object]] = []
    if reconcile:
        missing = CalendarSnapshotState(settings.data_dir).reconcile(
            reconciliation_key=reconciliation_key,
            observed=observed,
            window_start=start,
            window_end=end,
            observed_at=now,
            snapshot_complete=snapshot_complete,
        )
        for row in missing:
            google_event = minimal_cancelled_google_event(row)
            events.append(
                GoogleMirrorEvent(
                    source_key=str(row["source_key"]),
                    fingerprint=cancellation_fingerprint(
                        google_event,
                        "missing_from_complete_snapshot",
                    ),
                    google_event=google_event,
                    cancelled=True,
                    cancellation_reason="missing_from_complete_snapshot",
                )
            )
    return GoogleMirrorEventsResponse(
        status="ok",
        mailbox=settings.ews_email,
        start=start,
        end=end,
        scanned=len(items),
        returned=len(events),
        has_more=has_more,
        snapshot_complete=snapshot_complete,
        reconciliation_enabled=reconcile,
        reconciliation_key=reconciliation_key if reconcile else None,
        tombstones=len(missing),
        events=events,
    )


@app.get(
    "/v1/mail/inbox",
    response_model=InboxResponse,
    dependencies=[Depends(require_bearer_token)],
)
def list_inbox(
    limit: int = Query(default=25, ge=1, le=100),
    unread_only: bool = Query(default=False),
    settings: Settings = Depends(get_settings),
) -> InboxResponse:
    messages = [mail_summary(item) for item in safe_inbox_messages(settings, limit, unread_only)]
    return InboxResponse(mailbox=settings.ews_email, messages=messages)


@app.get(
    "/v1/mail/inbox/message",
    response_model=MailMessageDetail,
    dependencies=[Depends(require_bearer_token)],
)
def get_inbox_message(
    id: str = Query(..., min_length=1),
    changekey: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> MailMessageDetail:
    if not settings.ews_mail_body_enabled:
        raise HTTPException(status_code=403, detail="mail_body_reading_disabled")
    item = safe_inbox_message(settings, id, changekey)
    return mail_detail(item, settings.ews_mail_body_max_chars)


@app.get(
    "/v1/mail/inbox/intake",
    response_model=MailIntakeResponse,
    dependencies=[Depends(require_bearer_token)],
)
def inbox_intake(
    limit: int = Query(default=100, ge=1, le=200),
    hours: int = Query(default=72, ge=1, le=2160),
    unread_only: bool = Query(default=False),
    cursor: str | None = Query(default=None, min_length=1, max_length=2048),
    settings: Settings = Depends(get_settings),
) -> MailIntakeResponse:
    if cursor:
        start, end, page_offset = decode_inbox_cursor(settings, cursor)
    else:
        end = datetime.now(UTC)
        start = end - timedelta(hours=hours)
        page_offset = 0
    item_limit = min(limit, settings.ews_max_items)
    fetched_items = safe_inbox_messages(
        settings,
        item_limit + 1,
        unread_only,
        since=start,
        until=end,
        offset=page_offset,
        include_body=settings.ews_mail_body_enabled,
    )
    items = fetched_items[:item_limit]
    has_more = len(fetched_items) > item_limit
    next_cursor = encode_inbox_cursor(settings, start, end, page_offset + len(items)) if has_more else None
    messages = []
    for item in items:
        if settings.ews_mail_body_enabled:
            detail = mail_detail(item, settings.ews_mail_body_max_chars)
        else:
            detail = MailMessageDetail(**mail_summary(item).model_dump())
        messages.append(
            MailIntakeMessage(
                **{**detail.model_dump(), "message_ref": reference_for_message(settings, item, "self", "inbox")},
                needs_attention_hint=needs_attention_hint(item),
            )
        )
    return MailIntakeResponse(
        mailbox=settings.ews_email,
        start=start,
        end=end,
        scanned=len(items),
        body_enabled=settings.ews_mail_body_enabled,
        body_max_chars=settings.ews_mail_body_max_chars,
        page_offset=page_offset,
        has_more=has_more,
        next_cursor=next_cursor,
        saturated=has_more,
        messages=messages,
    )


@app.get(
    "/v1/mail/sent/intake",
    response_model=MailIntakeResponse,
    dependencies=[Depends(require_bearer_token)],
)
def sent_intake(
    limit: int = Query(default=100, ge=1, le=200),
    hours: int = Query(default=720, ge=1, le=2160),
    cursor: str | None = Query(default=None, min_length=1, max_length=2048),
    settings: Settings = Depends(get_settings),
) -> MailIntakeResponse:
    if cursor:
        start, end, page_offset = decode_sent_cursor(settings, cursor)
    else:
        end = datetime.now(UTC)
        start = end - timedelta(hours=hours)
        page_offset = 0
    item_limit = min(limit, settings.ews_max_items)
    fetched_items = safe_sent_messages(
        settings,
        item_limit + 1,
        since=start,
        until=end,
        offset=page_offset,
        include_body=settings.ews_mail_body_enabled,
    )
    items = fetched_items[:item_limit]
    has_more = len(fetched_items) > item_limit
    next_cursor = encode_sent_cursor(settings, start, end, page_offset + len(items)) if has_more else None
    messages = []
    for item in items:
        summary = mail_summary(item, folder="sent")
        if settings.ews_mail_body_enabled:
            text, truncated = body_text(item, settings.ews_mail_body_max_chars)
            detail = MailMessageDetail(
                **summary.model_dump(),
                body_text=text,
                body_truncated=truncated,
            )
        else:
            detail = MailMessageDetail(**summary.model_dump())
        messages.append(
            MailIntakeMessage(
                **{
                    **detail.model_dump(),
                    "message_ref": reference_for_message(settings, item, "self", "sent"),
                }
            )
        )
    return MailIntakeResponse(
        mailbox=settings.ews_email,
        folder="sent",
        start=start,
        end=end,
        scanned=len(items),
        body_enabled=settings.ews_mail_body_enabled,
        body_max_chars=settings.ews_mail_body_max_chars if settings.ews_mail_body_enabled else 0,
        page_offset=page_offset,
        has_more=has_more,
        next_cursor=next_cursor,
        saturated=has_more,
        messages=messages,
    )


from exchange_ews_bridge.change_reads import router as change_reads_router  # noqa: E402

app.include_router(change_reads_router)

from exchange_ews_bridge.calendar_reads import router as calendar_reads_router  # noqa: E402

app.include_router(calendar_reads_router)

from exchange_ews_bridge.attached_reads import router as attached_reads_router  # noqa: E402

app.include_router(attached_reads_router)

from exchange_ews_bridge.operations import read_router, write_router  # noqa: E402

app.include_router(read_router)
app.include_router(write_router)

from exchange_ews_bridge.capabilities import router as capabilities_router  # noqa: E402

app.include_router(capabilities_router)
