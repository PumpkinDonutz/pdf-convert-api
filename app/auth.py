"""Bearer-token authentication with constant-time comparison."""
from __future__ import annotations

import secrets

from fastapi import Request

from .schemas import invalid_token, missing_token


def authenticate(request: Request) -> None:
    """Raise ApiError(401) unless the request carries a configured bearer token."""
    values = request.headers.getlist("authorization")
    if not values:
        raise missing_token()
    if len(values) > 1:
        raise invalid_token()

    scheme, _, token = values[0].partition(" ")
    if scheme.lower() != "bearer" or not token or token != token.strip() or " " in token:
        raise invalid_token()

    candidate = token.encode("utf-8")
    matched = False
    for configured in request.app.state.settings.api_tokens:
        # Check every configured token (no short-circuit) to keep timing uniform.
        matched |= secrets.compare_digest(candidate, configured.encode("utf-8"))
    if not matched:
        raise invalid_token()
