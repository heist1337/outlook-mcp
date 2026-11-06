"""Exercise the real mounted MCP transport; Exchange is replaced with bounded fixtures."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.utilities.asgi_transport import StreamingASGITransport
from pydantic import SecretStr

from exchange_ews_bridge import mcp_app
from exchange_ews_bridge.mcp_app import READ_TOOLS, create_app

READ_TOKEN = "test-token-123456"
WRITE_TOKEN = "test-write-key-that-is-distinct-and-long-enough"


@asynccontextmanager
async def connection(app, token=READ_TOKEN, mode="auto"):
    transport = StreamableHttpTransport(
        url="http://testserver/mcp/",
        auth=token,
        httpx_client_factory=lambda **kw: httpx2.AsyncClient(transport=StreamingASGITransport(app), **kw),
    )
    async with app.router.lifespan_context(app), Client(transport, mode=mode) as client:
        yield client


@pytest.fixture
def mounted(client, monkeypatch):
    _, settings, _ = client
    monkeypatch.setattr(mcp_app, "get_settings", lambda: settings)
    return settings


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_curated_catalog_and_schemas_over_http(mounted, mode):
    async with connection(create_app(), mode=mode) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == {row[2] for row in READ_TOOLS}
        schema = json.dumps([t.model_dump() for t in tools])
        assert "authorization" not in schema.lower()
        assert READ_TOKEN not in schema
        assert WRITE_TOKEN not in schema
        by_name = {t.name: t for t in tools}
        assert by_name["mail_search"].annotations.read_only_hint
        assert "request_id" in json.dumps(by_name["outlook_prepare"].input_schema)
        assert "next_cursor" in by_name["mail_search"].description
        assert "calendar_context" in by_name
        assert not any("mirror" in t.name or "intake" in t.name for t in tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "invalid-test-key"])
async def test_http_requires_a_valid_bridge_credential(mounted, token):
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://testserver") as client,
    ):
        headers = {"Authorization": "Bearer " + token} if token else {}
        response = await client.post("/mcp/", json={}, headers=headers)
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_read_cannot_execute_or_override_credentials(mounted, monkeypatch):
    mounted.ews_write_api_token = SecretStr(WRITE_TOKEN)
    mounted.ews_enabled_write_groups = "calendar"
    mutation = Mock(side_effect=AssertionError("must not mutate Exchange"))
    monkeypatch.setattr("exchange_ews_bridge.operations.build_account", mutation)
    async with connection(create_app()) as client:
        assert "outlook_execute" not in {t.name for t in await client.list_tools()}
        with pytest.raises(ToolError):
            await client.call_tool("outlook_execute", {"plan_ref": "not-a-plan"})
        result = await client.call_tool("outlook_capabilities", {"authorization": "Bearer " + WRITE_TOKEN})
        assert result.data["enabled_write_groups"] == ["calendar"]
    mutation.assert_not_called()


@pytest.mark.asyncio
async def test_executor_has_no_read_privileges_and_invalid_plan_stays_blocked(mounted, monkeypatch):
    mounted.ews_write_api_token = SecretStr(WRITE_TOKEN)
    mounted.ews_enabled_write_groups = "calendar"
    mutation = Mock(side_effect=AssertionError("must not mutate Exchange"))
    monkeypatch.setattr("exchange_ews_bridge.operations.build_account", mutation)
    async with connection(create_app(), WRITE_TOKEN) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools] == ["outlook_execute"]
        assert tools[0].annotations.destructive_hint
        with pytest.raises(ToolError):
            await client.call_tool("outlook_capabilities", {})
        with pytest.raises(ToolError):
            await client.call_tool("outlook_execute", {"plan_ref": "tampered"})
    mutation.assert_not_called()


@pytest.mark.asyncio
async def test_same_read_write_key_never_enables_execution(mounted):
    mounted.ews_write_api_token = SecretStr(READ_TOKEN)
    mounted.ews_enabled_write_groups = "calendar"
    async with connection(create_app()) as client:
        assert "outlook_execute" not in {t.name for t in await client.list_tools()}


@pytest.mark.asyncio
async def test_opt_in_guard_is_an_mcp_error_not_an_empty_result(mounted, monkeypatch):
    lookup = Mock(side_effect=AssertionError("disabled reads must not contact Exchange"))
    monkeypatch.setattr("exchange_ews_bridge.calendar_reads.build_account", lookup)
    async with connection(create_app()) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "calendar_context",
                {
                    "mailbox": "self",
                    "start": "2026-11-02T00:00:00Z",
                    "end": "2026-11-03T00:00:00Z",
                },
            )
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_mail_pagination_keeps_the_api_continuation_contract(mounted, monkeypatch):
    folder = SimpleNamespace(
        children=[SimpleNamespace(name=str(i), folder_class="IPF.Note", total_count=i) for i in range(3)],
        folder_class="IPF.Note",
        total_count=3,
    )
    monkeypatch.setattr("exchange_ews_bridge.mail_reads.resolve_folder", Mock(return_value=folder))
    async with connection(create_app()) as client:
        first = (await client.call_tool("mail_folders", {"limit": 2})).data
        assert len(first["folders"]) == 2
        assert first["complete"] is False and first["next_offset"] == 2
        last = (await client.call_tool("mail_folders", {"limit": 2, "offset": first["next_offset"]})).data
        assert last["complete"] is True and last["next_offset"] is None
        assert last["folders"][0]["folder_path"] == "inbox/2"


@pytest.mark.asyncio
async def test_existing_api_and_health_remain_available(mounted):
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://testserver") as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/v1/ews/capabilities")).status_code == 401
        assert (
            await client.get("/v1/ews/capabilities", headers={"Authorization": "Bearer " + READ_TOKEN})
        ).status_code == 200
