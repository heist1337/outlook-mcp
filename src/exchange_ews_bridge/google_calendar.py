from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, cast

from exchangelib.items import CalendarItem
from fastapi import HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from exchange_ews_bridge.config import Settings
from exchange_ews_bridge.state import MirrorState

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def google_calendar_service(settings: Settings) -> Any:
    if not settings.google_calendar_enabled or not settings.google_calendar_id:
        raise HTTPException(status_code=503, detail="google_calendar_not_configured")
    if not settings.google_service_account_file.exists():
        raise HTTPException(status_code=503, detail="google_service_account_file_missing")
    credentials = service_account.Credentials.from_service_account_file(
        str(settings.google_service_account_file),
        scopes=[CALENDAR_SCOPE],
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def stable_source_key(item: object) -> str:
    raw_id = str(getattr(item, "id", ""))
    return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()


def source_fingerprint(event_body: dict[str, Any]) -> str:
    payload = json.dumps(event_body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def to_utc_datetime(value: datetime) -> datetime:
    return datetime.fromtimestamp(value.timestamp(), UTC)


def event_description(item: object, max_chars: int) -> str | None:
    value = getattr(item, "text_body", None) or getattr(item, "body", None)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n\n[truncated by Exchange EWS Bridge]"
    return text


def attendee_context(item: object) -> list[dict[str, Any]]:
    attendees: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attr in ("required_attendees", "optional_attendees"):
        for attendee in getattr(item, attr, None) or []:
            mailbox = getattr(attendee, "mailbox", None)
            email = getattr(attendee, "email_address", None)
            if not email:
                email = getattr(mailbox, "email_address", None) if mailbox else None
            if not email:
                continue
            normalized = str(email).strip()
            key = normalized.lower()
            if normalized and key not in seen:
                record = {"email": normalized}
                display_name = getattr(attendee, "name", None)
                if not display_name and mailbox:
                    display_name = getattr(mailbox, "name", None)
                clean_name = str(display_name or "").strip()
                if clean_name and clean_name.lower() != key:
                    record["displayName"] = clean_name
                if attr == "optional_attendees":
                    record["optional"] = True
                attendees.append(record)
                seen.add(key)
    return attendees


def organizer_context(item: object) -> dict[str, str] | None:
    organizer = getattr(item, "organizer", None)
    mailbox = getattr(organizer, "mailbox", None)
    email = getattr(organizer, "email_address", None)
    if not email and mailbox:
        email = getattr(mailbox, "email_address", None)
    normalized = str(email or "").strip()
    if not normalized:
        return None
    result = {"email": normalized}
    name = getattr(organizer, "name", None)
    if not name and mailbox:
        name = getattr(mailbox, "name", None)
    clean_name = str(name or "").strip()
    if clean_name and clean_name.lower() != normalized.lower():
        result["displayName"] = clean_name
    return result


def attendee_emails(item: object) -> list[str]:
    return [record["email"] for record in attendee_context(item)]


def google_event_body(item: CalendarItem, settings: Settings, source_key: str) -> dict[str, Any]:
    """Build the private EWS context returned to trusted internal consumers.

    This representation may contain location, description and attendee e-mail
    addresses when explicitly enabled. It must never be sent to Google by the
    mirror writer; ``google_shadow_event_body`` is the only write payload.
    """
    all_day = bool(getattr(item, "is_all_day", False))
    start = cast(datetime, item.start)
    end = cast(datetime, item.end)
    if settings.google_mirror_summary_mode == "busy":
        summary = "Busy"
    elif settings.google_mirror_summary_mode == "subject":
        summary = getattr(item, "subject", None) or settings.google_mirror_generic_summary
    else:
        summary = settings.google_mirror_generic_summary

    body: dict[str, Any] = {
        "summary": summary,
        "transparency": "opaque",
        "extendedProperties": {
            "private": {
                "exchangeEwsBridge": "true",
                "ewsSourceKey": source_key,
            }
        },
    }
    if all_day:
        body["start"] = {"date": to_utc_datetime(start).date().isoformat()}
        body["end"] = {"date": to_utc_datetime(end).date().isoformat()}
    else:
        body["start"] = {"dateTime": to_utc_datetime(start).isoformat().replace("+00:00", "Z")}
        body["end"] = {"dateTime": to_utc_datetime(end).isoformat().replace("+00:00", "Z")}
    if settings.google_mirror_include_location:
        location = getattr(item, "location", None)
        if location:
            body["location"] = location
    if settings.google_mirror_include_description:
        description = event_description(item, settings.google_mirror_description_max_chars)
        if description:
            body["description"] = description
    if settings.google_mirror_include_attendees:
        attendees = attendee_context(item)
        if attendees:
            body["attendees"] = attendees
    return body


def google_shadow_event_body(item: CalendarItem, settings: Settings, source_key: str) -> dict[str, Any]:
    """Build a private imported copy of an Exchange event.

    ``events.import`` stores a private copy rather than scheduling a new Google
    event. This lets Krisp read the original participant context without making
    the shadow calendar an organizer or sending invitations.
    """

    context = google_event_body(item, settings, source_key)
    body: dict[str, Any] = {
        **context,
        "iCalUID": str(getattr(item, "uid", None) or f"ews-{source_key}@exchange-ews-bridge.local"),
        "privateCopy": True,
        "transparency": "opaque",
        "visibility": "private",
        "guestsCanInviteOthers": False,
        "guestsCanModify": False,
        "guestsCanSeeOtherGuests": True,
        "reminders": {"useDefault": False},
    }
    organizer = organizer_context(item)
    if organizer:
        body["organizer"] = organizer
        attendee_keys = {str(value.get("email", "")).lower() for value in body.get("attendees", [])}
        if organizer["email"].lower() not in attendee_keys:
            body.setdefault("attendees", []).append(organizer)
    return body


def guarded_google_event(service: Any, settings: Settings, event_id: str) -> dict[str, Any] | None:
    """Read a managed event and refuse to mutate unrelated attendee events."""

    try:
        event = (
            service.events()
            .get(
                calendarId=settings.google_calendar_id,
                eventId=event_id,
            )
            .execute()
        )
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            return None
        raise HTTPException(status_code=502, detail="google_calendar_guard_read_failed") from exc
    marker = event.get("extendedProperties", {}).get("private", {}).get("exchangeEwsBridge")
    if event.get("attendees") and not (event.get("privateCopy") is True and marker == "true"):
        raise HTTPException(status_code=409, detail="google_calendar_event_has_attendees")
    return event


def operation_current(operation: dict[str, Any]) -> dict[str, Any] | sqlite3.Row:
    current = operation.get("current")
    if isinstance(current, dict | sqlite3.Row):
        return current
    if operation.get("action") == "create":
        return {}
    raise RuntimeError(f"mirror operation {operation.get('action')} lacks current state")


def mirror_events_to_google(
    settings: Settings,
    items: list[CalendarItem],
    dry_run: bool = False,
    snapshot_complete: bool = True,
) -> dict[str, int]:
    state = MirrorState(settings.data_dir)
    now = datetime.now(UTC).isoformat()
    stats = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    planned: list[dict[str, Any]] = []
    seen_source_keys: set[str] = set()

    for item in items:
        source_key = stable_source_key(item)
        seen_source_keys.add(source_key)
        current = state.get(source_key)
        if bool(getattr(item, "is_cancelled", False)):
            if not current:
                continue
            planned.append({"action": "delete", "source_key": source_key, "current": current})
            stats["deleted"] += 1
            continue
        body = google_shadow_event_body(item, settings, source_key)
        fingerprint = source_fingerprint(body)
        if current and current["source_hash"] == fingerprint:
            stats["unchanged"] += 1
            planned.append(
                {
                    "action": "unchanged",
                    "source_key": source_key,
                    "current": current,
                    "fingerprint": fingerprint,
                }
            )
            continue
        action = "update" if current else "create"
        planned.append(
            {
                "action": action,
                "source_key": source_key,
                "current": current,
                "body": body,
                "fingerprint": fingerprint,
            }
        )
        stats["updated" if current else "created"] += 1

    if settings.google_mirror_delete_missing and snapshot_complete:
        for stale in state.stale_since(now):
            if stale["source_key"] in seen_source_keys:
                continue
            planned.append({"action": "delete", "source_key": stale["source_key"], "current": stale})
            stats["deleted"] += 1

    if dry_run:
        return stats

    service = google_calendar_service(settings)
    guarded_events: dict[str, dict[str, Any] | None] = {}
    for operation in planned:
        if operation["action"] not in ("update", "delete"):
            continue
        event_id = operation["current"]["google_event_id"]
        if event_id not in guarded_events:
            guarded_events[event_id] = guarded_google_event(service, settings, event_id)
        if operation["action"] == "update" and guarded_events[event_id] is None:
            operation["action"] = "create"
            stats["updated"] -= 1
            stats["created"] += 1
        else:
            guarded = guarded_events[event_id]
        if (
            operation["action"] == "update"
            and operation["body"].get("attendees")
            and guarded is not None
            and guarded.get("privateCopy") is not True
        ):
            operation["action"] = "replace"

    for operation in planned:
        action = operation["action"]
        source_key = operation["source_key"]
        current = operation_current(operation)
        if action == "unchanged":
            state.upsert(source_key, current["google_event_id"], operation["fingerprint"], now)
            continue
        if action == "delete":
            event_id = current["google_event_id"]
            if guarded_events[event_id] is not None:
                try:
                    service.events().delete(
                        calendarId=settings.google_calendar_id,
                        eventId=event_id,
                        sendUpdates="none",
                    ).execute()
                except HttpError as exc:
                    if exc.resp.status not in (404, 410):
                        raise HTTPException(status_code=502, detail="google_calendar_delete_failed") from exc
            state.delete(source_key)
            continue
        try:
            if action == "update":
                google_event_id = current["google_event_id"]
                service.events().patch(
                    calendarId=settings.google_calendar_id,
                    eventId=google_event_id,
                    body=operation["body"],
                    sendUpdates="none",
                ).execute()
            else:
                result = (
                    service.events()
                    .import_(
                        calendarId=settings.google_calendar_id,
                        body=operation["body"],
                    )
                    .execute()
                )
                google_event_id = result["id"]
                if action == "replace" and guarded_events[current["google_event_id"]] is not None:
                    service.events().delete(
                        calendarId=settings.google_calendar_id,
                        eventId=current["google_event_id"],
                        sendUpdates="none",
                    ).execute()
            state.upsert(source_key, google_event_id, operation["fingerprint"], now)
        except HttpError as exc:
            raise HTTPException(status_code=502, detail="google_calendar_request_failed") from exc
    return stats
