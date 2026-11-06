"""Bounded live read: no mailbox contents, tokens or raw errors in output."""

import asyncio
import os

from fastmcp import Client


async def main() -> None:
    async with Client(
        os.environ.get("OUTLOOK_MCP_URL", "http://127.0.0.1:8080/mcp/"),
        auth=os.environ["BRIDGE_API_TOKEN"],
    ) as client:
        tools = await client.list_tools()
        result = await client.call_tool("mail_folders", {"mailbox": "self", "limit": 1})
        if result.is_error:
            raise RuntimeError("Mailbox read failed")
        print(f"Connected: {len(tools)} tools; mailbox folder read passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        raise SystemExit(f"Connection check failed ({type(error).__name__}); check configuration locally.") from None
