"""Cloudflare Access validation in front of the MCP application."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx2
import jwt
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Receive, Scope, Send

    from paperless_mcp.config import Identity

HEALTH_PATH = "/healthz"
# Where the middleware leaves the caller's identity for the tools.
IDENTITY_SCOPE_KEY = "paperless_mcp.identity"


class AccessDenied(Exception):
    """The Access JWT is missing or does not validate."""


class AccessValidator:
    def __init__(
        self,
        team_domain: str,
        audience: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._issuer = f"https://{team_domain}"
        self._certs_url = f"{self._issuer}/cdn-cgi/access/certs"
        self._audience = audience
        self._http = httpx2.AsyncClient(transport=transport, timeout=10.0)
        self._keys: dict[str, jwt.PyJWK] = {}
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

    # @spec DACC-ACC-004
    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            if not self._keys:
                await self._fetch_keys()
            # A key id the cached set does not hold may be a rotation: look once more.
            if kid not in self._keys:
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
            await _refuse(401, "unauthorized", "missing Access token")(scope, receive, send)
            return
        try:
            claims = await self._validator.validate(token)
        except AccessDenied as denied:
            await _refuse(401, "unauthorized", str(denied))(scope, receive, send)
            return
        # @spec DACC-ACC-005
        email = claims.get("email")
        identity = self._identities.get(email.lower()) if isinstance(email, str) else None
        if identity is None:
            await _refuse(403, "forbidden", "no identity for this caller")(scope, receive, send)
            return
        scope[IDENTITY_SCOPE_KEY] = identity
        await self._app(scope, receive, send)


def _refuse(status: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status)
