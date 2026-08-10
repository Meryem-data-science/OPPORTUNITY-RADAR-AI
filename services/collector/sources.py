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
    organization: str | None = None
    board_token: str | None = None
    category: str | None = None
    country: str | None = None
    frequency_minutes: int | None = None
    status: str = "active"
    gmail_query: str | None = None
    gmail_message_limit: int | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> "SourceConfig":
        if not isinstance(value, dict):
            raise SourceConfigurationError("each source must be a mapping")

        source_id = value.get("id")
        source_type = value.get("type")
        enabled = value.get("enabled")
        organization = value.get("organization")
        board_token = value.get("board_token")
        category = value.get("category")
        country = value.get("country")
        frequency_minutes = value.get("frequency_minutes")
        status = value.get("status", "active")
        gmail_query = value.get("gmail_query")
        gmail_message_limit = value.get("gmail_message_limit")
        if not isinstance(source_id, str) or not source_id.strip():
            raise SourceConfigurationError("source id must be a non-empty string")
        if source_type not in {"greenhouse", "gmail_linkedin_alert"}:
            raise SourceConfigurationError(
                f"source {source_id!r} has unsupported type {source_type!r}"
            )
        if not isinstance(enabled, bool):
            raise SourceConfigurationError(
                f"source {source_id!r} enabled must be a boolean"
            )
        if source_type == "greenhouse":
            for field_name, field_value in (("organization", organization), ("board_token", board_token)):
                if not isinstance(field_value, str) or not field_value.strip():
                    raise SourceConfigurationError(
                        f"source {source_id!r} {field_name} must be a non-empty string"
                    )
        else:
            if not isinstance(gmail_query, str) or not gmail_query.strip():
                raise SourceConfigurationError(
                    f"source {source_id!r} gmail_query must be a non-empty string"
                )
            if (
                isinstance(gmail_message_limit, bool)
                or not isinstance(gmail_message_limit, int)
                or not 1 <= gmail_message_limit <= 100
            ):
                raise SourceConfigurationError(
                    f"source {source_id!r} gmail_message_limit must be an integer between 1 and 100"
                )
        for field_name, field_value in (("category", category), ("country", country)):
            if field_value is not None and (
                not isinstance(field_value, str) or not field_value.strip()
            ):
                raise SourceConfigurationError(
                    f"source {source_id!r} {field_name} must be null or a non-empty string"
                )
        if frequency_minutes is not None and (
            isinstance(frequency_minutes, bool)
            or not isinstance(frequency_minutes, int)
            or frequency_minutes <= 0
        ):
            raise SourceConfigurationError(
                f"source {source_id!r} frequency_minutes must be null or a positive integer"
            )
        if not isinstance(status, str) or not status.strip():
            raise SourceConfigurationError(
                f"source {source_id!r} status must be a non-empty string"
            )
        return cls(
            id=source_id.strip(),
            type=source_type,
            enabled=enabled,
            organization=organization.strip() if isinstance(organization, str) else None,
            board_token=board_token.strip() if isinstance(board_token, str) else None,
            category=category.strip() if category is not None else None,
            country=country.strip() if country is not None else None,
            frequency_minutes=frequency_minutes,
            status=status.strip(),
            gmail_query=gmail_query.strip() if isinstance(gmail_query, str) else None,
            gmail_message_limit=gmail_message_limit,
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
