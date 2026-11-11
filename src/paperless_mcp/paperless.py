"""HTTP client for the Paperless-ngx REST API, acting as one identity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx2

from paperless_mcp.errors import PaperlessStatusError, PaperlessTimeout, PaperlessUnreachable

if TYPE_CHECKING:
    from collections.abc import Mapping

API_VERSION = 9
LIST_PAGE_SIZE = 100


class PaperlessClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._http = httpx2.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Token {token}",
                "Accept": f"application/json; version={API_VERSION}",
            },
            transport=transport,
            timeout=timeout,
        )

    # @spec DACC-ERR-001
    async def get(
        self, path: str, params: Mapping[str, str | int] | None = None
    ) -> dict[str, Any]:
        """GET `path` under `/api/` and return the decoded JSON object."""
        try:
            response = await self._http.get(path, params=dict(params or {}))
        except httpx2.TimeoutException:
            raise PaperlessTimeout("Paperless did not answer in time.") from None
        except httpx2.TransportError:
            raise PaperlessUnreachable("Could not reach Paperless.") from None
        # A redirect is Paperless (or something in front of it) not answering the API call.
        if response.status_code >= 300:
            raise PaperlessStatusError(response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise PaperlessStatusError(response.status_code) from None
        if not isinstance(body, dict):
            raise PaperlessStatusError(response.status_code)
        return body

    async def list_all(self, path: str) -> list[dict[str, Any]]:
        """Every object of a taxonomy endpoint, following its pages."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self.get(path, {"page": page, "page_size": LIST_PAGE_SIZE})
            items.extend(body["results"])
            if not body.get("next"):
                return items
            page += 1

    async def aclose(self) -> None:
        await self._http.aclose()
