"""The aggregate review token, as a pure function.

No database, no CV. Every decision below is built by hand from invented values
marked TEST ONLY, and the staged values only ever appear as SHA-256 digests —
which is the property this file is mostly about.
"""

import hashlib

from services.digital_twin.cv.replacement.repository import DecisionDigestInput
from services.digital_twin.cv.replacement.review_digest import (
    AGGREGATE_REVIEW_DIGEST_VERSION,
    aggregate_review_digest,
)

TEST_ONLY_STATE_DIGEST = "ab" * 32
TEST_ONLY_OTHER_STATE_DIGEST = "cd" * 32
TEST_ONLY_VALUE = "CorrectedTestOnlyToolkit"
TEST_ONLY_OTHER_VALUE = "OtherTestOnlyToolkit"


def digest_of(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def incoming(
    candidate_id: int,
    *,
    decision: str = "ACCEPT",
    difference: str = "NEW",
    state_digest: str = TEST_ONLY_STATE_DIGEST,
    staged: str | None = None,
    staged_normalized: str | None = None,
) -> DecisionDigestInput:
    return DecisionDigestInput(
        role="INCOMING",
        candidate_id=candidate_id,
        fact_id=None,
        difference=difference,
        decision=decision,
        review_state_digest=state_digest,
        staged_value_digest=None if staged is None else digest_of(staged),
        staged_normalized_value_digest=(
            None if staged_normalized is None else digest_of(staged_normalized)
        ),
    )


def existing(fact_id: int, *, decision: str = "KEEP") -> DecisionDigestInput:
    return DecisionDigestInput(
        role="EXISTING",
        candidate_id=None,
        fact_id=fact_id,
        difference="ABSENT_FROM_NEW_CV",
        decision=decision,
        review_state_digest=TEST_ONLY_STATE_DIGEST,
        staged_value_digest=None,
        staged_normalized_value_digest=None,
    )


def token(decisions, *, profile_id=1, replacement_id=7, extraction_id=3) -> str:
    return aggregate_review_digest(
        profile_id=profile_id,
        replacement_id=replacement_id,
        extraction_id=extraction_id,
        decisions=tuple(decisions),
    )


def test_the_version_is_named():
    assert AGGREGATE_REVIEW_DIGEST_VERSION == "cv-review-aggregate-v1"


def test_the_token_is_a_sha256():
    value = token([incoming(1)])
    assert len(value) == 64
    assert all(character in "0123456789abcdef" for character in value)


def test_the_token_is_deterministic():
    assert token([incoming(1), existing(2)]) == token([incoming(1), existing(2)])


def test_the_order_the_decisions_arrive_in_does_not_matter():
    forward = [incoming(1), incoming(2), existing(5), existing(9)]
    assert token(forward) == token(list(reversed(forward)))


def test_an_empty_review_still_has_a_token_of_its_own():
    assert token([]) != token([incoming(1)])
    assert len(token([])) == 64


# ------------------------------------------------ everything is covered


def test_changing_the_decision_changes_the_token():
    assert token([incoming(1, decision="ACCEPT")]) != token(
        [incoming(1, decision="REJECT")]
    )


def test_changing_the_difference_changes_the_token():
    assert token([incoming(1, difference="NEW")]) != token(
        [incoming(1, difference="UNCHANGED_STILL_SUPPORTED")]
    )


def test_changing_the_state_digest_changes_the_token():
    assert token([incoming(1)]) != token(
        [incoming(1, state_digest=TEST_ONLY_OTHER_STATE_DIGEST)]
    )


def test_changing_the_target_changes_the_token():
    assert token([incoming(1)]) != token([incoming(2)])
    assert token([existing(1)]) != token([existing(2)])


def test_an_incoming_and_an_existing_decision_are_not_interchangeable():
    """Same id, different role: two different reviews."""
    assert token([incoming(4)]) != token([existing(4)])


def test_removing_a_decision_changes_the_token():
    assert token([incoming(1), incoming(2)]) != token([incoming(1)])


def test_changing_the_staged_value_changes_the_token():
    assert token([incoming(1, decision="CORRECT", staged=TEST_ONLY_VALUE)]) != token(
        [incoming(1, decision="CORRECT", staged=TEST_ONLY_OTHER_VALUE)]
    )


def test_changing_only_the_staged_normalized_value_changes_the_token():
    """The one the review specifically asked about: the normalized form alone."""
    base = incoming(
        1,
        decision="CORRECT",
        staged=TEST_ONLY_VALUE,
        staged_normalized=TEST_ONLY_VALUE.casefold(),
    )
    moved = incoming(
        1,
        decision="CORRECT",
        staged=TEST_ONLY_VALUE,
        staged_normalized=TEST_ONLY_OTHER_VALUE.casefold(),
    )
    assert token([base]) != token([moved])


# --------------------------------------------- absent is not empty


def test_no_staged_value_is_distinct_from_an_empty_one():
    absent = incoming(1, decision="CORRECT", staged=None)
    empty = incoming(1, decision="CORRECT", staged="")
    assert token([absent]) != token([empty])


def test_no_staged_normalized_value_is_distinct_from_an_empty_one():
    absent = incoming(1, decision="CORRECT", staged=TEST_ONLY_VALUE)
    empty = incoming(
        1, decision="CORRECT", staged=TEST_ONLY_VALUE, staged_normalized=""
    )
    assert token([absent]) != token([empty])


def test_a_missing_value_cannot_be_forged_by_a_value_shaped_like_the_marker():
    """The absent marker is not 64 hex characters, so no digest can equal it."""
    forged = incoming(1, decision="CORRECT", staged="\x00")
    assert token([forged]) != token([incoming(1, decision="CORRECT", staged=None)])


# ------------------------------------------------------------- isolation


def test_a_token_belongs_to_one_profile():
    decisions = [incoming(1)]
    assert token(decisions, profile_id=1) != token(decisions, profile_id=2)


def test_a_token_belongs_to_one_replacement():
    decisions = [incoming(1)]
    assert token(decisions, replacement_id=7) != token(decisions, replacement_id=8)


def test_a_token_belongs_to_one_extraction():
    decisions = [incoming(1)]
    assert token(decisions, extraction_id=3) != token(decisions, extraction_id=4)


# -------------------------------------------------------------- privacy


def test_no_plaintext_can_reach_the_token():
    """The function only ever receives digests, and returns one."""
    value = token(
        [
            incoming(
                1,
                decision="CORRECT",
                staged=TEST_ONLY_VALUE,
                staged_normalized=TEST_ONLY_VALUE.casefold(),
            )
        ]
    )
    assert TEST_ONLY_VALUE not in value
    assert TEST_ONLY_VALUE.casefold() not in value
    # And the shape it is given carries digests, not readings.
    entry = incoming(1, decision="CORRECT", staged=TEST_ONLY_VALUE)
    assert TEST_ONLY_VALUE not in repr(entry)
    assert entry.staged_value_digest == digest_of(TEST_ONLY_VALUE)
