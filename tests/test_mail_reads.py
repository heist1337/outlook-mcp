from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from exchangelib import FileAttachment, ItemAttachment
from exchangelib.attachments import AttachmentId
from exchangelib.folders import Messages

from exchange_ews_bridge.mail_reads import mail_fields, make_ref, read_ref


def message_ref(settings, folder_path="inbox"):
    return make_ref(settings, "message", {"mailbox": "self", "folder_path": folder_path, "item_id": "message-1"})


def file_attachment(content=b"registry"):
    attachment = FileAttachment(
        name="../registry.xlsx",
        size=len(content),
        content_type="application/octet-stream",
        attachment_id=AttachmentId(id="file-1"),
    )
    attachment._fp = io.BytesIO(content)
    return attachment


def folder_with_message(monkeypatch, attachments):
    folder = Mock()
    folder.id = "folder-1"
    folder.all.return_value = folder
    folder.only.return_value = folder
    folder.get.return_value = SimpleNamespace(attachments=attachments, parent_folder_id=SimpleNamespace(id="folder-1"))
    resolver = Mock(return_value=folder)
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", resolver)
    return folder, resolver


def test_attachment_download_exact_bytes_no_exchange_writes(client, monkeypatch):
    http, settings, headers = client
    folder, _ = folder_with_message(monkeypatch, [file_attachment()])
    listed = http.get("/v1/mail/attachments", params={"message_ref": message_ref(settings)}, headers=headers)
    assert listed.status_code == 200
    entry = listed.json()["attachments"][0]
    response = http.get("/v1/mail/attachment", params={"attachment_ref": entry["attachment_ref"]}, headers=headers)
    assert response.status_code == 200
    assert base64.b64decode(response.json()["content_base64"]) == b"registry"
    assert response.json()["size"] == 8
    assert len(response.json()["sha256"]) == 64
    assert folder.get.call_count == 2
    for call in folder.only.call_args_list:
        assert set(call.args) == {"id", "changekey", "attachments", "parent_folder_id"}


def test_auth_gate_and_invalid_reference_fail_before_ews(client, monkeypatch):
    http, settings, headers = client
    resolver = Mock(side_effect=AssertionError("must not query Exchange"))
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", resolver)
    ref = message_ref(settings)
    assert http.get("/v1/mail/attachments", params={"message_ref": ref}).status_code == 401
    assert (
        http.get("/v1/mail/attachments", params={"message_ref": ref + "tampered"}, headers=headers).status_code == 422
    )
    settings.ews_mail_attachments_enabled = False
    assert http.get("/v1/mail/attachments", params={"message_ref": ref}, headers=headers).status_code == 403
    resolver.assert_not_called()


def test_download_chunks_reassemble_with_full_file_hash(client, monkeypatch):
    http, settings, headers = client
    folder, _ = folder_with_message(monkeypatch, [])
    folder.get.side_effect = lambda **_: SimpleNamespace(
        attachments=[file_attachment(b"abcdefgh")], parent_folder_id=SimpleNamespace(id="folder-1")
    )
    listed = http.get("/v1/mail/attachments", params={"message_ref": message_ref(settings)}, headers=headers).json()
    ref = listed["attachments"][0]["attachment_ref"]
    first = http.get("/v1/mail/attachment", params={"attachment_ref": ref, "chunk_size": 3}, headers=headers).json()
    second = http.get(
        "/v1/mail/attachment",
        params={"attachment_ref": ref, "offset": first["next_offset"], "chunk_size": 5},
        headers=headers,
    ).json()
    assert first["size"] == second["size"] == 8
    assert first["sha256"] == second["sha256"]
    assert not first["complete"] and second["complete"]
    assert base64.b64decode(first["content_base64"]) + base64.b64decode(second["content_base64"]) == b"abcdefgh"
    assert (
        http.get("/v1/mail/attachment", params={"attachment_ref": ref, "offset": 9}, headers=headers).status_code == 422
    )


