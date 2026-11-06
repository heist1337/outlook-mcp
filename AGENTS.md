# Development

Use Python 3.12 and uv. Keep deployment examples independent of any workstation,
company or private repository. Only synthetic fixtures and example.com addresses
belong in tests. Never commit credentials, real env files, mailbox contents or state.

Run `uv sync --locked`, `uv run --with ruff==0.17.3 ruff check src tests examples`,
`uv run pytest`, `uv build` and a redacted secret scan before publication.
Keep write scopes, signed references and unknown-outcome reconciliation intact.
Do not modify real mailboxes to test packaging or documentation.
