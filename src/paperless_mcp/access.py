"""Cloudflare Access validation in front of the MCP application."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2
import jwt
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from paperless_mcp.config import Identity

HEALTH_PATH = "/healthz"
# Where the middleware leaves the caller's identity for the tools.
IDENTITY_SCOPE_KEY = "paperless_mcp.identity"
# A refusal line records the path, which the caller chose: bound it.
PATH_LOG_LIMIT = 200
# How long a fetched key set is trusted to be complete, in seconds.
KEY_REFETCH_INTERVAL = 60.0


class AccessDenied(Exception):
    """The Access JWT is missing or does not validate."""


class AccessValidator:
    def __init__(
        self,
        team_domain: str,
        audience: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._issuer = f"https://{team_domain}"
        self._certs_url = f"{self._issuer}/cdn-cgi/access/certs"
        self._audience = audience
        self._http = httpx2.AsyncClient(transport=transport, timeout=10.0)
        self._keys: dict[str, jwt.PyJWK] = {}
        self._clock = clock
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def _fetch_keys(self) -> None:
        try:
            response = await self._http.get(self._certs_url)
            response.raise_for_status()
            body = response.json()
        except (httpx2.HTTPError, ValueError):
            raise AccessDenied("signing keys unavailable") from None
        entries = body.get("keys") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            raise AccessDenied("signing keys unavailable")
        keys: dict[str, jwt.PyJWK] = {}
        for entry in entries:
            try:
                key = jwt.PyJWK.from_dict(entry)
            except (jwt.PyJWTError, TypeError, AttributeError, KeyError, ValueError):
                continue
            if key.key_id:
                keys[key.key_id] = key
        self._keys = keys
        self._fetched_at = self._clock()

    # @spec DACC-ACC-004
    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            # A key id the cached set does not hold may be a rotation: look again, but
            # no more often than the interval, or any caller could make this server
            # fetch on every request by inventing key ids.
            if kid not in self._keys and (
                self._fetched_at is None
                or self._clock() - self._fetched_at >= KEY_REFETCH_INTERVAL
            ):
                await self._fetch_keys()
        try:
            return self._keys[kid]
        except KeyError:
            raise AccessDenied("unknown signing key") from None

    # @spec DACC-ACC-003
    async def validate(self, token: str) -> dict[str, Any]:
        """Return the JWT's claims, or raise `AccessDenied`."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            raise AccessDenied("malformed token") from None
        if not isinstance(kid, str) or not kid:
            raise AccessDenied("token names no signing key")
        key = await self._signing_key(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except jwt.PyJWTError:
            raise AccessDenied("token failed validation") from None
        return claims

    async def aclose(self) -> None:
        await self._http.aclose()


class AccessMiddleware:
    def __init__(
        self, app: ASGIApp, validator: AccessValidator, identities: Mapping[str, Identity]
    ) -> None:
        self._app = app
        self._validator = validator
        self._identities = identities

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        # @spec DACC-ACC-006
        if scope["path"] == HEALTH_PATH:
            await self._app(scope, receive, send)
            return
        # @spec DACC-ACC-002
        token = Headers(scope=scope).get("cf-access-jwt-assertion")
        if not token:
            await self._refuse(scope, receive, send, 401, "missing Access token")
            return
        try:
            claims = await self._validator.validate(token)
        except AccessDenied as denied:
            await self._refuse(scope, receive, send, 401, str(denied))
            return
        # @spec DACC-ACC-005
        email = claims.get("email")
        caller = email.lower() if isinstance(email, str) else None
        identity = self._identities.get(caller) if caller is not None else None
        if identity is None:
            await self._refuse(
                scope, receive, send, 403, "no identity for this caller", caller=caller
            )
            return
        scope[IDENTITY_SCOPE_KEY] = identity

        # An error the MCP application answers with (a refused Host header, an
        # unknown path) is as invisible to the caller's side as one raised here.
        async def watched(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] >= 400:
                _log_refusal(scope, message["status"], caller=caller)
            await send(message)

        await self._app(scope, receive, watched)

    async def _refuse(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        detail: str,
        *,
        caller: str | None = None,
    ) -> None:
        _log_refusal(scope, status, reason=detail, caller=caller)
        error = "unauthorized" if status == 401 else "forbidden"
        response = JSONResponse({"error": error, "detail": detail}, status_code=status)
        await response(scope, receive, send)


# @spec DACC-AUD-004, DACC-AUD-002
def _log_refusal(
    scope: Scope, status: int, *, reason: str | None = None, caller: str | None = None
) -> None:
    """One line on stdout per request answered with an error; never the token."""
    line: dict[str, Any] = {
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": status,
        "method": scope["method"],
        "path": scope["path"][:PATH_LOG_LIMIT],
    }
    if caller is not None:
        line["caller"] = caller
    if reason is not None:
        line["reason"] = reason
    print(json.dumps(line), file=sys.stdout, flush=True)
