"""The Phase 7A.1 target evaluator and profile target resolver, on values alone.

Pure functions, no database. The evaluator is given resolutions and a country
code; the declared-target resolver is given a mobility a person could have
typed. Both are the halves of the phase that must never guess, so most of what
follows pins the answers they refuse to give.
"""

import pytest

from services.digital_twin.preferences.models import MobilityScope
from services.geography.evaluator import (
    MATCH_RULE,
    NO_LOCATION_RULE,
    NO_TARGET_RULE,
    OUT_OF_TARGET_RULE,
    UNRESOLVED_LOCATION_RULE,
    evaluate_target,
)
from services.geography.models import (
    GeographicError,
    LocationResolution,
    RESOLVER_VERSION,
    ResolutionStatus,
    TargetVerdict,
)
from services.geography.profile_target import (
    MOBILITY_MULTIPLE_COUNTRIES_RULE,
    MOBILITY_OPEN_RULE,
    MOBILITY_UNRESOLVED_RULE,
    RESTRICTED_COUNTRY_RULE,
    resolve_declared_target,
)
from services.geography.resolver import resolve_location_text

TARGET = "MA"


def resolutions_for(*location_texts: str) -> tuple[LocationResolution, ...]:
    """The rows a posting with these location strings would have on disk."""
    rows: list[LocationResolution] = []
    for source_id, text in enumerate(location_texts, start=1):
        for position, segment in enumerate(resolve_location_text(text)):
            rows.append(
                LocationResolution(
                    opportunity_id=1,
                    source_location_id=source_id,
                    segment_position=position,
                    raw_segment=segment.raw_segment,
                    status=segment.status,
                    rule_id=segment.rule_id,
                    country_code=segment.country_code,
                    city_key=segment.city_key,
                    resolver_version=RESOLVER_VERSION,
                    source_fingerprint="0" * 64,
                )
            )
    return tuple(rows)


def verdict(*location_texts: str, target: str | None = TARGET) -> TargetVerdict:
    return evaluate_target(target, resolutions_for(*location_texts)).verdict


# --------------------------------------------------------------------------
# The three answers, for the target this product uses
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["Casablanca", "Casablanca, Maroc", "Rabat, Morocco", "Maroc"]
)
def test_a_posting_in_the_target_country_matches(text) -> None:
    assert verdict(text) is TargetVerdict.MATCH


@pytest.mark.parametrize("text", ["Paris, France", "London, UK", "Doha, Qatar"])
def test_a_posting_resolved_elsewhere_is_out_of_target(text) -> None:
    assert verdict(text) is TargetVerdict.OUT_OF_TARGET


@pytest.mark.parametrize("text", ["APAC", "Any Office", "New York, NY"])
def test_a_posting_nobody_could_place_is_unknown_and_never_out_of_target(text) -> None:
    """Absence of evidence is not evidence of being somewhere else."""
    assert verdict(text) is TargetVerdict.UNKNOWN


def test_a_posting_with_no_location_at_all_is_unknown() -> None:
    assessment = evaluate_target(TARGET, ())
    assert assessment.verdict is TargetVerdict.UNKNOWN
    assert assessment.rule_id == NO_LOCATION_RULE


# --------------------------------------------------------------------------
# Several places
# --------------------------------------------------------------------------


def test_one_matching_place_is_enough_however_many_others_there_are() -> None:
    assert verdict("Casablanca, Maroc; Paris, France") is TargetVerdict.MATCH
    assert verdict("Paris, France; Casablanca, Maroc") is TargetVerdict.MATCH
    assert verdict("Paris, France", "Casablanca") is TargetVerdict.MATCH


def test_every_place_resolved_elsewhere_is_out_of_target() -> None:
    assert verdict("Paris, France; London, UK") is TargetVerdict.OUT_OF_TARGET


def test_one_unresolved_place_withholds_the_refusal() -> None:
    """`APAC` may well contain Morocco; a string nobody could read excludes nothing."""
    assessment = evaluate_target(TARGET, resolutions_for("Paris, France; APAC"))
    assert assessment.verdict is TargetVerdict.UNKNOWN
    assert assessment.rule_id == UNRESOLVED_LOCATION_RULE


# --------------------------------------------------------------------------
# The rule each verdict names, and the counters it reports
# --------------------------------------------------------------------------


def test_each_verdict_names_the_rule_that_produced_it() -> None:
    assert evaluate_target(
        TARGET, resolutions_for("Casablanca")
    ).rule_id == MATCH_RULE
    assert evaluate_target(
        TARGET, resolutions_for("Paris, France")
    ).rule_id == OUT_OF_TARGET_RULE
    assert evaluate_target(None, resolutions_for("Casablanca")).rule_id == NO_TARGET_RULE


