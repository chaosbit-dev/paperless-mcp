"""Settings and the identity map."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from paperless_mcp.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping


class IdentityConfig(BaseModel):
    """One identity-map entry as configured: the token is named, not held."""

    email: str
    token_env: str
    mode: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAPERLESS_MCP_")

    paperless_url: str
    paperless_ui_url: str
    access_team_domain: str
    access_audience: str
    identities: list[IdentityConfig] = []
    excluded_document_types: list[str] = ["School Record"]
    allowed_hosts: list[str] = []
    host: str = "0.0.0.0"
    port: int = 8000
    permission_check_interval_seconds: float = 3600.0


@dataclass(frozen=True)
class Identity:
    email: str
    token: str
    mode: str


MODES = frozenset({"read"})


# @spec DACC-ID-002
def load_identities(settings: Settings, environ: Mapping[str, str]) -> dict[str, Identity]:
    """Resolve the identity map, keyed by lower-cased email."""
    if not settings.identities:
        raise ConfigError("The identity map is empty.")
    identities: dict[str, Identity] = {}
    for entry in settings.identities:
        email = entry.email.strip().lower()
        if not email:
            raise ConfigError("An identity has no email.")
        if entry.mode not in MODES:
            raise ConfigError(f"Identity {email} has mode {entry.mode!r}; only 'read' exists.")
        if email in identities:
            raise ConfigError(f"Identity {email} appears more than once.")
        token = environ.get(entry.token_env, "").strip()
        if not token:
            raise ConfigError(f"Identity {email} names {entry.token_env}, which is not set.")
        identities[email] = Identity(email=email, token=token, mode=entry.mode)
    return identities
