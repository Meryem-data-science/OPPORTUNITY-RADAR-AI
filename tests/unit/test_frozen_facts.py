"""The shared frozen-record primitives Phase 10.4 and Phase 10.5 both read.

The point of this module existing at all is that "explicitly out of target" and
"explicitly out of scope" are defined **once**, so these tests are about the
definition rather than about either phase's arithmetic. The regression tests
that prove Phase 10.4's numbers did not move when the reading was extracted live
in `test_business_metric_formulas.py`, beside the metrics they are about.
"""

from __future__ import annotations

import pytest

from evaluation.dataset import EvaluationDatasetError
from evaluation.frozen_facts import (
    FROZEN_DATA_AI_STATE_NAMES,
    FROZEN_SEGMENT_STATUS_NAMES,
    FrozenDataAiState,
    FrozenFactsError,
    FrozenSegmentStatus,
    FrozenTargetVerdict,
    data_ai_state_of,
    frozen_block_field,
    frozen_block_sequence,
    frozen_country_code,
    frozen_optional_block,
    frozen_text,
    record_geography_of,
    target_verdict_of,
)

from tests.unit.business_metric_fixtures import (
    OTHER_COUNTRY,
    TARGET_COUNTRY,
    qualification,
    record,
    segment,
)


class Refused(RuntimeError):
    """A caller's own error vocabulary, as every primitive here demands."""


def geography_of(*segments):
    return record_geography_of(
        record(1, geography_segments=segments),
        record_subject="frozen record 1",
        refuse=Refused,
    )


def verdict_of(*segments, target: str = TARGET_COUNTRY):
    return target_verdict_of(
        record(1, geography_segments=segments),
        target,
        record_subject="frozen record 1",
        refuse=Refused,
    )


def state_of(block):
    return data_ai_state_of(
        record(1, qualification=block),
        record_subject="frozen record 1",
        refuse=Refused,
    )


# ====================================================================
# the module's own boundaries
# ====================================================================


def test_the_error_type_is_a_phase_10_1_error() -> None:
    """Every evaluation package raises a subclass of Phase 10.1's error."""
    assert issubclass(FrozenFactsError, EvaluationDatasetError)


def test_the_caller_supplies_its_own_refusal() -> None:
    """The whole reason `refuse` is a parameter: each phase keeps its own type."""
    with pytest.raises(Refused):
        geography_of({**segment("RESOLVED"), "country_code": None})


def test_a_refusal_that_is_not_callable_is_refused_by_this_module() -> None:
    """`refuse` chooses how to say no, never whether to — and must be able to."""
    with pytest.raises(FrozenFactsError, match="not a refusal factory"):
        record_geography_of(
            record(1, geography_segments=({**segment("RESOLVED"), "country_code": None},)),
            record_subject="frozen record 1",
            refuse="not callable",
        )


def test_a_refusal_returning_a_non_exception_is_refused() -> None:
    with pytest.raises(FrozenFactsError, match="cannot be raised"):
        record_geography_of(
            record(1, geography_segments=({**segment("RESOLVED"), "country_code": None},)),
            record_subject="frozen record 1",
            refuse=lambda message: message,
        )


def test_no_country_is_named_anywhere_in_the_module() -> None:
    """No Morocco, and no country at all: the target is always an argument."""
    import pathlib

    source = pathlib.Path("evaluation/frozen_facts.py").read_text()
    # The only two-letter uppercase literals a country hardcode would use.
    for forbidden in ('"MA"', "'MA'", '"FR"', "'FR'"):
        assert forbidden not in source


# ====================================================================
# the vocabularies
# ====================================================================


def test_the_segment_statuses_are_the_three_the_resolver_produces() -> None:
    assert FROZEN_SEGMENT_STATUS_NAMES == {"RESOLVED", "AMBIGUOUS", "UNKNOWN"}
    assert {item.value for item in FrozenSegmentStatus} == FROZEN_SEGMENT_STATUS_NAMES


def test_the_data_ai_states_add_unclassified_to_the_production_vocabulary() -> None:
    """The fifth state is the *absence* of a block, which no record states."""
    from services.collector.qualification.taxonomy import Qualification

    assert FROZEN_DATA_AI_STATE_NAMES == {str(item) for item in Qualification}
    assert FrozenDataAiState.UNCLASSIFIED.value not in FROZEN_DATA_AI_STATE_NAMES
    assert set(FrozenDataAiState) == {
        FrozenDataAiState(name) for name in FROZEN_DATA_AI_STATE_NAMES
    } | {FrozenDataAiState.UNCLASSIFIED}


