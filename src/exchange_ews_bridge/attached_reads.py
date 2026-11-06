"""Bounded selected attached-message reading via native GetAttachment."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from exchangelib import ItemAttachment
from exchangelib.items import Message
from exchangelib.services import GetAttachment
from fastapi import APIRouter, Depends, HTTPException, Query

from exchange_ews_bridge.config import Settings, get_settings
from exchange_ews_bridge.mail_reads import _safe_error, mail_fields, make_ref, read_ref, resolve_folder, selected_item
from exchange_ews_bridge.security import require_bearer_token

router = APIRouter(prefix="/v1/mail", dependencies=[Depends(require_bearer_token)])


@router.get("/attached-message")
def attached_message(
    settings: Annotated[Settings, Depends(get_settings)],
    item_ref: str = Query(..., max_length=8192),
    cursor: str | None = Query(None, max_length=8192),
    body_chars: int = Query(8000, ge=1, le=8000),
) -> dict[str, Any]:
    from exchange_ews_bridge.app import mail_summary

    if not settings.ews_mail_attachments_enabled or not settings.ews_mail_body_enabled:
        raise HTTPException(403, "attached_message_read_disabled")
    ctx = read_ref(settings, item_ref, "attached_message")
    prior = read_ref(settings, cursor, "attached_body") if cursor else None
    if prior and any(prior.get(k) != ctx[k] for k in ("mailbox", "folder_path", "item_id", "attachment_id")):
        raise HTTPException(422, "attached_message_cursor_scope_mismatch")
    try:
        folder = resolve_folder(settings, ctx["mailbox"], ctx["folder_path"])
        parent = selected_item(folder, ctx["item_id"])
        attachment = next(
            (a for a in parent.attachments if getattr(a.attachment_id, "id", None) == ctx["attachment_id"]), None
        )
        if not isinstance(attachment, ItemAttachment):
            raise HTTPException(404, "attached_message_not_found")
        maximum = settings.ews_mail_attachment_max_bytes
        if attachment.size is None or attachment.size > maximum:
            raise HTTPException(413, "attached_message_size_unavailable_or_too_large")
        fetched = GetAttachment(account=folder.account).get(
            items=[attachment.attachment_id],
            include_mime_content=False,
            body_type="Text",
            filter_html_content=True,
            additional_fields={
                f for f in folder.normalize_fields(fields=mail_fields(True)) if not f.field.is_attribute
            },
        )
        item = getattr(fetched, "_item", None)
        if not isinstance(item, Message):
            raise HTTPException(422, "attachment_is_not_message")
        text = str(item.text_body or item.body or "")
        if len(text.encode()) > maximum:
            raise HTTPException(413, "attached_message_too_large")
        digest = hashlib.sha256(text.encode()).hexdigest()
        if prior and prior["sha256"] != digest:
            raise HTTPException(409, "attached_message_changed_restart_read")
        offset = prior["offset"] if prior else 0
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= len(text):
            raise HTTPException(422, "invalid_attached_message_offset")
        chunk = text[offset : offset + body_chars]
        complete = offset + len(chunk) >= len(text)
        return {
            "mailbox": ctx["mailbox"],
            "parent_folder": ctx["folder_path"],
            "attachment_name": attachment.name,
            "message": mail_summary(item, folder=ctx["folder_path"]).model_dump(
                exclude={"id", "changekey", "conversation_id"}
            ),
            "body_text": chunk,
            "offset_chars": offset,
            "total_chars": len(text),
            "complete": complete,
            "sha256_utf8": digest,
            "nested_attachment_coverage": "not_requested",
            "next_cursor": make_ref(settings, "attached_body", {**ctx, "offset": offset + len(chunk), "sha256": digest})
            if not complete
            else None,
        }
    except Exception as exc:
        raise _safe_error(exc) from exc
