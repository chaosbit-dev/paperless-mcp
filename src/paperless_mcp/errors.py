"""Errors raised by the server.

`ToolError` subclasses reach the caller as a tool error; their class name is what the
audit line records. `ConfigError` and `PermissionDrift` stop the server.
"""

from __future__ import annotations


class ConfigError(Exception):
    """The server's configuration, or the Paperless objects it names, cannot be used."""


class PermissionDrift(Exception):
    """An identity holds more Paperless permission than its mode allows."""


class ToolError(Exception):
    """A tool call that fails with a message for the caller."""


class InvalidArgument(ToolError):
    """A tool argument is out of range or malformed."""


class UnknownName(ToolError):
    """A tool argument names a Paperless object that does not exist."""

    def __init__(self, kind: str, name: str) -> None:
        super().__init__(f"No {kind} named {name!r}.")
        self.kind = kind
        self.name = name


class DocumentNotFound(ToolError):
    """No document with that id is available to the caller."""


class UnsupportedViewRule(ToolError):
    """A saved view holds a filter rule the server does not translate."""


class TooManyDocuments(ToolError):
    """More documents match than the server will read for one aggregation."""


class MixedCurrency(ToolError):
    """A monetary field carries more than one currency across the matching documents."""


class PaperlessError(ToolError):
    """Paperless did not answer a request usefully."""


class PaperlessUnreachable(PaperlessError):
    """Paperless could not be connected to."""


class PaperlessTimeout(PaperlessError):
    """Paperless did not answer in time."""


class PaperlessStatusError(PaperlessError):
    """Paperless answered with an error status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"Paperless answered with HTTP {status}.")
        self.status = status
