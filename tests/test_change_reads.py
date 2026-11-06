from types import SimpleNamespace
from unittest.mock import Mock

from exchange_ews_bridge.mail_reads import make_ref, read_ref


def test_one_batch_checkpoint_and_delete_read_flag(client, monkeypatch):
    http, settings, headers = client
    folder = SimpleNamespace(id="folder-1", account=Mock())
    monkeypatch.setattr("exchange_ews_bridge.change_reads.resolve_folder", lambda *a: folder)
    service = Mock()
    service.sync_state = "native-state"
    service.includes_last_item_in_range = False
    service.call.return_value = [
        ("create", SimpleNamespace(id="a", changekey="1")),
        ("delete", SimpleNamespace(id="b")),
        ("read_flag_change", (SimpleNamespace(id="c"), True)),
    ]
    monkeypatch.setattr("exchange_ews_bridge.change_reads.SyncFolderItems", lambda **k: service)
    response = http.get("/v1/mail/changes", headers=headers).json()
    assert response["bootstrap"] and not response["caught_up"]
    checkpoint = read_ref(settings, response["checkpoint"], "sync")
    assert checkpoint["expires"] is None and checkpoint["sync_state"] == "native-state"
    assert response["changes"][1]["message_ref"] is None
    assert response["changes"][2]["is_read"] is True
    service.call.assert_called_once()
    assert service.call.call_args.kwargs["shape"] == "IdOnly"
    service.includes_last_item_in_range = True
    result = http.get("/v1/mail/changes", params={"checkpoint": response["checkpoint"]}, headers=headers).json()
    assert result["caught_up"] and not result["bootstrap"]
    assert service.call.call_args.kwargs["sync_state"] == "native-state"
    folder.id = "moved-folder"
    assert (
        http.get("/v1/mail/changes", params={"checkpoint": response["checkpoint"]}, headers=headers).status_code == 409
    )


def test_bad_checkpoint_and_disabled_calendar_fail_closed(client, monkeypatch):
    http, settings, headers = client
    assert http.get("/v1/mail/changes", params={"folder_path": "calendar"}, headers=headers).status_code == 403
    assert (
        http.get(
            "/v1/calendar/context",
            params={"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z"},
            headers=headers,
        ).status_code
        == 403
    )
    ref = make_ref(
        settings, "sync", {"mailbox": "self", "folder_path": "inbox", "folder_id": "1", "sync_state": "state"}, ttl=None
    )
    with __import__("pytest").raises(Exception):
        read_ref(settings, ref, "message")


def test_new_selected_and_calendar_fields_match_native_models():
    from exchangelib.folders import Calendar, Messages

    from exchange_ews_bridge.calendar_reads import FIELDS
    from exchange_ews_bridge.mail_reads import mail_fields

    Messages(id="m").all().only(*mail_fields(True), "headers")
    Calendar(id="c").all().only(*FIELDS, "text_body", "body", "parent_folder_id")


def test_invalid_native_state_returns_no_advanced_checkpoint(client, monkeypatch):
    from exchangelib.errors import ErrorInvalidSyncStateData

    http, _, headers = client
    monkeypatch.setattr(
        "exchange_ews_bridge.change_reads.resolve_folder", lambda *a: SimpleNamespace(id="folder", account=Mock())
    )
    service = Mock()
    service.call.side_effect = ErrorInvalidSyncStateData("obsolete")
    monkeypatch.setattr("exchange_ews_bridge.change_reads.SyncFolderItems", lambda **k: service)
    response = http.get("/v1/mail/changes", headers=headers)
    assert response.status_code == 409
    assert response.json() == {"detail": "sync_state_invalid_rebootstrap"}


def test_extra_mail_roots_fail_closed_before_account_access(client, monkeypatch):
    import pytest
    from fastapi import HTTPException

    from exchange_ews_bridge.mail_reads import resolve_folder

    _, settings, _ = client
    build = Mock(side_effect=AssertionError("must not authenticate"))
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.build_account", build)
    with pytest.raises(HTTPException) as caught:
        resolve_folder(settings, "self", "deleted")
    assert caught.value.status_code == 403
    build.assert_not_called()


def test_index_filtered_empty_page_retains_scanned_continuation(client, monkeypatch):
    from datetime import UTC, datetime

    http, settings, headers = client
    query = Mock()
    query.only.return_value = query
    query.order_by.return_value = query
    query.__class__ = Mock
    # The native QuerySet slice can return out-of-window index candidates.
    from unittest.mock import MagicMock

    query = MagicMock()
    query.only.return_value = query
    query.order_by.return_value = query
    query.__getitem__.return_value = [
        SimpleNamespace(datetime_received=datetime(2026, 9, 1, tzinfo=UTC)),
        SimpleNamespace(datetime_received=datetime(2026, 9, 1, tzinfo=UTC)),
    ]
    folder = Mock()
    folder.filter.return_value = query
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", lambda *a: folder)
    response = http.get(
        "/v1/mail/search",
        headers=headers,
        params={"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z", "words": "обещание", "limit": 1},
    )
    assert response.status_code == 200
    value = response.json()
    assert value["messages"] == [] and value["has_more"]
    assert read_ref(settings, value["next_cursor"], "search")["offset"] == 1
    assert len(folder.filter.call_args.args) == 1