def test_the_status_names_render_as_plain_strings_in_a_message() -> None:
    """A message is read by a person, not by an enum repr."""
    assert sorted(FROZEN_SEGMENT_STATUS_NAMES) == [
        "AMBIGUOUS",
        "RESOLVED",
        "UNKNOWN",
    ]
    assert "FrozenSegmentStatus" not in f"{sorted(FROZEN_SEGMENT_STATUS_NAMES)}"


# ====================================================================
# the primitive validators
# ====================================================================


@pytest.mark.parametrize("value", ["", "  ", None, 3, True, b"MA"])
def test_frozen_text_refuses_anything_that_is_not_a_real_string(value) -> None:
    with pytest.raises(Refused):
        frozen_text(value, subject="a value", refuse=Refused)


def test_frozen_text_refuses_padding() -> None:
    """Padding is a difference nobody sees, and digests do."""
    with pytest.raises(Refused, match="padded"):
        frozen_text(" MA ", subject="a value", refuse=Refused)


@pytest.mark.parametrize("value", ["ma", "MAR", "M", "", None, 12, "M1"])
def test_frozen_country_code_is_a_shape_check_that_refuses_everything_else(
    value,
) -> None:
    with pytest.raises(Refused):
        frozen_country_code(value, subject="a country", refuse=Refused)


def test_frozen_country_code_accepts_any_uppercase_alpha_2() -> None:
    """A *shape* check. This module holds no country of its own."""
    for code in ("MA", "FR", "ES", "ZZ"):
        assert frozen_country_code(code, subject="a country", refuse=Refused) == code


def test_an_optional_block_absence_stays_none() -> None:
    """`None` is returned, never replaced by an empty object."""
    assert (
        frozen_optional_block(
            record(1, qualification=None),
            "qualification",
            record_subject="frozen record 1",
            refuse=Refused,
        )
        is None
    )


def test_an_optional_block_of_the_wrong_type_is_a_hard_error() -> None:
    with pytest.raises(Refused, match="not the optional block"):
        frozen_optional_block(
            record(1, qualification="CORE_TARGET"),
            "qualification",
            record_subject="frozen record 1",
            refuse=Refused,
        )


def test_a_block_sequence_refuses_a_string_and_a_non_mapping_member() -> None:
    with pytest.raises(Refused, match="not a sequence"):
        frozen_block_sequence(
            {"geography_segments": "MA"},
            "geography_segments",
            record_subject="frozen record 1",
            refuse=Refused,
        )
    with pytest.raises(Refused, match="not a mapping"):
        frozen_block_sequence(
            {"geography_segments": ["MA"]},
            "geography_segments",
            record_subject="frozen record 1",
            refuse=Refused,
        )


def test_a_missing_block_field_refuses_rather_than_reading_another_contract() -> None:
    with pytest.raises(Refused, match="another contract"):
        frozen_block_field({}, "status", subject="a segment", refuse=Refused)


# ====================================================================
# geography coherence
# ====================================================================


def test_a_resolved_segment_names_its_country() -> None:
    geography = geography_of(segment("RESOLVED"))
    assert geography.statuses == (FrozenSegmentStatus.RESOLVED,)
    assert geography.resolved_countries == (TARGET_COUNTRY,)
    assert geography.all_resolved is True
    assert geography.has_segments is True
    assert geography.has_ambiguous_segment is False
    assert geography.has_unknown_segment is False


def test_no_segment_at_all_is_no_segment_and_not_a_verdict() -> None:
    geography = geography_of()
    assert geography.statuses == ()
    assert geography.resolved_countries == ()
    # `all_resolved` is false for an empty set on purpose: "every segment
    # resolved" must not be vacuously true, or a posting with no geography would
    # read as explicitly placed.
    assert geography.all_resolved is False
    assert geography.has_segments is False


def test_ambiguous_and_unknown_are_separate_facts() -> None:
    geography = geography_of(
        segment("AMBIGUOUS", position=1), segment("UNKNOWN", position=2)
    )
    assert geography.has_ambiguous_segment is True
    assert geography.has_unknown_segment is True
    assert geography.all_resolved is False


@pytest.mark.parametrize(
    "broken",
    [
        {**segment("RESOLVED"), "country_code": None},
        {**segment("AMBIGUOUS"), "country_code": "MA"},
        {**segment("UNKNOWN"), "country_code": "MA"},
        {**segment("RESOLVED"), "status": "PROBABLY"},
        {**segment("RESOLVED"), "status": None},
    ],
)
def test_an_incoherent_segment_is_a_hard_error(broken) -> None:
    """The two halves of such a segment cannot both be true."""
    with pytest.raises(Refused):
        geography_of(broken)


