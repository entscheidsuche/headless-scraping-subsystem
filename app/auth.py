"""Bearer-token auth for the headless subsystem."""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from .config import settings


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """Reject requests without a matching Bearer token.

    When ``BEARER_TOKEN`` is empty (development only), auth is disabled.
    """
    if not settings.bearer_token:
        return
    expected = f"Bearer {settings.bearer_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
