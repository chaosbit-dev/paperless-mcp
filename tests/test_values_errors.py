"""Value rendering and Paperless failures."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest

from paperless_mcp.errors import PaperlessStatusError, PaperlessTimeout, PaperlessUnreachable

if TYPE_CHECKING:
    from paperless_mcp.service import DocumentService

    from .fake_paperless import FakePaperless


# @spec DACC-VAL-001
async def test_monetary_value_is_a_decimal_string_and_a_currency_code(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_document(30, "No code", fields={17: "45.00"})
    one = await service.get_document(1)
    assert one["fields"]["Patient Responsibility"] == {"amount": "120.50", "currency": "USD"}
    assert (await service.get_document(30))["fields"]["Patient Responsibility"] == {
        "amount": "45.00",
        "currency": None,
    }


# @spec DACC-VAL-002
async def test_dates_are_iso_8601(service: DocumentService) -> None:
    doc = await service.get_document(1)
    assert doc["created"] == "2026-01-10"
    assert doc["fields"]["Date of Service"] == "2026-01-05"
    datetime.fromisoformat(doc["notes"][0]["created"])


# @spec DACC-VAL-003
async def test_long_strings_are_cut_to_500_characters_and_marked(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_document(30, "T" * 600, fields={21: "C" * 600, 18: "short"})
    doc = await service.get_document(30)
    assert doc["title"] == "T" * 500
    assert doc["fields"]["Claim Number"] == "C" * 500
    assert set(doc["truncated"]) == {"title", "Claim Number"}
    assert "truncated" not in await service.get_document(1)
    (found,) = (await service.search_documents(title="TTTT"))["documents"]
    assert set(found["truncated"]) == {"title", "Claim Number"}


CALLS: list[Any] = [
    lambda s: s.list_schema(),
    lambda s: s.search_documents(),
    lambda s: s.get_document(1),
    lambda s: s.summarize_field("Patient Responsibility"),
    lambda s: s.run_saved_view("EOBs"),
]


# @spec DACC-ERR-001
@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize(
    ("failure", "error", "says"),
    [
        ("unreachable", PaperlessUnreachable, "reach"),
        ("timeout", PaperlessTimeout, "time"),
        (503, PaperlessStatusError, "503"),
    ],
)
async def test_paperless_failure_is_a_tool_error_saying_which(
    service: DocumentService,
    fake: FakePaperless,
    call: Any,
    failure: str | int,
    error: type[Exception],
    says: str,
) -> None:
    fake.add_saved_view(1, "EOBs", [(28, "1")])
    fake.fail = failure
    with pytest.raises(error) as raised:
        await call(service)
    assert says in str(raised.value).lower()
