"""The tools' own behaviour: schema, names, one document, saved views."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from paperless_mcp.errors import UnknownName, UnsupportedViewRule

from .conftest import UI_URL

if TYPE_CHECKING:
    from paperless_mcp.service import DocumentService

    from .fake_paperless import FakePaperless


def ids(result: dict[str, Any]) -> list[int]:
    return [d["id"] for d in result["documents"]]


# @spec DACC-TOOL-002
async def test_list_schema_returns_names_and_field_types_without_excluded_types(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_saved_view(1, "EOBs", [(28, "1")])
    schema = await service.list_schema()
    assert sorted(schema["document_types"]) == ["EOB", "Form 1098", "Invoice"]
    assert sorted(schema["tags"]) == ["Medical", "Tax 2025", "needs-review", "private"]
    assert sorted(schema["correspondents"]) == ["Acme Health", "Mortgage Servicer"]
    assert schema["saved_views"] == ["EOBs"]
    assert {"name": "Patient Responsibility", "data_type": "monetary"} in schema["custom_fields"]
    assert len(schema["custom_fields"]) == len(fake.custom_fields)


# @spec DACC-TOOL-003
async def test_names_match_case_insensitively(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_saved_view(1, "EOBs", [(28, "1")])
    found = await service.search_documents(
        document_type="eob",
        correspondent="ACME HEALTH",
        tags=["medical"],
        field_filters=[{"field": "provider", "op": "contains", "value": "alpha"}],
    )
    assert set(ids(found)) == {1, 3}
    assert (await service.summarize_field("patient responsibility"))["figures"]["count"] == 3
    assert (await service.run_saved_view("eobs"))["total"] == 5


# @spec DACC-TOOL-004
@pytest.mark.parametrize(
    ("kind", "call"),
    [
        ("document type", lambda s: s.search_documents(document_type="Nope")),
        ("tag", lambda s: s.search_documents(tags=["Medical", "Nope"])),
        ("correspondent", lambda s: s.search_documents(correspondent="Nope")),
        (
            "custom field",
            lambda s: s.search_documents(field_filters=[{"field": "Nope", "op": "has_value"}]),
        ),
        ("custom field", lambda s: s.summarize_field("Nope")),
        ("custom field", lambda s: s.summarize_field("Patient Responsibility", group_by="Nope")),
        (
            "document type",
            lambda s: s.summarize_field("Patient Responsibility", document_type="Nope"),
        ),
        ("saved view", lambda s: s.run_saved_view("Nope")),
    ],
)
async def test_unknown_name_is_an_error_naming_it_and_queries_nothing(
    service: DocumentService, fake: FakePaperless, kind: str, call: Any
) -> None:
    with pytest.raises(UnknownName) as error:
        await call(service)
    assert error.value.kind == kind
    assert error.value.name == "Nope"
    assert "Nope" in str(error.value)
    assert kind in str(error.value)
    assert fake.document_requests == []


# @spec DACC-TOOL-004
async def test_excluded_document_type_is_not_a_known_name(
    service: DocumentService, fake: FakePaperless
) -> None:
    with pytest.raises(UnknownName):
        await service.search_documents(document_type="School Record")
    assert fake.document_requests == []


# @spec DACC-TOOL-005
async def test_get_document_returns_metadata_every_filled_field_and_extractor_notes(
    service: DocumentService,
) -> None:
    doc = await service.get_document(1)
    assert doc["id"] == 1
    assert doc["title"] == "Acme Health EOB January"
    assert doc["document_type"] == "EOB"
    assert doc["correspondent"] == "Acme Health"
    assert doc["tags"] == ["Medical"]
    assert doc["created"] == "2026-01-10"
    assert set(doc["fields"]) == {
        "Patient Responsibility",
        "Provider",
        "Date of Service",
        "Claim Number",
        "Extraction",
        "Extraction Retries",
    }
    assert doc["fields"]["Extraction"] == "ok v3 2026-01-11 haiku EOB"
    assert [n["note"] for n in doc["notes"]] == ["Filled Provider (haiku)"]
    assert "values_withheld" not in doc


# @spec DACC-TOOL-005
async def test_get_document_omits_fields_that_are_attached_but_empty(
    service: DocumentService,
) -> None:
    assert set((await service.get_document(3))["fields"]) == {"Provider"}


# @spec DACC-TOOL-006
async def test_notes_by_people_are_never_returned(service: DocumentService) -> None:
    doc = await service.get_document(1)
    assert "call them back" not in str(doc)
    assert len(doc["notes"]) == 1


# @spec DACC-TOOL-007
async def test_ui_link_is_built_from_the_configured_base_url(service: DocumentService) -> None:
    assert (await service.get_document(1))["url"] == f"{UI_URL}/documents/1/details"
    (found,) = (await service.search_documents(title="January"))["documents"]
    assert found["url"] == f"{UI_URL}/documents/1/details"


# @spec DACC-TOOL-009
async def test_saved_view_rules_are_translated(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_saved_view(
        1,
        "Narrow",
        [
            (6, "12"),  # has tag Medical
            (17, "10"),  # does not have tag needs-review
            (4, "1"),  # document type is EOB
            (3, "1"),  # correspondent is Acme Health
            (9, "2026-01-31"),  # created after
            (8, "2026-03-31"),  # created before
        ],
    )
    assert ids(await service.run_saved_view("Narrow")) == [3]

    fake.add_saved_view(2, "Either type", [(28, "1"), (28, "2")], sort_reverse=False)
    assert ids(await service.run_saved_view("Either type")) == [7, 1, 2, 3, 4, 9]

    fake.add_saved_view(3, "Either correspondent", [(26, "1"), (26, "2")])
    assert ids(await service.run_saved_view("Either correspondent")) == [3, 2, 1, 7]


# @spec DACC-TOOL-009
async def test_saved_view_created_rules_exclude_the_boundary_day(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_saved_view(1, "Window", [(9, "2026-01-10"), (8, "2026-03-10")])
    assert ids(await service.run_saved_view("Window")) == [2]


# @spec DACC-TOOL-010
@pytest.mark.parametrize(
    ("rule_type", "label"), [(0, "title contains"), (32, "owner is"), (99, "99")]
)
async def test_untranslated_view_rule_is_an_error_naming_it(
    service: DocumentService, fake: FakePaperless, rule_type: int, label: str
) -> None:
    fake.add_saved_view(1, "Odd", [(28, "1"), (rule_type, "x")])
    with pytest.raises(UnsupportedViewRule) as error:
        await service.run_saved_view("Odd")
    assert label in str(error.value)
    assert fake.document_requests == []


# @spec DACC-RULE-012
async def test_view_of_untyped_documents_that_reads_values_is_an_error(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_saved_view(1, "Loose", [(4, None), (20, "statement")])
    with pytest.raises(UnsupportedViewRule, match="document type"):
        await service.run_saved_view("Loose")
    assert not fake.document_requests
