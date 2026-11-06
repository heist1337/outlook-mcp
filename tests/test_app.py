from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from exchange_ews_bridge.app import app, encode_sent_cursor
from exchange_ews_bridge.config import Settings, get_settings
from exchange_ews_bridge.google_calendar import (
    google_event_body,
    google_shadow_event_body,
    mirror_events_to_google,
    stable_source_key,
)
from exchange_ews_bridge.state import CalendarSnapshotState, MirrorState


def install_settings_override(settings: Settings, monkeypatch):
    def override_settings() -> Settings:
        return settings

    app.dependency_overrides[get_settings] = override_settings
    monkeypatch.setattr("exchange_ews_bridge.security.get_settings", override_settings)
    return override_settings


def test_healthz_does_not_require_auth() -> None:
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_freebusy_requires_auth() -> None:
    client = TestClient(app)
    response = client.get(
        "/v1/calendar/freebusy",
        params={"start": "2026-07-08T09:00:00Z", "end": "2026-07-08T10:00:00Z"},
    )
    assert response.status_code == 401


def test_freebusy_hides_subjects(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        ews_allow_event_subjects=False,
    )
    install_settings_override(settings, monkeypatch)

    def fake_items(_settings: Settings, _start: datetime, _end: datetime, _limit: int):
        return [
            SimpleNamespace(
                start=datetime(2026, 7, 8, 9, tzinfo=UTC),
                end=datetime(2026, 7, 8, 10, tzinfo=UTC),
                is_all_day=False,
                subject="Sensitive meeting",
            )
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.calendar_items", fake_items)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/calendar/upcoming",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"days": "1", "limit": "10"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["events"][0]["subject"] is None


def test_inbox_list_returns_metadata_only(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
    )
    install_settings_override(settings, monkeypatch)

    def fake_messages(
        _settings: Settings,
        _limit: int,
        _unread_only: bool,
        since: datetime | None = None,
        include_body: bool = False,
    ):
        assert since is None
        assert include_body is False
        return [
            SimpleNamespace(
                id="ews-id",
                changekey="ews-changekey",
                subject="Sensitive subject",
                sender=SimpleNamespace(name="Sender", email_address="sender@example.com"),
                datetime_received=datetime(2026, 7, 8, 9, tzinfo=UTC),
                is_read=False,
                has_attachments=True,
                text_body="This must not be returned by list endpoint",
            )
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.inbox_messages", fake_messages)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/mail/inbox",
            headers={"Authorization": "Bearer secret-token-123456"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["messages"][0]["id"] == "ews-id"
    assert payload["messages"][0]["source_key"].startswith("ews_mail_")
    assert "body_text" not in payload["messages"][0]


def test_inbox_body_requires_explicit_enable(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        ews_mail_body_enabled=False,
    )
    install_settings_override(settings, monkeypatch)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/mail/inbox/message",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"id": "ews-id", "changekey": "ews-changekey"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"] == "mail_body_reading_disabled"


def test_inbox_intake_returns_body_when_enabled(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        ews_mail_body_enabled=True,
        ews_mail_body_max_chars=500,
    )
    install_settings_override(settings, monkeypatch)

    def fake_messages(
        _settings: Settings,
        _limit: int,
        _unread_only: bool,
        since: datetime | None = None,
        until: datetime | None = None,
        offset: int = 0,
        include_body: bool = False,
    ):
        assert include_body is True
        assert since is not None
        assert until is not None
        assert offset == 0
        return [
            SimpleNamespace(
                id="ews-id",
                changekey="ews-changekey",
                subject="Needs reply",
                sender=SimpleNamespace(name="Sender", email_address="sender@example.com"),
                to_recipients=[SimpleNamespace(name="Me", email_address="me@example.com")],
                cc_recipients=[],
                datetime_received=datetime(2026, 7, 8, 9, tzinfo=UTC),
                is_read=False,
                has_attachments=False,
                conversation_id="conversation-id",
                conversation_topic="Needs reply",
                importance="Normal",
                categories=["Planfix"],
                text_body="Please answer this request.",
            )
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.inbox_messages", fake_messages)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/mail/inbox/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"hours": "24", "limit": "10"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["body_enabled"] is True
    assert payload["messages"][0]["body_text"] == "Please answer this request."
    assert payload["messages"][0]["needs_attention_hint"] is True


def test_inbox_intake_hides_body_when_disabled(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        ews_mail_body_enabled=False,
    )
    install_settings_override(settings, monkeypatch)

    def fake_messages(
        _settings: Settings,
        _limit: int,
        _unread_only: bool,
        since: datetime | None = None,
        until: datetime | None = None,
        offset: int = 0,
        include_body: bool = False,
    ):
        assert include_body is False
        assert until is not None
        assert offset == 0
        return [
            SimpleNamespace(
                id="ews-id",
                changekey="ews-changekey",
                subject="Metadata only",
                sender=SimpleNamespace(name="Sender", email_address="sender@example.com"),
                datetime_received=datetime(2026, 7, 8, 9, tzinfo=UTC),
                is_read=True,
                has_attachments=False,
                text_body="This must not be returned.",
            )
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.inbox_messages", fake_messages)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/mail/inbox/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["body_enabled"] is False
    assert payload["messages"][0]["body_text"] is None


def test_inbox_intake_paginates_anchored_window(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        ews_max_items=2,
    )
    install_settings_override(settings, monkeypatch)
    all_items = [
        SimpleNamespace(
            id=f"ews-inbox-{index}",
            datetime_received=datetime(2026, 7, 8, 12 - index, tzinfo=UTC),
            conversation_id=f"conversation-{index}",
        )
        for index in range(3)
    ]
    calls: list[tuple[datetime, datetime, int, int]] = []

    def fake_messages(
        _settings: Settings,
        fetch_limit: int,
        _unread_only: bool,
        since: datetime | None = None,
        until: datetime | None = None,
        offset: int = 0,
        include_body: bool = False,
    ):
        assert since is not None and until is not None
        assert include_body is False
        calls.append((since, until, offset, fetch_limit))
        return all_items[offset : offset + fetch_limit]

    monkeypatch.setattr("exchange_ews_bridge.app.inbox_messages", fake_messages)
    try:
        client = TestClient(app)
        first = client.get(
            "/v1/mail/inbox/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"hours": "2160", "limit": "2"},
        )
        cursor = first.json()["next_cursor"]
        second = client.get(
            "/v1/mail/inbox/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"cursor": cursor, "limit": "2"},
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["has_more"] is True
    assert second.status_code == 200
    assert second.json()["page_offset"] == 2
    assert second.json()["has_more"] is False
    assert calls[0][0:2] == calls[1][0:2]
    assert calls[0][2:] == (0, 3)
    assert calls[1][2:] == (2, 3)


def test_sent_intake_returns_metadata_and_conversation_key(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
    )
    install_settings_override(settings, monkeypatch)

    def fake_sent_messages(
        _settings: Settings,
        _limit: int,
        since: datetime | None = None,
        until: datetime | None = None,
        offset: int = 0,
    ):
        assert since is not None
        assert until is not None
        assert offset == 0
        return [
            SimpleNamespace(
                id="ews-sent-id",
                changekey="ews-sent-changekey",
                subject="Reply sent",
                sender=SimpleNamespace(name="Me", email_address="me@example.com"),
                to_recipients=[SimpleNamespace(name="Recipient", email_address="recipient@example.com")],
                cc_recipients=[],
                datetime_sent=datetime(2026, 7, 8, 10, tzinfo=UTC),
                has_attachments=False,
                conversation_id="conversation-id",
                conversation_topic="Needs reply",
                importance="Normal",
                categories=[],
            )
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.sent_messages", fake_sent_messages)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/mail/sent/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"hours": "24", "limit": "10"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["folder"] == "sent"
    assert payload["body_enabled"] is False
    assert payload["messages"][0]["folder"] == "sent"
    assert payload["messages"][0]["sent_at"] == "2026-07-08T10:00:00Z"
    assert payload["messages"][0]["conversation_key"].startswith("ews_conversation_")
    assert payload["messages"][0]["body_text"] is None


def test_sent_intake_returns_body_when_enabled(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        ews_mail_body_enabled=True,
        ews_mail_body_max_chars=500,
    )
    install_settings_override(settings, monkeypatch)

    def fake_sent_messages(
        _settings: Settings,
        _limit: int,
        since: datetime | None = None,
        until: datetime | None = None,
        offset: int = 0,
        include_body: bool = False,
    ):
        assert since is not None and until is not None and offset == 0
        assert include_body is True
        return [
            SimpleNamespace(
                id="ews-sent-id",
                subject="Reply sent",
                sender=SimpleNamespace(name="Me", email_address="me@example.com"),
                to_recipients=[SimpleNamespace(name="Recipient", email_address="recipient@example.com")],
                cc_recipients=[],
                datetime_sent=datetime(2026, 7, 8, 10, tzinfo=UTC),
                has_attachments=False,
                conversation_id="conversation-id",
                text_body="This is my sent reply.",
            )
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.sent_messages", fake_sent_messages)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/mail/sent/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"hours": "24", "limit": "10"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["body_enabled"] is True
    assert payload["messages"][0]["body_text"] == "This is my sent reply."


def test_sent_intake_paginates_an_anchored_snapshot_and_cursor_replay(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        ews_max_items=2,
    )
    install_settings_override(settings, monkeypatch)
    all_items = [
        SimpleNamespace(
            id=f"ews-sent-{index}",
            datetime_sent=datetime(2026, 7, 8, 12 - index, tzinfo=UTC),
            conversation_id=f"conversation-{index}",
        )
        for index in range(3)
    ]
    calls: list[tuple[datetime, datetime, int, int]] = []

    def fake_sent_messages(
        _settings: Settings,
        fetch_limit: int,
        since: datetime | None = None,
        until: datetime | None = None,
        offset: int = 0,
    ):
        assert since is not None
        assert until is not None
        calls.append((since, until, offset, fetch_limit))
        return all_items[offset : offset + fetch_limit]

    monkeypatch.setattr("exchange_ews_bridge.app.sent_messages", fake_sent_messages)
    try:
        client = TestClient(app)
        first = client.get(
            "/v1/mail/sent/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"hours": "24", "limit": "2"},
        )
        cursor = first.json()["next_cursor"]
        second = client.get(
            "/v1/mail/sent/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"hours": "24", "limit": "2", "cursor": cursor},
        )
        replay = client.get(
            "/v1/mail/sent/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"hours": "24", "limit": "2", "cursor": cursor},
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["scanned"] == 2
    assert first.json()["has_more"] is True
    assert first.json()["saturated"] is True
    assert cursor
    assert second.status_code == 200
    assert second.json()["page_offset"] == 2
    assert second.json()["scanned"] == 1
    assert second.json()["has_more"] is False
    assert second.json()["next_cursor"] is None
    assert replay.json() == second.json()
    assert calls[0][0:2] == calls[1][0:2] == calls[2][0:2]
    assert calls[0][2:] == (0, 3)
    assert calls[1][2:] == calls[2][2:] == (2, 3)


def test_sent_intake_exact_page_boundary_is_not_saturated(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        ews_max_items=2,
    )
    install_settings_override(settings, monkeypatch)

    def fake_sent_messages(
        _settings: Settings,
        fetch_limit: int,
        since: datetime | None = None,
        until: datetime | None = None,
        offset: int = 0,
    ):
        assert fetch_limit == 3
        assert since is not None and until is not None and offset == 0
        return [
            SimpleNamespace(id="ews-sent-1", datetime_sent=datetime(2026, 7, 8, 12, tzinfo=UTC)),
            SimpleNamespace(id="ews-sent-2", datetime_sent=datetime(2026, 7, 8, 11, tzinfo=UTC)),
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.sent_messages", fake_sent_messages)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/mail/sent/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"hours": "24", "limit": "2"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["scanned"] == 2
    assert response.json()["has_more"] is False
    assert response.json()["saturated"] is False
    assert response.json()["next_cursor"] is None


def test_sent_intake_rejects_malformed_or_tampered_cursor(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
    )
    install_settings_override(settings, monkeypatch)
    try:
        client = TestClient(app)
        malformed = client.get(
            "/v1/mail/sent/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"cursor": "not-a-valid-cursor"},
        )
        valid = encode_sent_cursor(
            settings,
            datetime(2026, 7, 8, 9, tzinfo=UTC),
            datetime(2026, 7, 8, 10, tzinfo=UTC),
            1,
        )
        encoded, signature = valid.split(".", 1)
        changed = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered = client.get(
            "/v1/mail/sent/intake",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"cursor": f"{encoded}.{changed}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert malformed.status_code == 422
    assert malformed.json()["detail"] == "invalid_cursor"
    assert tampered.status_code == 422
    assert tampered.json()["detail"] == "invalid_cursor"


def test_google_mirror_dry_run_without_google_credentials(monkeypatch, tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
        google_calendar_enabled=False,
    )
    install_settings_override(settings, monkeypatch)

    def fake_items(_settings: Settings, _start: datetime, _end: datetime, _limit: int):
        return [
            SimpleNamespace(
                id="ews-calendar-id",
                changekey="ews-calendar-changekey",
                start=datetime(2026, 7, 8, 9, tzinfo=UTC),
                end=datetime(2026, 7, 8, 10, tzinfo=UTC),
                is_all_day=False,
                subject="Sensitive meeting",
            )
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.calendar_items", fake_items)
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/calendar/google/mirror",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"days": "1", "dry_run": "true"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["created"] == 1
    assert payload["scanned"] == 1


def test_google_mirror_dry_run_counts_existing_source_cancellation(monkeypatch, tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
        google_calendar_enabled=False,
    )
    install_settings_override(settings, monkeypatch)
    item = SimpleNamespace(
        id="ews-cancelled-calendar-id",
        start=datetime.now(UTC) + timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=2),
        is_all_day=False,
        is_cancelled=True,
    )
    source_key = stable_source_key(item)
    state = MirrorState(tmp_path)
    state.upsert(source_key, "google-event-id", "old-hash", datetime.now(UTC).isoformat())

    def fake_items(_settings: Settings, _start: datetime, _end: datetime, _limit: int):
        return [item]

    monkeypatch.setattr("exchange_ews_bridge.app.calendar_items", fake_items)
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/calendar/google/mirror",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"days": "1", "dry_run": "true"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["deleted"] == 1
    assert response.json()["created"] == 0
    assert state.get(source_key) is not None


def test_google_events_can_include_recent_past_window(monkeypatch) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        google_mirror_summary_mode="subject",
        google_mirror_include_attendees=True,
    )
    install_settings_override(settings, monkeypatch)
    captured: dict[str, datetime | int] = {}

    def fake_items(_settings: Settings, start: datetime, end: datetime, limit: int):
        captured["start"] = start
        captured["end"] = end
        captured["limit"] = limit
        return [
            SimpleNamespace(
                id="ews-calendar-id",
                changekey="ews-calendar-changekey",
                start=datetime(2026, 7, 8, 9, tzinfo=UTC),
                end=datetime(2026, 7, 8, 10, tzinfo=UTC),
                is_all_day=False,
                subject="Recent meeting",
                required_attendees=[SimpleNamespace(email_address="person@example.com")],
                optional_attendees=[],
            )
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.calendar_items", fake_items)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/calendar/google/events",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={"days": "1", "past_hours": "24", "limit": "10"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["scanned"] == 1
    assert payload["events"][0]["google_event"]["attendees"] == [{"email": "person@example.com"}]
    assert captured["limit"] == 11
    assert captured["start"] < captured["end"]
    window_hours = (captured["end"] - captured["start"]).total_seconds() / 3600
    assert 47.5 <= window_hours <= 48.5


def test_google_events_exposes_source_cancellation_without_private_payload(monkeypatch, tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
        google_mirror_summary_mode="subject",
        google_mirror_include_attendees=True,
    )
    install_settings_override(settings, monkeypatch)
    now = datetime.now(UTC)

    def fake_items(_settings: Settings, _start: datetime, _end: datetime, _limit: int):
        return [
            SimpleNamespace(
                id="cancelled-ews-calendar-id",
                start=now + timedelta(hours=1),
                end=now + timedelta(hours=2),
                is_all_day=False,
                is_cancelled=True,
                subject="Private cancelled meeting",
                required_attendees=[SimpleNamespace(email_address="private@example.com")],
                optional_attendees=[],
            )
        ]

    monkeypatch.setattr("exchange_ews_bridge.app.calendar_items", fake_items)
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/calendar/google/events",
            headers={"Authorization": "Bearer secret-token-123456"},
            params={
                "days": "1",
                "past_hours": "1",
                "limit": "10",
                "reconcile": "true",
                "reconciliation_key": "cancel-test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot_complete"] is True
    assert payload["reconciliation_enabled"] is True
    assert payload["events"][0]["cancelled"] is True
    assert payload["events"][0]["cancellation_reason"] == "source_cancelled"
    state_bytes = (tmp_path / "mirror.sqlite3").read_bytes()
    assert b"Private cancelled meeting" not in state_bytes
    assert b"private@example.com" not in state_bytes


def test_google_events_emits_missing_tombstone_only_once_after_complete_snapshot(monkeypatch, tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
    )
    install_settings_override(settings, monkeypatch)
    now = datetime.now(UTC)
    active_item = SimpleNamespace(
        id="hard-deleted-ews-calendar-id",
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
        is_all_day=False,
        is_cancelled=False,
        subject="Must not persist",
    )
    snapshots = [[active_item], [], []]

    def fake_items(_settings: Settings, _start: datetime, _end: datetime, _limit: int):
        return snapshots.pop(0)

    monkeypatch.setattr("exchange_ews_bridge.app.calendar_items", fake_items)
    params = {
        "days": "1",
        "past_hours": "1",
        "limit": "10",
        "reconcile": "true",
        "reconciliation_key": "missing-test",
    }
    try:
        client = TestClient(app)
        first = client.get(
            "/v1/calendar/google/events",
            headers={"Authorization": "Bearer secret-token-123456"},
            params=params,
        )
        missing = client.get(
            "/v1/calendar/google/events",
            headers={"Authorization": "Bearer secret-token-123456"},
            params=params,
        )
        repeated = client.get(
            "/v1/calendar/google/events",
            headers={"Authorization": "Bearer secret-token-123456"},
            params=params,
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["tombstones"] == 0
    assert first.json()["events"][0]["cancelled"] is False
    assert missing.status_code == 200
    assert missing.json()["scanned"] == 0
    assert missing.json()["returned"] == 1
    assert missing.json()["tombstones"] == 1
    tombstone = missing.json()["events"][0]
    assert tombstone["cancelled"] is True
    assert tombstone["cancellation_reason"] == "missing_from_complete_snapshot"
    assert tombstone["google_event"]["summary"] == "Cancelled Exchange event"
    assert "attendees" not in tombstone["google_event"]
    assert repeated.status_code == 200
    assert repeated.json()["tombstones"] == 0
    assert repeated.json()["events"] == []


def test_google_events_never_infers_missing_from_incomplete_snapshot(monkeypatch, tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
        ews_max_items=1,
        google_mirror_limit=1,
    )
    install_settings_override(settings, monkeypatch)
    now = datetime.now(UTC)

    def event(item_id: str, hour: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=item_id,
            start=now + timedelta(hours=hour),
            end=now + timedelta(hours=hour + 1),
            is_all_day=False,
            is_cancelled=False,
        )

    snapshots = [[event("first", 1)], [event("second", 2), event("third", 3)]]

    def fake_items(_settings: Settings, _start: datetime, _end: datetime, fetch_limit: int):
        assert fetch_limit == 2
        return snapshots.pop(0)

    monkeypatch.setattr("exchange_ews_bridge.app.calendar_items", fake_items)
    params = {
        "days": "1",
        "past_hours": "1",
        "limit": "1",
        "reconcile": "true",
        "reconciliation_key": "incomplete-test",
    }
    try:
        client = TestClient(app)
        complete = client.get(
            "/v1/calendar/google/events",
            headers={"Authorization": "Bearer secret-token-123456"},
            params=params,
        )
        incomplete = client.get(
            "/v1/calendar/google/events",
            headers={"Authorization": "Bearer secret-token-123456"},
            params=params,
        )
    finally:
        app.dependency_overrides.clear()

    assert complete.status_code == 200
    assert complete.json()["snapshot_complete"] is True
    assert incomplete.status_code == 200
    assert incomplete.json()["has_more"] is True
    assert incomplete.json()["snapshot_complete"] is False
    assert incomplete.json()["tombstones"] == 0
    assert len(incomplete.json()["events"]) == 1
    assert incomplete.json()["events"][0]["cancelled"] is False


def test_calendar_snapshot_state_prunes_abandoned_scopes_after_120_days(tmp_path) -> None:
    state = CalendarSnapshotState(tmp_path)
    old_now = datetime(2026, 1, 1, tzinfo=UTC)
    state.reconcile(
        reconciliation_key="old-scope",
        observed=[
            {
                "source_key": "opaque-source-key",
                "source_hash": "opaque-fingerprint",
                "start_json": '{"dateTime":"2026-01-01T01:00:00Z"}',
                "end_json": '{"dateTime":"2026-01-01T02:00:00Z"}',
                "event_start": "2026-01-01T01:00:00+00:00",
                "event_end": "2026-01-01T02:00:00+00:00",
                "is_cancelled": False,
            }
        ],
        window_start=old_now,
        window_end=old_now + timedelta(days=1),
        observed_at=old_now,
        snapshot_complete=True,
    )
    with state.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM exchange_calendar_snapshot").fetchone()[0] == 1

    new_now = old_now + timedelta(days=121)
    state.reconcile(
        reconciliation_key="new-scope",
        observed=[],
        window_start=new_now,
        window_end=new_now + timedelta(days=1),
        observed_at=new_now,
        snapshot_complete=True,
    )
    with state.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM exchange_calendar_snapshot").fetchone()[0] == 0


def test_google_event_body_can_include_full_details() -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        google_mirror_summary_mode="subject",
        google_mirror_include_location=True,
        google_mirror_include_description=True,
        google_mirror_include_attendees=True,
        google_mirror_description_max_chars=500,
    )
    item = SimpleNamespace(
        start=datetime(2026, 7, 8, 9, tzinfo=UTC),
        end=datetime(2026, 7, 8, 10, tzinfo=UTC),
        is_all_day=False,
        subject="Outlook title",
        location="Meeting room",
        text_body="Outlook description",
        required_attendees=[
            SimpleNamespace(email_address="person@example.com", name="Person Name"),
        ],
        optional_attendees=[],
    )

    body = google_event_body(item, settings, "source-key")

    assert body["summary"] == "Outlook title"
    assert body["location"] == "Meeting room"
    assert body["description"] == "Outlook description"
    assert body["attendees"] == [{"email": "person@example.com", "displayName": "Person Name"}]


def test_google_shadow_event_body_is_private_import_with_context() -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        google_mirror_summary_mode="subject",
        google_mirror_include_location=True,
        google_mirror_include_description=True,
        google_mirror_include_attendees=True,
    )
    item = SimpleNamespace(
        start=datetime(2026, 7, 8, 9, tzinfo=UTC),
        end=datetime(2026, 7, 8, 10, tzinfo=UTC),
        is_all_day=False,
        subject="Outlook title",
        location="Meeting room",
        text_body="Outlook description",
        required_attendees=[SimpleNamespace(email_address="person@example.com")],
        optional_attendees=[],
    )

    body = google_shadow_event_body(item, settings, "source-key")

    assert body["summary"] == "Outlook title"
    assert body["visibility"] == "private"
    assert body["privateCopy"] is True
    assert body["iCalUID"] == "ews-source-key@exchange-ews-bridge.local"
    assert body["reminders"] == {"useDefault": False}
    assert body["attendees"] == [{"email": "person@example.com"}]
    assert body["location"] == "Meeting room"
    assert body["description"] == "Outlook description"


class FakeGoogleRequest:
    def __init__(self, result=None):
        self.result = result or {}

    def execute(self):
        return self.result


class FakeGoogleEvents:
    def __init__(self, existing=None):
        self.existing = existing or {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return FakeGoogleRequest(self.existing.get(kwargs["eventId"], {}))

    def insert(self, **kwargs):
        self.calls.append(("insert", kwargs))
        return FakeGoogleRequest({"id": "created-google-id"})

    def import_(self, **kwargs):
        self.calls.append(("import", kwargs))
        return FakeGoogleRequest({"id": "created-google-id"})

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return FakeGoogleRequest({"id": kwargs["eventId"]})

    def patch(self, **kwargs):
        self.calls.append(("patch", kwargs))
        return FakeGoogleRequest({"id": kwargs["eventId"]})

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return FakeGoogleRequest()


class FakeGoogleService:
    def __init__(self, existing=None):
        self.events_api = FakeGoogleEvents(existing)

    def events(self):
        return self.events_api


def google_mirror_item(*, item_id="ews-id", cancelled=False):
    return SimpleNamespace(
        id=item_id,
        start=datetime(2026, 7, 8, 9, tzinfo=UTC),
        end=datetime(2026, 7, 8, 10, tzinfo=UTC),
        is_all_day=False,
        is_cancelled=cancelled,
        subject="Private work meeting",
        location="Must not leave Exchange",
        text_body="Must not leave Exchange",
        required_attendees=[SimpleNamespace(email_address="person@example.com")],
        optional_attendees=[],
    )


def test_google_mirror_imports_private_copy_with_attendees(monkeypatch, tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
        google_calendar_enabled=True,
        google_calendar_id="private-shadow-calendar",
        google_mirror_summary_mode="subject",
        google_mirror_include_location=True,
        google_mirror_include_description=True,
        google_mirror_include_attendees=True,
    )
    service = FakeGoogleService()
    monkeypatch.setattr("exchange_ews_bridge.google_calendar.google_calendar_service", lambda _settings: service)

    stats = mirror_events_to_google(settings, [google_mirror_item()])

    assert stats["created"] == 1
    imported = next(kwargs for action, kwargs in service.events_api.calls if action == "import")
    assert "sendUpdates" not in imported
    assert imported["body"]["privateCopy"] is True
    assert imported["body"]["attendees"] == [{"email": "person@example.com"}]
    assert imported["body"]["location"] == "Must not leave Exchange"
    assert imported["body"]["description"] == "Must not leave Exchange"


def test_google_mirror_guards_update_and_delete_and_suppresses_updates(monkeypatch, tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
        google_calendar_enabled=True,
        google_calendar_id="private-shadow-calendar",
        google_mirror_summary_mode="subject",
    )
    active = google_mirror_item(item_id="active")
    cancelled = google_mirror_item(item_id="cancelled", cancelled=True)
    state = MirrorState(tmp_path)
    state.upsert(stable_source_key(active), "active-google-id", "old-hash", datetime.now(UTC).isoformat())
    state.upsert(stable_source_key(cancelled), "cancelled-google-id", "old-hash", datetime.now(UTC).isoformat())
    service = FakeGoogleService(
        {
            "active-google-id": {
                "id": "active-google-id",
                "privateCopy": True,
                "extendedProperties": {"private": {"exchangeEwsBridge": "true"}},
            },
            "cancelled-google-id": {"id": "cancelled-google-id"},
        }
    )
    monkeypatch.setattr("exchange_ews_bridge.google_calendar.google_calendar_service", lambda _settings: service)

    stats = mirror_events_to_google(settings, [active, cancelled])

    assert stats["updated"] == 1
    assert stats["deleted"] == 1
    update = next(kwargs for action, kwargs in service.events_api.calls if action == "patch")
    delete = next(kwargs for action, kwargs in service.events_api.calls if action == "delete")
    assert update["sendUpdates"] == "none"
    assert delete["sendUpdates"] == "none"


def test_google_mirror_replaces_legacy_shadow_with_private_import(monkeypatch, tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
        google_calendar_enabled=True,
        google_calendar_id="private-shadow-calendar",
        google_mirror_summary_mode="subject",
        google_mirror_include_attendees=True,
    )
    item = google_mirror_item()
    state = MirrorState(tmp_path)
    state.upsert(stable_source_key(item), "legacy-google-id", "old-hash", datetime.now(UTC).isoformat())
    service = FakeGoogleService({"legacy-google-id": {"id": "legacy-google-id"}})
    monkeypatch.setattr("exchange_ews_bridge.google_calendar.google_calendar_service", lambda _settings: service)

    stats = mirror_events_to_google(settings, [item])

    assert stats["updated"] == 1
    assert [action for action, _kwargs in service.events_api.calls] == ["get", "import", "delete"]
    delete = next(kwargs for action, kwargs in service.events_api.calls if action == "delete")
    assert delete["sendUpdates"] == "none"
    assert state.get(stable_source_key(item))["google_event_id"] == "created-google-id"


def test_google_mirror_refuses_any_event_with_attendees_before_mutation(monkeypatch, tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
        google_calendar_enabled=True,
        google_calendar_id="private-shadow-calendar",
        google_mirror_summary_mode="subject",
    )
    active = google_mirror_item(item_id="active")
    state = MirrorState(tmp_path)
    state.upsert(stable_source_key(active), "active-google-id", "old-hash", datetime.now(UTC).isoformat())
    service = FakeGoogleService(
        {"active-google-id": {"id": "active-google-id", "attendees": [{"email": "person@example.com"}]}}
    )
    monkeypatch.setattr("exchange_ews_bridge.google_calendar.google_calendar_service", lambda _settings: service)

    with pytest.raises(Exception) as exc_info:
        mirror_events_to_google(settings, [active])

    assert getattr(exc_info.value, "status_code", None) == 409
    assert [action for action, _kwargs in service.events_api.calls] == ["get"]


def test_google_mirror_never_deletes_missing_from_saturated_snapshot(tmp_path) -> None:
    settings = Settings(
        ews_password="test",
        bridge_api_token="secret-token-123456",
        data_dir=tmp_path,
        google_mirror_delete_missing=True,
    )
    state = MirrorState(tmp_path)
    state.upsert("missing-source", "missing-google-id", "old-hash", datetime.now(UTC).isoformat())

    stats = mirror_events_to_google(settings, [], dry_run=True, snapshot_complete=False)

    assert stats["deleted"] == 0
    assert state.get("missing-source") is not None
