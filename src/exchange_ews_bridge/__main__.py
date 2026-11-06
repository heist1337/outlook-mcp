"""Local authenticated HTTP MCP entry point."""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Outlook MCP for Exchange EWS")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(
        "exchange_ews_bridge.mcp_app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
