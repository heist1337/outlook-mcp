"""Explicit opt-in calendar context; ordinary calendar routes keep their defaults."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from exchange_ews_bridge.config import Settings, get_settings
from exchange_ews_bridge.ews import build_account
from exchange_ews_bridge.mail_reads import _safe_error, mailbox_address, make_ref, read_ref, selected_item
from exchange_ews_bridge.security import require_bearer_token

router = APIRouter(prefix="/v1/calendar", dependencies=[Depends(require_bearer_token)])
FIELDS = [
    "id",
    "changekey",
    "subject",
    "start",
    "end",
    "uid",
    "organizer",
    "required_attendees",
    "optional_attendees",
    "location",
    "is_cancelled",
    "is_all_day",
    "my_response_type",
    "type",
]


def summary(item: Any) -> dict[str, Any]:
    return {
        "source_key": hashlib.sha256(str(item.id).encode()).hexdigest(),
        **{
            k: getattr(item, k, None)
            for k in (
                "subject",
                "start",
                "end",
                "uid",
                "location",
                "is_cancelled",
                "is_all_day",
                "my_response_type",
                "type",
            )
        },
        "organizer": getattr(getattr(item, "organizer", None), "email_address", None),
        "attendees": [
            {"email": getattr(a.mailbox, "email_address", None), "response": a.response_type, "kind": kind}
            for kind in ("required", "optional")
            for a in (getattr(item, kind + "_attendees", None) or [])
        ],
        "attendance_status": "invitation_not_proof_of_attendance",
    }


def enabled(settings: Settings) -> None:
    if not settings.ews_calendar_context_enabled:
        raise HTTPException(403, "calendar_context_disabled")


@router.get("/context")
def context(
    settings: Annotated[Settings, Depends(get_settings)],
    start: datetime,
    end: datetime,
    mailbox: str = Query("self", max_length=64),
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    enabled(settings)
    if not start.tzinfo or not end.tzinfo or not timedelta(0) < end - start <= timedelta(days=90):
        raise HTTPException(422, "invalid_calendar_window")
    try:
        account = build_account(settings, mailbox=mailbox_address(settings, mailbox))
        items = list(
            account.calendar.view(start=start.astimezone(UTC), end=end.astimezone(UTC))
            .only(*FIELDS)
            .order_by("start")[: limit + 1]
        )
        return {
            "mailbox": mailbox,
            "start": start.astimezone(UTC),
            "end": end.astimezone(UTC),
            "complete": len(items) <= limit,
            "has_more": len(items) > limit,
            "events": [
                {
                    **summary(item),
                    "event_ref": make_ref(
                        settings, "event", {"mailbox": mailbox, "folder_path": "calendar", "item_id": item.id}
                    ),
                }
                for item in items[:limit]
            ],
            "coverage": "expanded_occurrences_in_selected_window; narrow_window_if_incomplete",
        }
    except Exception as exc:
        raise _safe_error(exc) from exc


@router.get("/event")
def event(
    settings: Annotated[Settings, Depends(get_settings)],
    event_ref: str = Query(..., max_length=4096),
    cursor: str | None = Query(None, max_length=4096),
    body_chars: int = Query(8000, ge=1, le=8000),
) -> dict[str, Any]:
    enabled(settings)
    ctx = read_ref(settings, event_ref, "event")
    previous = read_ref(settings, cursor, "event_body") if cursor else None
    if ctx["folder_path"] != "calendar" or (
        previous and any(previous.get(k) != ctx[k] for k in ("mailbox", "folder_path", "item_id"))
    ):
        raise HTTPException(422, "calendar_reference_scope_mismatch")
    try:
        account = build_account(settings, mailbox=mailbox_address(settings, ctx["mailbox"]))
        item = selected_item(account.calendar, ctx["item_id"], fields=[*FIELDS, "text_body", "body"])
        text = str(getattr(item, "text_body", None) or getattr(item, "body", None) or "")
        digest = hashlib.sha256(text.encode()).hexdigest()
        offset = previous["offset"] if previous else 0
        if previous and previous["sha256"] != digest:
            raise HTTPException(409, "calendar_body_changed_restart_read")
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= len(text):
            raise HTTPException(422, "invalid_calendar_body_offset")
        chunk = text[offset : offset + body_chars]
        complete = offset + len(chunk) >= len(text)
        return {
            "mailbox": ctx["mailbox"],
            "event": summary(item),
            "body_text": chunk,
            "total_chars": len(text),
            "offset_chars": offset,
            "sha256_utf8": digest,
            "complete": complete,
            "next_cursor": make_ref(settings, "event_body", {**ctx, "offset": offset + len(chunk), "sha256": digest})
            if not complete
            else None,
        }
    except Exception as exc:
        raise _safe_error(exc) from exc
