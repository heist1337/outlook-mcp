"""Evidence about native EWS coverage, not a promise of server access."""

from __future__ import annotations

import importlib.metadata
import json
from importlib.resources import files
from typing import Annotated, Any

from exchangelib import services
from fastapi import APIRouter, Depends, HTTPException

from exchange_ews_bridge.config import Settings, get_settings
from exchange_ews_bridge.ews import build_account
from exchange_ews_bridge.mail_reads import _safe_error, mailbox_address
from exchange_ews_bridge.operations import WRITE_GROUPS, ReadRequest, serialize
from exchange_ews_bridge.security import require_bearer_token

router = APIRouter(prefix="/v1/ews", dependencies=[Depends(require_bearer_token)])
# Mapping means a bounded use of the operation, never every optional EWS field.
BRIDGE_SCOPES = {
    "FindItem": "mail search; calendar occurrences; contacts and tasks",
    "GetItem": "selected mail, calendar, contacts and tasks",
    "GetAttachment": "selected files and attached messages",
    "FindFolder": "configured roots and immediate children",
    "GetFolder": "folder metadata and effective rights",
    "SyncFolderItems": "one bounded native checkpoint batch",
    "CreateItem": "calendar, drafts, reply/forward drafts, meeting responses, contacts, tasks",
    "UpdateItem": "selected typed fields with NeverOverwrite",
    "DeleteItem": "move selected item to Deleted Items; no hard deletion",
    "CopyItem": "selected mail within configured mailbox",
    "MoveItem": "selected mail within configured mailbox",
    "SendItem": "send one saved draft and save a sent copy",
    "CreateAttachment": "one file of at most 32 KiB on a saved draft",
    "DeleteAttachment": "one selected draft attachment",
    "CreateFolder": "one child folder",
    "UpdateFolder": "rename selected nondistinguished folder",
    "MoveFolder": "move selected nondistinguished folder",
    "ResolveNames": "bounded exact directory candidates",
    "ExpandDL": "selected distribution list",
    "GetRoomLists": "room list metadata",
    "GetRooms": "rooms in selected room list",
    "GetUserAvailability": "up to 20 recipients, at most 31 days, FreeBusy only",
    "GetServerTimeZones": "timezone metadata",
    "GetUserOofSettings": "auto-reply state and selected configuration fingerprint",
    "SetUserOofSettings": "explicit internal/external auto-reply settings",
    "UpdateInboxRules": "typed create/update/delete; preserve Outlook rule blob; forwarding requires send group",
    "GetInboxRules": "bounded rule listing",
    "GetDelegate": "bounded delegate listing; no access changes",
    "GetMailTips": "up to 20 selected recipients",
    "Subscribe": "short-lived pull subscription; no automatic scheduler",
    "GetEvents": "one pull notification batch; reconcile via SyncFolderItems",
    "Unsubscribe": "selected pull subscription",
}


@router.get("/capabilities")
def capabilities(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    catalog = json.loads(files("exchange_ews_bridge").joinpath("operation_catalog.json").read_text())
    native = {
        getattr(services, n).SERVICE_NAME
        for n in dir(services)
        if isinstance(getattr(services, n), type) and getattr(getattr(services, n), "SERVICE_NAME", None)
    }
    for row in catalog["operations"]:
        name = row["operation"]
        row.update(
            native_library_support=name in native,
            bridge_scope=BRIDGE_SCOPES.get(name),
            server_access="not_probed",
            bridge_status="implemented_bounded_scope"
            if name in BRIDGE_SCOPES
            else "native_library_only"
            if name in native
            else "not_in_pinned_native_library",
        )
    return {
        **catalog,
        "library_version": importlib.metadata.version("exchangelib"),
        "read_actions": list(ReadRequest.model_fields["action"].annotation.__args__),
        "write_actions": WRITE_GROUPS,
        "extended_reads_enabled": settings.ews_extended_reads_enabled,
        "enabled_write_groups": settings.ews_enabled_write_groups.split(",")
        if settings.ews_enabled_write_groups
        else [],
        "write_token_configured": settings.ews_write_api_token is not None,
        "read_roots": settings.ews_mail_read_roots.split(","),
        "counts": {
            "documented_operations": len(catalog["operations"]),
            "native_catalog_operations": sum(r["native_library_support"] for r in catalog["operations"]),
            "bridge_catalog_operations": sum(bool(r["bridge_scope"]) for r in catalog["operations"]),
        },
        "limits": "Catalogue coverage does not establish full parameter coverage, deployment, permissions, or runtime success.",
    }


@router.get("/inspect")
def inspect_access(settings: Annotated[Settings, Depends(get_settings)], mailbox: str = "self") -> dict[str, Any]:
    if not settings.ews_extended_reads_enabled:
        raise HTTPException(403, "extended_reads_disabled")
    try:
        account = build_account(settings, mailbox=mailbox_address(settings, mailbox))
        folders = {}
        for name in ("calendar", "contacts", "tasks"):
            try:
                folder = getattr(account, name)
                folder.refresh()
                folders[name] = {"effective_rights": serialize(folder.effective_rights), "status": "verified_read"}
            except Exception:
                folders[name] = {"status": "probe_failed", "rights": "unknown"}
        return {
            "mailbox": mailbox,
            "build": str(account.version.build),
            "api_version": account.version.api_version,
            "server": account.version.fullname,
            "folders": folders,
            "rights_do_not_prove": "Each operation still needs native server acceptance; no mutation was performed.",
        }
    except Exception as exc:
        raise _safe_error(exc) from exc
