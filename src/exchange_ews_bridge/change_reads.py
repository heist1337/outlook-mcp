"""One native EWS change batch with durable, scope-bound continuation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any

from exchangelib.errors import ErrorInvalidSyncStateData
from exchangelib.services import SyncFolderItems as NativeSyncFolderItems
from exchangelib.services.common import EWSAccountService
from fastapi import APIRouter, Depends, HTTPException, Query

from exchange_ews_bridge.config import Settings, get_settings
from exchange_ews_bridge.ews import build_account
from exchange_ews_bridge.mail_reads import _safe_error, make_ref, read_ref, reference_for_message, resolve_folder
from exchange_ews_bridge.security import require_bearer_token


class SyncFolderItems(NativeSyncFolderItems):
    """Pinned 5.6.0 parses absent SyncState before ResponseCode on errors.

    Delegate error messages to the library's own error decoder first. Successful
    responses, transport, throttling and SOAP serialization remain native.
    """

    def _get_element_container(self, message, name=None):
        if message.get("ResponseClass") != "Success":
            return EWSAccountService._get_element_container(self, message=message, name=name)
        return super()._get_element_container(message=message, name=name)


router = APIRouter(prefix="/v1/mail", dependencies=[Depends(require_bearer_token)])


@router.get("/changes")
def read_changes(
    settings: Annotated[Settings, Depends(get_settings)],
    mailbox: str = Query("self", max_length=64),
    folder_path: str = Query("inbox", max_length=256),
    checkpoint: str | None = Query(None, max_length=131072),
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    try:
        if folder_path == "calendar":
            from exchange_ews_bridge.calendar_reads import enabled
            from exchange_ews_bridge.mail_reads import mailbox_address

            enabled(settings)
            folder = build_account(settings, mailbox=mailbox_address(settings, mailbox)).calendar
        else:
            folder = resolve_folder(settings, mailbox, folder_path)
        scope = {"mailbox": mailbox, "folder_path": folder_path, "folder_id": folder.id}
        prior = read_ref(settings, checkpoint, "sync") if checkpoint else None
        if prior and any(prior.get(k) != v for k, v in scope.items()):
            raise HTTPException(409, "sync_scope_changed_rebootstrap")
        service = SyncFolderItems(account=folder.account)
        # call() is one EWS response; folder.sync_items() drains multiple pages.
        batch = list(
            service.call(
                folder=folder,
                shape="IdOnly",
                additional_fields=None,
                sync_state=prior["sync_state"] if prior else None,
                ignore=None,
                max_changes_returned=limit,
                sync_scope="NormalItems",
            )
        )
        if len(batch) > limit or not service.sync_state or not isinstance(service.includes_last_item_in_range, bool):
            raise HTTPException(502, "incomplete_sync_response")
        changes = []
        for change_type, value in batch:
            if isinstance(value, Exception):
                raise value
            item, is_read = value if change_type == "read_flag_change" else (value, None)
            if not item.id:
                raise HTTPException(502, "sync_item_identity_missing")
            # Delete identifiers remain useful even though GetItem is impossible.
            from exchange_ews_bridge.app import stable_mail_source_key

            source_key = (
                hashlib.sha256(str(item.id).encode()).hexdigest()
                if folder_path == "calendar"
                else stable_mail_source_key(item)
            )
            changes.append(
                {
                    "change": change_type,
                    "source_key": source_key,
                    "item_id": item.id,
                    "version": getattr(item, "changekey", None),
                    "is_read": is_read,
                    "message_ref": reference_for_message(settings, item, mailbox, folder_path)
                    if change_type in {"create", "update"} and folder_path != "calendar"
                    else None,
                    "event_ref": make_ref(settings, "event", {**scope, "item_id": item.id})
                    if change_type in {"create", "update"} and folder_path == "calendar"
                    else None,
                }
            )
        return {
            **{k: v for k, v in scope.items() if k != "folder_id"},
            "changes": changes,
            "checkpoint": make_ref(settings, "sync", {**scope, "sync_state": service.sync_state}, ttl=None),
            "caught_up": service.includes_last_item_in_range,
            "bootstrap": prior is None,
            "observed_at": datetime.now(UTC).isoformat(),
            "checkpoint_policy": "no_time_expiry; credential rotation or server invalidation requires rebootstrap",
        }
    except ErrorInvalidSyncStateData as exc:
        raise HTTPException(409, "sync_state_invalid_rebootstrap") from exc
    except Exception as exc:
        raise _safe_error(exc) from exc
