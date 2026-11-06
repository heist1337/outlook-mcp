"""On-demand, bounded mail and attachment reads. No Exchange mutations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from itertools import islice
from typing import Annotated, Any

from exchangelib import FileAttachment
from exchangelib.errors import ErrorItemNotFound, UnauthorizedError
from exchangelib.properties import ConversationId
from fastapi import APIRouter, Depends, HTTPException, Query

from exchange_ews_bridge.config import Settings, get_settings
from exchange_ews_bridge.ews import build_account
from exchange_ews_bridge.security import require_bearer_token

router = APIRouter(prefix="/v1/mail", dependencies=[Depends(require_bearer_token)])
REF_TTL = 86400
MAIL_ROOTS = {
    "inbox": "inbox",
    "sent": "sent",
    "drafts": "drafts",
    "deleted": "trash",
    "junk": "junk",
    "archive": "archive_msg_folder_root",
    "mail": "msg_folder_root",
}


class MailTextParser(HTMLParser):
    """Render EWS HTML locally; retain words and links, never fetch resources."""

    blocks = frozenset(
        {
            "br",
            "p",
            "div",
            "tr",
            "li",
            "blockquote",
            "table",
            "ul",
            "ol",
            "pre",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "dt",
            "dd",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.head_title = False
        self.cells = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head" and "head" in self.hidden:
            return
        # HTML allows </head> to be omitted before body content. Do not mistake
        # a valid implicit head boundary for unterminated hidden content.
        if self.hidden == ["head"] and tag not in {
            "head",
            "title",
            "meta",
            "link",
            "base",
            "script",
            "style",
            "template",
            "noscript",
        }:
            self.hidden.clear()
        if tag in {"script", "style", "head"}:
            self.hidden.append(tag)
        if tag == "title" and "head" in self.hidden:
            self.head_title = True
        if not self.hidden and tag in self.blocks:
            self.parts.append("\n")
        if not self.hidden and tag in {"table", "tr"}:
            self.cells = 0
        if not self.hidden and tag in {"td", "th"}:
            if self.cells:
                self.parts.append("\t")
            self.cells += 1
        if not self.hidden and tag == "a":
            href = dict(attrs).get("href") or ""
            if href.casefold().startswith(("https://", "http://", "mailto:")):
                self.parts.append(f" ({href}) ")
        if not self.hidden and tag == "img" and (alt := dict(attrs).get("alt")):
            self.parts.append(f" {alt} ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.head_title = False
        if tag in self.hidden:
            self.hidden = self.hidden[: self.hidden.index(tag)]
        elif not self.hidden and tag in self.blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden == ["head"] and not self.head_title and data.strip():
            self.hidden.clear()
        if not self.hidden:
            self.parts.append(data)


def render_body_text(value: Any) -> str:
    if getattr(value, "body_type", None) == "Text":
        return str(value)
    if getattr(value, "body_type", None) == "HTML":
        parser = MailTextParser()
        parser.feed(str(value))
        parser.close()
        if parser.head_title or any(tag in {"script", "style"} for tag in parser.hidden):
            raise ValueError("unbalanced_mail_html")
        return "\n".join(line.rstrip() for line in "".join(parser.parts).splitlines() if line.strip())
    return ""


def complete_body_text(item: Any) -> tuple[str, str, str]:
    """Prefer native TextBody; repair only from a verified complete HTML view."""
    primary = getattr(item, "text_body", None)
    html = getattr(item, "body", None)
    raw = primary or html or ""
    source_format = "html" if getattr(raw, "body_type", None) == "HTML" else "text"
    text = render_body_text(raw) if source_format == "html" else str(raw)
    source = "text_body" if primary else "body"
    if primary and "\ufffd" in text and getattr(html, "body_type", None) == "HTML":
        try:
            alternative = render_body_text(html)
        except ValueError:
            alternative = ""

        # No guessed words: all undamaged text must survive in source order.
        def normalize(value: str) -> str:
            value = re.sub(r"\[cid:[^\]\r\n]+\]", "", value, flags=re.I)
            return " ".join(value.split())

        candidate, offset = normalize(alternative), 0
        aligned = bool(candidate) and "\ufffd" not in candidate
        for part in normalize(text).split("\ufffd"):
            if not part:
                continue
            found = candidate.find(part, offset)
            if found < 0:
                aligned = False
                break
            offset = found + len(part)
        if aligned:
            return alternative, "html", "html_body_replacement_fallback"
    return text, source_format, source


def mailbox_address(settings: Settings, alias: str) -> str:
    if alias == "self":
        return settings.ews_email
    address = settings.ews_shared_mailbox if alias == "shared" else ""
    if not address:
        raise HTTPException(403, "mailbox_not_allowed")
    return address


def validate_folder_path(path: str) -> list[str]:
    parts = path.split("/")
    if (
        len(path) > 256
        or len(parts) > 12
        or parts[0] not in {*MAIL_ROOTS, "calendar", "contacts", "tasks"}
        or any(not part or part in {".", ".."} or any(ord(c) < 32 for c in part) for part in parts)
    ):
        raise HTTPException(422, "invalid_folder_path")
    return parts


def resolve_folder(settings: Settings, mailbox: str, folder_path: str) -> Any:
    parts = validate_folder_path(folder_path)
    if parts[0] not in settings.ews_mail_read_roots.split(","):
        raise HTTPException(403, "mail_root_not_enabled")
    account = build_account(settings, mailbox=mailbox_address(settings, mailbox))
    folder = getattr(account, MAIL_ROOTS[parts[0]])
    for part in parts[1:]:
        folder = folder / part
    return folder


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(data: str) -> bytes:
    return base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)


def reference_key(settings: Settings) -> bytes:
    # API callers know the bearer token. Only the backend holds EWS credentials.
    return hmac.new(
        settings.ews_password.get_secret_value().encode(),
        b"exchange-mail-reference:v1\0" + settings.bridge_api_token.get_secret_value().encode(),
        hashlib.sha256,
    ).digest()


def make_ref(settings: Settings, kind: str, payload: dict[str, Any], *, ttl: int | None = REF_TTL) -> str:
    encoded = _encode(
        json.dumps(
            {**payload, "kind": kind, "expires": int(time.time()) + ttl if ttl is not None else None},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    signature = hmac.new(reference_key(settings), encoded.encode(), hashlib.sha256).digest()
    return encoded + "." + _encode(signature)


def read_ref(settings: Settings, ref: str, kind: str) -> dict[str, Any]:
    try:
        encoded, signature = ref.split(".")
        expected = hmac.new(reference_key(settings), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_decode(signature), expected):
            raise ValueError
        payload = json.loads(_decode(encoded))
        if not isinstance(payload, dict) or payload.get("kind") != kind:
            raise ValueError
        if (payload["expires"] is None and kind != "sync") or (
            payload["expires"] is not None and payload["expires"] <= time.time()
        ):
            raise ValueError
        mailbox_address(settings, payload["mailbox"])
        validate_folder_path(payload["folder_path"])
        return payload
    except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error) as exc:
        raise HTTPException(422, "invalid_mail_reference") from exc


def reference_for_message(settings: Settings, item: Any, mailbox: str, folder_path: str) -> str:
    return make_ref(
        settings,
        "message",
        {
            "mailbox": mailbox,
            "folder_path": folder_path,
            "item_id": item.id,
        },
    )


def selected_item(folder: Any, item_id: str, fields: list[str] | None = None) -> Any:
    # GetItem by ID is account-wide even when called on a Folder queryset.
    item = folder.all().only(*(fields or ["id", "changekey", "attachments"]), "parent_folder_id").get(id=item_id)
    if not folder.id or getattr(item.parent_folder_id, "id", None) != folder.id:
        raise HTTPException(403, "mail_item_outside_selected_folder")
    return item


def selected_message(settings: Settings, ref: str) -> tuple[Any, dict[str, Any]]:
    context = read_ref(settings, ref, "message")
    folder = resolve_folder(settings, context["mailbox"], context["folder_path"])
    return selected_item(folder, context["item_id"]), context


def mail_fields(include_body: bool) -> list[str]:
    fields = [
        "id",
        "changekey",
        "subject",
        "sender",
        "author",
        "to_recipients",
        "cc_recipients",
        "datetime_received",
        "datetime_sent",
        "has_attachments",
        "conversation_id",
        "message_id",
        "is_read",
        "importance",
        "categories",
    ]
    return fields + (["text_body", "body"] if include_body else [])


def _safe_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, ErrorItemNotFound):
        return HTTPException(404, "mail_item_not_found")
    return HTTPException(502, "ews_auth_failed" if isinstance(exc, UnauthorizedError) else "ews_request_failed")


@router.get("/folders")
def mail_folders(
    settings: Annotated[Settings, Depends(get_settings)],
    mailbox: str = Query("self", min_length=1, max_length=64),
    folder_path: str = Query("inbox", min_length=1, max_length=256),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
) -> dict[str, Any]:
    try:
        folder = resolve_folder(settings, mailbox, folder_path)
        children = [
            {
                "name": child.name,
                "folder_path": folder_path + "/" + child.name,
                "folder_class": child.folder_class,
                "total_count": child.total_count,
            }
            for child in islice(folder.children, offset, offset + limit + 1)
        ]
        return {
            "mailbox": mailbox,
            "folder_path": folder_path,
            "folder_class": folder.folder_class,
            "total_count": folder.total_count,
            "folders": children[:limit],
            "complete": len(children) <= limit,
            "next_offset": offset + limit if len(children) > limit else None,
        }
    except Exception as exc:
        raise _safe_error(exc) from exc


@router.get("/search")
def search_mail(
    settings: Annotated[Settings, Depends(get_settings)],
    start: datetime,
    end: datetime,
    mailbox: str = Query("self", min_length=1, max_length=64),
    folder_path: str = Query("inbox", min_length=1, max_length=256),
    subject: str = Query("", max_length=200),
    sender: str = Query("", max_length=254),
    is_read: bool | None = None,
    has_attachments: bool | None = None,
    include_body: bool = True,
    conversation_ref: str | None = Query(None, min_length=1, max_length=4096),
    words: str = Query("", max_length=300),
    recipient: str = Query("", max_length=254),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, min_length=1, max_length=4096),
) -> dict[str, Any]:
    # Import locally to reuse the existing normalizer without a module cycle.
    from exchange_ews_bridge.app import body_text, mail_summary

    if not start.tzinfo or not end.tzinfo or not timedelta(0) < end - start <= timedelta(days=90):
        raise HTTPException(422, "mail_window_must_be_1_to_90_days_with_timezone")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    conversation_id = None
    if conversation_ref:
        conversation = read_ref(settings, conversation_ref, "conversation")
        if conversation["mailbox"] != mailbox:
            raise HTTPException(422, "mail_conversation_scope_mismatch")
        conversation_id = conversation["conversation_id"]
    context = {
        "mailbox": mailbox,
        "folder_path": folder_path,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "subject": subject,
        "sender": sender,
        "is_read": is_read,
        "has_attachments": has_attachments,
        "include_body": include_body,
        "conversation_id": conversation_id,
        "words": words,
        "recipient": recipient,
    }
    offset = 0
    if cursor:
        previous = read_ref(settings, cursor, "search")
        if any(previous.get(key) != value for key, value in context.items()):
            raise HTTPException(422, "mail_cursor_scope_mismatch")
        offset = previous["offset"]
        if not isinstance(offset, int) or not 0 < offset <= 100_000:
            raise HTTPException(422, "invalid_mail_reference")
    try:
        folder = resolve_folder(settings, mailbox, folder_path)
        date_field = "datetime_sent" if folder_path.split("/")[0] == "sent" else "datetime_received"
        indexed = bool(words or recipient)
        if indexed:
            if conversation_id or any(c in words + recipient for c in '"\\\r\n'):
                raise HTTPException(422, "invalid_index_search_filter")

            def quoted(value: str) -> str:
                return '"' + value + '"'

            # AQS and Restriction cannot be combined in EWS. Date-only AQS is
            # an indexed candidate window; exact timezone-aware bounds and
            # typed filters are checked against returned metadata below.
            key = "sent" if date_field == "datetime_sent" else "received"
            aqs = " ".join(
                [
                    key + ":>=" + (start - timedelta(days=1)).date().isoformat(),
                    key + ":<=" + (end + timedelta(days=1)).date().isoformat(),
                    *(quoted(word) for word in words.split()),
                    *(["to:" + quoted(recipient)] if recipient else []),
                ]
            )
            query = folder.filter(aqs)
        else:
            query = folder.filter(**{date_field + "__gte": start, date_field + "__lt": end})
            if subject:
                query = query.filter(subject__icontains=subject)
            if conversation_id:
                query = query.filter(conversation_id=ConversationId(id=conversation_id))
            if sender:
                query = query.filter(sender=sender)
            if is_read is not None:
                query = query.filter(is_read=is_read)
            if has_attachments is not None:
                query = query.filter(has_attachments=has_attachments)
        with_body = include_body and settings.ews_mail_body_enabled
        # Reserve result space for signed references, Unicode and recipients.
        item_limit = min(limit, settings.ews_max_items, 5 if with_body else 25)
        items = list(query.only(*mail_fields(with_body)).order_by("-" + date_field)[offset : offset + item_limit + 1])
        has_more = len(items) > item_limit
        messages = []
        for item in items[:item_limit]:
            if indexed:
                at = getattr(item, date_field, None)
                recipients = [
                    getattr(r, "email_address", "").casefold() for r in (getattr(item, "to_recipients", None) or [])
                ]
                if (
                    not at
                    or not start <= at < end
                    or (subject and subject.casefold() not in str(getattr(item, "subject", "")).casefold())
                    or (
                        sender
                        and sender.casefold()
                        != str(getattr(getattr(item, "sender", None), "email_address", "")).casefold()
                    )
                    or (recipient and recipient.casefold() not in recipients)
                    or (is_read is not None and getattr(item, "is_read", None) != is_read)
                    or (has_attachments is not None and getattr(item, "has_attachments", None) != has_attachments)
                ):
                    continue
            summary = mail_summary(item, folder=folder_path).model_dump(exclude={"id", "changekey", "conversation_id"})
            summary["message_ref"] = reference_for_message(settings, item, mailbox, folder_path)
            native_conversation_id = getattr(getattr(item, "conversation_id", None), "id", None)
            summary["conversation_ref"] = (
                make_ref(
                    settings,
                    "conversation",
                    {"mailbox": mailbox, "folder_path": folder_path, "conversation_id": native_conversation_id},
                )
                if native_conversation_id
                else None
            )
            summary["internet_message_id"] = getattr(item, "message_id", None)
            if with_body:
                summary["body_text"], summary["body_truncated"] = body_text(
                    item, min(settings.ews_mail_body_max_chars, 2000)
                )
            messages.append(summary)
        return {
            "mailbox": mailbox,
            "folder_path": folder_path,
            "start": start,
            "end": end,
            "messages": messages,
            "body_included": with_body,
            "search_coverage": "server_index_candidates" if indexed else "folder_restriction",
            "scanned_count": min(len(items), item_limit),
            "page_limit": item_limit,
            "pagination_consistency": "live_offset_not_snapshot",
            "complete": not has_more,
            "has_more": has_more,
            "next_cursor": make_ref(settings, "search", {**context, "offset": offset + item_limit})
            if has_more
            else None,
        }
    except Exception as exc:
        raise _safe_error(exc) from exc


@router.get("/message")
def read_message(
    settings: Annotated[Settings, Depends(get_settings)],
    message_ref: str = Query(..., min_length=1, max_length=4096),
    cursor: str | None = Query(None, min_length=1, max_length=4096),
    body_chars: int = Query(8000, ge=1, le=8000),
    include_unique_body: bool = Query(False),
) -> dict[str, Any]:
    """Read a selected message without marking it read; paginate the complete body."""
    from exchange_ews_bridge.app import mail_summary

    if not settings.ews_mail_body_enabled:
        raise HTTPException(403, "mail_body_disabled")
    context = read_ref(settings, message_ref, "message")
    offset = 0
    previous = None
    if cursor:
        previous = read_ref(settings, cursor, "body")
        if any(previous.get(key) != context[key] for key in ("mailbox", "folder_path", "item_id")):
            raise HTTPException(422, "mail_cursor_scope_mismatch")
        offset = previous.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset <= 0:
            raise HTTPException(422, "invalid_mail_reference")
    try:
        folder = resolve_folder(settings, context["mailbox"], context["folder_path"])
        item = selected_item(folder, context["item_id"], fields=[*mail_fields(True), "headers"])
        # EWS GetItem retrieves one body. The API returns bounded characters,
        # not a truncated body falsely described as complete.
        # The locked EWS library preserves BodyType as Body/HTMLBody. Render
        # the entire HTML body locally, including quotations and inline answers;
        # never substitute UniqueBody. Native TextBody remains unchanged.
        text, source_body_format, body_source = complete_body_text(item)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if previous and previous.get("sha256") != digest:
            raise HTTPException(409, "mail_body_changed_restart_read")
        if offset > len(text):
            raise HTTPException(422, "mail_body_offset_out_of_range")
        chunk = text[offset : offset + body_chars]
        complete = offset + len(chunk) >= len(text)
        summary = mail_summary(item, folder=context["folder_path"]).model_dump(
            exclude={"id", "changekey", "conversation_id"}
        )
        unique = {}
        if include_unique_body and not cursor:
            # Optional second GetItem: unsupported UniqueBody must not break
            # complete original reads. Never return truncated unique text.
            unique = {"unique_body_status": "unavailable"}
            try:
                candidate = selected_item(folder, context["item_id"], fields=["unique_body", "changekey"])
                value = getattr(candidate, "unique_body", None)
                if not getattr(item, "changekey", None) or getattr(candidate, "changekey", None) != item.changekey:
                    unique = {"unique_body_status": "changed"}
                elif value and (rendered := render_body_text(value)).strip():
                    if len(rendered) <= 8000:
                        unique = {
                            "unique_body_status": "available",
                            "unique_body_text": rendered,
                            "unique_body_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                        }
                    else:
                        unique = {"unique_body_status": "too_large"}
                elif value:
                    unique = {"unique_body_status": "non_text"}
            except Exception:
                # Permission/network/property failures are not evidence that
                # an empty reply was sent. Keep the original body intact.
                unique = {"unique_body_status": "unavailable"}
        return {
            **unique,
            "mailbox": context["mailbox"],
            "folder_path": context["folder_path"],
            "message": {
                **summary,
                "internet_message_id": getattr(item, "message_id", None),
                "reply_headers": {
                    h.name: h.value
                    for h in (getattr(item, "headers", None) or [])
                    if h.name.casefold() in {"in-reply-to", "references"}
                },
            },
            "body_text": chunk,
            "body_format": "text",
            "source_body_format": source_body_format,
            "body_source": body_source,
            "offset_chars": offset,
            "total_chars": len(text),
            "sha256_utf8": digest,
            "complete": complete,
            "next_cursor": make_ref(settings, "body", {**context, "offset": offset + len(chunk), "sha256": digest})
            if not complete
            else None,
        }
    except Exception as exc:
        raise _safe_error(exc) from exc


@router.get("/attachments")
def list_attachments(
    settings: Annotated[Settings, Depends(get_settings)],
    message_ref: str = Query(..., min_length=1, max_length=4096),
) -> dict[str, Any]:
    if not settings.ews_mail_attachments_enabled:
        raise HTTPException(403, "mail_attachments_disabled")
    try:
        item, context = selected_message(settings, message_ref)
        attachments = []
        for attachment in item.attachments:
            is_file = isinstance(attachment, FileAttachment)
            identifier = getattr(attachment.attachment_id, "id", None)
            size = attachment.size
            available = (
                is_file and bool(identifier) and (size is None or size <= settings.ews_mail_attachment_max_bytes)
            )
            attachments.append(
                {
                    "name": attachment.name,
                    "size": size,
                    "content_type": attachment.content_type,
                    "is_inline": bool(attachment.is_inline),
                    "kind": "file" if is_file else "item",
                    "downloadable": available,
                    "item_ref": make_ref(settings, "attached_message", {**context, "attachment_id": identifier})
                    if not is_file
                    and identifier
                    and size is not None
                    and size <= settings.ews_mail_attachment_max_bytes
                    else None,
                    "attachment_ref": make_ref(
                        settings,
                        "attachment",
                        {
                            **context,
                            "attachment_id": identifier,
                        },
                    )
                    if available
                    else None,
                }
            )
        return {"attachments": attachments, "max_bytes": settings.ews_mail_attachment_max_bytes}
    except Exception as exc:
        raise _safe_error(exc) from exc


@router.get("/attachment")
def download_attachment(
    settings: Annotated[Settings, Depends(get_settings)],
    attachment_ref: str = Query(..., min_length=1, max_length=8192),
    offset: int = Query(0, ge=0, le=20_971_520),
    chunk_size: int = Query(262144, ge=1, le=524288),
) -> dict[str, Any]:
    if not settings.ews_mail_attachments_enabled:
        raise HTTPException(403, "mail_attachments_disabled")
    try:
        context = read_ref(settings, attachment_ref, "attachment")
        folder = resolve_folder(settings, context["mailbox"], context["folder_path"])
        item = selected_item(folder, context["item_id"])
        attachment = next(
            (a for a in item.attachments if getattr(a.attachment_id, "id", None) == context["attachment_id"]), None
        )
        if attachment is None:
            raise HTTPException(404, "attachment_not_found")
        if not isinstance(attachment, FileAttachment):
            raise HTTPException(422, "item_attachment_not_supported")
        maximum = settings.ews_mail_attachment_max_bytes
        if attachment.size is not None and attachment.size > maximum:
            raise HTTPException(413, "attachment_too_large")
        content = bytearray()
        digest = hashlib.sha256()
        total = 0
        with attachment.fp as stream:
            while chunk := stream.read(min(65536, maximum + 1 - total)):
                digest.update(chunk)
                begin = max(0, offset - total)
                finish = min(len(chunk), offset + chunk_size - total)
                if finish > begin:
                    content.extend(chunk[begin:finish])
                total += len(chunk)
                if total > maximum:
                    raise HTTPException(413, "attachment_too_large")
        if offset > total:
            raise HTTPException(422, "attachment_offset_out_of_range")
        complete = offset + len(content) >= total
        return {
            "name": attachment.name,
            "content_type": attachment.content_type,
            "size": total,
            "sha256": digest.hexdigest(),
            "offset": offset,
            "returned_bytes": len(content),
            "complete": complete,
            "next_offset": None if complete else offset + len(content),
            "encoding": "base64",
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
    except Exception as exc:
        raise _safe_error(exc) from exc


@router.get("/reference")
def refresh_reference(
    settings: Annotated[Settings, Depends(get_settings)],
    item_id: str = Query(..., min_length=1, max_length=2048),
    mailbox: str = Query("self", max_length=64),
    folder_path: str = Query("inbox", max_length=256),
) -> dict[str, Any]:
    """Resolve a persisted native identity only inside its configured scope."""
    try:
        if folder_path == "calendar":
            from exchange_ews_bridge.calendar_reads import enabled

            enabled(settings)
            folder = build_account(settings, mailbox=mailbox_address(settings, mailbox)).calendar
            kind = "event"
        else:
            folder = resolve_folder(settings, mailbox, folder_path)
            kind = "message"
        item = selected_item(folder, item_id, fields=["id", "changekey"])
        return {
            "mailbox": mailbox,
            "folder_path": folder_path,
            kind + "_ref": make_ref(
                settings, kind, {"mailbox": mailbox, "folder_path": folder_path, "item_id": item.id}
            ),
        }
    except Exception as exc:
        raise _safe_error(exc) from exc