def test_native_identity_reference_refresh_checks_actual_folder(client, monkeypatch):
    http, settings, headers = client
    folder = Mock()
    folder.id = "folder-1"
    folder.all.return_value = folder
    folder.only.return_value = folder
    folder.get.return_value = SimpleNamespace(
        id="durable-native", changekey="new", parent_folder_id=SimpleNamespace(id="folder-1")
    )
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", lambda *args: folder)
    response = http.get(
        "/v1/mail/reference", headers=headers, params={"item_id": "durable-native", "folder_path": "sent"}
    )
    assert response.status_code == 200
    assert read_ref(settings, response.json()["message_ref"], "message")["folder_path"] == "sent"
    folder.get.return_value.parent_folder_id.id = "outside"
    assert http.get("/v1/mail/reference", headers=headers, params={"item_id": "durable-native"}).status_code == 403


def test_attached_email_uses_native_getattachment_without_mime(client, monkeypatch):
    from exchangelib import ItemAttachment, Message
    from exchangelib.attachments import AttachmentId
    from exchangelib.folders import Messages

    from exchange_ews_bridge.mail_reads import make_ref

    http, settings, headers = client
    settings.ews_mail_body_enabled = True
    attachment = ItemAttachment(attachment_id=AttachmentId(id="a"), size=20)
    folder = SimpleNamespace(account=Mock(), normalize_fields=Messages(id="folder").normalize_fields)
    monkeypatch.setattr("exchange_ews_bridge.attached_reads.resolve_folder", lambda *a: folder)
    monkeypatch.setattr(
        "exchange_ews_bridge.attached_reads.selected_item", lambda *a: SimpleNamespace(attachments=[attachment])
    )
    result = ItemAttachment(item=Message(id="embedded", subject="Forwarded", text_body="promise"))
    service = Mock()
    service.get.return_value = result
    monkeypatch.setattr("exchange_ews_bridge.attached_reads.GetAttachment", lambda **k: service)
    ref = make_ref(
        settings,
        "attached_message",
        {"mailbox": "self", "folder_path": "sent", "item_id": "parent", "attachment_id": "a"},
    )
    response = http.get("/v1/mail/attached-message", headers=headers, params={"item_ref": ref})
    assert response.status_code == 200, response.text
    assert response.json()["body_text"] == "promise"
    assert service.get.call_args.kwargs["include_mime_content"] is False
    assert all(not f.field.is_attribute for f in service.get.call_args.kwargs["additional_fields"])
    attachment.size = None
    assert http.get("/v1/mail/attached-message", headers=headers, params={"item_ref": ref}).status_code == 413


def test_native_sync_error_xml_decodes_invalid_state_without_advancing():
    import pytest
    from exchangelib.errors import ErrorInvalidSyncStateData
    from lxml import etree

    from exchange_ews_bridge.change_reads import SyncFolderItems

    service = SyncFolderItems.__new__(SyncFolderItems)
    service.sync_state = "prior"
    service.includes_last_item_in_range = None
    response = etree.fromstring(
        b'<m:SyncFolderItemsResponseMessage xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" ResponseClass="Error"><m:ResponseCode>ErrorInvalidSyncStateData</m:ResponseCode><m:MessageText>obsolete</m:MessageText></m:SyncFolderItemsResponseMessage>'
    )
    with pytest.raises(ErrorInvalidSyncStateData):
        service._get_element_container(response)
    assert service.sync_state == "prior"


def test_native_change_key_matches_selected_mail_and_calendar(client, monkeypatch):
    import hashlib

    from exchange_ews_bridge.app import stable_mail_source_key

    http, settings, headers = client
    folder = SimpleNamespace(id="folder", account=Mock())
    monkeypatch.setattr("exchange_ews_bridge.change_reads.resolve_folder", lambda *a: folder)
    service = Mock(sync_state="next", includes_last_item_in_range=True)
    item = SimpleNamespace(id="native-id", changekey="v")
    service.call.return_value = [("delete", item)]
    monkeypatch.setattr("exchange_ews_bridge.change_reads.SyncFolderItems", lambda **k: service)
    response = http.get("/v1/mail/changes", headers=headers).json()
    assert response["changes"][0]["source_key"] == stable_mail_source_key(item)
    settings.ews_calendar_context_enabled = True
    monkeypatch.setattr(
        "exchange_ews_bridge.change_reads.build_account", lambda *a, **k: SimpleNamespace(calendar=folder)
    )
    response = http.get("/v1/mail/changes", headers=headers, params={"folder_path": "calendar"}).json()
    assert response["changes"][0]["source_key"] == hashlib.sha256(b"native-id").hexdigest()


@__import__("pytest").mark.parametrize("offset", ["Z", "+03:00"])
def test_calendar_http_dates_serialize_with_native_timezone(client, monkeypatch, offset):
    from datetime import UTC, datetime

    from exchangelib.properties import CalendarView
    from exchangelib.version import Version

    http, settings, headers = client
    settings.ews_calendar_context_enabled = True
    captured = {}

    def view(**kwargs):
        captured.update(kwargs)
        # Real native serialization rejects pydantic_core.TzInfo, even for UTC.
        CalendarView(**kwargs).to_xml(version=Version(build=None, api_version="Exchange2016"))
        result = Mock()
        result.only.return_value.order_by.return_value.__getitem__ = Mock(return_value=[])
        return result

    account = SimpleNamespace(calendar=SimpleNamespace(view=view))
    monkeypatch.setattr("exchange_ews_bridge.calendar_reads.build_account", lambda *a, **k: account)
    response = http.get(
        "/v1/calendar/context",
        params={"start": "2026-11-27T00:00:00" + offset, "end": "2026-11-30T00:00:00" + offset},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["complete"]
    assert captured["start"].tzinfo is UTC
    assert captured["start"] == datetime.fromisoformat("2026-11-27T00:00:00" + offset)
