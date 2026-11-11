"""The permission check: an identity may hold no more than its mode allows."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from paperless_mcp.errors import ConfigError, PaperlessError, PermissionDrift

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from paperless_mcp.paperless import PaperlessClient


logger = logging.getLogger(__name__)

PRIVATE_TAG = "private"

# What each mode may hold, as a codename prefix.
ALLOWED_PREFIXES = {"read": ("view_",)}


# @spec DACC-ID-003, DACC-ID-004
async def verify_permissions(client: PaperlessClient, mode: str) -> str:
    """Return the identity's Paperless username, or raise `PermissionDrift`."""
    body = await client.get("/api/ui_settings/")
    user = body.get("user")
    permissions = body.get("permissions")
    # A body that does not say what the identity holds proves nothing about it.
    if (
        not isinstance(user, dict)
        or not isinstance(user.get("username"), str)
        or not isinstance(user.get("is_superuser"), bool)
        or not isinstance(permissions, list)
    ):
        raise PaperlessError("Paperless answered the permission read with an unexpected body.")
    username = str(user["username"])
    if user["is_superuser"]:
        raise PermissionDrift(f"{username} is a Paperless superuser.")
    allowed = ALLOWED_PREFIXES[mode]
    excess = sorted(str(p) for p in permissions if not str(p).startswith(allowed))
    if excess:
        raise PermissionDrift(
            f"{username} holds more than mode {mode!r} allows: {', '.join(excess)}."
        )
    return username


# @spec DACC-ID-008
async def verify_names(client: PaperlessClient, excluded_document_types: Sequence[str]) -> None:
    """Raise `ConfigError` unless the `private` tag and every excluded type exist by name."""
    tags = {str(t["name"]).casefold() for t in await client.list_all("/api/tags/")}
    if PRIVATE_TAG not in tags:
        raise ConfigError(f"Paperless has no tag named {PRIVATE_TAG!r}.")
    types = {str(t["name"]).casefold() for t in await client.list_all("/api/document_types/")}
    for name in excluded_document_types:
        if name.casefold() not in types:
            raise ConfigError(f"Paperless has no document type named {name!r}.")


class PermissionMonitor:
    """Repeats the permission check while the server runs."""

    def __init__(
        self,
        check: Callable[[], Awaitable[None]],
        *,
        interval: float,
        on_drift: Callable[[], None],
    ) -> None:
        self._check = check
        self._interval = interval
        self._on_drift = on_drift

    # @spec DACC-ID-006, DACC-ID-007
    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._check()
            except PermissionDrift as drift:
                logger.error("Permission check failed: %s", drift)
                self._on_drift()
                return
            except PaperlessError as error:
                logger.warning(
                    "Permission check could not be made (%s); retrying.", type(error).__name__
                )
            except Exception as error:
                # The check must outlive a failure nobody foresaw, or drift goes unseen.
                logger.error(
                    "Permission check failed unexpectedly (%s); retrying.", type(error).__name__
                )
