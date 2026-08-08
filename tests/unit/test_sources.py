"""Tests for the YAML source registry."""

from pathlib import Path

import pytest

from services.collector.sources import (
    DisabledSourceError,
    SourceConfigurationError,
    UnknownSourceError,
    get_enabled_source,
    load_source_registry,
)


def write_registry(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(f"sources:\n{source}", encoding="utf-8")
    return path


def test_load_valid_source_registry() -> None:
    sources = load_source_registry(Path("config/sources.yaml"))
    assert len(sources) == 1
    assert sources[0].id == "scale_ai_greenhouse"
    assert sources[0].board_token == "scaleai"
    assert sources[0].organization == "Scale AI"


def test_unknown_source_is_explicit() -> None:
    with pytest.raises(UnknownSourceError, match="unknown source"):
        get_enabled_source("missing")


def test_disabled_source_is_rejected(tmp_path: Path) -> None:
    path = write_registry(
        tmp_path,
        "  - id: disabled\n    type: greenhouse\n    enabled: false\n"
        "    organization: Test\n    board_token: test\n",
    )
    with pytest.raises(DisabledSourceError, match="disabled"):
        get_enabled_source("disabled", path)


@pytest.mark.parametrize(
    "source",
    [
        "  - id: ''\n    type: greenhouse\n    enabled: true\n    organization: Test\n    board_token: test\n",
        "  - id: test\n    type: other\n    enabled: true\n    organization: Test\n    board_token: test\n",
        "  - id: test\n    type: greenhouse\n    enabled: 1\n    organization: Test\n    board_token: test\n",
        "  - id: test\n    type: greenhouse\n    enabled: true\n    organization: ''\n    board_token: test\n",
        "  - id: test\n    type: greenhouse\n    enabled: true\n    organization: Test\n    board_token: ''\n",
    ],
)
def test_invalid_source_configuration_is_explicit(tmp_path: Path, source: str) -> None:
    with pytest.raises(SourceConfigurationError):
        load_source_registry(write_registry(tmp_path, source))