def test_geography_never_falls_back_to_the_location_text() -> None:
    """The frozen Phase 7A segments and nothing else."""
    subject = record(
        1, location="Casablanca, Morocco", country="MA", geography_segments=()
    )
    geography = record_geography_of(
        subject, record_subject="frozen record 1", refuse=Refused
    )
    assert geography.resolved_countries == ()
    assert (
        target_verdict_of(
            subject, TARGET_COUNTRY, record_subject="frozen record 1", refuse=Refused
        )
        is FrozenTargetVerdict.UNKNOWN
    )


# ====================================================================
# the target verdict — the UNKNOWN != FALSE rule
# ====================================================================


def test_a_segment_resolved_to_the_target_is_a_match() -> None:
    assert verdict_of(segment("RESOLVED")) is FrozenTargetVerdict.MATCH


def test_one_matching_segment_among_several_is_a_match() -> None:
    """Any segment naming the target is enough; the rest do not outvote it."""
    assert (
        verdict_of(
            segment("RESOLVED", country_code=OTHER_COUNTRY, position=1),
            segment("RESOLVED", country_code=TARGET_COUNTRY, position=2),
        )
        is FrozenTargetVerdict.MATCH
    )


def test_all_resolved_and_none_to_the_target_is_out_of_target() -> None:
    assert (
        verdict_of(segment("RESOLVED", country_code=OTHER_COUNTRY))
        is FrozenTargetVerdict.OUT_OF_TARGET
    )


def test_an_ambiguous_segment_is_never_out_of_target() -> None:
    """A resolver that found several places did not place it elsewhere."""
    assert verdict_of(segment("AMBIGUOUS")) is FrozenTargetVerdict.UNKNOWN
    assert (
        verdict_of(
            segment("RESOLVED", country_code=OTHER_COUNTRY, position=1),
            segment("AMBIGUOUS", position=2),
        )
        is FrozenTargetVerdict.UNKNOWN
    )


def test_an_unknown_segment_is_never_out_of_target() -> None:
    assert verdict_of(segment("UNKNOWN")) is FrozenTargetVerdict.UNKNOWN


def test_no_segment_at_all_is_unknown_and_not_out_of_target() -> None:
    assert verdict_of() is FrozenTargetVerdict.UNKNOWN


def test_the_verdict_depends_only_on_the_bound_target() -> None:
    """The same cohort against two targets. No country is hardcoded."""
    segments = (segment("RESOLVED", country_code="FR"),)
    assert verdict_of(*segments, target="FR") is FrozenTargetVerdict.MATCH
    assert (
        verdict_of(*segments, target="MA") is FrozenTargetVerdict.OUT_OF_TARGET
    )


def test_a_malformed_target_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(Refused, match="country code"):
        verdict_of(segment("RESOLVED"), target="Morocco")


# ====================================================================
# the Data/AI state — an unread posting is not a negative verdict
# ====================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("CORE_TARGET", FrozenDataAiState.CORE_TARGET),
        ("ADJACENT_TARGET", FrozenDataAiState.ADJACENT_TARGET),
        ("UNCERTAIN", FrozenDataAiState.UNCERTAIN),
        ("OUT_OF_SCOPE", FrozenDataAiState.OUT_OF_SCOPE),
    ],
)
def test_each_frozen_qualification_maps_to_its_state(value, expected) -> None:
    assert state_of(qualification(value=value)) is expected


def test_no_qualification_block_is_unclassified_and_not_out_of_scope() -> None:
    """The single most consequential confusion available here."""
    state = state_of(None)
    assert state is FrozenDataAiState.UNCLASSIFIED
    assert state is not FrozenDataAiState.OUT_OF_SCOPE
    assert state is not FrozenDataAiState.UNCERTAIN


def test_an_unknown_qualification_value_is_a_hard_error() -> None:
    """Not folded into `UNCLASSIFIED`: an unread posting is not an unknown word."""
    with pytest.raises(Refused, match="does not define"):
        state_of(qualification(value="PROBABLY_FINE"))


def test_a_qualification_block_with_no_qualification_field_is_refused() -> None:
    block = qualification()
    del block["qualification"]
    with pytest.raises(Refused, match="another contract"):
        state_of(block)


def test_the_state_never_falls_back_to_any_other_field() -> None:
    """A posting screaming "machine learning" is still unclassified."""
    subject = record(
        1,
        canonical_title="Machine Learning Engineer",
        description="deep learning, data science, AI",
        opportunity_type="PFE",
        qualification=None,
    )
    assert (
        data_ai_state_of(
            subject, record_subject="frozen record 1", refuse=Refused
        )
        is FrozenDataAiState.UNCLASSIFIED
    )
