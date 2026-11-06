from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import pytest
from exchangelib import CalendarItem, Mailbox, Message
from exchangelib.version import Build, Version
from fastapi import HTTPException
from pydantic import SecretStr

from exchange_ews_bridge import operations as ops
from exchange_ews_bridge.mail_reads import make_ref

WRITE_HEADERS = {"Authorization": "Bearer write-token-separate-32-characters-minimum"}


def enable(client, tmp_path, groups="calendar,mail,send,contacts,tasks,folders,settings,subscriptions"):
    http, settings, _ = client
    settings.ews_extended_reads_enabled = True
    settings.ews_enabled_write_groups = groups
    settings.ews_write_api_token = SecretStr(WRITE_HEADERS["Authorization"].removeprefix("Bearer "))
    settings.data_dir = tmp_path
    return http, settings


def command(**changes):
    return {
        "action": "calendar.create",
        "request_id": "birthday-test-001",
        "values": {
            "subject": "Birthday",
            "start": "2026-11-28T00:00:00+03:00",
            "end": "2026-11-29T00:00:00+03:00",
            "timezone": "Europe/Moscow",
            "all_day": True,
            "free_busy": "Free",
            "recurrence": {"frequency": "yearly"},
        },
        **changes,
    }


def prepare(http, headers, payload):
    response = http.post("/v1/ews/prepare", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return {"plan_ref": response.json()["plan_ref"]}


def test_catalog_covers_official_operations_without_promising_server_access(client):
    http, _, headers = client
    assert http.get("/v1/ews/capabilities").status_code == 401
    result = http.get("/v1/ews/capabilities", headers=headers).json()
    assert result["counts"]["documented_operations"] == 96
    assert result["enabled_write_groups"] == []
    rows = {r["operation"]: r for r in result["operations"]}
    assert rows["CreateItem"]["bridge_status"] == "implemented_bounded_scope"
    assert rows["AddDelegate"]["bridge_status"] == "not_in_pinned_native_library"
    assert all(r["server_access"] == "not_probed" for r in rows.values())


def test_existing_read_token_cannot_execute_and_disabled_groups_fail_closed(client, tmp_path):
    http, settings = enable(client, tmp_path)
    response = http.post("/v1/ews/execute", json={"plan_ref": "unused"}, headers=client[2])
    assert response.status_code == 403
    settings.ews_enabled_write_groups = ""
    assert http.post("/v1/ews/execute", json={"plan_ref": "unused"}, headers=WRITE_HEADERS).status_code == 403
    settings.ews_enabled_write_groups = "calendar"
    settings.ews_write_api_token = settings.bridge_api_token
    assert (
        http.post("/v1/ews/execute", json={"plan_ref": "unused"}, headers=client[2]).json()["detail"]
        == "distinct_write_token_required"
    )


def test_extended_reads_are_opt_in_and_no_arbitrary_service_or_path(client):
    http, _, headers = client
    assert http.post("/v1/ews/read", json={"action": "rooms.list"}, headers=headers).status_code == 403
    assert (
        http.post("/v1/ews/read", json={"action": "GetItem", "url": "http://elsewhere"}, headers=headers).status_code
        == 422
    )
    assert http.post("/v1/ews/prepare", json=command(), headers=headers).status_code == 403


def test_plan_is_signed_and_execution_does_not_accept_changed_fields(client, tmp_path, monkeypatch):
    http, _ = enable(client, tmp_path)
    monkeypatch.setattr(ops, "command_state", lambda *a, **k: {"folder_path": "calendar", "folder_id": "calendar"})
    plan = prepare(http, client[2], command())
    changed = plan["plan_ref"][:-3] + "bad"
    assert http.post("/v1/ews/execute", json={"plan_ref": changed}, headers=WRITE_HEADERS).status_code == 422
    assert http.post("/v1/ews/execute", json={**plan, "action": "mail.send"}, headers=WRITE_HEADERS).status_code == 422


def test_exact_execution_is_idempotent_even_when_target_disappears(client, tmp_path, monkeypatch):
    http, _ = enable(client, tmp_path)
    state = Mock(return_value={"folder_path": "calendar", "folder_id": "calendar"})
    monkeypatch.setattr(ops, "command_state", state)
    apply = Mock(return_value={"item_id": "new", "verified_by": "GetItem"})
    monkeypatch.setattr(ops, "apply_command", apply)
    plan = prepare(http, client[2], command())
    result = http.post("/v1/ews/execute", json=plan, headers=WRITE_HEADERS)
    assert result.status_code == 200, result.text
    state.side_effect = HTTPException(404, "item_disappeared")
    replay = http.post("/v1/ews/execute", json=plan, headers=WRITE_HEADERS)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert apply.call_count == 1


def test_timeout_never_repeats_an_ambiguous_mutation(client, tmp_path, monkeypatch):
    http, _ = enable(client, tmp_path)
    monkeypatch.setattr(ops, "command_state", lambda *a, **k: {"folder_path": "calendar"})
    apply = Mock(side_effect=TimeoutError("secret server payload"))
    monkeypatch.setattr(ops, "apply_command", apply)
    plan = prepare(http, client[2], command())
    first = http.post("/v1/ews/execute", json=plan, headers=WRITE_HEADERS)
    assert first.status_code == 502
    assert "secret" not in first.text
    second = http.post("/v1/ews/execute", json=plan, headers=WRITE_HEADERS)
    assert second.status_code == 409
    assert second.json()["detail"] == "action_outcome_uncertain_reconcile_before_retry"
    assert apply.call_count == 1


def test_changed_target_and_disabled_group_stop_before_mutation(client, tmp_path, monkeypatch):
    http, settings = enable(client, tmp_path)
    state = {"folder_path": "calendar", "changekey": "one"}
    monkeypatch.setattr(ops, "command_state", lambda *a, **k: dict(state))
    apply = Mock()
    monkeypatch.setattr(ops, "apply_command", apply)
    plan = prepare(http, client[2], command())
    state["changekey"] = "two"
    assert http.post("/v1/ews/execute", json=plan, headers=WRITE_HEADERS).status_code == 409
    settings.ews_enabled_write_groups = "mail"
    assert http.post("/v1/ews/execute", json=plan, headers=WRITE_HEADERS).status_code == 403
    apply.assert_not_called()


def test_request_id_cannot_be_reused_with_new_content(client, tmp_path, monkeypatch):
    http, _ = enable(client, tmp_path)
    monkeypatch.setattr(ops, "command_state", lambda *a, **k: {"folder_path": "calendar"})
    monkeypatch.setattr(ops, "apply_command", lambda *a: {"created": True})
    first = prepare(http, client[2], command())
    assert http.post("/v1/ews/execute", json=first, headers=WRITE_HEADERS).status_code == 200
    changed = command()
    changed["values"]["subject"] = "Different birthday"
    second = prepare(http, client[2], changed)
    assert http.post("/v1/ews/execute", json=second, headers=WRITE_HEADERS).status_code == 409


def test_receipt_claim_is_atomic_across_connections(client, tmp_path):
    _, settings = enable(client, tmp_path)
    one, two = ops.Receipts(settings), ops.Receipts(settings)
    try:
        assert one.claim("id", "content") is None
        with pytest.raises(HTTPException, match="uncertain"):
            two.claim("id", "content")
        one.complete("id", {"executed": True})
        assert two.claim("id", "content") == {"executed": True}
    finally:
        one.close()
        two.close()


def test_native_calendar_serialization_preserves_local_birthday_and_recurrence():
    c = ops.Command.model_validate(command())
    ops.validate_command(c)
    values = ops.native_values(c.values, "calendar")
    item = CalendarItem(**values)
    version = Version(build=Build(15, 1, 2176, 14))
    item.clean(version=version)
    xml = item.to_xml(version=version)
    from lxml import etree

    text = etree.tostring(xml).decode()
    assert "2026-11-28" in text
    assert "AbsoluteYearlyRecurrence" in text
    assert "November" in text
    assert "IsAllDayEvent" in text
    assert "LegacyFreeBusyStatus" in text and "Free" in text
    assert item.start.hour == 0
    assert item.start.utcoffset().total_seconds() == 10800


@pytest.mark.parametrize(
    "values",
    [
        {"start": "2026-11-28T00:00:00", "end": "2026-11-29T00:00:00"},
        {"start": "2026-11-28T10:00:00+03:00", "end": "2026-11-29T10:00:00+03:00"},
        {"to": ["person@example.com"]},
        {"is_read": True},
    ],
)
def test_calendar_rejects_invalid_dates_or_unrequested_notifications(values):
    payload = command()
    payload["values"].update(values)
    with pytest.raises(HTTPException):
        ops.validate_command(ops.Command.model_validate(payload))


def test_native_contact_and_task_fields_use_pinned_models():
    from exchangelib import Contact, Task

    contact = ops.Values(display_name="Test", email="test@example.com")
    item = Contact(**ops.native_values(contact, "contact"))
    item.clean()
    assert item.email_addresses[0].email == "test@example.com"
    task = Task(**ops.native_values(ops.Values(subject="Test", due_date=date(2026, 11, 28)), "task"))
    task.clean()
    assert task.due_date.year == 2026


def test_only_saved_drafts_can_be_sent_or_have_attachments(client):
    item = Message(is_draft=False)
    c = ops.Command(action="mail.send", request_id="send-test-001")
    with pytest.raises(HTTPException, match="saved_draft"):
        ops.validate_target(c, item, {"mailbox": "self", "folder_path": "inbox"})
    item.is_draft = True
    ops.validate_target(c, item, {"mailbox": "self", "folder_path": "drafts"})
    c.mailbox = "shared"
    with pytest.raises(HTTPException, match="scope"):
        ops.validate_target(c, item, {"mailbox": "self", "folder_path": "drafts"})


@pytest.mark.parametrize("action", ["mail.reply_draft", "mail.forward_draft"])
def test_response_draft_uses_source_account_and_reads_back_saved_message(client, monkeypatch, action):
    from types import SimpleNamespace

    _, settings, _ = client
    outer_account, source_account = Mock(), Mock()
    source_drafts = SimpleNamespace(account=source_account)
    source_account.drafts = source_drafts
    original = Mock(changekey="current", account=source_account)
    response = Mock()
    original.create_reply.return_value = response
    original.create_forward.return_value = response

    def save_response(*, folder):
        # Match exchangelib 5.6.0 CreateItem.call()'s native account check.
        if folder.account is not source_account:
            raise ValueError("Folder must belong to account")
        return SimpleNamespace(id="saved-id", changekey="saved-ck")

    response.save.side_effect = save_response
    saved = Message(id="saved-id", changekey="saved-ck", subject="Re: topic", is_draft=True)
    fetch = Mock(return_value=saved)
    monkeypatch.setattr(ops, "folder_for", lambda *a, **k: SimpleNamespace(account=outer_account))
    monkeypatch.setattr(ops, "selected", lambda *a, **k: (original, {}))
    monkeypatch.setattr(ops, "selected_item", fetch)
    cmd = ops.Command(
        action=action,
        request_id="response-draft-test-001",
        item_ref="selected",
        values=ops.Values(subject="Re: topic", body="Reply text", to=["partner@example.com"]),
    )
    result = ops.apply_command(settings, cmd, {"folder_path": "inbox", "changekey": "current"})

    fetch.assert_called_once_with(source_drafts, "saved-id", fields=ops.fields_for("drafts"))
    assert result["item"]["id"] == "saved-id"
    assert result["item"]["is_draft"] is True
    assert result["verified_by"] == "GetItem"
    original.send.assert_not_called()
    response.send.assert_not_called()


def test_read_directory_metadata_never_exposes_secrets(client, tmp_path, monkeypatch):
    http, _ = enable(client, tmp_path)
    from exchangelib import Mailbox

    account = Mock()
    account.protocol.resolve_names.return_value = [Mailbox(name="Test", email_address="test@example.com")]
    monkeypatch.setattr(ops, "build_account", lambda *a, **k: account)
    response = http.post("/v1/ews/read", json={"action": "directory.resolve", "names": ["Test"]}, headers=client[2])
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["email_address"] == "test@example.com"
    account.protocol.resolve_names.assert_called_once_with(names=["Test"], return_full_contact_data=False)


def test_item_reference_scope_is_validated_before_returning_data(client, monkeypatch):
    _, settings, _ = client
    folder = Mock(id="selected-folder")
    outside = Message(id="outside", changekey="ck")
    from exchangelib.properties import ParentFolderId

    outside.parent_folder_id = ParentFolderId(id="different-folder")
    folder.all.return_value.only.return_value.get.return_value = outside
    monkeypatch.setattr(ops, "folder_for", lambda *a, **k: folder)
    ref = make_ref(settings, "ews-item", {"mailbox": "self", "folder_path": "inbox", "item_id": "outside"})
    with pytest.raises(HTTPException, match="outside_selected_folder"):
        ops.selected(settings, ref)


def test_send_and_meeting_notification_groups_are_separate(client, tmp_path):
    _, settings = enable(client, tmp_path, "calendar,mail")
    create = ops.Command.model_validate(command())
    assert ops.group_enabled(settings, create)
    create.notify_attendees = True
    assert not ops.group_enabled(settings, create)
    send = ops.Command(action="mail.send", request_id="mail-send-001")
    assert not ops.group_enabled(settings, send)


def test_serializer_never_returns_mime_or_body():
    item = Message(subject="Selected metadata", body="private body", mime_content=b"private MIME")
    serialized = ops.serialize(item)
    assert "body" not in serialized
    assert "text_body" not in serialized
    assert "mime_content" not in serialized
    assert "private" not in str(serialized)


def test_rules_preserve_client_side_outlook_rule_blob_and_require_explicit_scope(client, tmp_path):
    _, settings = enable(client, tmp_path, "settings")
    from types import SimpleNamespace

    from exchangelib.services import CreateInboxRule
    from lxml import etree

    rule = ops.RuleValues(name="Partner", priority=1, subject_words=["invoice"], mark_read=True)
    cmd = ops.Command(action="rule.create", request_id="rule-create-001", values=ops.Values(rule=rule))
    native = ops.native_rule(settings, cmd, None)
    version = Version(build=Build(15, 1, 2176, 14))
    native.clean(version=version)
    service = CreateInboxRule(account=SimpleNamespace(version=version, protocol=SimpleNamespace(version=version)))
    xml = etree.tostring(service.get_payload(rule=native, remove_outlook_rule_blob=False)).decode()
    assert "RemoveOutlookRuleBlob>0" in xml
    assert "MarkAsRead>1" in xml
    assert "invoice" in xml
    rule.forward_to = ["recipient@example.com"]
    assert not ops.group_enabled(settings, cmd)


def test_configuration_conflict_fingerprints_cover_text_beyond_display_limit():
    from exchangelib.settings import OofSettings

    a = OofSettings(state="Enabled", internal_reply="a" * 3000, external_reply="same")
    b = OofSettings(state="Enabled", internal_reply="a" * 2999 + "b", external_reply="same")
    assert ops.serialize(a) == ops.serialize(b)
    assert ops.full_fingerprint(a) != ops.full_fingerprint(b)


def test_task_completion_never_overwrites_unloaded_fields(client, monkeypatch):
    from types import SimpleNamespace

    _, settings, _ = client
    item = Mock(changekey="current")
    monkeypatch.setattr(ops, "folder_for", lambda *a, **k: SimpleNamespace(account=Mock()))
    monkeypatch.setattr(ops, "selected", lambda *a, **k: (item, {}))
    monkeypatch.setattr(ops, "item_projection", lambda *a: {"id": "task"})
    cmd = ops.Command(action="task.complete", request_id="task-complete-001", item_ref="selected")
    ops.apply_command(settings, cmd, {"folder_path": "tasks", "changekey": "current"})
    item.save.assert_called_once_with(
        update_fields=["status", "percent_complete"], conflict_resolution="NeverOverwrite"
    )
    item.refresh.assert_called_once()


@pytest.mark.parametrize("action", ["mail.copy", "mail.move"])
def test_native_move_and_copy_return_contracts(client, monkeypatch, action):
    from types import SimpleNamespace

    _, settings, _ = client
    item = Mock(id="original", changekey="old")
    item.copy.return_value = ("new-id", "new-ck")

    def moved(**kwargs):
        item.id, item.changekey = "new-id", "new-ck"

    item.move.side_effect = moved
    folder = SimpleNamespace(account=Mock())
    monkeypatch.setattr(ops, "folder_for", lambda *a, **k: folder)
    monkeypatch.setattr(ops, "selected", lambda *a, **k: (item, {}))
    cmd = ops.Command(action=action, request_id="mail-move-001", item_ref="ref", destination="inbox/Archive")
    result = ops.apply_command(settings, cmd, {"folder_path": "inbox", "changekey": "old"})
    assert result["item_id"] == "new-id"
    assert result["changekey"] == "new-ck"
    assert result["folder_path"] == "inbox/Archive"


def test_native_reply_and_forward_are_saved_drafts():
    from types import SimpleNamespace

    from exchangelib.properties import ReferenceItemId
    from exchangelib.services import CreateItem
    from lxml import etree

    version = Version(build=Build(15, 1, 2176, 14))
    # Real native serialization checks SaveOnly; no live Exchange transport.
    account = SimpleNamespace(version=version, protocol=SimpleNamespace(version=version), default_timezone=None)
    item = Message(id="source", changekey="current")
    from exchangelib.items.message import ForwardItem, ReplyToItem

    for cls in (ReplyToItem, ForwardItem):
        draft = cls(
            subject="Subject",
            body="Body",
            to_recipients=[Mailbox(email_address="recipient@example.com")],
            reference_item_id=ReferenceItemId(id=item.id, changekey=item.changekey),
        )
        service = CreateItem(account=account)
        # Serialization itself does not require a transport or a real parent Folder.
        xml = etree.tostring(
            service.get_payload(
                items=[draft], folder=None, message_disposition="SaveOnly", send_meeting_invitations="SendToNone"
            )
        ).decode()
        assert 'MessageDisposition="SaveOnly"' in xml
        assert 'SendMeetingInvitations="SendToNone"' in xml
        assert "recipient@example.com" in xml


def test_rule_update_cannot_silently_remove_unrepresented_conditions_or_exceptions():
    from exchangelib.properties import Actions, Conditions, Exceptions, Rule

    rule = Rule(
        display_name="Existing",
        priority=1,
        conditions=Conditions(contains_subject_strings=["invoice"]),
        actions=Actions(mark_as_read=True),
    )
    ops.supported_rule_update(rule)
    rule.conditions.contains_body_strings = ["do not drop"]
    with pytest.raises(HTTPException, match="unrepresented_fields"):
        ops.supported_rule_update(rule)
    rule.conditions.contains_body_strings = None
    rule.exceptions = Exceptions(has_attachments=True)
    with pytest.raises(HTTPException, match="unrepresented_fields"):
        ops.supported_rule_update(rule)


def test_saved_draft_projection_includes_hidden_recipients_and_sender(client):
    _, settings, _ = client
    item = Message(
        id="draft",
        changekey="current",
        subject="Draft",
        is_draft=True,
        bcc_recipients=[Mailbox(email_address="hidden@example.com")],
        author=Mailbox(email_address="sender@example.com"),
    )
    projected = ops.item_projection(settings, item, "self", "drafts")
    assert projected["bcc_recipients"][0]["email_address"] == "hidden@example.com"
    assert projected["author"]["email_address"] == "sender@example.com"


def test_all_day_readback_converts_native_inclusive_end_to_exclusive_boundary(client):
    from exchangelib import EWSDate

    _, settings, _ = client
    item = CalendarItem(
        id="birthday", changekey="current", is_all_day=True, start=EWSDate(2026, 11, 28), end=EWSDate(2026, 11, 28)
    )
    projected = ops.item_projection(settings, item, "self", "calendar")
    assert projected["start"] == "2026-11-28"
    assert projected["end"] == "2026-11-29"
    assert projected["end_boundary"] == "exclusive"


def test_calendar_timezone_changes_include_native_timezone_fields():
    values = ops.native_values(ops.Command.model_validate(command()).values, "calendar")
    assert values["_start_timezone"].key == "Europe/Moscow"
    assert values["_end_timezone"].key == "Europe/Moscow"


def test_all_day_update_without_explicit_conversion_cannot_be_rounded_to_another_date():
    from exchangelib import EWSDate

    item = CalendarItem(is_all_day=True, start=EWSDate(2026, 11, 28), end=EWSDate(2026, 11, 28))
    cmd = ops.Command(
        action="calendar.update",
        request_id="calendar-update-001",
        values=ops.Values(start="2026-11-28T12:00:00+03:00", end="2026-11-29T12:00:00+03:00"),
    )
    with pytest.raises(HTTPException, match="local_midnight"):
        ops.validate_target(cmd, item, {"mailbox": "self", "folder_path": "calendar"})
    cmd.values.all_day = False
    ops.validate_target(cmd, item, {"mailbox": "self", "folder_path": "calendar"})


@pytest.mark.parametrize("action", ["oof.set", "rule.create", "folder.rename", "subscription.create"])
def test_non_item_actions_cannot_bypass_their_own_scope_snapshot_with_mail_reference(action):
    cmd = ops.Command(action=action, request_id="non-item-test-001", item_ref="signed-message-ref")
    with pytest.raises(HTTPException, match="non_item_action"):
        ops.validate_command(cmd)


def native_account():
    from exchangelib import DELEGATE, NTLM, Account, Configuration, Credentials, EWSTimeZone

    return Account(
        primary_smtp_address="synthetic@example.com",
        autodiscover=False,
        access_type=DELEGATE,
        default_timezone=EWSTimeZone("UTC"),
        config=Configuration(
            service_endpoint="https://synthetic.example.com/EWS/Exchange.asmx",
            auth_type=NTLM,
            credentials=Credentials(username="synthetic", password="test"),
            version=Version(build=Build(15, 1, 2176, 14)),
        ),
    )


def test_oof_write_reads_uncached_native_property_without_false_failure(client, monkeypatch):
    from types import SimpleNamespace

    import exchangelib.account
    from exchangelib.settings import OofSettings

    _, settings, _ = client
    account = native_account()
    setter = Mock()
    getter = Mock(
        return_value=OofSettings(
            state="Enabled", external_audience="None", internal_reply="Away", external_reply="Away"
        )
    )
    monkeypatch.setattr(exchangelib.account.SetUserOofSettings, "get", setter)
    monkeypatch.setattr(exchangelib.account.GetUserOofSettings, "get", getter)
    monkeypatch.setattr(ops, "folder_for", lambda *a, **k: SimpleNamespace(account=account))
    cmd = ops.Command(
        action="oof.set",
        request_id="oof-success-001",
        values=ops.Values(oof_state="Enabled", internal_reply="Away", external_reply="Away"),
    )
    result = ops.apply_command(settings, cmd, {"folder_path": "inbox"})
    assert result == {"state": "Enabled", "verified_by": "GetUserOofSettings"}
    setter.assert_called_once()
    getter.assert_called_once()


def test_primary_contact_email_update_preserves_all_other_indexed_addresses(client, monkeypatch):
    from types import SimpleNamespace

    from exchangelib import Contact
    from exchangelib.indexed_properties import EmailAddress

    _, settings, _ = client
    item = Contact(
        id="contact",
        changekey="current",
        email_addresses=[
            EmailAddress(label="EmailAddress1", email="old@example.com"),
            EmailAddress(label="EmailAddress2", email="personal@example.com"),
            EmailAddress(label="EmailAddress3", email="secondary@example.com"),
        ],
    )
    saved = Mock()
    monkeypatch.setattr(Contact, "save", lambda self, **kwargs: saved(kwargs, list(self.email_addresses)))
    monkeypatch.setattr(Contact, "refresh", lambda self: self)
    monkeypatch.setattr(ops, "folder_for", lambda *a, **k: SimpleNamespace(account=Mock()))
    monkeypatch.setattr(ops, "selected", lambda *a, **k: (item, {}))
    cmd = ops.Command(
        action="contact.update",
        request_id="contact-update-001",
        item_ref="selected",
        values=ops.Values(email="new@example.com"),
    )
    result = ops.apply_command(settings, cmd, {"folder_path": "contacts", "changekey": "current"})
    values = {a.label: a.email for a in saved.call_args.args[1]}
    assert values == {
        "EmailAddress1": "new@example.com",
        "EmailAddress2": "personal@example.com",
        "EmailAddress3": "secondary@example.com",
    }
    assert saved.call_args.args[0]["update_fields"] == ["email_addresses"]
    assert saved.call_args.args[0]["conflict_resolution"] == "NeverOverwrite"
    assert len(result["item"]["email_addresses"]) == 3


def test_oof_readback_mismatch_is_not_reported_as_success(client, monkeypatch):
    from types import SimpleNamespace

    import exchangelib.account
    from exchangelib.settings import OofSettings

    _, settings, _ = client
    account = native_account()
    monkeypatch.setattr(exchangelib.account.SetUserOofSettings, "get", Mock())
    monkeypatch.setattr(exchangelib.account.GetUserOofSettings, "get", Mock(return_value=OofSettings(state="Disabled")))
    monkeypatch.setattr(ops, "folder_for", lambda *a, **k: SimpleNamespace(account=account))
    cmd = ops.Command(
        action="oof.set",
        request_id="oof-mismatch-001",
        values=ops.Values(oof_state="Enabled", internal_reply="Away", external_reply="Away"),
    )
    with pytest.raises(HTTPException, match="not_verified"):
        ops.apply_command(settings, cmd, {"folder_path": "inbox"})


def test_oof_conflict_checks_ignore_server_generated_irrelevant_duration():
    from datetime import UTC, datetime

    from exchangelib.settings import OofSettings

    a = OofSettings(
        state="Enabled", internal_reply="Away", external_reply="Away", start=datetime(2026, 10, 1, tzinfo=UTC)
    )
    b = OofSettings(
        state="Enabled", internal_reply="Away", external_reply="Away", start=datetime(2026, 10, 2, tzinfo=UTC)
    )
    assert ops.oof_fingerprint(a) == ops.oof_fingerprint(b)
    b.internal_reply = "Changed"
    assert ops.oof_fingerprint(a) != ops.oof_fingerprint(b)
