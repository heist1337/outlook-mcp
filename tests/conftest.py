from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from exchange_ews_bridge.app import app
from exchange_ews_bridge.config import Settings, get_settings


@pytest.fixture
def client(monkeypatch):
    settings = Settings(
        ews_password="test",
        bridge_api_token="test-token-123456",
        ews_mail_attachments_enabled=True,
        ews_mail_attachment_max_bytes=1024,
        ews_shared_mailbox="settlements@example.com",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr("exchange_ews_bridge.security.get_settings", lambda: settings)
    try:
        yield TestClient(app), settings, {"Authorization": "Bearer test-token-123456"}
    finally:
        app.dependency_overrides.clear()
