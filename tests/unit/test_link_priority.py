"""Unit coverage for the deterministic official-link priority policy."""

import pytest

from services.api.link_priority import (
    JOB_BOARD_SOURCE_TYPES,
    OFFICIAL_SOURCE_TYPES,
    SourceObservation,
    is_official_source_type,
    official_rank,
    preferred_link,
    select_original_url,
)


def observation(**changes: object) -> SourceObservation:
    values = {
        "observation_id": 1,
        "source_type": "greenhouse",
        "application_url": "https://example.invalid/greenhouse/apply",
        "source_url": "https://example.invalid/greenhouse/source",
        "canonical_url": "https://example.invalid/greenhouse/canonical",
    }
    values.update(changes)
    return SourceObservation(**values)  # type: ignore[arg-type]


def test_supported_source_types_are_classified_explicitly() -> None:
    assert OFFICIAL_SOURCE_TYPES == ("greenhouse",)
    assert JOB_BOARD_SOURCE_TYPES == ("gmail_linkedin_alert",)
    assert is_official_source_type("greenhouse") is True
    assert is_official_source_type("gmail_linkedin_alert") is False
    assert is_official_source_type(None) is False
    assert official_rank("greenhouse") == 0
    assert official_rank("gmail_linkedin_alert") is None
    assert official_rank("unknown_future_type") is None


@pytest.mark.parametrize(
    ("application_url", "source_url", "canonical_url", "expected"),
    [
        ("apply", "source", "canonical", "apply"),
        (None, "source", "canonical", "source"),
        ("   ", "source", "canonical", "source"),
        (None, None, "canonical", "canonical"),
        (None, "", None, None),
    ],
)
def test_preferred_link_order_is_application_then_source_then_canonical(
    application_url, source_url, canonical_url, expected
) -> None:
    assert preferred_link(application_url, source_url, canonical_url) == expected


def test_fallback_is_used_when_no_official_observation_exists() -> None:
    board = observation(
        observation_id=4,
        source_type="gmail_linkedin_alert",
        application_url="https://example.invalid/linkedin/apply",
    )

    assert select_original_url([], fallback="fallback") == "fallback"
    assert select_original_url([board], fallback="fallback") == "fallback"


def test_official_observation_wins_over_the_fallback() -> None:
    board = observation(observation_id=1, source_type="gmail_linkedin_alert")
    official = observation(observation_id=9)

    assert (
        select_original_url([board, official], fallback="fallback")
        == "https://example.invalid/greenhouse/apply"
    )


def test_official_observation_without_any_link_does_not_win() -> None:
    empty = observation(application_url=None, source_url=" ", canonical_url=None)

    assert select_original_url([empty], fallback="fallback") == "fallback"


def test_official_tie_break_is_the_smallest_observation_id() -> None:
    late = observation(
        observation_id=12, application_url="https://example.invalid/late/apply"
    )
    early = observation(
        observation_id=3, application_url="https://example.invalid/early/apply"
    )

    assert (
        select_original_url([late, early], fallback="fallback")
        == "https://example.invalid/early/apply"
    )
    assert select_original_url([early, late], fallback="fallback") == select_original_url(
        [late, early], fallback="fallback"
    )
