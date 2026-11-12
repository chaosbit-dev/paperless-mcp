"""The assembled server, driven by an MCP client over streamable HTTP."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from paperless_mcp.server import INSTRUCTIONS, QUIET_LOGGERS
from paperless_mcp.service import DocumentService

from .conftest import HOST, build, http_client, mcp_connect, mcp_session, payload

if TYPE_CHECKING:
    import pytest

    from paperless_mcp.config import Settings

    from .conftest import AccessKeys
    from .fake_paperless import FakePaperless

AUDIT_KEYS = {"time", "caller", "paperless_user", "tool", "document_ids", "result_count", "ok"}


def audit_lines(captured: str) -> list[dict[str, Any]]:
    lines = []
    for line in captured.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "tool" in entry:
            lines.append(entry)
    return lines


# @spec DACC-TOOL-001
async def test_read_identity_sees_exactly_the_five_read_tools(
    settings: Settings, fake: FakePaperless, access_keys: AccessKeys
) -> None:
    app = await build(settings, fake, access_keys)
    async with mcp_session(app, access_keys.token()) as session:
        tools = (await session.list_tools()).tools
    assert sorted(t.name for t in tools) == [
        "get_document",
        "list_schema",
        "run_saved_view",
        "search_documents",
        "summarize_field",
    ]


# @spec DACC-TOOL-008
async def test_instructions_say_returned_strings_are_untrusted_data(
    settings: Settings, fake: FakePaperless, access_keys: AccessKeys
) -> None:
    assert "untrusted" in INSTRUCTIONS
    assert "not instructions" in INSTRUCTIONS
    app = await build(settings, fake, access_keys)
    async with mcp_connect(app, access_keys.token()) as (_, initialized):
        assert initialized.instructions == INSTRUCTIONS


async def test_every_tool_answers_through_mcp(
    settings: Settings, fake: FakePaperless, access_keys: AccessKeys
) -> None:
    fake.add_saved_view(1, "EOBs", [(28, "1")])
    app = await build(settings, fake, access_keys)
    async with mcp_session(app, access_keys.token()) as session:
        found = payload(
            await session.call_tool(
                "search_documents",
                {
                    "document_type": "EOB",
                    "tags": ["Medical"],
                    "created_from": "2026-01-01",
                    "field_filters": [
                        {"field": "Patient Responsibility", "op": "gt", "value": 10}
                    ],
                    "limit": 10,
                },
            )
        )
        assert [d["id"] for d in found["documents"]] == [2, 1]
        assert payload(await session.call_tool("get_document", {"id": 1}))["id"] == 1
        summed = payload(
            await session.call_tool(
                "summarize_field", {"field": "Patient Responsibility", "group_by": "Provider"}
            )
        )
        assert len(summed["groups"]) == 3
        assert payload(await session.call_tool("run_saved_view", {"view": "EOBs"}))["total"] == 5


# @spec DACC-ERR-001
async def test_failures_reach_the_client_as_tool_errors(
    settings: Settings, fake: FakePaperless, access_keys: AccessKeys
) -> None:
    app = await build(settings, fake, access_keys)
    async with mcp_session(app, access_keys.token()) as session:
        unknown = await session.call_tool("search_documents", {"document_type": "Nope"})
        assert unknown.is_error
        assert "Nope" in unknown.content[0].text

        fake.fail = 503
        down = await session.call_tool("search_documents", {})
        assert down.is_error
        assert "503" in down.content[0].text


# @spec DACC-AUD-001
async def test_each_tool_call_writes_one_audit_line(
    settings: Settings,
    fake: FakePaperless,
    access_keys: AccessKeys,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = await build(settings, fake, access_keys)
    async with mcp_session(app, access_keys.token(email="Owner@Example.com")) as session:
        capsys.readouterr()
        await session.call_tool("search_documents", {"title": "Acme Health EOB", "limit": 2})
        await session.call_tool("get_document", {"id": 1})
        await session.call_tool("list_schema", {})
        await session.call_tool("summarize_field", {"field": "Patient Responsibility"})
    search, get, schema, summary = audit_lines(capsys.readouterr().out)

    assert set(search) == AUDIT_KEYS
    assert search["caller"] == "owner@example.com"
    assert search["paperless_user"] == "claude-reader"
    assert search["tool"] == "search_documents"
    assert search["document_ids"] == [9, 3]
    assert search["result_count"] == 2
    assert search["ok"] is True
    assert search["time"].startswith("20")

    assert (get["tool"], get["document_ids"], get["result_count"]) == ("get_document", [1], 1)
    assert (schema["tool"], schema["document_ids"]) == ("list_schema", [])
    assert (summary["tool"], summary["document_ids"]) == ("summarize_field", [])
    assert summary["result_count"] == 3


# @spec DACC-AUD-002
async def test_logs_hold_no_values_titles_search_strings_or_tokens(
    settings: Settings,
    fake: FakePaperless,
    access_keys: AccessKeys,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    for name in QUIET_LOGGERS:
        caplog.set_level("WARNING", logger=name)
    app = await build(settings, fake, access_keys)
    token = access_keys.token()
    async with mcp_session(app, token) as session:
        await session.call_tool("search_documents", {"query": "benefits"})
        await session.call_tool("search_documents", {"title": "January"})
        await session.call_tool(
            "search_documents",
            {"field_filters": [{"field": "Provider", "op": "equals", "value": "Dr. Alpha"}]},
        )
        await session.call_tool("get_document", {"id": 1})
        await session.call_tool("summarize_field", {"field": "Patient Responsibility"})
        await session.call_tool("search_documents", {"correspondent": "Nobody Inc"})
    captured = capsys.readouterr()
    logged = captured.out + captured.err + caplog.text
    assert audit_lines(captured.out)
    for secret in (
        "benefits",
        "January",
        "Dr. Alpha",
        "120.50",
        "155.50",
        "Nobody Inc",
        "tok-reader",
        token,
    ):
        assert secret not in logged, secret


# @spec DACC-AUD-003
async def test_failed_call_is_audited_as_an_error_class_without_the_message(
    settings: Settings,
    fake: FakePaperless,
    access_keys: AccessKeys,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = await build(settings, fake, access_keys)
    async with mcp_session(app, access_keys.token()) as session:
        capsys.readouterr()
        result = await session.call_tool("search_documents", {"tags": ["Nope"]})
        assert result.is_error
    (line,) = audit_lines(capsys.readouterr().out)
    assert set(line) == AUDIT_KEYS | {"error"}
    assert line["ok"] is False
    assert line["error"] == "UnknownName"
    assert line["document_ids"] == []
    assert line["result_count"] == 0
    assert "Nope" not in json.dumps(line)


# @spec DACC-ACC-007
async def test_request_for_an_unlisted_host_is_refused(
    settings: Settings, fake: FakePaperless, access_keys: AccessKeys
) -> None:
    """DNS-rebinding protection is on only when `transport_security` is passed explicitly."""
    assert settings.allowed_hosts == [HOST]
    app = await build(settings, fake, access_keys)
    async with http_client(app, access_keys.token()) as client:
        response = await client.post(
            "/mcp",
            headers={"host": "evil.example", "accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert response.status_code == 421


# @spec DACC-AUD-002, DACC-AUD-003
async def test_unexpected_failure_reaches_neither_the_caller_nor_the_logs(
    settings: Settings,
    fake: FakePaperless,
    access_keys: AccessKeys,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(self: DocumentService) -> dict[str, Any]:
        raise RuntimeError("could not parse 'Dr. Alpha'")

    monkeypatch.setattr(DocumentService, "list_schema", broken)
    caplog.set_level("DEBUG")
    for name in QUIET_LOGGERS:
        caplog.set_level("WARNING", logger=name)
    app = await build(settings, fake, access_keys)
    async with mcp_session(app, access_keys.token()) as session:
        result = await session.call_tool("list_schema", {})
    assert result.is_error
    assert "Dr. Alpha" not in str(result.content)
    captured = capsys.readouterr()
    assert "Dr. Alpha" not in captured.out + captured.err + caplog.text
    (line,) = audit_lines(captured.out)
    assert line["ok"] is False
    assert line["error"] == "RuntimeError"
