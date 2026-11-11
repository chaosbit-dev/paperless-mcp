from __future__ import annotations

import contextlib
import json
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from paperless_mcp.access import AccessValidator
from paperless_mcp.config import IdentityConfig, Settings
from paperless_mcp.paperless import PaperlessClient
from paperless_mcp.server import build_app, prepare
from paperless_mcp.service import DocumentService

from .fake_paperless import FakePaperless

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.applications import Starlette

TEAM_DOMAIN = "team.cloudflareaccess.test"
AUDIENCE = "aud-123"
UI_URL = "https://paperless.home.example"
OWNER_EMAIL = "owner@example.com"

EXTRACTOR = "jocasta-extractor"


@pytest.fixture
def fake() -> FakePaperless:
    """A small archive covering every kind of document the rules distinguish.

    1, 2, 3, 9  ordinary EOBs (3 has Patient Responsibility attached but empty)
    4           private EOB                -> metadata-only
    5           no document type           -> metadata-only
    6, 8        School Record              -> excluded (8 is also private)
    7           Form 1098, no fields
    """
    f = FakePaperless()
    f.add_document(
        1,
        "Acme Health EOB January",
        correspondent=1,
        tags=[12],
        created="2026-01-10",
        fields={
            17: "USD120.50",
            18: "Dr. Alpha",
            19: "2026-01-05",
            21: "C-1",
            8: "ok v3 2026-01-11 haiku EOB",
            9: 0,
        },
        notes=[(EXTRACTOR, "Filled Provider (haiku)"), ("someone", "call them back")],
        content="statement of benefits",
    )
    f.add_document(
        2,
        "Acme Health EOB February",
        correspondent=1,
        tags=[12, 10],
        created="2026-02-10",
        fields={17: "USD30.00", 18: "Dr. Beta"},
        content="statement statement",
    )
    f.add_document(
        3,
        "Acme Health EOB March",
        correspondent=1,
        tags=[12],
        created="2026-03-10",
        fields={17: None, 18: "Dr. Alpha"},
        content="statement",
    )
    f.add_document(
        4,
        "Private EOB",
        tags=[9, 12],
        created="2026-03-15",
        fields={17: "USD999.00", 18: "Dr. Secret"},
        content="statement",
    )
    f.add_document(
        5,
        "Loose scan",
        document_type=None,
        created="2026-04-01",
        fields={17: "USD777.00"},
        content="statement",
    )
    f.add_document(
        6,
        "Report card",
        document_type=3,
        created="2026-04-02",
        fields={18: "School"},
        content="statement",
    )
    f.add_document(
        7,
        "Mortgage interest 2025",
        document_type=2,
        correspondent=2,
        tags=[11],
        created="2025-12-31",
    )
    f.add_document(8, "Private report card", document_type=3, tags=[9], created="2026-04-03")
    f.add_document(9, "Acme Health EOB April", tags=[12], created="2026-04-10", fields={17: "USD5.00"})
    return f


@pytest.fixture
async def service(fake: FakePaperless) -> AsyncIterator[DocumentService]:
    client = PaperlessClient("http://paperless.test", "tok-reader", transport=fake.transport())
    yield DocumentService(client, ui_base_url=UI_URL, excluded_document_types=["School Record"])
    await client.aclose()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        paperless_url="http://paperless.test",
        paperless_ui_url=UI_URL,
        access_team_domain=TEAM_DOMAIN,
        access_audience=AUDIENCE,
        identities=[
            IdentityConfig(email="Owner@Example.com", token_env="TOKEN_OWNER", mode="read")
        ],
        excluded_document_types=["School Record"],
        allowed_hosts=["paperless-mcp.test"],
    )


ENVIRON = {"TOKEN_OWNER": "tok-reader"}


class AccessKeys:
    """The Access team's signing keys, served the way Cloudflare serves them."""

    def __init__(self) -> None:
        self.fetches = 0
        self._keys: dict[str, rsa.RSAPrivateKey] = {}
        self.current = self.rotate()

    def rotate(self) -> str:
        kid = f"kid-{len(self._keys) + 1}"
        self._keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.current = kid
        return kid

    def transport(self) -> httpx2.MockTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            assert request.url.host == TEAM_DOMAIN
            assert request.url.path == "/cdn-cgi/access/certs"
            self.fetches += 1
            keys = []
            for kid, key in self._keys.items():
                jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
                keys.append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})
            return httpx2.Response(200, json={"keys": keys})

        return httpx2.MockTransport(handle)

    def token(
        self,
        email: str = OWNER_EMAIL,
        *,
        kid: str | None = None,
        key: rsa.RSAPrivateKey | None = None,
        algorithm: str = "RS256",
        **overrides: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "aud": [AUDIENCE],
            "iss": f"https://{TEAM_DOMAIN}",
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "email": email,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        kid = kid or self.current
        signing_key: Any = key or self._keys.get(kid) or next(iter(self._keys.values()))
        return jwt.encode(claims, signing_key, algorithm=algorithm, headers={"kid": kid})


@pytest.fixture
def access_keys() -> AccessKeys:
    return AccessKeys()


HOST = "paperless-mcp.test"


async def build(
    settings: Settings,
    fake: FakePaperless,
    access_keys: AccessKeys,
    environ: dict[str, str] | None = None,
) -> Starlette:
    """Start the server the way `main` does, against the fakes."""
    runtime = await prepare(settings, environ or ENVIRON, transport=fake.transport())
    validator = AccessValidator(TEAM_DOMAIN, AUDIENCE, transport=access_keys.transport())
    return build_app(runtime, validator=validator, transport=fake.transport())


@asynccontextmanager
async def http_client(
    app: Starlette, token: str | None = None, *, serve: bool = True
) -> AsyncIterator[httpx2.AsyncClient]:
    """An HTTP client on the app. The app's lifespan runs once: pass `serve=False` under
    a lifespan the test already entered."""
    headers = {"cf-access-jwt-assertion": token} if token else {}
    async with contextlib.AsyncExitStack() as stack:
        if serve:
            await stack.enter_async_context(app.router.lifespan_context(app))
        yield await stack.enter_async_context(
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=f"http://{HOST}", headers=headers
            )
        )


@asynccontextmanager
async def mcp_connect(
    app: Starlette, token: str, *, serve: bool = True
) -> AsyncIterator[tuple[ClientSession, Any]]:
    """An MCP client session over streamable HTTP, with the server's initialize result."""
    async with (
        http_client(app, token, serve=serve) as client,
        streamable_http_client(f"http://{HOST}/mcp", http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        yield session, await session.initialize()


@asynccontextmanager
async def mcp_session(
    app: Starlette, token: str, *, serve: bool = True
) -> AsyncIterator[ClientSession]:
    """An MCP client session as the caller the token names."""
    async with mcp_connect(app, token, serve=serve) as (session, _):
        yield session


def payload(result: Any) -> dict[str, Any]:
    """A tool result as the dict the tool returned."""
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))
