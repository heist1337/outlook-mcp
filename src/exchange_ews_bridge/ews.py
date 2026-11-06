from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from exchangelib import BASIC, DELEGATE, NTLM, Account, Configuration, Credentials
from exchangelib.items import CalendarItem, Message
from exchangelib.protocol import FailFast, FaultTolerance

from exchange_ews_bridge.config import Settings


def build_account(settings: Settings, mailbox: str | None = None, *, mutation: bool = False) -> Account:
    credentials = Credentials(
        username=settings.ews_username,
        password=settings.ews_password.get_secret_value(),
    )
    auth_type = NTLM if settings.ews_auth_type == "ntlm" else BASIC
    config = Configuration(
        service_endpoint=settings.ews_endpoint,
        credentials=credentials,
        auth_type=auth_type,
        retry_policy=FailFast() if mutation else FaultTolerance(max_wait=30),
    )
    return Account(
        primary_smtp_address=mailbox or settings.ews_email,
        config=config,
        autodiscover=False,
        access_type=DELEGATE,
    )


def calendar_items(settings: Settings, start: datetime, end: datetime, limit: int) -> Iterable[CalendarItem]:
    account = build_account(settings)
    fields = ["id", "changekey", "start", "end", "is_all_day", "is_cancelled"]
    if settings.ews_allow_event_subjects or settings.google_mirror_summary_mode == "subject":
        fields.append("subject")
    if settings.google_mirror_include_location:
        fields.append("location")
    if settings.google_mirror_include_description:
        fields.append("text_body")
        fields.append("body")
    if settings.google_mirror_include_attendees:
        fields.append("required_attendees")
        fields.append("optional_attendees")
        fields.append("organizer")
        fields.append("uid")
    return account.calendar.view(start=start, end=end).only(*fields).order_by("start")[:limit]


def inbox_messages(
    settings: Settings,
    limit: int,
    unread_only: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
    include_body: bool = False,
) -> Iterable[Message]:
    account = build_account(settings)
    query = account.inbox.all()
    if since is not None:
        query = query.filter(datetime_received__gte=since)
    if until is not None:
        query = query.filter(datetime_received__lte=until)
    if unread_only:
        query = query.filter(is_read=False)
    fields = [
        "id",
        "changekey",
        "subject",
        "sender",
        "to_recipients",
        "cc_recipients",
        "datetime_received",
        "is_read",
        "has_attachments",
        "conversation_id",
        "conversation_topic",
        "importance",
        "categories",
    ]
    if include_body:
        fields.extend(["text_body", "body"])
    return query.only(*fields).order_by("-datetime_received")[offset : offset + limit]


def sent_messages(
    settings: Settings,
    limit: int,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
    include_body: bool = False,
) -> Iterable[Message]:
    account = build_account(settings)
    query = account.sent.all()
    if since is not None:
        query = query.filter(datetime_sent__gte=since)
    if until is not None:
        query = query.filter(datetime_sent__lte=until)
    fields = [
        "id",
        "changekey",
        "subject",
        "sender",
        "to_recipients",
        "cc_recipients",
        "datetime_sent",
        "has_attachments",
        "conversation_id",
        "conversation_topic",
        "importance",
        "categories",
    ]
    if include_body:
        fields.extend(["text_body", "body"])
    return query.only(*fields).order_by("-datetime_sent")[offset : offset + limit]


def inbox_message(settings: Settings, item_id: str, changekey: str | None = None) -> Message:
    account = build_account(settings)
    if changekey:
        return account.inbox.get(id=item_id, changekey=changekey)
    return account.inbox.get(id=item_id)
