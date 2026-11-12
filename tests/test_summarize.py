"""summarize_field: complete totals or an error."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from paperless_mcp.errors import (
    InvalidArgument,
    MixedCurrency,
    PaperlessStatusError,
    TooManyDocuments,
)

if TYPE_CHECKING:
    from paperless_mcp.service import DocumentService

    from .fake_paperless import FakePaperless


def groups(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {g["group"]: g for g in result["groups"]}


# @spec DACC-SUM-001
async def test_figures_cover_every_matching_document_with_a_value(
    service: DocumentService,
) -> None:
    result = await service.summarize_field("Patient Responsibility", document_type="EOB")
    assert result["currency"] == "USD"
    figures = result["figures"]
    assert figures["sum"] == "155.50"
    assert figures["count"] == 3
    assert figures["min"] == "5.00"
    assert figures["max"] == "120.50"
    assert figures["documents_empty"] == 1


# @spec DACC-SUM-001
async def test_integer_fields_are_summed(service: DocumentService, fake: FakePaperless) -> None:
    fake.add_document(20, "Two pages", fields={20: 2})
    fake.add_document(21, "Three pages", fields={20: 3})
    figures = (await service.summarize_field("Page Count"))["figures"]
    assert (figures["sum"], figures["count"], figures["min"], figures["max"]) == (5, 2, 2, 3)


# @spec DACC-SUM-002
@pytest.mark.parametrize("field", ["Provider", "Date of Service"])
async def test_non_numeric_field_is_an_error_and_queries_nothing(
    service: DocumentService, fake: FakePaperless, field: str
) -> None:
    with pytest.raises(InvalidArgument):
        await service.summarize_field(field)
    assert fake.document_requests == []


# @spec DACC-SUM-003
async def test_group_by_custom_field_with_an_empty_group(service: DocumentService) -> None:
    by_provider = groups(
        await service.summarize_field("Patient Responsibility", group_by="Provider")
    )
    assert set(by_provider) == {"Dr. Alpha", "Dr. Beta", "(empty)"}
    assert by_provider["Dr. Alpha"]["sum"] == "120.50"
    assert by_provider["Dr. Alpha"]["documents_empty"] == 1
    assert by_provider["Dr. Beta"]["sum"] == "30.00"
    assert by_provider["(empty)"]["sum"] == "5.00"


# @spec DACC-SUM-003
async def test_group_by_correspondent_and_document_type(service: DocumentService) -> None:
    by_correspondent = groups(
        await service.summarize_field("Patient Responsibility", group_by="correspondent")
    )
    assert by_correspondent["Acme Health"]["sum"] == "150.50"
    assert by_correspondent["(empty)"]["sum"] == "5.00"
    assert by_correspondent["Mortgage Servicer"]["count"] == 0

    by_type = groups(
        await service.summarize_field("Patient Responsibility", group_by="document_type")
    )
    assert set(by_type) == {"EOB", "Form 1098"}
    assert by_type["EOB"]["count"] == 3
    assert by_type["Form 1098"]["documents_empty"] == 1


# @spec DACC-SUM-004
async def test_more_than_two_thousand_matches_is_an_error(
    service: DocumentService, fake: FakePaperless
) -> None:
    for n in range(2000):
        fake.add_document(100 + n, f"Bulk {n}", document_type=4, fields={17: "USD1.00"})
    assert (await service.summarize_field("Patient Responsibility", document_type="Invoice"))[
        "figures"
    ]["count"] == 2000
    fake.add_document(5000, "One more", document_type=4, fields={17: "USD1.00"})
    with pytest.raises(TooManyDocuments):
        await service.summarize_field("Patient Responsibility", document_type="Invoice")


# @spec DACC-SUM-005
async def test_a_failed_page_fails_the_whole_summary(
    service: DocumentService, fake: FakePaperless
) -> None:
    for n in range(250):
        fake.add_document(100 + n, f"Bulk {n}", document_type=4, fields={17: "USD1.00"})
    fake.fail_documents_from_page = 2
    with pytest.raises(PaperlessStatusError):
        await service.summarize_field("Patient Responsibility", document_type="Invoice")


# @spec DACC-SUM-006
@pytest.mark.parametrize(("value", "named"), [("EUR10.00", "EUR"), ("10.00", "USD")])
async def test_mixed_currencies_are_an_error_naming_them(
    service: DocumentService, fake: FakePaperless, value: str, named: str
) -> None:
    fake.add_document(30, "Foreign", fields={17: value})
    with pytest.raises(MixedCurrency) as error:
        await service.summarize_field("Patient Responsibility")
    assert named in str(error.value)
    with pytest.raises(MixedCurrency):
        await service.summarize_field("Patient Responsibility", group_by="Provider")


# @spec DACC-SUM-007
async def test_each_set_of_figures_counts_needs_review_documents(
    service: DocumentService,
) -> None:
    assert (await service.summarize_field("Patient Responsibility"))["figures"][
        "needs_review"
    ] == 1
    by_provider = groups(
        await service.summarize_field("Patient Responsibility", group_by="Provider")
    )
    assert by_provider["Dr. Beta"]["needs_review"] == 1
    assert by_provider["Dr. Alpha"]["needs_review"] == 0


# @spec DACC-SUM-001
async def test_value_that_is_not_a_number_is_an_error_that_does_not_repeat_it(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.add_document(30, "Odd count", fields={20: "twelve-ish"})
    with pytest.raises(InvalidArgument) as error:
        await service.summarize_field("Page Count")
    assert "twelve-ish" not in str(error.value)


# @spec DACC-SUM-001, DACC-VAL-001
async def test_monetary_figures_keep_every_decimal_place_held(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.custom_fields[22] = ("Unit Price", "monetary")
    fake.add_document(31, "Fine print", fields={22: "USD0.125"})
    fake.add_document(32, "Fine print 2", fields={22: "USD0.125"})
    figures = (await service.summarize_field("Unit Price"))["figures"]
    assert figures["sum"] == "0.25"
    assert figures["min"] == "0.125"
    assert figures["max"] == "0.125"
    document = await service.get_document(31)
    assert document["fields"]["Unit Price"] == {"amount": "0.125", "currency": "USD"}


# @spec DACC-SUM-001
async def test_float_figures_are_summed_exactly(
    service: DocumentService, fake: FakePaperless
) -> None:
    fake.custom_fields[23] = ("Rate", "float")
    for doc_id, value in ((33, 0.1), (34, 0.2)):
        fake.add_document(doc_id, f"Rate {doc_id}", fields={23: value})
    assert (await service.summarize_field("Rate"))["figures"]["sum"] == 0.3


# @spec DACC-SUM-003
async def test_group_by_keywords_are_matched_without_regard_to_case(
    service: DocumentService,
) -> None:
    result = await service.summarize_field(
        "Patient Responsibility", document_type="EOB", group_by="Correspondent"
    )
    assert set(groups(result)) == {"Acme Health", "(empty)"}


# @spec DACC-SUM-008
async def test_group_values_agreeing_in_their_first_500_characters_share_a_group(
    service: DocumentService, fake: FakePaperless
) -> None:
    stem = "p" * 500
    fake.add_document(30, "Long A", fields={17: "USD1.00", 18: stem + "a"})
    fake.add_document(31, "Long B", fields={17: "USD2.00", 18: stem + "b"})
    by_provider = groups(
        await service.summarize_field("Patient Responsibility", group_by="Provider")
    )
    assert by_provider[stem]["count"] == 2
    assert by_provider[stem]["sum"] == "3.00"