def test_shared_alias_revocation_blocks_existing_signed_reference(client, monkeypatch):
    http, settings, headers = client
    ref = make_ref(settings, "message", {"mailbox": "shared", "folder_path": "inbox", "item_id": "message-1"})
    settings.ews_shared_mailbox = ""
    resolver = Mock(side_effect=AssertionError("must not query Exchange"))
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", resolver)
    assert http.get("/v1/mail/attachments", params={"message_ref": ref}, headers=headers).status_code == 403
    resolver.assert_not_called()


def test_oversize_misreported_size_and_item_attachments(client, monkeypatch):
    http, settings, headers = client
    big = file_attachment(b"x" * 1025)
    item = ItemAttachment(name="forwarded.eml", attachment_id=AttachmentId(id="item-1"))
    folder_with_message(monkeypatch, [big, item])
    listed = http.get("/v1/mail/attachments", params={"message_ref": message_ref(settings)}, headers=headers).json()
    assert all(not entry["downloadable"] for entry in listed["attachments"])
    big.size = 1  # An untrusted reported length must not bypass the actual byte limit.
    listed = http.get("/v1/mail/attachments", params={"message_ref": message_ref(settings)}, headers=headers).json()
    ref = listed["attachments"][0]["attachment_ref"]
    response = http.get("/v1/mail/attachment", params={"attachment_ref": ref}, headers=headers)
    assert response.status_code == 413
    assert response.json() == {"detail": "attachment_too_large"}


def test_reference_expiry_kind_and_allowlist_revocation(client, monkeypatch):
    _, settings, _ = client
    ref = message_ref(settings)
    with pytest.raises(Exception, match="invalid_mail_reference"):
        read_ref(settings, ref, "attachment")
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.time.time", lambda: 10**12)
    with pytest.raises(Exception, match="invalid_mail_reference"):
        read_ref(settings, ref, "message")


def test_historical_search_paging_and_scope(client, monkeypatch):
    http, settings, headers = client
    item = SimpleNamespace(id="message-1", subject="Example subject", has_attachments=True)

    # Use a simple query chain with real slice behavior.
    class FakeQuery:
        def __init__(self):
            self.slices = []

        def filter(self, **kwargs):
            return self

        def only(self, *fields):
            assert "attachments" not in fields
            return self

        def order_by(self, *fields):
            return self

        def __getitem__(self, value):
            self.slices.append(value)
            return [item, item][value]

    query = FakeQuery()
    folder = Mock()
    folder.filter.return_value = query
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", Mock(return_value=folder))
    params = {"start": "2025-12-01T00:00:00Z", "end": "2026-01-01T00:00:00Z", "subject": "РГС", "limit": 1}
    first = http.get("/v1/mail/search", params=params, headers=headers)
    assert first.status_code == 200
    assert first.json()["has_more"] is True
    assert "id" not in first.json()["messages"][0]
    read_ref(settings, first.json()["messages"][0]["message_ref"], "message")
    second = http.get("/v1/mail/search", params={**params, "cursor": first.json()["next_cursor"]}, headers=headers)
    assert second.status_code == 200
    assert second.json()["complete"] is True
    mismatch = http.get(
        "/v1/mail/search", params={**params, "subject": "other", "cursor": first.json()["next_cursor"]}, headers=headers
    )
    assert mismatch.status_code == 422
    assert query.slices == [slice(0, 2), slice(1, 3)]


