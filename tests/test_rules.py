"""Withholding: what the server removes that Paperless would have allowed."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from paperless_mcp.errors import DocumentNotFound

from .fake_paperless import CONTENT_SENTINEL

if TYPE_CHECKING:
    from paperless_mcp.service import DocumentService

    from .fake_paperless import FakePaperless

METADATA_ONLY_KEYS = {
    "id",
    "title",
    "document_type",
    "correspondent",
    "tags",
    "created",
    "url",
    "values_withheld",
}


def ids(result: dict[str, Any]) -> list[int]:
    return [d["id"] for d in result["documents"]]


async def every_tool(service: DocumentService, fake: FakePaperless) -> list[dict[str, Any]]:
    fake.add_saved_view(1, "EOBs", [(28, "1")])
    return [
        await service.search_documents(),
        await service.search_documents(query="statement"),
        await service.get_document(1),
        await service.summarize_field("Patient Responsibility"),
        await service.run_saved_view("EOBs"),
    ]


# @spec DACC-RULE-001
async def test_every_document_request_names_its_fields_and_omits_content(
    service: DocumentService, fake: FakePaperless
) -> None:
    await every_tool(service, fake)
    assert fake.document_requests
    for request in fake.document_requests:
        assert "fields" in request.params, request.path
        assert "content" not in request.params["fields"].split(",")


# @spec DACC-RULE-002
async def test_no_tool_returns_content_or_search_hit_excerpts(
    service: DocumentService, fake: FakePaperless
) -> None:
    rendered = json.dumps(await every_tool(service, fake))
    assert CONTENT_SENTINEL not in rendered
    assert "__search_hit__" not in rendered
    assert "highlights" not in rendered


# @spec DACC-RULE-003
@pytest.mark.parametrize(
    ("title", "reason"), [("Private EOB", "private"), ("Loose scan", "untyped")]
)
async def test_metadata_only_document_carries_only_metadata_and_the_reason(
    service: DocumentService, title: str, reason: str
) -> None:
    (doc,) = (await service.search_documents(title=title))["documents"]
    assert set(doc) == METADATA_ONLY_KEYS
    assert doc["values_withheld"] == reason
    assert set(await service.get_document(doc["id"])) == METADATA_ONLY_KEYS


# @spec DACC-RULE-004
async def test_excluded_documents_never_leave_paperless(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_saved_view(1, "Typed", [(28, "1"), (28, "3")])
    assert not {6, 8} & set(ids(await service.search_documents(limit=100)))
    assert not {6, 8} & set(ids(await service.search_documents(query="statement")))
    assert not {6, 8} & set(ids(await service.search_documents(title="report card")))
    assert not {6, 8} & set(ids(await service.run_saved_view("Typed")))
    await service.summarize_field("Patient Responsibility", group_by="document_type")
    assert not {6, 8} & fake.returned_document_ids()


# @spec DACC-RULE-005
async def test_query_and_field_filters_leave_metadata_only_documents_in_paperless(
    service: DocumentService, fake: FakePaperless
) -> None:
    assert set(ids(await service.search_documents(query="statement"))) == {1, 2, 3}
    has_value = [{"field": "Patient Responsibility", "op": "has_value"}]
    assert set(ids(await service.search_documents(field_filters=has_value))) == {1, 2, 9}
    # Paperless cannot count without returning a row, so the count of metadata-only
    # documents (DACC-SRCH-007) may bring one back: by id alone, and never from a request
    # that searches text or compares a value.
    searching = [
        r
        for r in fake.document_requests
        if "query" in r.params or "custom_field_query" in r.params
    ]
    assert len(searching) == 2
    for request in fake.document_requests:
        if request in searching:
            assert not {4, 5} & set(request.returned_ids)
        else:
            assert request.params["fields"] == "id"


# @spec DACC-RULE-006
async def test_summarize_leaves_metadata_only_documents_in_paperless(
    service: DocumentService, fake: FakePaperless
) -> None:
    result = await service.summarize_field("Patient Responsibility")
    assert result["figures"]["sum"] == "155.50"
    assert not {4, 5} & fake.returned_document_ids()


# @spec DACC-RULE-007
async def test_excluded_document_is_indistinguishable_from_a_missing_one(
    service: DocumentService,
) -> None:
    with pytest.raises(DocumentNotFound) as missing:
        await service.get_document(999)
    for excluded in (6, 8):
        with pytest.raises(DocumentNotFound) as refused:
            await service.get_document(excluded)
        assert str(refused.value).replace(str(excluded), "N") == str(missing.value).replace(
            "999", "N"
        )


# @spec DACC-RULE-008
async def test_saved_view_applies_the_server_rules_and_says_its_count_may_be_lower(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_saved_view(1, "Typed", [(28, "1"), (28, "3")])
    result = await service.run_saved_view("Typed", limit=100)
    assert set(ids(result)) == {1, 2, 3, 4, 9}
    assert result["total"] == 5
    private = next(d for d in result["documents"] if d["id"] == 4)
    assert set(private) == METADATA_ONLY_KEYS
    assert "lower than the Paperless UI shows" in result["note"]


# @spec DACC-RULE-009
@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ((20, "statement"), {1, 2, 3}),
        ((42, json.dumps([17, "isnull", False])), {1, 2, 9}),
    ],
)
async def test_saved_view_with_text_or_field_rule_leaves_metadata_only_documents_out(
    service: DocumentService, fake: FakePaperless, rule: tuple[int, str], expected: set[int]
) -> None:
    fake.add_saved_view(1, "Rule", [rule])
    assert set(ids(await service.run_saved_view("Rule", limit=100))) == expected
    assert not {4, 5} & fake.returned_document_ids()


# @spec DACC-RULE-011
async def test_every_document_type_matching_an_excluded_name_is_excluded(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.document_types[5] = "school record"
    fake.add_document(20, "Lower-case report card", document_type=5, fields={18: "School"})
    result = await service.search_documents(limit=100)
    assert {6, 8, 20}.isdisjoint(ids(result))
    assert {6, 8, 20}.isdisjoint(fake.returned_document_ids())
    with pytest.raises(DocumentNotFound):
        await service.get_document(20)


# @spec DACC-RULE-011
async def test_every_tag_matching_the_private_name_withholds_values(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.tags[13] = "Private"
    fake.add_document(21, "Other private EOB", tags=[13], fields={18: "Dr. Alpha"})
    listed = await service.search_documents(limit=100)
    assert set(next(d for d in listed["documents"] if d["id"] == 21)) == METADATA_ONLY_KEYS
    found = await service.search_documents(
        field_filters=[{"field": "Provider", "op": "equals", "value": "Dr. Alpha"}]
    )
    assert 21 not in ids(found)
    assert found["metadata_only_not_searched"] == 3


# @spec DACC-RULE-010
async def test_documents_paperless_returns_against_the_filter_are_dropped(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.ignored_params = {"document_type__id__none", "tags__id__none", "document_type__isnull"}
    listed = await service.search_documents(limit=100)
    assert {6, 8}.isdisjoint(ids(listed))
    found = await service.search_documents(query="statement", limit=100)
    assert {4, 5, 6, 8}.isdisjoint(ids(found))


# @spec DACC-RULE-010
async def test_documents_paperless_returns_against_the_filter_are_not_summed(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.ignored_params = {"document_type__id__none", "tags__id__none", "document_type__isnull"}
    result = await service.summarize_field("Patient Responsibility")
    assert result["figures"]["sum"] == "155.50"
    assert result["figures"]["count"] == 3
