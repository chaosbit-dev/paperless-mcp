"""The five read tools, as plain functions over one identity's Paperless client."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from paperless_mcp.errors import (
    ConfigError,
    DocumentNotFound,
    InvalidArgument,
    MixedCurrency,
    PaperlessStatusError,
    TooManyDocuments,
    UnknownName,
    UnsupportedViewRule,
)
from paperless_mcp.permissions import PRIVATE_TAG

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from paperless_mcp.paperless import PaperlessClient

NEEDS_REVIEW_TAG = "needs-review"
EXTRACTOR_USERNAME = "jocasta-extractor"
# The extractor's own bookkeeping: shown by get_document, left out of lists.
BOOKKEEPING_FIELDS = frozenset({"extraction", "extraction retries"})

MAX_LIMIT = 100
MAX_STRING = 500
MAX_SUMMARIZED = 2000
SUMMARY_PAGE_SIZE = 100
EMPTY_GROUP = "(empty)"

# Never `content`: document text stays in Paperless.
LIST_FIELDS = "id,title,document_type,correspondent,tags,created,custom_fields"
DETAIL_FIELDS = f"{LIST_FIELDS},notes"

NUMERIC_TYPES = frozenset({"monetary", "integer", "float"})
FILTER_OPS = {
    "has_value": "isnull",
    "is_empty": "isnull",
    "equals": "exact",
    "contains": "icontains",
    "gt": "gt",
    "gte": "gte",
    "lt": "lt",
    "lte": "lte",
}
VALUELESS_OPS = frozenset({"has_value", "is_empty"})

VIEW_NOTE = (
    "Results come from this server's translation of the saved view's filter rules, with its "
    "own access rules applied; the count can be lower than the Paperless UI shows for the view."
)
VIEW_ORDERINGS = frozenset(
    {
        "created",
        "added",
        "modified",
        "title",
        "correspondent__name",
        "document_type__name",
        "archive_serial_number",
        "num_notes",
        "owner",
        "page_count",
    }
)
# Paperless's saved-view rule types, for naming one the server does not translate.
RULE_LABELS = {
    0: "title contains",
    1: "content contains",
    2: "ASN is",
    3: "correspondent is",
    4: "document type is",
    5: "is in inbox",
    6: "has tag",
    7: "has any tag",
    8: "created before",
    9: "created after",
    10: "created year is",
    11: "created month is",
    12: "created day is",
    13: "added before",
    14: "added after",
    15: "modified before",
    16: "modified after",
    17: "does not have tag",
    18: "does not have ASN",
    19: "title or content contains",
    20: "fulltext query",
    21: "more like this",
    22: "has tags in",
    23: "ASN greater than",
    24: "ASN less than",
    25: "storage path is",
    26: "has correspondent in",
    27: "does not have correspondent in",
    28: "has document type in",
    29: "does not have document type in",
    30: "has storage path in",
    31: "does not have storage path in",
    32: "owner is",
    33: "has owner in",
    34: "does not have owner",
    35: "does not have owner in",
    36: "has custom field value",
    37: "is shared by me",
    38: "has custom fields",
    39: "has custom field in",
    40: "does not have custom field in",
    41: "does not have custom field",
    42: "custom fields query",
    43: "created to",
    44: "created from",
    45: "added to",
    46: "added from",
    47: "mime type is",
}

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONEY = re.compile(r"([A-Za-z]{3})?(-?\d+(?:\.\d+)?)")

Params = dict[str, str | int]


@dataclass(frozen=True)
class CustomField:
    id: int
    name: str
    data_type: str
    options: Mapping[str, str]


# @spec DACC-TOOL-003, DACC-TOOL-004
@dataclass(frozen=True)
class Taxonomy:
    tags: Mapping[int, str]
    document_types: Mapping[int, str]
    correspondents: Mapping[int, str]
    fields: Mapping[int, CustomField]
    # Every tag and type whose name matches, whatever its case: Paperless allows two.
    private_tags: frozenset[int]
    needs_review_tags: frozenset[int]
    excluded_types: frozenset[int]

    def tag(self, name: str) -> int:
        return _resolve(self.tags, "tag", name)

    def correspondent(self, name: str) -> int:
        return _resolve(self.correspondents, "correspondent", name)

    def document_type(self, name: str) -> int:
        visible = {i: n for i, n in self.document_types.items() if i not in self.excluded_types}
        return _resolve(visible, "document type", name)

    def field(self, name: str) -> CustomField:
        by_id = {f.id: f.name for f in self.fields.values()}
        return self.fields[_resolve(by_id, "custom field", name)]


def _resolve(names: Mapping[int, str], kind: str, name: str) -> int:
    wanted = name.strip().casefold()
    for ident, candidate in names.items():
        if candidate.casefold() == wanted:
            return ident
    raise UnknownName(kind, name)


def _ids(values: set[int] | frozenset[int]) -> str:
    return ",".join(str(v) for v in sorted(values))


# @spec DACC-SRCH-002
def _check_paging(limit: int, page: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidArgument(f"limit must be between 1 and {MAX_LIMIT}.")
    if page < 1:
        raise InvalidArgument("page must be 1 or greater.")


def _check_date(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        if not _DATE.fullmatch(value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise InvalidArgument(f"{name} must be a date written YYYY-MM-DD.") from None
    return value


def _money(value: Any) -> tuple[Decimal, str | None] | None:
    """A Paperless monetary value as an amount and its currency code, if it has one."""
    match = _MONEY.fullmatch(str(value).strip())
    if match is None:
        return None
    try:
        return Decimal(match[2]), (match[1].upper() if match[1] else None)
    except InvalidOperation:
        return None


def _amount(value: Decimal) -> str:
    """An amount to at least two decimal places, keeping any further place it holds."""
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        return f"{value.normalize():f}"
    return f"{value:.2f}"


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


class DocumentService:
    def __init__(
        self,
        client: PaperlessClient,
        *,
        ui_base_url: str,
        excluded_document_types: Sequence[str],
    ) -> None:
        self._client = client
        self._ui = ui_base_url.rstrip("/")
        self._excluded = list(excluded_document_types)

    # -- shared -------------------------------------------------------------------------

    async def _taxonomy(self) -> Taxonomy:
        tags, types, correspondents, fields = await asyncio.gather(
            self._client.list_all("/api/tags/"),
            self._client.list_all("/api/document_types/"),
            self._client.list_all("/api/correspondents/"),
            self._client.list_all("/api/custom_fields/"),
        )
        tag_names = {int(t["id"]): str(t["name"]) for t in tags}
        type_names = {int(t["id"]): str(t["name"]) for t in types}

        # @spec DACC-RULE-011
        def named(names: Mapping[int, str], *wanted: str) -> frozenset[int]:
            keys = {w.casefold() for w in wanted}
            return frozenset(i for i, n in names.items() if n.casefold() in keys)

        # Without these the rules cannot be applied, so nothing is served.
        private_tags = named(tag_names, PRIVATE_TAG)
        if not private_tags:
            raise ConfigError(f"Paperless has no tag named {PRIVATE_TAG!r}.")
        missing = [n for n in self._excluded if not named(type_names, n)]
        if missing:
            raise ConfigError(f"Paperless has no document type named {missing[0]!r}.")
        return Taxonomy(
            tags=tag_names,
            document_types=type_names,
            correspondents={int(c["id"]): str(c["name"]) for c in correspondents},
            fields={
                int(f["id"]): CustomField(
                    id=int(f["id"]),
                    name=str(f["name"]),
                    data_type=str(f["data_type"]),
                    options={
                        str(o["id"]): str(o["label"])
                        for o in (f.get("extra_data") or {}).get("select_options") or []
                        if isinstance(o, dict) and "id" in o and "label" in o
                    },
                )
                for f in fields
            },
            private_tags=private_tags,
            needs_review_tags=named(tag_names, NEEDS_REVIEW_TAG),
            excluded_types=named(type_names, *self._excluded),
        )

    # @spec DACC-RULE-001, DACC-RULE-002, DACC-RULE-004
    def _base(self, taxonomy: Taxonomy) -> Params:
        """Parameters every document list request carries: no excluded type, no content."""
        params: Params = {"fields": LIST_FIELDS}
        if taxonomy.excluded_types:
            params["document_type__id__none"] = _ids(taxonomy.excluded_types)
        return params

    # @spec DACC-RULE-005, DACC-RULE-006, DACC-RULE-009
    @staticmethod
    def _keep_values_only(
        params: Params, taxonomy: Taxonomy, also_without: frozenset[int] = frozenset()
    ) -> None:
        """Constrain the query so Paperless never matches a metadata-only document."""
        params["tags__id__none"] = _ids(taxonomy.private_tags | also_without)
        params["document_type__isnull"] = "false"

    async def _count(self, params: Params) -> int:
        body = await self._client.get(
            "/api/documents/", {**params, "fields": "id", "page": 1, "page_size": 1}
        )
        return int(body["count"])

    # @spec DACC-RULE-010
    def _admitted(
        self, doc: Mapping[str, Any], taxonomy: Taxonomy, *, by_value: bool = False
    ) -> bool:
        """Whether a document Paperless returned may be used at all.

        The filters sent to Paperless already say so. Paperless ignores a parameter it does
        not recognise, so what it returns is checked again here.
        """
        if doc.get("document_type") in taxonomy.excluded_types:
            return False
        return not by_value or self._withheld(doc, taxonomy) is None

    # @spec DACC-SRCH-001, DACC-SRCH-009
    async def _page(
        self, params: Params, limit: int, page: int, taxonomy: Taxonomy
    ) -> tuple[int, list[Any]]:
        by_value = "query" in params or "custom_field_query" in params
        try:
            body = await self._client.get(
                "/api/documents/", {**params, "page": page, "page_size": limit}
            )
        except PaperlessStatusError as error:
            # Paperless answers 404 for a page past the end.
            if error.status != 404 or page == 1:
                raise
            return await self._count(params), []
        results = [d for d in body["results"] if self._admitted(d, taxonomy, by_value=by_value)]
        return int(body["count"]), results

    @staticmethod
    def _withheld(doc: Mapping[str, Any], taxonomy: Taxonomy) -> str | None:
        if taxonomy.private_tags.intersection(doc.get("tags") or []):
            return "private"
        if doc.get("document_type") is None:
            return "untyped"
        return None

    # @spec DACC-VAL-001, DACC-VAL-002
    @staticmethod
    def _value(field: CustomField, value: Any) -> Any:
        if field.data_type == "monetary":
            money = _money(value)
            if money is not None:
                return {"amount": _amount(money[0]), "currency": money[1]}
        if field.data_type == "select":
            return field.options.get(str(value), value)
        return value

    # @spec DACC-RULE-003, DACC-SRCH-003, DACC-TOOL-007, DACC-VAL-003
    def _render(
        self, doc: Mapping[str, Any], taxonomy: Taxonomy, *, detail: bool = False
    ) -> dict[str, Any]:
        truncated: list[str] = []

        def cut(name: str, value: Any) -> Any:
            if isinstance(value, str) and len(value) > MAX_STRING:
                truncated.append(name)
                return value[:MAX_STRING]
            return value

        doc_type = doc.get("document_type")
        correspondent = doc.get("correspondent")
        out: dict[str, Any] = {
            "id": doc["id"],
            "title": cut("title", doc.get("title")),
            "document_type": taxonomy.document_types.get(doc_type) if doc_type else None,
            "correspondent": taxonomy.correspondents.get(correspondent) if correspondent else None,
            "tags": [taxonomy.tags[t] for t in doc.get("tags", []) if t in taxonomy.tags],
            "created": str(doc["created"])[:10] if doc.get("created") else None,
            "url": f"{self._ui}/documents/{doc['id']}/details",
        }
        withheld = self._withheld(doc, taxonomy)
        if withheld is not None:
            out["values_withheld"] = withheld
        else:
            fields: dict[str, Any] = {}
            for entry in doc.get("custom_fields") or []:
                field = taxonomy.fields.get(entry["field"])
                if field is None or _is_empty(entry.get("value")):
                    continue
                if not detail and field.name.casefold() in BOOKKEEPING_FIELDS:
                    continue
                fields[field.name] = cut(field.name, self._value(field, entry["value"]))
            out["fields"] = fields
            if detail:
                out["notes"] = self._notes(doc, cut)
        if truncated:
            out["truncated"] = truncated
        return out

    # @spec DACC-TOOL-006
    @staticmethod
    def _notes(doc: Mapping[str, Any], cut: Any) -> list[dict[str, Any]]:
        notes = []
        for note in doc.get("notes") or []:
            user = note.get("user")
            if isinstance(user, dict) and user.get("username") == EXTRACTOR_USERNAME:
                notes.append({"created": note.get("created"), "note": cut("notes", note["note"])})
        return notes

    # -- list_schema --------------------------------------------------------------------

    # @spec DACC-TOOL-002
    async def list_schema(self) -> dict[str, Any]:
        taxonomy = await self._taxonomy()
        views = await self._client.list_all("/api/saved_views/")
        return {
            "document_types": sorted(
                n for i, n in taxonomy.document_types.items() if i not in taxonomy.excluded_types
            ),
            "tags": sorted(taxonomy.tags.values()),
            "correspondents": sorted(taxonomy.correspondents.values()),
            "custom_fields": [
                {"name": f.name, "data_type": f.data_type}
                for f in sorted(taxonomy.fields.values(), key=lambda f: f.name)
            ],
            "saved_views": sorted(str(v["name"]) for v in views),
        }

    # -- search_documents ---------------------------------------------------------------

    # @spec DACC-SRCH-006
    @staticmethod
    def _field_query(filters: Sequence[Mapping[str, Any]], taxonomy: Taxonomy) -> str:
        clauses: list[list[Any]] = []
        for item in filters:
            if not isinstance(item, dict) or set(item) - {"field", "op", "value"}:
                raise InvalidArgument("Each field filter is an object with field, op and value.")
            op = item.get("op")
            if op not in FILTER_OPS:
                raise InvalidArgument(
                    f"Unknown field filter op {op!r}; use one of {', '.join(FILTER_OPS)}."
                )
            name = item.get("field")
            if not isinstance(name, str):
                raise InvalidArgument("Each field filter names a custom field.")
            field = taxonomy.field(name)
            if op in VALUELESS_OPS:
                # `exists` would also match a field attached with no value.
                clauses.append([field.id, "isnull", op == "is_empty"])
                continue
            value = item.get("value")
            if _is_empty(value) or isinstance(value, (dict, list)):
                raise InvalidArgument(f"Field filter op {op!r} needs a value.")
            if field.data_type == "select":
                labels = {label.casefold(): ident for ident, label in field.options.items()}
                value = labels.get(str(value).casefold(), value)
            clauses.append([field.id, FILTER_OPS[op], value])
        return json.dumps(clauses[0] if len(clauses) == 1 else ["AND", clauses])

    # @spec DACC-SRCH-007
    async def _metadata_only_matches(
        self, taxonomy: Taxonomy, metadata: Params, *, typed_filter: bool
    ) -> int:
        """How many metadata-only documents the call's metadata filters match.

        Counted by id alone, from requests that carry neither the text query nor a field
        filter, so no value of such a document is ever compared or read.
        """
        private: Params = {**metadata, "tags__id__in": _ids(taxonomy.private_tags)}
        counts = [self._count(private)]
        if not typed_filter:
            untyped: Params = {
                **metadata,
                "document_type__isnull": "true",
                "tags__id__none": _ids(taxonomy.private_tags),
            }
            counts.append(self._count(untyped))
        return sum(await asyncio.gather(*counts))

    # @spec DACC-SRCH-004, DACC-SRCH-005, DACC-SRCH-008, DACC-SRCH-010, DACC-SRCH-011
    # @spec DACC-SRCH-012, DACC-RULE-005
    async def search_documents(
        self,
        *,
        query: str | None = None,
        title: str | None = None,
        document_type: str | None = None,
        correspondent: str | None = None,
        tags: Sequence[str] | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        field_filters: Sequence[dict[str, Any]] | None = None,
        limit: int = 25,
        page: int = 1,
    ) -> dict[str, Any]:
        _check_paging(limit, page)
        created_from = _check_date("created_from", created_from)
        created_to = _check_date("created_to", created_to)
        query = (query or "").strip() or None
        title = (title or "").strip() or None

        taxonomy = await self._taxonomy()
        metadata = self._base(taxonomy)
        tag_ids = {taxonomy.tag(name) for name in tags or []}
        if tag_ids:
            metadata["tags__id__all"] = _ids(tag_ids)
        if document_type is not None:
            metadata["document_type__id__in"] = taxonomy.document_type(document_type)
        if correspondent is not None:
            metadata["correspondent__id__in"] = taxonomy.correspondent(correspondent)
        if title:
            metadata["title__icontains"] = title
        if created_from:
            metadata["created__date__gte"] = created_from
        if created_to:
            metadata["created__date__lte"] = created_to
        field_query = self._field_query(field_filters, taxonomy) if field_filters else None

        params: Params = dict(metadata)
        reads_values = query is not None or field_query is not None
        if reads_values:
            self._keep_values_only(params, taxonomy)
        if field_query is not None:
            params["custom_field_query"] = field_query
        if query is not None:
            params["query"] = query
        else:
            params["ordering"] = "-created"

        total, results = await self._page(params, limit, page, taxonomy)
        out: dict[str, Any] = {
            "total": total,
            "documents": [self._render(doc, taxonomy) for doc in results],
        }
        if reads_values:
            out["metadata_only_not_searched"] = await self._metadata_only_matches(
                taxonomy, metadata, typed_filter=document_type is not None
            )
        return out

    # -- get_document -------------------------------------------------------------------

    # @spec DACC-TOOL-005, DACC-RULE-007
    async def get_document(self, document_id: int) -> dict[str, Any]:
        taxonomy = await self._taxonomy()
        # One message for a missing document and an excluded one: neither exists here.
        missing = DocumentNotFound(f"No document with id {document_id}.")
        try:
            doc = await self._client.get(
                f"/api/documents/{int(document_id)}/", {"fields": DETAIL_FIELDS}
            )
        except PaperlessStatusError as error:
            if error.status == 404:
                raise missing from None
            raise
        if doc.get("document_type") in taxonomy.excluded_types:
            raise missing
        return self._render(doc, taxonomy, detail=True)

    # -- summarize_field ----------------------------------------------------------------

    # @spec DACC-SUM-001, DACC-SUM-002, DACC-SUM-003, DACC-SUM-004, DACC-SUM-005
    # @spec DACC-SUM-006, DACC-SUM-007, DACC-RULE-006, DACC-SRCH-011
    async def summarize_field(
        self,
        field: str,
        *,
        document_type: str | None = None,
        group_by: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> dict[str, Any]:
        created_from = _check_date("created_from", created_from)
        created_to = _check_date("created_to", created_to)
        taxonomy = await self._taxonomy()
        target = taxonomy.field(field)
        if target.data_type not in NUMERIC_TYPES:
            raise InvalidArgument(
                f"{target.name} is a {target.data_type} field; only monetary, integer and "
                "float fields can be summarized."
            )
        group_field: CustomField | None = None
        if group_by is not None:
            keyword = group_by.strip().casefold()
            if keyword in ("correspondent", "document_type"):
                group_by = keyword
            else:
                group_field = taxonomy.field(group_by)

        params = self._base(taxonomy)
        self._keep_values_only(params, taxonomy)
        params["ordering"] = "-created"
        if document_type is not None:
            params["document_type__id__in"] = taxonomy.document_type(document_type)
        if created_from:
            params["created__date__gte"] = created_from
        if created_to:
            params["created__date__lte"] = created_to

        docs: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self._client.get(
                "/api/documents/", {**params, "page": page, "page_size": SUMMARY_PAGE_SIZE}
            )
            if int(body["count"]) > MAX_SUMMARIZED:
                raise TooManyDocuments(
                    f"{body['count']} documents match, more than the {MAX_SUMMARIZED} one "
                    "summary reads. Use a narrower filter."
                )
            docs.extend(body["results"])
            if not body.get("next"):
                break
            page += 1

        overall = _Figures()
        groups: dict[str, _Figures] = {}
        currencies: set[str | None] = set()
        for doc in docs:
            if not self._admitted(doc, taxonomy, by_value=True):
                continue
            values = {e["field"]: e.get("value") for e in doc.get("custom_fields") or []}
            figures = [overall]
            if group_by is not None:
                key = self._group_key(doc, values, taxonomy, group_by, group_field)
                figures.append(groups.setdefault(key, _Figures()))
            raw = values.get(target.id)
            if _is_empty(raw):
                for f in figures:
                    f.empty += 1
                continue
            number: Decimal | int
            if target.data_type == "monetary":
                money = _money(raw)
                if money is None:
                    raise InvalidArgument(
                        f"Document {doc['id']} holds a {target.name} value that is not an amount."
                    )
                number, currency = money
                currencies.add(currency)
            else:
                try:
                    number = int(str(raw)) if target.data_type == "integer" else Decimal(str(raw))
                    if not Decimal(number).is_finite():
                        raise ValueError
                except (ValueError, InvalidOperation):
                    # The value is not repeated: an error message is logged by the SDK.
                    raise InvalidArgument(
                        f"Document {doc['id']} holds a {target.name} value that is not a number."
                    ) from None
            review = bool(taxonomy.needs_review_tags.intersection(doc.get("tags") or []))
            for f in figures:
                f.add(number, review)

        if len(currencies) > 1:
            named = sorted(c or "no currency code" for c in currencies)
            raise MixedCurrency(
                f"{target.name} holds more than one currency across these documents "
                f"({', '.join(named)}), so they cannot be summed together."
            )
        kind = target.data_type
        out: dict[str, Any] = {"field": target.name}
        if kind == "monetary":
            out["currency"] = next(iter(currencies), None)
        out["figures"] = overall.render(kind)
        if group_by is not None:
            out["groups"] = [{"group": key, **groups[key].render(kind)} for key in sorted(groups)]
        return out

    def _group_key(
        self,
        doc: Mapping[str, Any],
        values: Mapping[int, Any],
        taxonomy: Taxonomy,
        group_by: str,
        group_field: CustomField | None,
    ) -> str:
        name: Any
        if group_field is not None:
            raw = values.get(group_field.id)
            name = None if _is_empty(raw) else self._value(group_field, raw)
            if isinstance(name, dict):
                name = f"{name['currency'] or ''}{name['amount']}"
        elif group_by == "correspondent":
            name = taxonomy.correspondents.get(doc.get("correspondent") or 0)
        else:
            name = taxonomy.document_types.get(doc.get("document_type") or 0)
        return EMPTY_GROUP if name is None else str(name)[:MAX_STRING]

    # -- run_saved_view -----------------------------------------------------------------

    # @spec DACC-TOOL-009, DACC-TOOL-010, DACC-RULE-008, DACC-RULE-009, DACC-RULE-012
    async def run_saved_view(self, view: str, *, limit: int = 25, page: int = 1) -> dict[str, Any]:
        _check_paging(limit, page)
        taxonomy = await self._taxonomy()
        views = {int(v["id"]): v for v in await self._client.list_all("/api/saved_views/")}
        chosen = views[_resolve({i: str(v["name"]) for i, v in views.items()}, "saved view", view)]
        params = self._view_params(chosen, taxonomy)
        total, results = await self._page(params, limit, page, taxonomy)
        return {
            "total": total,
            "documents": [self._render(doc, taxonomy) for doc in results],
            "note": VIEW_NOTE,
        }

    def _view_params(self, view: Mapping[str, Any], taxonomy: Taxonomy) -> Params:
        rules: dict[int, list[str | None]] = {}
        for rule in view.get("filter_rules") or []:
            rule_type = rule.get("rule_type")
            if rule_type not in (3, 4, 6, 8, 9, 17, 20, 26, 28, 42):
                label = RULE_LABELS.get(rule_type, str(rule_type))
                raise UnsupportedViewRule(
                    f"Saved view {view['name']!r} uses the rule {label!r}, which this server "
                    "does not translate."
                )
            rules.setdefault(rule_type, []).append(rule.get("value"))

        def unsupported(why: str) -> UnsupportedViewRule:
            return UnsupportedViewRule(f"Saved view {view['name']!r} {why}.")

        def ids(rule_type: int) -> set[int]:
            try:
                return {int(v) for v in rules[rule_type] if v is not None}
            except ValueError:
                raise unsupported(f"has a malformed {RULE_LABELS[rule_type]!r} rule") from None

        def single(rule_type: int) -> str | None:
            if len(rules[rule_type]) > 1:
                raise unsupported(f"repeats the rule {RULE_LABELS[rule_type]!r}")
            return rules[rule_type][0]

        params = self._base(taxonomy)
        if 6 in rules:
            params["tags__id__all"] = _ids(ids(6))
        without_tags = frozenset(ids(17)) if 17 in rules else frozenset()
        if without_tags:
            params["tags__id__none"] = _ids(without_tags)
        for one, many, name in ((3, 26, "correspondent"), (4, 28, "document_type")):
            if one in rules and many in rules:
                raise unsupported(f"mixes two kinds of {name.replace('_', ' ')} rule")
            if one in rules:
                value = single(one)
                if value is None:
                    params[f"{name}__isnull"] = "true"
                else:
                    params[f"{name}__id__in"] = _ids(ids(one))
            elif many in rules:
                params[f"{name}__id__in"] = _ids(ids(many))
        for rule_type, key in ((8, "created__date__lt"), (9, "created__date__gt")):
            if rule_type in rules:
                value = _check_view_date(single(rule_type))
                if value is None:
                    raise unsupported(f"has a malformed {RULE_LABELS[rule_type]!r} rule")
                params[key] = value
        query = None
        if 20 in rules:
            query = (single(20) or "").strip()
            if not query:
                raise unsupported("has an empty 'fulltext query' rule")
            params["query"] = query
        if 42 in rules:
            try:
                params["custom_field_query"] = json.dumps(json.loads(single(42) or ""))
            except ValueError:
                raise unsupported("has a malformed 'custom fields query' rule") from None
        if 20 in rules or 42 in rules:
            if params.get("document_type__isnull") == "true":
                raise unsupported(
                    "asks for documents with no document type together with a rule that reads "
                    "their values, which this server does not search"
                )
            self._keep_values_only(params, taxonomy, without_tags)

        sort_field = view.get("sort_field")
        if sort_field in VIEW_ORDERINGS:
            params["ordering"] = f"-{sort_field}" if view.get("sort_reverse") else str(sort_field)
        elif query is None:
            params["ordering"] = "-created"
        return params


def _check_view_date(value: str | None) -> str | None:
    day = (value or "")[:10]
    try:
        if not _DATE.fullmatch(day):
            return None
        date.fromisoformat(day)
    except ValueError:
        return None
    return day


class _Figures:
    def __init__(self) -> None:
        self.values: list[Decimal | int] = []
        self.empty = 0
        self.review = 0

    def add(self, value: Decimal | int, needs_review: bool) -> None:
        self.values.append(value)
        self.review += needs_review

    def render(self, kind: str) -> dict[str, Any]:
        """Figures for a field of data type `kind`; decimals are summed exactly."""

        def show(value: Decimal | int | None) -> Any:
            if value is None or kind == "integer":
                return value
            return _amount(Decimal(value)) if kind == "monetary" else float(value)

        total: Decimal | int = sum(self.values, 0 if kind == "integer" else Decimal(0))
        return {
            "sum": show(total),
            "count": len(self.values),
            "min": show(min(self.values, default=None)),
            "max": show(max(self.values, default=None)),
            "documents_empty": self.empty,
            "needs_review": self.review,
        }