def test_shared_mailbox_folder_and_time_boundaries(client, monkeypatch):
    http, settings, headers = client
    account = Mock()
    account.inbox = Mock()
    account.inbox.folder_class = "IPF.Note"
    account.inbox.total_count = 10
    account.inbox.children = [SimpleNamespace(name="2026", folder_class="IPF.Note", total_count=3)]
    builder = Mock(return_value=account)
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.build_account", builder)
    response = http.get("/v1/mail/folders", params={"mailbox": "shared"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["folder_class"] == "IPF.Note"
    assert response.json()["total_count"] == 10
    builder.assert_called_once_with(settings, mailbox="settlements@example.com")
    response = http.get("/v1/mail/folders", params={"mailbox": "arbitrary@example.com"}, headers=headers)
    assert response.status_code == 403
    assert http.get("/v1/mail/folders", params={"folder_path": "inbox/../sent"}, headers=headers).status_code == 422
    start = datetime(2025, 12, 1, tzinfo=UTC)
    for end in (start, start + timedelta(days=91)):
        response = http.get(
            "/v1/mail/search", params={"start": start.isoformat(), "end": end.isoformat()}, headers=headers
        )
        assert response.status_code == 422


def test_folder_inventory_paginates_and_distinguishes_non_mail(client, monkeypatch):
    http, _, headers = client
    children = [SimpleNamespace(name=str(i), folder_class="IPF.Note", total_count=i) for i in range(101)]
    children[-1].folder_class = "IPF.Appointment"
    folder = SimpleNamespace(children=children, folder_class=None, total_count=0)
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", Mock(return_value=folder))
    first = http.get("/v1/mail/folders", params={"limit": 100}, headers=headers).json()
    assert not first["complete"] and first["next_offset"] == 100
    assert len(first["folders"]) == 100
    last = http.get("/v1/mail/folders", params={"limit": 100, "offset": first["next_offset"]}, headers=headers).json()
    assert last["complete"] and last["next_offset"] is None
    assert last["folders"][0]["folder_class"] == "IPF.Appointment"
    assert last["folders"][0]["folder_path"] == "inbox/100"


@pytest.mark.parametrize("kind,path", [("message", "/v1/mail/attachments"), ("attachment", "/v1/mail/attachment")])
def test_item_from_other_folder_is_denied_before_attachment_access(client, monkeypatch, kind, path):
    http, settings, headers = client
    folder, _ = folder_with_message(monkeypatch, [])
    folder.get.return_value = SimpleNamespace(parent_folder_id=SimpleNamespace(id="another-mailbox-folder"))
    ref = make_ref(
        settings,
        kind,
        {"mailbox": "self", "folder_path": "inbox", "item_id": "foreign-item", "attachment_id": "file-1"},
    )
    response = http.get(path, params={kind + "_ref": ref}, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "mail_item_outside_selected_folder"


def test_api_token_cannot_sign_reference(client, monkeypatch):
    http, settings, headers = client
    resolver = Mock(side_effect=AssertionError("must not query Exchange"))
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", resolver)
    payload = {
        "kind": "message",
        "expires": 10**12,
        "mailbox": "self",
        "folder_path": "inbox",
        "item_id": "foreign-item",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signature = hmac.new(
        settings.bridge_api_token.get_secret_value().encode(), encoded.encode(), hashlib.sha256
    ).digest()
    forged = encoded + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    assert http.get("/v1/mail/attachments", params={"message_ref": forged}, headers=headers).status_code == 422
    resolver.assert_not_called()


def test_native_folder_query_supports_restricted_attachment_fields():
    query = Messages(id="synthetic-folder").all().only("id", "changekey", "attachments", "parent_folder_id")
    assert {field.field.name for field in query.only_fields} == {"id", "changekey", "attachments", "parent_folder_id"}


def test_selected_body_pages_unicode_restart_and_scope(client, monkeypatch):
    http, settings, headers = client
    settings.ews_mail_body_enabled = True
    folder, _ = folder_with_message(monkeypatch, [])
    item = SimpleNamespace(
        id="message-1",
        subject="Реестр",
        text_body="абв🙂д",
        body=None,
        message_id="<registry@example.com>",
        parent_folder_id=SimpleNamespace(id="folder-1"),
    )
    folder.get.return_value = item
    ref = message_ref(settings)
    first = http.get("/v1/mail/message", params={"message_ref": ref, "body_chars": 3}, headers=headers)
    assert first.status_code == 200
    first = first.json()
    assert first["body_text"] == "абв" and not first["complete"]
    assert first["message"]["internet_message_id"] == "<registry@example.com>"
    second = http.get(
        "/v1/mail/message", params={"message_ref": ref, "cursor": first["next_cursor"]}, headers=headers
    ).json()
    assert second["body_text"] == "🙂д" and second["complete"]
    assert first["sha256_utf8"] == hashlib.sha256("абв🙂д".encode()).hexdigest()
    item.text_body = "изменено"
    assert (
        http.get(
            "/v1/mail/message", params={"message_ref": ref, "cursor": first["next_cursor"]}, headers=headers
        ).status_code
        == 409
    )
    item.parent_folder_id.id = "foreign-folder"
    assert http.get("/v1/mail/message", params={"message_ref": ref}, headers=headers).status_code == 403
    settings.ews_mail_body_enabled = False
    folder.reset_mock()
    assert http.get("/v1/mail/message", params={"message_ref": ref}, headers=headers).status_code == 403
    folder.get.assert_not_called()


@pytest.mark.parametrize("native_text_body", [False, True])
def test_complete_html_body_is_rendered_without_removing_quoted_answers(client, monkeypatch, native_text_body):
    from exchangelib.properties import HTMLBody

    http, settings, headers = client
    settings.ews_mail_body_enabled = True
    folder, _ = folder_with_message(monkeypatch, [])
    html = HTMLBody(
        "<html><head><style>transport-css</style></head><body><p>Принято</p>"
        "<blockquote><p>Доля заёмщика — 100%.</p><p>Ответ: оставить 100%.</p></blockquote>"
        "<table><tr><td>Лимит</td><td>500 ₽</td></tr></table>"
        '<a href="HTTPS://example.com/terms">Условия</a>'
        '<img src="cid:chart" alt="Схема продаж"><script>transport-js</script></body></html>'
    )
    folder.get.return_value = SimpleNamespace(
        id="message-1",
        text_body=html if native_text_body else None,
        body=None if native_text_body else html,
        parent_folder_id=SimpleNamespace(id="folder-1"),
    )
    first = http.get(
        "/v1/mail/message", params={"message_ref": message_ref(settings), "body_chars": 20}, headers=headers
    ).json()
    second = http.get(
        "/v1/mail/message",
        params={"message_ref": message_ref(settings), "cursor": first["next_cursor"]},
        headers=headers,
    ).json()
    text = first["body_text"] + second["body_text"]
    assert first["body_format"] == second["body_format"] == "text"
    assert first["source_body_format"] == "html"
    assert second["complete"]
    for fragment in (
        "Принято",
        "Доля заёмщика — 100%.",
        "Ответ: оставить 100%.",
        "500 ₽",
        "HTTPS://example.com/terms",
        "Схема продаж",
    ):
        assert fragment in text
    assert "Лимит\t500 ₽" in text
    assert all(noise not in text for noise in ("<html", "<blockquote", "transport-css", "transport-js", "cid:chart"))
    assert first["sha256_utf8"] == hashlib.sha256(text.encode()).hexdigest()
    assert first["total_chars"] == len(text)


def test_native_filter_serialization_matches_locked_library():
    from exchangelib import Q
    from exchangelib.properties import ConversationId
    from exchangelib.util import xml_to_str
    from exchangelib.version import EXCHANGE_2016, Version

    folder = Messages(id="synthetic-folder")
    query = Q(
        sender="person@example.com",
        is_read=False,
        has_attachments=True,
        conversation_id=ConversationId(id="native-conversation"),
    )
    xml = xml_to_str(query.to_xml(folders=[folder], version=Version(build=EXCHANGE_2016), applies_to="items"))
    assert "item:ConversationId" in xml and "native-conversation" in xml
    assert "person@example.com" in xml
    fields = folder.all().only(*mail_fields(True), "parent_folder_id")
    assert "message_id" in {field.field.name for field in fields.only_fields}


def test_conversation_scope_rejected_before_exchange(client, monkeypatch):
    http, settings, headers = client
    resolver = Mock(side_effect=AssertionError("must not query Exchange"))
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", resolver)
    ref = make_ref(settings, "conversation", {"mailbox": "shared", "folder_path": "inbox", "conversation_id": "thread"})
    response = http.get(
        "/v1/mail/search",
        params={"start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z", "conversation_ref": ref},
        headers=headers,
    )
    assert response.status_code == 422
    resolver.assert_not_called()


@pytest.mark.parametrize("html_body", [False, True])
def test_optional_unique_body_preserves_original(client, monkeypatch, html_body):
    from exchangelib.properties import Body, HTMLBody

    http, settings, headers = client
    settings.ews_mail_body_enabled = True
    folder, _ = folder_with_message(monkeypatch, [])
    unique = (
        HTMLBody('<style>noise</style><p>New reply</p><p>Цена &amp; <a href="https://example.com">terms</a></p>')
        if html_body
        else Body("New reply")
    )
    item = SimpleNamespace(
        id="message-1",
        text_body="New reply\nQuoted history",
        body=None,
        unique_body=unique,
        changekey="version-1",
        parent_folder_id=SimpleNamespace(id="folder-1"),
    )
    folder.get.return_value = item
    response = http.get(
        "/v1/mail/message", params={"message_ref": message_ref(settings), "include_unique_body": True}, headers=headers
    )
    data = response.json()
    assert response.status_code == 200
    assert data["body_text"] == item.text_body
    assert data["unique_body_status"] == "available"
    assert "Quoted history" not in data["unique_body_text"]
    assert "New reply" in data["unique_body_text"]
    assert "noise" not in data["unique_body_text"]
    assert data["unique_body_sha256"] == hashlib.sha256(data["unique_body_text"].encode()).hexdigest()
    if html_body:
        assert "https://example.com" in data["unique_body_text"]
        assert "Цена &" in data["unique_body_text"]


@pytest.mark.parametrize("mode", ["missing", "oversized", "error", "changed"])
def test_unique_body_failure_keeps_complete_original(client, monkeypatch, mode):
    from exchangelib.properties import Body

    http, settings, headers = client
    settings.ews_mail_body_enabled = True
    folder, _ = folder_with_message(monkeypatch, [])
    item = SimpleNamespace(
        id="message-1", text_body="Original", body=None, changekey="v1", parent_folder_id=SimpleNamespace(id="folder-1")
    )
    candidate = SimpleNamespace(
        parent_folder_id=item.parent_folder_id,
        changekey="v2" if mode == "changed" else "v1",
        unique_body=Body("x" * 8001) if mode == "oversized" else None,
    )
    folder.get.side_effect = [item, RuntimeError("unavailable") if mode == "error" else candidate]
    data = http.get(
        "/v1/mail/message", params={"message_ref": message_ref(settings), "include_unique_body": True}, headers=headers
    ).json()
    assert data["body_text"] == "Original" and data["complete"]
    assert "unique_body_text" not in data
    assert (
        data["unique_body_status"]
        == {"missing": "unavailable", "oversized": "too_large", "error": "unavailable", "changed": "changed"}[mode]
    )


@pytest.mark.parametrize("tag", ["style", "script"])
def test_unterminated_hidden_html_is_not_returned_as_usable_text(tag):
    from exchangelib.properties import HTMLBody

    from exchange_ews_bridge.mail_reads import render_body_text

    with pytest.raises(ValueError, match="unbalanced_mail_html"):
        render_body_text(HTMLBody(f"<p>First answer</p><{tag}>Lost answer"))


@pytest.mark.parametrize(
    "markup",
    [
        "<html><head><title>Subject</title><meta charset=utf8><body><p>First answer<p>Second answer",
        "<p>First answer</p><head><p>Second answer</p>",
        "<head><title>Subject</title>First answer<br>Second answer",
        "<head><head><body><p>First answer<p>Second answer",
        "<p>First answer</p><head><head><p>Second answer",
    ],
)
def test_optional_head_and_paragraph_end_tags_keep_body_words(markup):
    from exchangelib.properties import HTMLBody

    from exchange_ews_bridge.mail_reads import render_body_text

    text = render_body_text(HTMLBody(markup))
    assert "First answer" in text and "Second answer" in text
    assert "Subject" not in text


def test_optional_cell_end_tags_do_not_merge_values():
    from exchangelib.properties import HTMLBody

    from exchange_ews_bridge.mail_reads import render_body_text

    text = render_body_text(
        HTMLBody("<table><tr><td>Лимит<td>100<td>500</tr></table><dl><dt>Срок</dt><dd>30 дней</dd></dl>")
    )
    assert "Лимит\t100\t500" in text
    assert "Срок\n30 дней" in text
    assert "100500" not in text
