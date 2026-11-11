"""search_documents, and the paging it shares with run_saved_view."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from paperless_mcp.errors import InvalidArgument

if TYPE_CHECKING:
    from paperless_mcp.service import DocumentService

    from .fake_paperless import FakePaperless

# Everything the reader may see: 6 and 8 are excluded.
VISIBLE = {1, 2, 3, 4, 5, 7, 9}


def ids(result: dict[str, Any]) -> list[int]:
    return [d["id"] for d in result["documents"]]


# @spec DACC-SRCH-001
async def test_results_are_paged_and_carry_the_total(
    service: DocumentService, fake: FakePaperless
) -> None:
    first = await service.search_documents(limit=3, page=1)
    third = await service.search_documents(limit=3, page=3)
    assert len(first["documents"]) == 3
    assert len(third["documents"]) == 1
    assert first["total"] == third["total"] == len(VISIBLE)

    fake.add_saved_view(1, "EOBs", [(28, "1")])
    view = await service.run_saved_view("EOBs", limit=2, page=2)
    assert len(view["documents"]) == 2
    assert view["total"] == 5


# @spec DACC-SRCH-002
@pytest.mark.parametrize("bad", [{"limit": 0}, {"limit": 101}, {"page": 0}, {"limit": -1}])
async def test_out_of_range_paging_is_an_error_and_queries_nothing(
    service: DocumentService, fake: FakePaperless, bad: dict[str, int]
) -> None:
    fake.add_saved_view(1, "EOBs", [(28, "1")])
    with pytest.raises(InvalidArgument):
        await service.search_documents(**bad)
    with pytest.raises(InvalidArgument):
        await service.run_saved_view("EOBs", **bad)
    assert fake.document_requests == []


# @spec DACC-SRCH-003
async def test_result_shape_holds_filled_fields_without_extractor_bookkeeping(
    service: DocumentService, fake: FakePaperless
) -> None:
    (doc,) = (await service.search_documents(title="January"))["documents"]
    assert set(doc) == {
        "id",
        "title",
        "document_type",
        "correspondent",
        "tags",
        "created",
        "url",
        "fields",
    }
    assert set(doc["fields"]) == {
        "Patient Responsibility",
        "Provider",
        "Date of Service",
        "Claim Number",
    }
    (march,) = (await service.search_documents(title="March"))["documents"]
    assert set(march["fields"]) == {"Provider"}

    fake.add_saved_view(1, "EOBs", [(28, "1")])
    viewed = next(d for d in (await service.run_saved_view("EOBs"))["documents"] if d["id"] == 1)
    assert viewed == doc


# @spec DACC-SRCH-004
async def test_title_is_a_case_insensitive_substring_match(service: DocumentService) -> None:
    assert set(ids(await service.search_documents(title="acme health eob"))) == {1, 2, 3, 9}


# @spec DACC-SRCH-005
async def test_query_is_paperless_full_text_search(
    service: DocumentService, fake: FakePaperless
) -> None:
    assert set(ids(await service.search_documents(query="benefits"))) == {1}
    assert any(r.params.get("query") == "benefits" for r in fake.document_requests)


# @spec DACC-SRCH-006
async def test_has_value_does_not_match_a_field_attached_but_empty(
    service: DocumentService, fake: FakePaperless
) -> None:
    has_value = [{"field": "Patient Responsibility", "op": "has_value"}]
    assert 3 not in ids(await service.search_documents(field_filters=has_value))
    assert all("exists" not in r.params.get("custom_field_query", "") for r in fake.requests)


async def test_field_filters_compare_and_combine(service: DocumentService) -> None:
    over_ten = [{"field": "Patient Responsibility", "op": "gt", "value": 10}]
    assert set(ids(await service.search_documents(field_filters=over_ten))) == {1, 2}
    both = [*over_ten, {"field": "Provider", "op": "equals", "value": "Dr. Beta"}]
    assert ids(await service.search_documents(field_filters=both)) == [2]
    empty = [{"field": "Patient Responsibility", "op": "is_empty"}]
    assert set(ids(await service.search_documents(field_filters=empty, document_type="EOB"))) == {
        3
    }
    with pytest.raises(InvalidArgument):
        await service.search_documents(field_filters=[{"field": "Provider", "op": "regex"}])


# @spec DACC-SRCH-007
async def test_search_reports_metadata_only_documents_it_did_not_search(
    service: DocumentService,
) -> None:
    assert (await service.search_documents(query="nothing"))["metadata_only_not_searched"] == 2
    typed = await service.search_documents(query="nothing", document_type="EOB")
    assert typed["metadata_only_not_searched"] == 1
    filtered = await service.search_documents(
        field_filters=[{"field": "Provider", "op": "has_value"}], tags=["Medical"]
    )
    assert filtered["metadata_only_not_searched"] == 1
    assert "metadata_only_not_searched" not in await service.search_documents(title="EOB")


# @spec DACC-SRCH-008
async def test_metadata_filters_reach_metadata_only_documents(service: DocumentService) -> None:
    assert set(ids(await service.search_documents(limit=100))) == VISIBLE
    assert set(ids(await service.search_documents(tags=["private"]))) == {4}
    assert set(ids(await service.search_documents(created_from="2026-04-01"))) == {5, 9}


# @spec DACC-SRCH-009
async def test_page_past_the_end_is_empty_with_the_true_total(
    service: DocumentService, fake: FakePaperless
) -> None:
    result = await service.search_documents(limit=5, page=4)
    assert result["documents"] == []
    assert result["total"] == len(VISIBLE)

    fake.add_saved_view(1, "EOBs", [(28, "1")])
    view = await service.run_saved_view("EOBs", limit=5, page=9)
    assert view["documents"] == []
    assert view["total"] == 5


# @spec DACC-SRCH-010
async def test_several_tags_must_all_be_present(service: DocumentService) -> None:
    assert set(ids(await service.search_documents(tags=["Medical"]))) == {1, 2, 3, 4, 9}
    assert ids(await service.search_documents(tags=["Medical", "needs-review"])) == [2]


# @spec DACC-SRCH-011
async def test_created_bounds_are_inclusive(service: DocumentService) -> None:
    found = await service.search_documents(created_from="2026-02-10", created_to="2026-03-10")
    assert set(ids(found)) == {2, 3}
    summed = await service.summarize_field(
        "Patient Responsibility", created_from="2026-01-10", created_to="2026-02-10"
    )
    assert summed["figures"]["sum"] == "150.50"


# @spec DACC-SRCH-011
@pytest.mark.parametrize("bad", ["03/10/2026", "2026-13-01", "yesterday"])
async def test_created_bounds_must_be_iso_dates(
    service: DocumentService, fake: FakePaperless, bad: str
) -> None:
    with pytest.raises(InvalidArgument):
        await service.search_documents(created_from=bad)
    assert fake.document_requests == []


# @spec DACC-SRCH-012
async def test_order_is_relevance_with_query_and_newest_first_without(
    service: DocumentService,
) -> None:
    assert ids(await service.search_documents(query="statement"))[0] == 2
    assert ids(await service.search_documents(limit=100)) == [9, 5, 4, 3, 2, 1, 7]
