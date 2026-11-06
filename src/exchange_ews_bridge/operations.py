"""Typed native mailbox operations. No arbitrary SOAP, URLs, or service names.

Preparing a signed command is read-only. Execution needs a separate write token,
an enabled operation group, and an exact, short-lived plan. Receipts prevent
repeating an ambiguous external mutation after a timeout or process crash.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from itertools import islice
from typing import Annotated, Any, Literal
from uuid import uuid4

from exchangelib import CalendarItem, Contact, EWSDateTime, EWSTimeZone, FileAttachment, Mailbox, Message, Task
from exchangelib.fields import WEEKDAY_NAMES
from exchangelib.indexed_properties import EmailAddress
from exchangelib.properties import Actions, Address, Conditions, CopyToFolder, MoveToFolder, Rule, SendingAs
from exchangelib.recurrence import (
    AbsoluteMonthlyPattern,
    AbsoluteYearlyPattern,
    DailyPattern,
    Recurrence,
    RelativeMonthlyPattern,
    RelativeYearlyPattern,
    WeeklyPattern,
)
from exchangelib.services import CreateInboxRule, DeleteInboxRule, GetMailTips, SetInboxRule
from exchangelib.settings import OofSettings
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from exchange_ews_bridge.config import Settings, get_settings
from exchange_ews_bridge.ews import build_account
from exchange_ews_bridge.mail_reads import (
    MAIL_ROOTS,
    _safe_error,
    mailbox_address,
    make_ref,
    read_ref,
    resolve_folder,
    selected_item,
    validate_folder_path,
)
from exchange_ews_bridge.security import require_bearer_token, require_write_token

read_router = APIRouter(prefix="/v1/ews", dependencies=[Depends(require_bearer_token)])
write_router = APIRouter(prefix="/v1/ews", dependencies=[Depends(require_write_token)])
ROOTS = {**MAIL_ROOTS, "calendar": "calendar", "contacts": "contacts", "tasks": "tasks"}
ReadAction = Literal[
    "folder.info",
    "folder.children",
    "contacts.list",
    "tasks.list",
    "item.get",
    "directory.resolve",
    "directory.expand",
    "rooms.list",
    "rooms.members",
    "availability",
    "oof.get",
    "rules.list",
    "delegates.list",
    "timezones.list",
    "mail.tips",
]
WRITE_GROUPS = {
    "calendar.create": "calendar",
    "calendar.update": "calendar",
    "calendar.delete": "calendar",
    "calendar.cancel": "send",
    "calendar.accept": "send",
    "calendar.decline": "send",
    "calendar.tentative": "send",
    "mail.draft": "mail",
    "mail.update": "mail",
    "mail.copy": "mail",
    "mail.move": "mail",
    "mail.delete": "mail",
    "mail.reply_draft": "mail",
    "mail.forward_draft": "mail",
    "mail.send": "send",
    "attachment.add": "mail",
    "attachment.remove": "mail",
    "folder.create": "folders",
    "folder.rename": "folders",
    "folder.move": "folders",
    "contact.create": "contacts",
    "contact.update": "contacts",
    "contact.delete": "contacts",
    "task.create": "tasks",
    "task.update": "tasks",
    "task.complete": "tasks",
    "task.delete": "tasks",
    "oof.set": "settings",
    "rule.create": "settings",
    "rule.update": "settings",
    "rule.delete": "settings",
    "subscription.create": "subscriptions",
    "subscription.poll": "subscriptions",
    "subscription.delete": "subscriptions",
}
ITEM_FIELDS = {
    "calendar": [
        "subject",
        "start",
        "end",
        "is_all_day",
        "is_cancelled",
        "uid",
        "type",
        "categories",
        "recurrence",
        "_start_timezone",
        "_end_timezone",
    ],
    "contacts": [
        "display_name",
        "given_name",
        "surname",
        "company_name",
        "email_addresses",
        "phone_numbers",
        "birthday",
    ],
    "tasks": ["subject", "start_date", "due_date", "status", "percent_complete", "is_complete", "categories"],
}
MAIL_FIELDS = [
    "subject",
    "is_draft",
    "is_read",
    "categories",
    "importance",
    "sensitivity",
    "author",
    "to_recipients",
    "cc_recipients",
    "bcc_recipients",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadRequest(StrictModel):
    action: ReadAction
    mailbox: Literal["self", "shared"] = "self"
    folder_path: str = Field(default="inbox", min_length=1, max_length=256)
    item_ref: str = Field(default="", max_length=4096)
    names: list[Annotated[str, Field(min_length=1, max_length=254)]] = Field(default_factory=list, max_length=20)
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10000)


class Repeat(StrictModel):
    frequency: Literal["daily", "weekly", "monthly", "yearly", "relative_monthly", "relative_yearly"]
    interval: int = Field(default=1, ge=1, le=99)
    weekdays: list[Annotated[int, Field(ge=1, le=7)]] = Field(default_factory=list, max_length=7)
    weekday: int = Field(default=1, ge=1, le=10)
    week_number: int = Field(default=1, ge=1, le=5)
    until: date | None = None
    count: int | None = Field(default=None, ge=1, le=999)

    @model_validator(mode="after")
    def valid(self) -> Repeat:
        if self.until and self.count:
            raise ValueError("choose until or count")
        if self.frequency == "weekly" and not self.weekdays:
            raise ValueError("weekly recurrence requires weekdays")
        if self.frequency in {"yearly", "relative_yearly"} and self.interval != 1:
            raise ValueError("EWS yearly recurrence has no interval")
        return self


class RuleValues(StrictModel):
    id: str | None = Field(default=None, max_length=2048)
    name: str = Field(min_length=1, max_length=200)
    priority: int = Field(ge=0, le=1000)
    enabled: bool = True
    subject_words: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=20
    )
    senders: list[Annotated[str, Field(min_length=3, max_length=254)]] = Field(default_factory=list, max_length=20)
    has_attachments: bool | None = None
    categories: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=20)
    mark_read: bool | None = None
    stop_processing: bool = False
    move_to: str = Field(default="", max_length=256)
    copy_to: str = Field(default="", max_length=256)
    forward_to: list[Annotated[str, Field(min_length=3, max_length=254)]] = Field(default_factory=list, max_length=20)
    apply_to_all_incoming: bool = False

    @model_validator(mode="after")
    def valid(self) -> RuleValues:
        if (
            not self.subject_words
            and not self.senders
            and self.has_attachments is None
            and not self.apply_to_all_incoming
        ):
            raise ValueError("rule requires a condition or explicit apply_to_all_incoming")
        if (
            not self.categories
            and self.mark_read is None
            and not self.move_to
            and not self.copy_to
            and not self.forward_to
        ):
            raise ValueError("rule requires an action")
        if any(
            "@" not in address or any(ord(c) < 33 for c in address) for address in [*self.senders, *self.forward_to]
        ):
            raise ValueError("invalid rule address")
        return self


class Values(StrictModel):
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=8000)
    start: datetime | None = None
    end: datetime | None = None
    timezone: str = Field(default="Europe/Moscow", max_length=64)
    all_day: bool | None = None
    location: str | None = Field(default=None, max_length=256)
    free_busy: Literal["Free", "Tentative", "Busy", "OOF", "NoData"] | None = None
    reminder_minutes: int | None = Field(default=None, ge=0, le=40320)
    reminder_enabled: bool | None = None
    recurrence: Repeat | None = None
    categories: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(default=None, max_length=20)
    importance: Literal["Low", "Normal", "High"] | None = None
    sensitivity: Literal["Normal", "Personal", "Private", "Confidential"] | None = None
    is_read: bool | None = None
    to: list[Annotated[str, Field(min_length=3, max_length=254)]] | None = Field(default=None, max_length=20)
    cc: list[Annotated[str, Field(min_length=3, max_length=254)]] | None = Field(default=None, max_length=20)
    display_name: str | None = Field(default=None, max_length=200)
    given_name: str | None = Field(default=None, max_length=100)
    surname: str | None = Field(default=None, max_length=100)
    company_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    birthday: date | None = None
    start_date: date | None = None
    due_date: date | None = None
    percent_complete: int | None = Field(default=None, ge=0, le=100)
    status: Literal["NotStarted", "InProgress", "Completed", "WaitingOnOthers", "Deferred"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)
    filename: str | None = Field(default=None, min_length=1, max_length=200)
    content_base64: str | None = Field(default=None, max_length=45000)
    attachment_id: str | None = Field(default=None, max_length=2048)
    oof_state: Literal["Disabled", "Enabled", "Scheduled"] | None = None
    external_audience: Literal["None", "Known", "All"] | None = None
    internal_reply: str | None = Field(default=None, max_length=4000)
    external_reply: str | None = Field(default=None, max_length=4000)
    rule: RuleValues | None = None
    rule_id: str | None = Field(default=None, max_length=2048)
    subscription_ref: str | None = Field(default=None, max_length=4096)
    timeout_minutes: int | None = Field(default=None, ge=1, le=60)


class Command(StrictModel):
    action: str = Field(min_length=1, max_length=40)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    mailbox: Literal["self", "shared"] = "self"
    folder_path: str = Field(default="", max_length=256)
    item_ref: str = Field(default="", max_length=4096)
    destination: str = Field(default="", max_length=256)
    values: Values = Field(default_factory=Values)
    notify_attendees: bool = False

    @model_validator(mode="after")
    def valid(self) -> Command:
        if self.action not in WRITE_GROUPS:
            raise ValueError("unsupported write action")
        return self


class ExecuteRequest(StrictModel):
    plan_ref: str = Field(min_length=1, max_length=131072)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def serialize(value: Any, depth: int = 0) -> Any:
    """Bounded EWS value projection; never stringify raw objects or XML."""
    if isinstance(value, Exception):
        raise value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, EWSTimeZone):
        return value.key
    if depth >= 6:
        return {"truncated": True}
    if isinstance(value, (list, tuple)):
        return [serialize(v, depth + 1) for v in value[:50]]
    if hasattr(value, "FIELDS"):
        forbidden = {
            "mime_content",
            "body",
            "text_body",
            "binary_data",
            "xml_data",
            "photo",
            "user_smime_certificate",
            "ms_exchange_certificate",
        }
        return {
            f.name: serialize(getattr(value, f.name, None), depth + 1) for f in value.FIELDS if f.name not in forbidden
        }
    return None


def bounded(values: Iterable[Any], limit: int, offset: int = 0) -> dict[str, Any]:
    rows = list(islice(values, offset, offset + limit + 1))
    return {
        "items": [serialize(v) for v in rows[:limit]],
        "complete": len(rows) <= limit,
        "has_more": len(rows) > limit,
        "next_offset": offset + limit if len(rows) > limit else None,
        "pagination_consistency": "live_offset_not_snapshot",
    }


def folder_for(settings: Settings, alias: str, path: str, *, mutation: bool = False) -> Any:
    parts = validate_folder_path(path)
    if not mutation and parts[0] in MAIL_ROOTS:
        return resolve_folder(settings, alias, path)
    if parts[0] not in ROOTS:
        raise HTTPException(422, "unsupported_folder_root")
    account = build_account(settings, mailbox=mailbox_address(settings, alias), mutation=mutation)
    folder = getattr(account, ROOTS[parts[0]])
    for part in parts[1:]:
        folder = folder / part
    return folder


def fields_for(path: str) -> list[str]:
    return ["id", "changekey", *ITEM_FIELDS.get(path.split("/")[0], MAIL_FIELDS)]


def item_projection(settings: Settings, item: Any, alias: str, path: str) -> dict[str, Any]:
    return {
        "item_ref": make_ref(settings, "ews-item", {"mailbox": alias, "folder_path": path, "item_id": item.id}),
        **projected_values(item, path),
    }


def selected(
    settings: Settings, ref: str, *, mutation: bool = False, extra: list[str] | None = None
) -> tuple[Any, dict[str, Any]]:
    # Accept the existing native mail/calendar references as well as new ones.
    ctx = None
    for kind in ("ews-item", "event", "message"):
        try:
            ctx = read_ref(settings, ref, kind)
            break
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
    if ctx is None:
        raise HTTPException(422, "invalid_item_reference")
    folder = folder_for(settings, ctx["mailbox"], ctx["folder_path"], mutation=mutation)
    return selected_item(folder, ctx["item_id"], fields=[*fields_for(ctx["folder_path"]), *(extra or [])]), ctx


def aware(value: datetime | None) -> datetime:
    if value is None or value.tzinfo is None:
        raise HTTPException(422, "timezone_aware_datetime_required")
    return value


def ews_datetime(value: datetime, timezone: str = "UTC") -> EWSDateTime:
    aware(value)
    return EWSDateTime.from_datetime(value.astimezone(UTC)).astimezone(EWSTimeZone(timezone))


@read_router.post("/read")
def read(request: ReadRequest, settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    if not settings.ews_extended_reads_enabled:
        raise HTTPException(403, "extended_reads_disabled")
    try:
        account = build_account(settings, mailbox=mailbox_address(settings, request.mailbox))
        action = request.action
        if action == "item.get":
            item, ctx = selected(settings, request.item_ref)
            return {"item": item_projection(settings, item, ctx["mailbox"], ctx["folder_path"])}
        if action.startswith("folder.") or action in {"contacts.list", "tasks.list"}:
            path = {"contacts.list": "contacts", "tasks.list": "tasks"}.get(action, request.folder_path)
            folder = folder_for(settings, request.mailbox, path)
            if action == "folder.info":
                folder.refresh()
                return {
                    "folder_path": path,
                    "id": folder.id,
                    "name": folder.name,
                    "effective_rights": serialize(folder.effective_rights),
                    "total_count": folder.total_count,
                    "unread_count": folder.unread_count,
                }
            if action == "folder.children":
                rows = list(islice(folder.children, request.offset, request.offset + request.limit + 1))
                return {
                    "items": [
                        {"name": f.name, "id": f.id, "folder_path": path + "/" + f.name} for f in rows[: request.limit]
                    ],
                    "complete": len(rows) <= request.limit,
                    "next_offset": request.offset + request.limit if len(rows) > request.limit else None,
                }
            rows = list(folder.all().only(*fields_for(path))[request.offset : request.offset + request.limit + 1])
            return {
                "items": [item_projection(settings, i, request.mailbox, path) for i in rows[: request.limit]],
                "complete": len(rows) <= request.limit,
                "next_offset": request.offset + request.limit if len(rows) > request.limit else None,
            }
        if action == "directory.resolve":
            if not request.names:
                raise HTTPException(422, "names_required")
            result = account.protocol.resolve_names(names=request.names, return_full_contact_data=False)
        elif action in {"directory.expand", "rooms.members"}:
            if len(request.names) != 1:
                raise HTTPException(422, "one_directory_address_required")
            result = (
                account.protocol.expand_dl(request.names[0])
                if action == "directory.expand"
                else account.protocol.get_rooms(request.names[0])
            )
        elif action == "rooms.list":
            result = account.protocol.get_roomlists()
        elif action == "timezones.list":
            zones = [EWSTimeZone(name) for name in request.names] if request.names else None
            result = account.protocol.get_timezones(timezones=zones, return_full_timezone_data=False)
        elif action == "availability":
            start, end = aware(request.start), aware(request.end)
            if not request.names or not timedelta(0) < end - start <= timedelta(days=31):
                raise HTTPException(422, "invalid_availability_scope")
            result = account.protocol.get_free_busy_info(
                accounts=[(n, "Required", False) for n in request.names],
                start=ews_datetime(start),
                end=ews_datetime(end),
                requested_view="FreeBusy",
            )
        elif action == "oof.get":
            oof = account.oof_settings
            return {
                "state": oof.state,
                "external_audience": oof.external_audience,
                "start": serialize(oof.start),
                "end": serialize(oof.end),
                "fingerprint": oof_fingerprint(oof),
            }
        elif action == "rules.list":
            result = account.rules
        elif action == "delegates.list":
            result = account.delegates
        elif action == "mail.tips":
            if not request.names:
                raise HTTPException(422, "recipients_required")
            result = GetMailTips(protocol=account.protocol).call(
                sending_as=SendingAs(email_address=account.primary_smtp_address),
                recipients=[Mailbox(email_address=n) for n in request.names],
                mail_tips_requested="All",
            )
        else:
            raise HTTPException(422, "unsupported_read_action")
        return {"action": action, "mailbox": request.mailbox, **bounded(result, request.limit, request.offset)}
    except Exception as exc:
        raise _safe_error(exc) from exc


# Exactly these fields may change; mismatched fields are rejected, never ignored.
COMMON = {"subject", "body", "categories", "importance", "sensitivity"}
ALLOWED = {
    "calendar": COMMON
    | {
        "start",
        "end",
        "timezone",
        "all_day",
        "location",
        "free_busy",
        "reminder_minutes",
        "reminder_enabled",
        "recurrence",
        "to",
        "cc",
    },
    "mail": COMMON | {"is_read", "to", "cc"},
    "contact": {"display_name", "given_name", "surname", "company_name", "email", "birthday", "categories"},
    "task": COMMON | {"start_date", "due_date", "percent_complete", "status"},
    "folder": {"name"},
    "attachment": {"filename", "content_base64", "attachment_id"},
    "rule": {"rule", "rule_id"},
    "oof": {"oof_state", "external_audience", "start", "end", "internal_reply", "external_reply"},
    "subscription": {"subscription_ref", "timeout_minutes"},
}
NO_VALUES = {
    "calendar.delete",
    "mail.copy",
    "mail.move",
    "mail.delete",
    "mail.send",
    "contact.delete",
    "task.delete",
    "task.complete",
    "folder.move",
}


def validate_command(command: Command) -> None:
    action = command.action
    values = command.values.model_dump(exclude_unset=True)
    domain = action.split(".")[0]
    if domain in {"folder", "oof", "rule", "subscription"} and command.item_ref:
        raise HTTPException(422, "item_reference_not_allowed_for_non_item_action")
    special = {
        "calendar.cancel": {"body"},
        "calendar.accept": {"body"},
        "calendar.decline": {"body"},
        "calendar.tentative": {"body"},
        "mail.reply_draft": {"subject", "body", "to", "cc"},
        "mail.forward_draft": {"subject", "body", "to", "cc"},
        "attachment.add": {"filename", "content_base64"},
        "attachment.remove": {"attachment_id"},
        "rule.create": {"rule"},
        "rule.update": {"rule"},
        "rule.delete": {"rule_id"},
        "subscription.create": {"timeout_minutes"},
        "subscription.poll": {"subscription_ref"},
        "subscription.delete": {"subscription_ref"},
    }
    allowed = set() if action in NO_VALUES else special.get(action, ALLOWED[domain])
    if set(values) - allowed:
        raise HTTPException(422, "fields_not_allowed_for_action")
    if any(value is None for value in values.values()):
        raise HTTPException(422, "null_fields_not_supported")
    if command.notify_attendees and domain != "calendar":
        raise HTTPException(422, "notification_mode_only_for_calendar")
    for key in ("to", "cc"):
        if any("@" not in s or any(ord(c) < 33 for c in s) for s in values.get(key, [])):
            raise HTTPException(422, "invalid_recipient")
    if domain == "calendar" and action in {"calendar.create", "calendar.update"}:
        if action == "calendar.create" and not all(k in values for k in ("subject", "start", "end")):
            raise HTTPException(422, "calendar_subject_start_end_required")
        # Require an explicit pair when changing time to avoid partial window errors.
        if "start" in values or "end" in values:
            start, end = aware(command.values.start), aware(command.values.end)
            if not timedelta(0) < end - start <= timedelta(days=31):
                raise HTTPException(422, "invalid_event_window")
            tz = EWSTimeZone(command.values.timezone)
            if command.values.all_day and any(v.astimezone(tz).time().isoformat() != "00:00:00" for v in (start, end)):
                raise HTTPException(422, "all_day_boundaries_must_be_local_midnight")
        if action == "calendar.update" and "all_day" in values and not command.values.start:
            raise HTTPException(422, "all_day_update_requires_start_end")
        if command.values.recurrence and not command.values.start:
            raise HTTPException(422, "recurrence_requires_start_end")
        if (values.get("to") or values.get("cc")) and not command.notify_attendees:
            raise HTTPException(422, "attendees_require_explicit_notification")
    if action in {"mail.draft", "mail.reply_draft", "mail.forward_draft"}:
        if not command.values.subject or command.values.body is None:
            raise HTTPException(422, "draft_subject_and_body_required")
        if action == "mail.forward_draft" and not command.values.to:
            raise HTTPException(422, "forward_recipients_required")
    if action == "contact.create" and not command.values.display_name:
        raise HTTPException(422, "contact_display_name_required")
    if action == "task.update" and not values:
        raise HTTPException(422, "update_requires_fields")
    if action.startswith("task."):
        if (
            command.values.start_date
            and command.values.due_date
            and command.values.start_date > command.values.due_date
        ):
            raise HTTPException(422, "task_due_date_before_start")
        if (
            command.values.percent_complete is not None
            and command.values.status in {None, "NotStarted"}
            and command.values.percent_complete != 0
        ):
            raise HTTPException(422, "task_progress_requires_status")
        if command.values.status == "Completed" and command.values.percent_complete not in {None, 100}:
            raise HTTPException(422, "completed_task_progress_must_be_100")
    if action == "task.create" and not command.values.subject:
        raise HTTPException(422, "task_subject_required")
    if action == "folder.create" or action == "folder.rename":
        if (
            not command.values.name
            or "/" in command.values.name
            or any(ord(c) < 32 for c in command.values.name)
            or command.values.name in {".", ".."}
        ):
            raise HTTPException(422, "invalid_folder_name")
    if action == "attachment.add":
        if not command.values.filename or not command.values.content_base64:
            raise HTTPException(422, "attachment_name_and_content_required")
        try:
            data = base64.b64decode(command.values.content_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(422, "invalid_base64") from exc
        if not 0 < len(data) <= 32768:
            raise HTTPException(422, "attachment_chunk_limit_32768")
    if action == "attachment.remove" and not command.values.attachment_id:
        raise HTTPException(422, "attachment_id_required")
    if action.startswith("rule."):
        if action == "rule.create" and (not command.values.rule or command.values.rule.id):
            raise HTTPException(422, "new_rule_required")
        if action == "rule.update" and (not command.values.rule or not command.values.rule.id):
            raise HTTPException(422, "existing_rule_id_required")
        if action == "rule.delete" and not command.values.rule_id:
            raise HTTPException(422, "rule_id_required")
    if action == "oof.set":
        oof = native_oof(command.values)
        oof.clean()


def native_oof(v: Values) -> OofSettings:
    if not v.oof_state:
        raise HTTPException(422, "oof_state_required")
    return OofSettings(
        state=v.oof_state,
        external_audience=v.external_audience or "None",
        start=ews_datetime(v.start) if v.start else None,
        end=ews_datetime(v.end) if v.end else None,
        internal_reply=v.internal_reply or "",
        external_reply=v.external_reply or "",
    )


def repeat(v: Values) -> Recurrence | None:
    r = v.recurrence
    if not r:
        return None
    start = aware(v.start).astimezone(EWSTimeZone(v.timezone)).date()
    patterns = {
        "daily": lambda: DailyPattern(interval=r.interval),
        "weekly": lambda: WeeklyPattern(interval=r.interval, weekdays=[WEEKDAY_NAMES[i - 1] for i in r.weekdays]),
        "monthly": lambda: AbsoluteMonthlyPattern(interval=r.interval, day_of_month=start.day),
        "yearly": lambda: AbsoluteYearlyPattern(day_of_month=start.day, month=start.month),
        "relative_monthly": lambda: RelativeMonthlyPattern(
            interval=r.interval, weekday=r.weekday, week_number=r.week_number
        ),
        "relative_yearly": lambda: RelativeYearlyPattern(
            month=start.month, weekday=r.weekday, week_number=r.week_number
        ),
    }
    if r.until and r.until < start:
        raise HTTPException(422, "recurrence_end_before_start")
    return Recurrence(pattern=patterns[r.frequency](), start=start, end=r.until, number=r.count)


def native_values(v: Values, domain: str) -> dict[str, Any]:
    values = v.model_dump(exclude_unset=True)
    renames = {
        "all_day": "is_all_day",
        "free_busy": "legacy_free_busy_status",
        "reminder_minutes": "reminder_minutes_before_start",
        "reminder_enabled": "reminder_is_set",
        "to": "to_recipients",
        "cc": "cc_recipients",
    }
    result = {}
    for key, value in values.items():
        if key == "timezone":
            continue
        if key in {"start_date", "due_date", "birthday"}:
            from exchangelib import EWSDate

            value = EWSDate.from_date(getattr(v, key))
        elif key in {"start", "end"}:
            value = ews_datetime(getattr(v, key), v.timezone)
        elif key == "recurrence":
            value = repeat(v)
        elif key in {"to", "cc"}:
            value = [Mailbox(email_address=s) for s in value]
            if domain == "calendar":
                from exchangelib import Attendee

                value = [Attendee(mailbox=m, response_type="Unknown") for m in value]
                result["required_attendees" if key == "to" else "optional_attendees"] = value
                continue
        elif key == "email":
            result["email_addresses"] = [EmailAddress(label="EmailAddress1", email=value)]
            continue
        result[renames.get(key, key)] = value
        if domain == "calendar" and key in {"start", "end"}:
            result["_" + key + "_timezone"] = ews_datetime(getattr(v, key), v.timezone).tzinfo
    return result


def default_path(command: Command) -> str:
    return (
        command.folder_path
        or {
            "calendar": "calendar",
            "mail": "drafts",
            "contact": "contacts",
            "task": "tasks",
            "folder": "inbox",
            "attachment": "drafts",
            "subscription": "inbox",
            "oof": "inbox",
            "rule": "inbox",
        }[command.action.split(".")[0]]
    )


def validate_target(command: Command, item: Any, ctx: dict[str, Any]) -> None:
    if ctx["mailbox"] != command.mailbox or (command.folder_path and ctx["folder_path"] != command.folder_path):
        raise HTTPException(422, "item_scope_mismatch")
    domain = command.action.split(".")[0]
    model = {"calendar": CalendarItem, "mail": Message, "attachment": Message, "contact": Contact, "task": Task}.get(
        domain
    )
    if model and not isinstance(item, model):
        raise HTTPException(422, "item_type_mismatch")
    if domain in {"mail", "attachment"} and command.action in {"mail.send", "attachment.add", "attachment.remove"}:
        if ctx["folder_path"] != "drafts" or not item.is_draft:
            raise HTTPException(422, "operation_requires_saved_draft")
    if command.action == "mail.update" and set(command.values.model_dump(exclude_unset=True)) - {
        "is_read",
        "categories",
        "importance",
        "sensitivity",
    }:
        if not item.is_draft or ctx["folder_path"] != "drafts":
            raise HTTPException(422, "content_update_requires_draft")
    if (
        domain == "calendar"
        and command.action == "calendar.update"
        and command.values.start
        and item.is_all_day
        and command.values.all_day is not False
    ):
        tz = EWSTimeZone(command.values.timezone)
        if any(
            aware(v).astimezone(tz).time().isoformat() != "00:00:00" for v in (command.values.start, command.values.end)
        ):
            raise HTTPException(422, "all_day_boundaries_must_be_local_midnight")
    if domain == "calendar" and command.action in {"calendar.update", "calendar.delete"}:
        # An occurrence is a separate explicit target. Updating the master means the entire series.
        if getattr(item, "required_attendees", None) or getattr(item, "optional_attendees", None):
            if not command.notify_attendees:
                raise HTTPException(422, "meeting_changes_require_explicit_notification")
    if domain == "calendar" and command.action in {"calendar.update", "calendar.delete", "calendar.cancel"}:
        if str(item.my_response_type) != "Organizer" and (
            getattr(item, "required_attendees", None) or getattr(item, "optional_attendees", None)
        ):
            raise HTTPException(422, "organizer_required_for_meeting_changes")


def command_state(settings: Settings, command: Command, *, mutation: bool = False) -> dict[str, Any]:
    validate_command(command)
    action = command.action
    path = default_path(command)
    state: dict[str, Any] = {"folder_path": path}
    creates = {
        "calendar.create": "calendar",
        "mail.draft": "drafts",
        "contact.create": "contacts",
        "task.create": "tasks",
    }
    if action in creates and path.split("/")[0] != creates[action]:
        raise HTTPException(422, "create_folder_type_mismatch")
    if action in creates and command.item_ref:
        raise HTTPException(422, "new_item_must_not_have_reference")
    if command.item_ref:
        item, ctx = selected(
            settings,
            command.item_ref,
            mutation=mutation,
            extra=["required_attendees", "optional_attendees", "my_response_type"]
            if action.startswith("calendar.")
            else [],
        )
        validate_target(command, item, ctx)
        state["target"] = projected_values(item, ctx["folder_path"])
        if action.startswith("calendar."):
            attendees = (
                [*(getattr(item, "required_attendees", None) or []), *(getattr(item, "optional_attendees", None) or [])]
                if getattr(item, "required_attendees", None) or getattr(item, "optional_attendees", None)
                else []
            )
            state["attendees"] = serialize(attendees)
            state["attendee_count"] = len(attendees)
        state.update(
            folder_path=ctx["folder_path"],
            item_id=item.id,
            changekey=item.changekey,
            target_type=getattr(item, "type", None),
        )
        if not item.changekey:
            raise HTTPException(409, "target_version_unavailable")
    elif action not in {
        "calendar.create",
        "mail.draft",
        "contact.create",
        "task.create",
        "folder.create",
        "folder.rename",
        "folder.move",
        "oof.set",
        "rule.create",
        "rule.update",
        "rule.delete",
        "subscription.create",
        "subscription.poll",
        "subscription.delete",
    }:
        raise HTTPException(422, "selected_item_reference_required")
    else:
        folder = folder_for(settings, command.mailbox, path, mutation=mutation)
        state.update(folder_id=folder.id, folder_changekey=folder.changekey)
        if action.startswith("rule."):
            rules = folder.account.rules
            if any(r.is_not_supported or r.is_in_error for r in rules):
                raise HTTPException(409, "unsupported_rules_require_manual_review")
            state["rules_fingerprint"] = full_fingerprint(rules)
            rule_id = command.values.rule.id if command.values.rule else command.values.rule_id
            if action != "rule.create" and not any(r.id == rule_id for r in rules):
                raise HTTPException(404, "selected_rule_not_found")
            if (
                action == "rule.create"
                and command.values.rule
                and any(r.display_name == command.values.rule.name for r in rules)
            ):
                raise HTTPException(409, "rule_name_already_exists")
            if action == "rule.update":
                old = next(r for r in rules if r.id == rule_id)
                supported_rule_update(old)
            if command.values.rule:
                native_rule(settings, command, folder.account).clean(version=folder.account.version)
        if action == "oof.set":
            state["settings_fingerprint"] = oof_fingerprint(folder.account.oof_settings)
        if action.startswith("subscription.") and action != "subscription.create":
            subscription = read_ref(settings, command.values.subscription_ref or "", "subscription")
            if subscription["mailbox"] != command.mailbox or subscription["folder_path"] != path:
                raise HTTPException(422, "subscription_scope_mismatch")
    if command.destination:
        if action not in {"mail.copy", "mail.move", "folder.move"}:
            raise HTTPException(422, "destination_not_allowed")
        if action == "folder.move" and (command.destination == path or command.destination.startswith(path + "/")):
            raise HTTPException(422, "cannot_move_folder_into_itself")
        destination = folder_for(settings, command.mailbox, command.destination, mutation=mutation)
        if command.destination.split("/")[0] in {"calendar", "contacts", "tasks"}:
            raise HTTPException(422, "mail_destination_required")
        state["destination_id"] = destination.id
    elif action in {"mail.copy", "mail.move", "folder.move"}:
        raise HTTPException(422, "destination_required")
    if action.startswith("folder.") and path.split("/")[0] not in MAIL_ROOTS:
        raise HTTPException(422, "mail_folder_required")
    if action.startswith("folder.") and action != "folder.create" and len(validate_folder_path(path)) < 2:
        raise HTTPException(422, "distinguished_folder_mutation_forbidden")
    return state


@read_router.post("/prepare")
def prepare(command: Command, settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    if not settings.ews_extended_reads_enabled:
        raise HTTPException(403, "extended_reads_disabled")
    try:
        state = command_state(settings, command)
        payload = command.model_dump(mode="json", exclude_unset=True)
        # Entire command is signed; the execute endpoint accepts no replacement fields.
        ref = make_ref(
            settings,
            "ews-plan",
            {
                "mailbox": command.mailbox,
                "folder_path": state["folder_path"],
                "command": payload,
                "state": state,
                "plan_id": str(uuid4()),
            },
            ttl=1800,
        )
        return {
            "plan_ref": ref,
            "command": payload,
            "target": state,
            "expires_in_seconds": 1800,
            "write_group": WRITE_GROUPS[command.action],
            "external_effect": command.action
            in {"mail.send", "calendar.cancel", "calendar.accept", "calendar.decline", "calendar.tentative", "oof.set"}
            or command.notify_attendees
            or bool(command.values.rule and command.values.rule.forward_to),
            "series_scope": state.get("target_type"),
            "execution_enabled": group_enabled(settings, command),
            "request_id": command.request_id,
        }
    except Exception as exc:
        raise _safe_error(exc) from exc


def group_enabled(settings: Settings, command: Command) -> bool:
    groups = set(settings.ews_enabled_write_groups.split(","))
    needed = {WRITE_GROUPS[command.action]}
    if command.notify_attendees:
        needed.add("send")
    if command.action.startswith("calendar.") and command.action in {
        "calendar.cancel",
        "calendar.accept",
        "calendar.decline",
        "calendar.tentative",
    }:
        needed.add("calendar")
    if command.action == "mail.send":
        needed.add("mail")
    if command.values.rule and command.values.rule.forward_to:
        needed.add("send")
    return needed <= groups


class Receipts:
    def __init__(self, settings: Settings):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.data_dir / "ews-actions.sqlite3", timeout=10)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, result TEXT)"
        )
        self.db.commit()

    def lookup(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT fingerprint,state,result FROM actions WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        if row[0] != fingerprint:
            raise HTTPException(409, "request_id_already_used_for_different_command")
        if row[1] != "done":
            raise HTTPException(409, "action_outcome_uncertain_reconcile_before_retry")
        return json.loads(row[2])

    def claim(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        self.db.execute("BEGIN IMMEDIATE")
        row = self.db.execute("SELECT fingerprint,state,result FROM actions WHERE key=?", (key,)).fetchone()
        if row:
            self.db.rollback()
            if row[0] != fingerprint:
                raise HTTPException(409, "request_id_already_used_for_different_command")
            if row[1] == "done":
                return json.loads(row[2])
            raise HTTPException(409, "action_outcome_uncertain_reconcile_before_retry")
        self.db.execute("INSERT INTO actions VALUES (?,?,?,NULL)", (key, fingerprint, "started"))
        self.db.commit()
        return None

    def complete(self, key: str, result: dict[str, Any]) -> None:
        self.db.execute("UPDATE actions SET state='done',result=? WHERE key=?", (canonical(result), key))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


@write_router.post("/execute")
def execute(request: ExecuteRequest, settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    ctx = read_ref(settings, request.plan_ref, "ews-plan")
    command = Command.model_validate(ctx["command"])
    if not group_enabled(settings, command):
        raise HTTPException(403, "write_group_not_enabled")
    receipts = Receipts(settings)
    key = digest({"mailbox": command.mailbox, "request_id": command.request_id})
    try:
        fingerprint = digest(command.model_dump(mode="json"))
        old = receipts.lookup(key, fingerprint)
        if old is not None:
            return {**old, "replayed": True}
        # A stale target is refused before any mutation. FailFast avoids throttling waits during writes.
        state = command_state(settings, command, mutation=True)
        if state != ctx["state"]:
            raise HTTPException(409, "target_changed_prepare_again")
        old = receipts.claim(key, fingerprint)
        if old is not None:
            return {**old, "replayed": True}
        result = apply_command(settings, command, state)
        response = {"status": "executed", "action": command.action, "request_id": command.request_id, **result}
        receipts.complete(key, response)
        return response
    except Exception as exc:
        raise _safe_error(exc) from exc
    finally:
        receipts.close()


def apply_command(settings: Settings, command: Command, state: dict[str, Any]) -> dict[str, Any]:
    action = command.action
    path = state["folder_path"]
    folder = folder_for(settings, command.mailbox, path, mutation=True)
    account = folder.account
    v = command.values
    notify = "SendToAllAndSaveCopy" if command.notify_attendees else "SendToNone"
    if action in {"calendar.create", "mail.draft", "contact.create", "task.create"}:
        cls = {"calendar.create": CalendarItem, "mail.draft": Message, "contact.create": Contact, "task.create": Task}[
            action
        ]
        expected_root = {
            "calendar.create": "calendar",
            "mail.draft": "drafts",
            "contact.create": "contacts",
            "task.create": "tasks",
        }[action]
        if path.split("/")[0] != expected_root:
            raise HTTPException(422, "create_folder_type_mismatch")
        item = cls(folder=folder, **native_values(v, action.split(".")[0]))
        item.save(send_meeting_invitations=notify)
        item.refresh()
        return {"item": item_projection(settings, item, command.mailbox, path), "verified_by": "GetItem"}
    if action.startswith("folder."):
        if action == "folder.create":
            from exchangelib import Folder

            new = Folder(parent=folder, name=v.name)
            new.save()
            new.refresh()
            return {"folder_id": new.id, "folder_path": path + "/" + str(new.name), "verified_by": "GetFolder"}
        if action == "folder.rename":
            folder.name = v.name
            folder.save(update_fields=["name"])
        else:
            folder.move(to_folder=folder_for(settings, command.mailbox, command.destination, mutation=True))
        folder.refresh()
        return {"folder_id": folder.id, "name": folder.name, "verified_by": "GetFolder"}
    if action.startswith("rule."):
        if full_fingerprint(account.rules) != state["rules_fingerprint"]:
            raise HTTPException(409, "rules_changed_before_write")
        native = native_rule(settings, command, account) if action != "rule.delete" else Rule(id=v.rule_id)
        service = {"rule.create": CreateInboxRule, "rule.update": SetInboxRule, "rule.delete": DeleteInboxRule}[action]
        service(account=account).get(rule=native, remove_outlook_rule_blob=False)
        rules = account.rules
        if action == "rule.delete":
            if any(r.id == v.rule_id for r in rules):
                raise HTTPException(502, "rule_delete_not_verified")
            return {"deleted_rule_id": v.rule_id, "verified_by": "GetInboxRules"}
        if v.rule is None:
            raise HTTPException(422, "rule_required")
        rule = v.rule
        matched = [r for r in rules if r.display_name == rule.name and (not rule.id or r.id == rule.id)]
        if len(matched) != 1:
            raise HTTPException(502, "rule_identity_not_verified")
        return {"rule": serialize(matched[0]), "verified_by": "GetInboxRules"}
    if action == "oof.set":
        expected = native_oof(v)
        account.oof_settings = expected
        actual = account.oof_settings
        if actual != expected:
            raise HTTPException(502, "oof_settings_not_verified")
        return {"state": actual.state, "verified_by": "GetUserOofSettings"}
    if action.startswith("subscription."):
        if action == "subscription.create":
            subscription_id, watermark = folder.subscribe_to_pull(timeout=v.timeout_minutes or 10)
        else:
            previous = read_ref(settings, v.subscription_ref or "", "subscription")
            subscription_id, watermark = previous["subscription_id"], previous["watermark"]
            if action == "subscription.delete":
                folder.unsubscribe(subscription_id)
                return {"unsubscribed": True}
            events = list(folder.get_events(subscription_id, watermark))
            if not events:
                raise HTTPException(502, "empty_notification_response")
            watermark = events[-1].events[-1].watermark if events[-1].events else watermark
            # Notifications are only hints; native SyncFolderItems is the reconciliation authority.
            return {
                "notifications": serialize(events),
                "subscription_ref": make_ref(
                    settings,
                    "subscription",
                    {
                        "mailbox": command.mailbox,
                        "folder_path": path,
                        "subscription_id": subscription_id,
                        "watermark": watermark,
                    },
                ),
                "coverage": "notifications_require_native_sync_reconciliation",
            }
        return {
            "subscription_ref": make_ref(
                settings,
                "subscription",
                {
                    "mailbox": command.mailbox,
                    "folder_path": path,
                    "subscription_id": subscription_id,
                    "watermark": watermark,
                },
            ),
            "timeout_minutes": v.timeout_minutes or 10,
            "background_polling_enabled": False,
        }
    extra = (
        ["body", "required_attendees", "optional_attendees", "my_response_type"]
        if action.startswith("calendar.")
        else ["attachments"]
        if action.startswith("attachment.")
        else []
    )
    item, _ = selected(settings, command.item_ref, mutation=True, extra=extra)
    if item.changekey != state["changekey"]:
        raise HTTPException(409, "target_changed_before_write")
    if action.endswith(".update"):
        changes = native_values(v, action.split(".")[0])
        if action == "contact.update" and "email_addresses" in changes:
            # Indexed UpdateItem fields delete labels omitted from the list.
            # Only replace EmailAddress1; retain all other fetched addresses.
            changes["email_addresses"] = [
                *changes["email_addresses"],
                *(address for address in (item.email_addresses or []) if address.label != "EmailAddress1"),
            ]
        if not changes:
            raise HTTPException(422, "update_requires_fields")
        for key, value in changes.items():
            setattr(item, key, value)
        item.save(update_fields=list(changes), conflict_resolution="NeverOverwrite", send_meeting_invitations=notify)
    elif action == "mail.send":
        item.send(save_copy=True)
        return {"sent": True, "verified_by": "SendItem_response", "receipt_does_not_prove_delivery": True}
    elif action in {"mail.reply_draft", "mail.forward_draft"}:
        draft = (
            item.create_reply(
                subject=v.subject,
                body=v.body,
                to_recipients=[Mailbox(email_address=s) for s in v.to] if v.to else None,
                cc_recipients=[Mailbox(email_address=s) for s in v.cc] if v.cc else None,
            )
            if action == "mail.reply_draft"
            else item.create_forward(
                subject=v.subject,
                body=v.body,
                to_recipients=[Mailbox(email_address=s) for s in v.to or []],
                cc_recipients=[Mailbox(email_address=s) for s in v.cc] if v.cc else None,
            )
        )
        # selected() owns another Account instance. The native CreateItem
        # service requires the destination folder to belong to draft.account.
        drafts_folder = item.account.drafts
        saved = draft.save(folder=drafts_folder)
        if not getattr(saved, "id", None):
            raise HTTPException(502, "reply_identity_missing_reconcile")
        saved = selected_item(drafts_folder, saved.id, fields=fields_for("drafts"))
        if not isinstance(saved, Message) or not saved.is_draft:
            raise HTTPException(502, "saved_reply_not_verified_reconcile")
        return {
            "item": item_projection(settings, saved, command.mailbox, "drafts"),
            "verified_by": "GetItem",
        }
    elif action in {"mail.copy", "mail.move"}:
        destination = folder_for(settings, command.mailbox, command.destination, mutation=True)
        if action == "mail.copy":
            identity = item.copy(to_folder=destination)
            if identity is None:
                raise HTTPException(502, "copy_identity_missing_reconcile")
            new_id, changekey = identity
        else:
            item.move(to_folder=destination)
            if not item.id:
                raise HTTPException(502, "move_identity_missing_reconcile")
            new_id, changekey = item.id, item.changekey
        return {
            "item_id": new_id,
            "changekey": changekey,
            "folder_path": command.destination,
            "item_ref": make_ref(
                settings,
                "ews-item",
                {"mailbox": command.mailbox, "folder_path": command.destination, "item_id": new_id},
            ),
            "verified_by": "CopyItem_or_MoveItem_response",
        }
    elif action.endswith(".delete"):
        item.move_to_trash(send_meeting_cancellations=notify)
        return {"moved_to_deleted_items": True, "verified_by": "DeleteItem_response", "hard_delete": False}
    elif action == "calendar.cancel":
        item.cancel(body=v.body or "")
        return {"cancellation_sent": True, "verified_by": "CreateItem_response"}
    elif action in {"calendar.accept", "calendar.decline", "calendar.tentative"}:
        method = {
            "calendar.accept": item.accept,
            "calendar.decline": item.decline,
            "calendar.tentative": item.tentatively_accept,
        }[action]
        method(body=v.body or "")
        return {"response_sent": True, "verified_by": "CreateItem_response"}
    elif action == "task.complete":
        from decimal import Decimal

        item.status = Task.COMPLETED
        item.percent_complete = Decimal(100)
        item.save(update_fields=["status", "percent_complete"], conflict_resolution="NeverOverwrite")
    elif action == "attachment.add":
        item.attach(FileAttachment(name=v.filename, content=base64.b64decode(v.content_base64 or "", validate=True)))
    elif action == "attachment.remove":
        attachments = [a for a in item.attachments if getattr(a.attachment_id, "id", None) == v.attachment_id]
        if len(attachments) != 1:
            raise HTTPException(422, "selected_attachment_not_found")
        item.detach(attachments[0])
    else:
        raise HTTPException(422, "unsupported_write_action")
    expected_emails = (
        {a.label: a.email for a in item.email_addresses} if action == "contact.update" and v.email is not None else None
    )
    item.refresh()
    if expected_emails is not None and {a.label: a.email for a in (item.email_addresses or [])} != expected_emails:
        raise HTTPException(502, "contact_email_update_not_verified_reconcile")
    return {"item": item_projection(settings, item, command.mailbox, path), "verified_by": "GetItem"}


def native_rule(settings: Settings, command: Command, account: Any) -> Rule:
    r = command.values.rule
    if r is None:
        raise HTTPException(422, "rule_required")
    actions = Actions(
        assign_categories=r.categories or None,
        mark_as_read=r.mark_read,
        stop_processing_rules=r.stop_processing,
        forward_to_recipients=[Address(email_address=a) for a in r.forward_to] or None,
    )
    for path, cls, field in [(r.move_to, MoveToFolder, "move_to_folder"), (r.copy_to, CopyToFolder, "copy_to_folder")]:
        if path:
            if path.split("/")[0] not in MAIL_ROOTS:
                raise HTTPException(422, "rule_mail_destination_required")
            destination = folder_for(settings, command.mailbox, path)
            setattr(actions, field, cls(id=destination.id))
    return Rule(
        id=r.id,
        display_name=r.name,
        priority=r.priority,
        is_enabled=r.enabled,
        conditions=Conditions(
            contains_subject_strings=r.subject_words or None,
            from_addresses=[Address(email_address=a) for a in r.senders] or None,
            has_attachments=r.has_attachments,
        ),
        actions=actions,
    )


def full_fingerprint(value: Any) -> str:
    # Serialize complete selected configuration fields for conflict detection.
    # Only its digest is returned/stored; display truncation must not hide changes.
    def raw(v: Any) -> Any:
        if v is None or isinstance(v, (str, bool, int, float)):
            return v
        if isinstance(v, (datetime, date)):
            return v.isoformat()
        if isinstance(v, (list, tuple)):
            return [raw(i) for i in v]
        if hasattr(v, "FIELDS"):
            return {f.name: raw(getattr(v, f.name, None)) for f in v.FIELDS}
        return None

    return digest(raw(value))


def supported_rule_update(rule: Any) -> None:
    # The typed interface replaces this rule, so do not silently drop conditions,
    # actions or exceptions that it cannot represent. Deletion is a separate intent.
    supported = {
        "conditions": {"contains_subject_strings", "from_addresses", "has_attachments"},
        "actions": {
            "assign_categories",
            "mark_as_read",
            "stop_processing_rules",
            "move_to_folder",
            "copy_to_folder",
            "forward_to_recipients",
        },
        "exceptions": set(),
    }
    for group, allowed in supported.items():
        value = getattr(rule, group, None)
        if value is None:
            continue
        for field in value.FIELDS:
            field_value = getattr(value, field.name, None)
            if field.name not in allowed and field_value is not None and field_value != []:
                raise HTTPException(422, "rule_contains_unrepresented_fields_do_not_replace")


def projected_values(item: Any, path: str) -> dict[str, Any]:
    result = {f: serialize(getattr(item, f, None)) for f in fields_for(path)}
    if path.split("/")[0] == "calendar" and getattr(item, "is_all_day", False):
        end = getattr(item, "end", None)
        # exchangelib 5.6.0 returns the last inclusive date for all-day End.
        # HTTP/agent consumers use exclusive end boundaries, as on creation.
        if isinstance(end, date) and not isinstance(end, datetime):
            result["end"] = (end + timedelta(days=1)).isoformat()
        result["end_boundary"] = "exclusive"
    return result


def oof_fingerprint(value: OofSettings) -> str:
    # Native OofSettings ignores message/audience fields when Disabled and the
    # server-generated Duration when Enabled. Match that documented applicability.
    fields = (
        ["state"]
        if value.state == OofSettings.DISABLED
        else ["state", "external_audience", "internal_reply", "external_reply"]
    )
    if value.state == OofSettings.SCHEDULED:
        fields += ["start", "end"]
    return digest(
        {
            name: getattr(value, name).isoformat()
            if isinstance(getattr(value, name), datetime)
            else getattr(value, name)
            for name in fields
        }
    )
