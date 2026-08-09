"""Typed loading and validation for the opportunity source catalogue."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_SOURCE_REGISTRY = Path("config/sources.yaml")


class SourceConfigurationError(ValueError):
    """Raised when the source registry or one of its entries is invalid."""


class UnknownSourceError(LookupError):
    """Raised when a requested source is not present in the registry."""


class DisabledSourceError(ValueError):
    """Raised when collection is requested for a disabled source."""


@dataclass(frozen=True)
class SourceConfig:
    """Validated configuration needed to collect one source."""

    id: str
    type: str
    enabled: bool
    organization: str
    board_token: str

    @classmethod
    def from_mapping(cls, value: Any) -> "SourceConfig":
        if not isinstance(value, dict):
            raise SourceConfigurationError("each source must be a mapping")

        source_id = value.get("id")
        source_type = value.get("type")
        enabled = value.get("enabled")
        organization = value.get("organization")
        board_token = value.get("board_token")
        if not isinstance(source_id, str) or not source_id.strip():
            raise SourceConfigurationError("source id must be a non-empty string")
        if source_type != "greenhouse":
            raise SourceConfigurationError(
                f"source {source_id!r} has unsupported type {source_type!r}"
            )
        if not isinstance(enabled, bool):
            raise SourceConfigurationError(
                f"source {source_id!r} enabled must be a boolean"
            )
        if not isinstance(board_token, str) or not board_token.strip():
            raise SourceConfigurationError(
                f"source {source_id!r} board_token must be a non-empty string"
            )
        if not isinstance(organization, str) or not organization.strip():
            raise SourceConfigurationError(
                f"source {source_id!r} organization must be a non-empty string"
            )
        return cls(
            id=source_id.strip(),
            type=source_type,
            enabled=enabled,
            organization=organization.strip(),
            board_token=board_token.strip(),
        )


def load_source_registry(path: Path = DEFAULT_SOURCE_REGISTRY) -> list[SourceConfig]:
    """Load a YAML source registry and validate every configured source."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SourceConfigurationError(
            f"cannot read source registry: {error}"
        ) from error
    except yaml.YAMLError as error:
        raise SourceConfigurationError(
            f"invalid source registry YAML: {error}"
        ) from error
    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        raise SourceConfigurationError("source registry must contain a sources list")

    sources: list[SourceConfig] = []
    source_ids: set[str] = set()

    for item in document["sources"]:
        source = SourceConfig.from_mapping(item)
        if source.id in source_ids:
            raise SourceConfigurationError(f"duplicate source id: {source.id}")
        source_ids.add(source.id)
        sources.append(source)

    return sources


def get_enabled_source(
    source_id: str, path: Path = DEFAULT_SOURCE_REGISTRY
) -> SourceConfig:
    """Return an exact source match, rejecting unknown and disabled sources."""
    source = next(
        (item for item in load_source_registry(path) if item.id == source_id), None
    )
    if source is None:
        raise UnknownSourceError(f"unknown source: {source_id}")
    if not source.enabled:
        raise DisabledSourceError(f"source is disabled: {source_id}")
    return source
