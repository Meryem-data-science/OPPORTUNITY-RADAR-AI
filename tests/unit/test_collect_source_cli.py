"""Tests for dry-run CLI argument safeguards."""

import pytest

from services.collector.cli.collect_source import build_parser, positive_limit


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_limit_is_rejected(value: str) -> None:
    with pytest.raises(Exception, match="greater than zero"):
        positive_limit(value)


def test_dry_run_flag_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--source", "scale_ai_greenhouse"])