def test_an_unknown_target_answers_unknown_about_every_posting() -> None:
    """A comparison with nothing has no result, not a negative one."""
    for text in ("Casablanca, Maroc", "Paris, France", "Any Office"):
        assert verdict(text, target=None) is TargetVerdict.UNKNOWN


def test_the_assessment_counts_segments_without_naming_them() -> None:
    assessment = evaluate_target(
        TARGET, resolutions_for("Casablanca, Maroc; Paris, France; APAC")
    )
    assert assessment.as_dict() == {
        "verdict": "MATCH",
        "rule_id": MATCH_RULE,
        "segments": 3,
        "resolved_segments": 2,
        "matching_segments": 1,
    }


def test_a_malformed_target_is_refused_rather_than_normalised() -> None:
    with pytest.raises(GeographicError):
        evaluate_target("ma", resolutions_for("Casablanca"))
    with pytest.raises(GeographicError):
        evaluate_target("MAR", resolutions_for("Casablanca"))


# --------------------------------------------------------------------------
# The profile side: which country a declared mobility restricts one to
# --------------------------------------------------------------------------


def target_of(scope: MobilityScope, *locations: str) -> tuple[str | None, str]:
    resolved = resolve_declared_target(7, scope, locations)
    return resolved.country_code, resolved.rule_id


def test_a_restriction_to_morocco_resolves_to_ma() -> None:
    assert target_of(MobilityScope.RESTRICTED, "Maroc") == (
        "MA",
        RESTRICTED_COUNTRY_RULE,
    )
    assert target_of(MobilityScope.RESTRICTED, "Morocco")[0] == "MA"
    assert target_of(MobilityScope.RESTRICTED, "Casablanca, Maroc")[0] == "MA"


def test_several_entries_naming_one_country_are_unanimous_not_multiple() -> None:
    """`["Maroc", "Casablanca"]` says Morocco twice, which is still Morocco."""
    assert target_of(MobilityScope.RESTRICTED, "Maroc", "Casablanca") == (
        "MA",
        RESTRICTED_COUNTRY_RULE,
    )


def test_a_mobility_naming_several_countries_is_unknown_and_never_the_first() -> None:
    """`["Maroc", "France"]` is "not restricted to one country", not "Morocco"."""
    assert target_of(MobilityScope.RESTRICTED, "Maroc", "France") == (
        None,
        MOBILITY_MULTIPLE_COUNTRIES_RULE,
    )


def test_a_mobility_the_registry_does_not_recognise_is_unknown() -> None:
    assert target_of(MobilityScope.RESTRICTED, "Atlantis") == (
        None,
        MOBILITY_UNRESOLVED_RULE,
    )
    assert target_of(MobilityScope.RESTRICTED, "APAC")[0] is None


@pytest.mark.parametrize("unplaceable", ["Atlantis", "APAC", "Any Office"])
def test_one_unplaceable_entry_withholds_the_whole_target(unplaceable) -> None:
    """An entry nobody could place is never skipped to reach a target.

    Dropping it would read absence of evidence as agreement: `["Maroc", X]`
    would answer `MA`, silently narrowing a restriction the person may well
    have written wider than one country, and the evaluator would then answer
    OUT_OF_TARGET about places X might have covered. UNKNOWN is not FALSE on
    this side of the database either.
    """
    assert target_of(MobilityScope.RESTRICTED, "Maroc", unplaceable) == (
        None,
        MOBILITY_UNRESOLVED_RULE,
    )


def test_a_mobility_that_resolves_to_nothing_at_all_is_unknown() -> None:
    assert target_of(MobilityScope.RESTRICTED, "Unsupported Place") == (
        None,
        MOBILITY_UNRESOLVED_RULE,
    )
    assert target_of(MobilityScope.RESTRICTED) == (None, MOBILITY_UNRESOLVED_RULE)


def test_an_open_mobility_names_no_target_however_it_is_written() -> None:
    """A person open to anywhere has not excluded anywhere."""
    assert target_of(MobilityScope.OPEN) == (None, MOBILITY_OPEN_RULE)
    assert target_of(MobilityScope.OPEN, "Maroc") == (None, MOBILITY_OPEN_RULE)


def test_the_profile_side_reads_the_same_registry_as_the_posting_side() -> None:
    """`Maroc` cannot mean one thing on a profile and another on a posting."""
    assert (
        target_of(MobilityScope.RESTRICTED, "Maroc")[0]
        == resolve_location_text("Maroc")[0].country_code
    )
