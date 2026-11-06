"""Curated native FastMCP providers over the existing authenticated EWS API."""

from __future__ import annotations

import hmac
import re
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier, restrict_tag
from fastmcp.server.middleware import AuthMiddleware
from fastmcp.server.providers.openapi import MCPType, OpenAPIProvider, RouteMap
from fastmcp.utilities.lifespan import combine_lifespans
from mcp_types import ToolAnnotations

from exchange_ews_bridge.config import Settings, get_settings

# Method, path, public name, and continuation/effect contract. Everything else is excluded.
READ_TOOLS = (
    (
        "GET",
        "/v1/ews/capabilities",
        "outlook_capabilities",
        "Discover enabled capabilities and limits; catalogue entries do not prove mailbox access.",
    ),
    (
        "GET",
        "/v1/ews/inspect",
        "outlook_inspect",
        "Inspect access for a configured mailbox alias without changing permissions.",
    ),
    (
        "GET",
        "/v1/mail/folders",
        "mail_folders",
        "List immediate child folders. Check complete and continue with next_offset.",
    ),
    (
        "GET",
        "/v1/mail/search",
        "mail_search",
        "Search a bounded mailbox/date range. Follow next_cursor until complete; a page is not a full-period result.",
    ),
    (
        "GET",
        "/v1/mail/message",
        "mail_message",
        "Read selected message text by signed reference. Follow the returned continuation for long bodies.",
    ),
    (
        "GET",
        "/v1/mail/attachments",
        "mail_attachments",
        "List selected message attachments; listing does not download their content.",
    ),
    (
        "GET",
        "/v1/mail/attachment",
        "mail_attachment",
        "Download one bounded attachment chunk. Follow next_offset and verify the final SHA-256.",
    ),
    (
        "GET",
        "/v1/mail/attached-message",
        "mail_attached_message",
        "Read an attached message using its signed attachment reference.",
    ),
    (
        "GET",
        "/v1/mail/reference",
        "mail_refresh_reference",
        "Refresh an existing message reference; do not invent or edit signed references.",
    ),
    (
        "GET",
        "/v1/mail/changes",
        "mail_changes",
        "Read one incremental checkpoint batch. Continue until caught_up, then retain the checkpoint.",
    ),
    (
        "GET",
        "/v1/calendar/context",
        "calendar_context",
        "Read native Outlook occurrences for an explicit bounded timezone-aware date range.",
    ),
    (
        "GET",
        "/v1/calendar/event",
        "calendar_event",
        "Read a selected event using its signed event reference and returned continuation.",
    ),
    (
        "GET",
        "/v1/calendar/freebusy",
        "calendar_freebusy",
        "Read availability in a bounded timezone-aware interval without exposing event contents.",
    ),
    (
        "GET",
        "/v1/calendar/upcoming",
        "calendar_upcoming",
        "Read upcoming Outlook events; account for configured limits and field visibility.",
    ),
    (
        "POST",
        "/v1/ews/read",
        "outlook_read",
        "Perform one typed native EWS read. Use configured mailbox aliases and respect pagination and coverage.",
    ),
    (
        "POST",
        "/v1/ews/prepare",
        "outlook_prepare",
        "Prepare one typed action without performing it. Inspect scope, effects and the expiring signed plan_ref.",
    ),
)
EXECUTE_TOOL = (
    "POST",
    "/v1/ews/execute",
    "outlook_execute",
    "Execute an explicitly authorized signed plan_ref with a stable request_id. On an unknown outcome reconcile before retrying; never change request_id to bypass the receipt ledger.",
)


class BridgeTokenVerifier(TokenVerifier):
    """Adapt existing opaque bridge keys; FastMCP owns HTTP auth and authorization."""

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        read = self.settings.bridge_api_token.get_secret_value()
        if hmac.compare_digest(token, read):
            return AccessToken(token=token, client_id="outlook-reader", scopes=["outlook:read"])
        write = self.settings.ews_write_api_token
        if write and self.settings.ews_enabled_write_groups and hmac.compare_digest(token, write.get_secret_value()):
            return AccessToken(token=token, client_id="outlook-executor", scopes=["outlook:execute"])
        return None


def create_mcp(api: FastAPI, settings: Settings) -> FastMCP:
    """Expose curated API operations in-process, retaining every API dependency/guard."""
    spec = api.openapi()
    clients: list[httpx2.AsyncClient] = []

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        async with AsyncExitStack() as stack:
            for client in clients:
                await stack.enter_async_context(client)
            yield

    mcp = FastMCP(
        "Outlook",
        instructions="Use capabilities first. Mail and calendar are authoritative live sources. Follow continuation markers; never treat a partial page as complete. Writes require prepare, explicit user intent, and execute with the same request_id when reconciling retries.",
        auth=BridgeTokenVerifier(settings),
        middleware=[
            AuthMiddleware(
                auth=[
                    restrict_tag("read", scopes=["outlook:read"]),
                    restrict_tag("execute", scopes=["outlook:execute"]),
                ]
            )
        ],
        lifespan=lifespan,
        mask_error_details=True,
    )

    def add_provider(tools, token: str, tag: str) -> None:
        descriptions = {name: description for _, _, name, description in tools}
        names = {spec["paths"][path][method.lower()]["operationId"]: name for method, path, name, _ in tools}
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=api),
            base_url="http://exchange-bridge",
            headers={"Authorization": "Bearer " + token},
            timeout=60,
            follow_redirects=False,
            trust_env=False,
        )
        clients.append(client)

        def describe(route: Any, component: Any) -> None:
            component.description = descriptions[component.name]
            component.annotations = ToolAnnotations(
                read_only_hint=tag == "read",
                destructive_hint=tag == "execute",
                idempotent_hint=tag == "read",
                open_world_hint=True,
            )

        mcp.add_provider(
            OpenAPIProvider(
                openapi_spec=spec,
                client=client,
                mcp_names=names,
                mcp_component_fn=describe,
                route_maps=[
                    *[
                        RouteMap(
                            methods=[method], pattern="^" + re.escape(path) + "$", mcp_type=MCPType.TOOL, mcp_tags={tag}
                        )
                        for method, path, _, _ in tools
                    ],
                    RouteMap(mcp_type=MCPType.EXCLUDE),
                ],
            )
        )

    add_provider(READ_TOOLS, settings.bridge_api_token.get_secret_value(), "read")
    write = settings.ews_write_api_token
    if (
        write
        and settings.ews_enabled_write_groups
        and not hmac.compare_digest(write.get_secret_value(), settings.bridge_api_token.get_secret_value())
    ):
        add_provider((EXECUTE_TOOL,), write.get_secret_value(), "execute")
    return mcp


def create_app() -> FastAPI:
    """Uvicorn factory: existing REST API and authenticated MCP, without Windmill."""
    from exchange_ews_bridge.app import app as api

    mcp_http = create_mcp(api, get_settings()).http_app(path="/")
    combined = FastAPI(
        title=api.title,
        version=api.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=combine_lifespans(api.router.lifespan_context, mcp_http.lifespan),
    )
    combined.mount("/mcp", mcp_http)
    combined.mount("/", api)
    return combined
