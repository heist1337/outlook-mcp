from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from exchange_ews_bridge.config import get_settings


def require_bearer_token(
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings = get_settings()
    supplied = authorization.removeprefix("Bearer ").strip()
    expected = settings.bridge_api_token.get_secret_value()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid bearer token",
        )


def require_write_token(authorization: Annotated[str | None, Header(include_in_schema=False)] = None) -> None:
    settings = get_settings()
    write = settings.ews_write_api_token
    if write is None or not settings.ews_enabled_write_groups:
        raise HTTPException(403, "ews_writes_disabled")
    expected = write.get_secret_value()
    # The existing read token must never gain mutation privileges.
    if hmac.compare_digest(expected, settings.bridge_api_token.get_secret_value()):
        raise HTTPException(403, "distinct_write_token_required")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing_write_token")
    if not hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), expected):
        raise HTTPException(403, "invalid_write_token")
