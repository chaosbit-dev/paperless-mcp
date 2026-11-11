"""The perimeter: Cloudflare Access in front of the MCP application."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from paperless_mcp.access import AccessMiddleware, AccessValidator
from paperless_mcp.config import Identity

from .conftest import AUDIENCE, TEAM_DOMAIN, build, http_client, mcp_session, payload

if TYPE_CHECKING:
    from paperless_mcp.config import Settings

    from .conftest import AccessKeys
    from .fake_paperless import FakePaperless

IDENTITIES = {"owner@example.com": Identity("owner@example.com", "tok-reader", "read")}


class Inner:
    """Stands in for the MCP application and records whether a request reached it."""

    def __init__(self) -> None:
        self.reached = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.reached += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})


async def post(inner: Inner, keys: AccessKeys, token: str | None) -> httpx2.Response:
    validator = AccessValidator(TEAM_DOMAIN, AUDIENCE, transport=keys.transport())
    app = AccessMiddleware(inner, validator, IDENTITIES)
    headers = {"cf-access-jwt-assertion": token} if token is not None else {}
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://paperless-mcp.test"
    ) as client:
        return await client.post("/mcp", headers=headers, json={})


# @spec DACC-ACC-001
async def test_mcp_is_served_over_streamable_http_at_mcp(
    settings: Settings, fake: FakePaperless, access_keys: AccessKeys
) -> None:
    app = await build(settings, fake, access_keys)
    async with mcp_session(app, access_keys.token()) as session:
        schema = payload(await session.call_tool("list_schema", {}))
    assert "EOB" in schema["document_types"]


# @spec DACC-ACC-002
async def test_request_without_the_access_header_is_401_and_never_reaches_mcp(
    access_keys: AccessKeys,
) -> None:
    inner = Inner()
    assert (await post(inner, access_keys, None)).status_code == 401
    assert (await post(inner, access_keys, "")).status_code == 401
    assert inner.reached == 0


# @spec DACC-ACC-003
@pytest.mark.parametrize(
    "claims",
    [
        {"aud": ["someone-else"]},
        {"iss": "https://evil.cloudflareaccess.test"},
        {"exp": int(time.time()) - 60},
        {"nbf": int(time.time()) + 3600},
        {"exp": None},
        {"aud": None},
        {"iss": None},
    ],
)
async def test_jwt_failing_claim_validation_is_401(
    access_keys: AccessKeys, claims: dict[str, Any]
) -> None:
    inner = Inner()
    assert (await post(inner, access_keys, access_keys.token(**claims))).status_code == 401
    assert inner.reached == 0


# @spec DACC-ACC-003
async def test_jwt_failing_signature_verification_is_401(access_keys: AccessKeys) -> None:
    inner = Inner()
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = access_keys.token(key=stranger)
    assert (await post(inner, access_keys, forged)).status_code == 401
    unsigned = access_keys.token(key="shared-secret-of-at-least-32-bytes!!", algorithm="HS256")  # type: ignore[arg-type]
    assert (await post(inner, access_keys, unsigned)).status_code == 401
    assert (await post(inner, access_keys, "not-a-jwt")).status_code == 401
    assert inner.reached == 0


async def test_valid_jwt_for_a_known_caller_reaches_mcp(access_keys: AccessKeys) -> None:
    inner = Inner()
    assert (await post(inner, access_keys, access_keys.token())).status_code == 200
    assert inner.reached == 1


# @spec DACC-ACC-004
async def test_unknown_key_id_refetches_the_key_set_once(access_keys: AccessKeys) -> None:
    inner = Inner()
    validator = AccessValidator(TEAM_DOMAIN, AUDIENCE, transport=access_keys.transport())
    await validator.validate(access_keys.token())
    await validator.validate(access_keys.token())
    assert access_keys.fetches == 1

    rotated = access_keys.rotate()
    await validator.validate(access_keys.token(kid=rotated))
    assert access_keys.fetches == 2

    assert (
        await post(inner, access_keys, access_keys.token(kid="kid-unknown"))
    ).status_code == 401
    assert access_keys.fetches == 4  # the fresh validator's first fetch, then one refetch


# @spec DACC-ACC-005
async def test_email_matching_no_identity_is_403(access_keys: AccessKeys) -> None:
    inner = Inner()
    stranger = access_keys.token(email="stranger@example.com")
    assert (await post(inner, access_keys, stranger)).status_code == 403
    assert (await post(inner, access_keys, access_keys.token(email=None))).status_code == 403
    assert inner.reached == 0


# @spec DACC-ACC-005
async def test_email_is_compared_case_insensitively(access_keys: AccessKeys) -> None:
    inner = Inner()
    shouting = access_keys.token(email="OWNER@EXAMPLE.COM")
    assert (await post(inner, access_keys, shouting)).status_code == 200


# @spec DACC-ACC-006
async def test_healthz_is_200_and_empty_without_a_jwt(
    settings: Settings, fake: FakePaperless, access_keys: AccessKeys
) -> None:
    app = await build(settings, fake, access_keys)
    async with http_client(app) as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.content == b""
        assert (await client.post("/mcp", json={})).status_code == 401
    assert access_keys.fetches == 0


# @spec DACC-ACC-003
@pytest.mark.parametrize("body", [None, [], {"keys": None}, {"keys": ["x"]}, {"keys": [{}]}])
async def test_unusable_key_set_is_401(access_keys: AccessKeys, body: Any) -> None:
    validator = AccessValidator(
        TEAM_DOMAIN,
        AUDIENCE,
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json=body)),
    )
    inner = Inner()
    app = AccessMiddleware(inner, validator, IDENTITIES)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://paperless-mcp.test"
    ) as client:
        response = await client.post(
            "/mcp", headers={"cf-access-jwt-assertion": access_keys.token()}, json={}
        )
    assert response.status_code == 401
    assert inner.reached == 0
