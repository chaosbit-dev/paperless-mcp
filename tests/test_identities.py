"""The identity map and the permission check."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from paperless_mcp.config import IdentityConfig, Settings, load_identities
from paperless_mcp.errors import ConfigError, PaperlessError, PaperlessUnreachable, PermissionDrift
from paperless_mcp.permissions import PermissionMonitor
from paperless_mcp.server import main, prepare

from .conftest import ENVIRON, build, http_client, mcp_session, payload
from .fake_paperless import User

if TYPE_CHECKING:
    from .conftest import AccessKeys
    from .fake_paperless import FakePaperless

OWNER = IdentityConfig(email="owner@example.com", token_env="TOKEN_OWNER", mode="read")


# @spec DACC-ID-001
async def test_tool_calls_use_only_the_callers_token(
    settings: Settings, fake: FakePaperless, access_keys: AccessKeys
) -> None:
    fake.users["tok-other"] = User("other-reader")
    settings.identities.append(
        IdentityConfig(email="other@example.com", token_env="TOKEN_OTHER", mode="read")
    )
    environ = {**ENVIRON, "TOKEN_OTHER": "tok-other"}
    app = await build(settings, fake, access_keys, environ)

    callers = (("owner@example.com", "tok-reader"), ("OTHER@example.com", "tok-other"))
    async with app.router.lifespan_context(app):
        for email, token in callers:
            before = len(fake.requests)
            jwt = access_keys.token(email=email)
            async with mcp_session(app, jwt, serve=False) as session:
                payload(await session.call_tool("search_documents", {"title": "EOB"}))
                payload(await session.call_tool("get_document", {"id": 1}))
            made = fake.requests[before:]
            assert any(r.path.startswith("/api/documents/") for r in made)
            assert {r.token for r in made} == {token}


# @spec DACC-ID-002
@pytest.mark.parametrize(
    ("identities", "environ"),
    [
        ([], ENVIRON),
        ([OWNER.model_copy(update={"mode": "write"})], ENVIRON),
        ([OWNER.model_copy(update={"mode": "admin"})], ENVIRON),
        ([OWNER, OWNER.model_copy(update={"email": "Owner@Example.COM"})], ENVIRON),
        ([OWNER], {}),
        ([OWNER], {"TOKEN_OWNER": ""}),
    ],
)
async def test_invalid_identity_map_refuses_to_start(
    settings: Settings,
    fake: FakePaperless,
    identities: list[IdentityConfig],
    environ: dict[str, str],
) -> None:
    settings.identities = identities
    with pytest.raises(ConfigError):
        load_identities(settings, environ)
    with pytest.raises(ConfigError):
        await prepare(settings, environ, transport=fake.transport())
    assert fake.requests == []


# @spec DACC-ID-002
def test_main_exits_non_zero_on_an_invalid_identity_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERLESS_MCP_PAPERLESS_URL", "http://paperless.invalid")
    monkeypatch.setenv("PAPERLESS_MCP_PAPERLESS_UI_URL", "http://paperless.invalid")
    monkeypatch.setenv("PAPERLESS_MCP_ACCESS_TEAM_DOMAIN", "team.invalid")
    monkeypatch.setenv("PAPERLESS_MCP_ACCESS_AUDIENCE", "aud")
    monkeypatch.setenv("PAPERLESS_MCP_IDENTITIES", "[]")
    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code not in (0, None)


def test_identity_map_is_keyed_by_lower_cased_email(settings: Settings) -> None:
    identities = load_identities(settings, ENVIRON)
    assert list(identities) == ["owner@example.com"]
    assert identities["owner@example.com"].token == "tok-reader"


# @spec DACC-ID-003
async def test_startup_reads_each_identitys_permissions_with_its_own_token(
    settings: Settings, fake: FakePaperless
) -> None:
    fake.users["tok-other"] = User("other-reader")
    settings.identities.append(
        IdentityConfig(email="other@example.com", token_env="TOKEN_OTHER", mode="read")
    )
    runtime = await prepare(
        settings, {**ENVIRON, "TOKEN_OTHER": "tok-other"}, transport=fake.transport()
    )
    reads = [r.token for r in fake.requests if r.path == "/api/ui_settings/"]
    assert sorted(reads) == ["tok-other", "tok-reader"]
    assert runtime.usernames == {
        "owner@example.com": "claude-reader",
        "other@example.com": "other-reader",
    }


# @spec DACC-ID-004
@pytest.mark.parametrize(
    "user",
    [
        User("claude-reader", is_superuser=True),
        User("claude-reader", permissions=["view_document", "change_document"]),
        User("claude-reader", permissions=["view_document", "add_note"]),
        User("claude-reader", permissions=["delete_tag"]),
    ],
)
async def test_read_identity_holding_more_than_view_refuses_to_start(
    settings: Settings, fake: FakePaperless, user: User
) -> None:
    fake.users["tok-reader"] = user
    with pytest.raises(PermissionDrift):
        await prepare(settings, ENVIRON, transport=fake.transport())


# @spec DACC-ID-005
@pytest.mark.parametrize("failure", ["unreachable", "timeout", 500])
async def test_failed_permission_read_at_startup_refuses_to_start(
    settings: Settings, fake: FakePaperless, failure: str | int
) -> None:
    fake.fail = failure
    with pytest.raises(PaperlessError):
        await prepare(settings, ENVIRON, transport=fake.transport())


# @spec DACC-ID-005
async def test_token_paperless_rejects_refuses_to_start(
    settings: Settings, fake: FakePaperless
) -> None:
    with pytest.raises(PaperlessError):
        await prepare(settings, {"TOKEN_OWNER": "tok-revoked"}, transport=fake.transport())


# @spec DACC-ID-006
async def test_permission_check_repeats_hourly_while_serving(
    settings: Settings, fake: FakePaperless, access_keys: AccessKeys
) -> None:
    assert Settings.model_fields["permission_check_interval_seconds"].default == 3600

    settings.permission_check_interval_seconds = 0.02
    app = await build(settings, fake, access_keys)
    at_start = sum(r.path == "/api/ui_settings/" for r in fake.requests)
    async with http_client(app):
        await asyncio.sleep(0.2)
    assert sum(r.path == "/api/ui_settings/" for r in fake.requests) >= at_start + 3


# @spec DACC-ID-006
async def test_drift_found_by_a_repeated_check_is_acted_on() -> None:
    calls = 0
    drifted: list[bool] = []

    async def check() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionDrift("claude-reader is a superuser")

    monitor = PermissionMonitor(check, interval=0.01, on_drift=lambda: drifted.append(True))
    await asyncio.wait_for(monitor.run(), timeout=2)
    assert calls == 3
    assert drifted == [True]


# @spec DACC-ID-007
async def test_unreachable_paperless_during_a_repeated_check_keeps_serving() -> None:
    calls = 0
    drifted: list[bool] = []

    async def check() -> None:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise PaperlessUnreachable("Could not reach Paperless.")
        raise PermissionDrift("stop")

    monitor = PermissionMonitor(check, interval=0.01, on_drift=lambda: drifted.append(True))
    await asyncio.wait_for(monitor.run(), timeout=2)
    assert calls == 4
    assert drifted == [True]


# @spec DACC-ID-008
async def test_missing_private_tag_refuses_to_start(
    settings: Settings, fake: FakePaperless
) -> None:
    fake.tags[9] = "confidential"
    with pytest.raises(ConfigError, match="private"):
        await prepare(settings, ENVIRON, transport=fake.transport())


# @spec DACC-ID-008
async def test_missing_excluded_document_type_refuses_to_start(
    settings: Settings, fake: FakePaperless
) -> None:
    fake.document_types[3] = "School Records"
    with pytest.raises(ConfigError, match="School Record"):
        await prepare(settings, ENVIRON, transport=fake.transport())


# @spec DACC-ID-004, DACC-ID-005
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"user": {"username": "claude-reader"}},
        {"user": {"username": "claude-reader"}, "permissions": None},
        {"user": None, "permissions": ["view_document"]},
        {"user": {"username": "claude-reader"}, "permissions": "view_document"},
    ],
)
async def test_permission_read_without_the_expected_shape_refuses_to_start(
    settings: Settings, fake: FakePaperless, body: Any
) -> None:
    fake.ui_settings_body = body
    with pytest.raises(PaperlessError):
        await prepare(settings, ENVIRON, transport=fake.transport())


# @spec DACC-ID-006
async def test_unexpected_failure_of_a_repeated_check_does_not_end_the_checks() -> None:
    calls = 0
    drifted: list[bool] = []

    async def check() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TypeError("unexpected")
        raise PermissionDrift("stop")

    monitor = PermissionMonitor(check, interval=0.01, on_drift=lambda: drifted.append(True))
    await asyncio.wait_for(monitor.run(), timeout=2)
    assert calls == 3
    assert drifted == [True]
