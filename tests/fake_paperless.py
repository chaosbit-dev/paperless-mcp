"""An in-memory Paperless-ngx API (v9) for tests.

It filters for real, so a test asserts on what Paperless *returned* rather than on the
parameter strings the server chose. Two behaviours are deliberately stricter or truer than
a convenient fake would be:

- An unknown query parameter is an `AssertionError`. Real Paperless ignores unknown
  parameters and returns everything, which turns a mistyped constraint into a silent
  widening. Every parameter accepted here was confirmed against Paperless 3.4.2.
- A custom field that is attached with no value matches `exists` and `isnull`, as it does
  in Paperless.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx2

CONTENT_SENTINEL = "SECRET-BODY-TEXT"

TAXONOMY_PARAMS = {"page", "page_size", "ordering"}
DOCUMENT_PARAMS = {
    "page",
    "page_size",
    "ordering",
    "fields",
    "query",
    "title__icontains",
    "tags__id__all",
    "tags__id__none",
    "tags__id__in",
    "document_type__id__in",
    "document_type__id__none",
    "document_type__isnull",
    "correspondent__id__in",
    "correspondent__isnull",
    "created__date__gte",
    "created__date__lte",
    "created__date__gt",
    "created__date__lt",
    "custom_field_query",
}
ORDERINGS = {"created", "-created", "added", "-added"}

VIEW_PERMISSIONS = [
    "view_document",
    "view_tag",
    "view_documenttype",
    "view_correspondent",
    "view_customfield",
    "view_savedview",
    "view_note",
    "view_uisettings",
]


@dataclass
class Request:
    path: str
    params: dict[str, str]
    token: str
    accept: str
    returned_ids: list[int] = field(default_factory=list)


@dataclass
class User:
    username: str
    is_superuser: bool = False
    permissions: list[str] = field(default_factory=lambda: list(VIEW_PERMISSIONS))


class FakePaperless:
    def __init__(self) -> None:
        self.users: dict[str, User] = {"tok-reader": User("claude-reader")}
        self.tags = {9: "private", 10: "needs-review", 11: "Tax 2025", 12: "Medical"}
        self.document_types = {1: "EOB", 2: "Form 1098", 3: "School Record", 4: "Invoice"}
        self.correspondents = {1: "Acme Health", 2: "Mortgage Servicer"}
        self.custom_fields: dict[int, tuple[str, str]] = {
            8: ("Extraction", "string"),
            9: ("Extraction Retries", "integer"),
            17: ("Patient Responsibility", "monetary"),
            18: ("Provider", "string"),
            19: ("Date of Service", "date"),
            20: ("Page Count", "integer"),
            21: ("Claim Number", "string"),
        }
        self.saved_views: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.requests: list[Request] = []
        # Set to "unreachable", "timeout" or an HTTP status to make every request fail.
        self.fail: str | int | None = None
        # Document filters Paperless stops honouring, as it does for a parameter it
        # does not know.
        self.ignored_params: set[str] = set()
        # Replaces the body of the permission read.
        self.ui_settings_body: Any = None
        # Fail only document list requests for this page number or later.
        self.fail_documents_from_page: int | None = None

    # -- building data -----------------------------------------------------------------

    def add_document(
        self,
        doc_id: int,
        title: str,
        *,
        document_type: int | None = 1,
        correspondent: int | None = None,
        tags: list[int] | None = None,
        created: str = "2026-03-01",
        fields: dict[int, Any] | None = None,
        notes: list[tuple[str, str]] | None = None,
        content: str = "",
    ) -> None:
        self.documents.append(
            {
                "id": doc_id,
                "title": title,
                "content": f"{CONTENT_SENTINEL} {content}",
                "document_type": document_type,
                "correspondent": correspondent,
                "tags": list(tags or []),
                "created": created,
                "added": f"{created}T12:00:00Z",
                "owner": None,
                "notes": [
                    {
                        "id": 1000 * doc_id + i,
                        "note": text,
                        "created": f"{created}T12:00:00Z",
                        "user": {"id": 5, "username": username},
                    }
                    for i, (username, text) in enumerate(notes or [])
                ],
                "custom_fields": [{"field": k, "value": v} for k, v in (fields or {}).items()],
            }
        )

    def add_saved_view(
        self,
        view_id: int,
        name: str,
        rules: list[tuple[int, str | None]],
        *,
        sort_field: str = "created",
        sort_reverse: bool = True,
    ) -> None:
        self.saved_views.append(
            {
                "id": view_id,
                "name": name,
                "show_on_dashboard": False,
                "show_in_sidebar": True,
                "sort_field": sort_field,
                "sort_reverse": sort_reverse,
                "filter_rules": [{"rule_type": t, "value": v} for t, v in rules],
                "page_size": None,
                "display_mode": None,
                "display_fields": None,
                "owner": 2,
                "user_can_change": False,
            }
        )

    # -- what tests read back ----------------------------------------------------------

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    @property
    def document_requests(self) -> list[Request]:
        return [r for r in self.requests if r.path.startswith("/api/documents/")]

    def returned_document_ids(self) -> set[int]:
        """Every document id Paperless sent the server, across all requests."""
        return {i for r in self.requests for i in r.returned_ids}

    # -- request handling --------------------------------------------------------------

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.fail == "unreachable":
            raise httpx2.ConnectError("connection refused", request=request)
        if self.fail == "timeout":
            raise httpx2.ReadTimeout("timed out", request=request)
        if isinstance(self.fail, int):
            return httpx2.Response(self.fail, json={"detail": "error"})

        token = request.headers.get("authorization", "").removeprefix("Token ")
        params = dict(request.url.params)
        record = Request(request.url.path, params, token, request.headers.get("accept", ""))
        self.requests.append(record)
        user = self.users.get(token)
        if user is None:
            return httpx2.Response(401, json={"detail": "Invalid token."})

        path = request.url.path
        if path == "/api/ui_settings/" and self.ui_settings_body is not None:
            return httpx2.Response(200, json=self.ui_settings_body)
        if path == "/api/ui_settings/":
            return httpx2.Response(
                200,
                json={
                    "user": {
                        "id": 7,
                        "username": user.username,
                        "is_superuser": user.is_superuser,
                    },
                    "settings": {},
                    "permissions": list(user.permissions),
                },
            )
        taxonomy: dict[str, list[dict[str, Any]]] = {
            "/api/tags/": [{"id": i, "name": n, "owner": None} for i, n in self.tags.items()],
            "/api/document_types/": [
                {"id": i, "name": n, "owner": None} for i, n in self.document_types.items()
            ],
            "/api/correspondents/": [
                {"id": i, "name": n, "owner": None} for i, n in self.correspondents.items()
            ],
            "/api/custom_fields/": [
                {"id": i, "name": n, "data_type": t, "extra_data": {}, "document_count": 0}
                for i, (n, t) in self.custom_fields.items()
            ],
            "/api/saved_views/": self.saved_views,
        }
        if path in taxonomy:
            self._check_params(params, TAXONOMY_PARAMS)
            return self._page(taxonomy[path], params)
        if path == "/api/documents/":
            return self._list_documents(params, record)
        match = re.fullmatch(r"/api/documents/(\d+)/", path)
        if match:
            self._check_params(params, {"fields"})
            for doc in self.documents:
                if doc["id"] == int(match[1]):
                    record.returned_ids.append(doc["id"])
                    return httpx2.Response(200, json=self._project(doc, params))
            return httpx2.Response(404, json={"detail": "Not found."})
        raise AssertionError(f"fake Paperless has no route {path}")

    @staticmethod
    def _check_params(params: dict[str, str], allowed: set[str]) -> None:
        unknown = set(params) - allowed
        assert not unknown, f"Paperless would silently ignore unknown parameters: {unknown}"

    @staticmethod
    def _page(items: list[dict[str, Any]], params: dict[str, str]) -> httpx2.Response:
        size = int(params.get("page_size", 25))
        page = int(params.get("page", 1))
        start = (page - 1) * size
        if page > 1 and start >= len(items):
            return httpx2.Response(404, json={"detail": "Invalid page."})
        chunk = items[start : start + size]
        more = start + size < len(items)
        return httpx2.Response(
            200,
            json={
                "count": len(items),
                "next": "next-page" if more else None,
                "previous": None,
                "all": [d["id"] for d in items],
                "results": chunk,
            },
        )

    def _list_documents(self, params: dict[str, str], record: Request) -> httpx2.Response:
        self._check_params(params, DOCUMENT_PARAMS)
        page = int(params.get("page", 1))
        if self.fail_documents_from_page is not None and page >= self.fail_documents_from_page:
            return httpx2.Response(500, json={"detail": "error"})

        docs = [d for d in self.documents if self._matches(d, params)]
        query = params.get("query")
        ordering = params.get("ordering")
        if ordering is not None:
            assert ordering in ORDERINGS, f"unsupported ordering {ordering}"
            key = ordering.lstrip("-")
            docs.sort(key=lambda d: (d[key], d["id"]), reverse=ordering.startswith("-"))
        elif query:
            docs.sort(key=lambda d: -self._score(d, query))

        projected = []
        for doc in docs:
            out = self._project(doc, params)
            if query:
                out["__search_hit__"] = {
                    "score": float(self._score(doc, query)),
                    "highlights": doc["content"][:80],
                    "note_highlights": None,
                    "rank": 0,
                }
            projected.append(out)
        return self._page_documents(projected, docs, params, record)

    def _page_documents(
        self,
        projected: list[dict[str, Any]],
        docs: list[dict[str, Any]],
        params: dict[str, str],
        record: Request,
    ) -> httpx2.Response:
        size = int(params.get("page_size", 25))
        page = int(params.get("page", 1))
        start = (page - 1) * size
        if page > 1 and start >= len(docs):
            return httpx2.Response(404, json={"detail": "Invalid page."})
        record.returned_ids.extend(d["id"] for d in docs[start : start + size])
        return httpx2.Response(
            200,
            json={
                "count": len(docs),
                "next": "next-page" if start + size < len(docs) else None,
                "previous": None,
                "all": [d["id"] for d in docs],
                "results": projected[start : start + size],
            },
        )

    @staticmethod
    def _project(doc: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
        if "fields" not in params:
            return dict(doc)
        wanted = params["fields"].split(",")
        return {k: v for k, v in doc.items() if k in wanted}

    @staticmethod
    def _score(doc: dict[str, Any], query: str) -> int:
        return (doc["content"] + " " + doc["title"]).lower().count(query.lower())

    def _matches(self, doc: dict[str, Any], params: dict[str, str]) -> bool:
        def ids(name: str) -> set[int]:
            return {int(x) for x in params[name].split(",") if x}

        def flag(name: str) -> bool:
            assert params[name] in {"true", "false"}, f"{name} must be true or false"
            return params[name] == "true"

        tags = set(doc["tags"])
        checks = {
            "query": lambda: self._score(doc, params["query"]) > 0,
            "title__icontains": lambda: params["title__icontains"].lower() in doc["title"].lower(),
            "tags__id__all": lambda: ids("tags__id__all") <= tags,
            "tags__id__none": lambda: not (ids("tags__id__none") & tags),
            "tags__id__in": lambda: bool(ids("tags__id__in") & tags),
            "document_type__id__in": lambda: doc["document_type"] in ids("document_type__id__in"),
            "document_type__id__none": lambda: (
                doc["document_type"] not in ids("document_type__id__none")
            ),
            "document_type__isnull": lambda: (
                (doc["document_type"] is None) == flag("document_type__isnull")
            ),
            "correspondent__id__in": lambda: doc["correspondent"] in ids("correspondent__id__in"),
            "correspondent__isnull": lambda: (
                (doc["correspondent"] is None) == flag("correspondent__isnull")
            ),
            "created__date__gte": lambda: doc["created"] >= params["created__date__gte"],
            "created__date__lte": lambda: doc["created"] <= params["created__date__lte"],
            "created__date__gt": lambda: doc["created"] > params["created__date__gt"],
            "created__date__lt": lambda: doc["created"] < params["created__date__lt"],
            "custom_field_query": lambda: self._field_query(
                doc, json.loads(params["custom_field_query"])
            ),
        }
        return all(
            check()
            for name, check in checks.items()
            if name in params and name not in self.ignored_params
        )

    def _field_query(self, doc: dict[str, Any], expr: list[Any]) -> bool:
        head = expr[0]
        if head in ("AND", "OR"):
            results = [self._field_query(doc, e) for e in expr[1]]
            return all(results) if head == "AND" else any(results)
        if head == "NOT":
            return not self._field_query(doc, expr[1])
        ref, op, value = expr
        if isinstance(ref, str):
            ref = next(i for i, (n, _) in self.custom_fields.items() if n == ref)
        assert ref in self.custom_fields, f"no custom field {ref}"
        attached = {f["field"]: f["value"] for f in doc["custom_fields"]}
        present = ref in attached
        actual = attached.get(ref)
        if op == "exists":
            return present == value
        if op == "isnull":
            return (actual is None) == value
        if actual is None:
            return False
        if self.custom_fields[ref][1] == "monetary":
            actual = Decimal(re.sub(r"^[A-Za-z]{3}", "", actual))
            value = Decimal(str(value))
        if op == "exact":
            return bool(actual == value)
        if op == "iexact":
            return str(actual).lower() == str(value).lower()
        if op == "icontains":
            return str(value).lower() in str(actual).lower()
        if op in ("gt", "gte", "lt", "lte"):
            return bool(
                {
                    "gt": actual > value,
                    "gte": actual >= value,
                    "lt": actual < value,
                    "lte": actual <= value,
                }[op]
            )
        raise AssertionError(f"fake Paperless does not implement operator {op}")
