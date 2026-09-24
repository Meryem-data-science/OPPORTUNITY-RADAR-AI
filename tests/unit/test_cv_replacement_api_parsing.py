"""The request parsers of the CV replacement surface, as pure functions.

No database, no HTTP, no configuration. Each body below is invented and marked
TEST ONLY, and the correction is a unique sentinel the privacy tests hunt for.

These parsers decide the *shape* of a request and nothing else. Whether an
answer is allowed — the role it belongs to, the difference it faces, a terminal
fact, a protected fact, a stale target — belongs to
`services/digital_twin/cv/replacement/staging.py`, and none of it is repeated
here or below.
"""

import pytest

from services.api.cv_replacement import (
    MAX_STAGED_VALUE_LENGTH,
    CvReplacementApiRequestError,
    parse_activate_request,
    parse_decision_request,
    parse_open_request,
)
from services.digital_twin.cv.replacement.models import ReviewDecision

SENTINEL_CORRECTION = "ZqCorrectionSentinel7431TestOnly"
VALID_DIGEST = "ab" * 32


# ------------------------------------------------------------------- open


def test_the_open_body_carries_one_extraction_and_nothing_else():
    assert parse_open_request({"extraction_id": 7}) == 7


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"extraction_id": 1, "profile_id": 2},
        {"extraction_id": 1, "anything": "else"},
        {"profile_id": 2},
        {"extraction_id": 0},
        {"extraction_id": -1},
        {"extraction_id": True},
        {"extraction_id": "7"},
        {"extraction_id": 7.0},
        {"extraction_id": None},
        [],
        "not-an-object",
        None,
        {1: "non-string key"},
    ],
    ids=[
        "empty",
        "profile-id-alongside",
        "unknown-field",
        "profile-id-alone",
        "zero",
        "negative",
        "boolean",
        "string",
        "float",
        "null",
        "list",
        "text",
        "none",
        "non-string-key",
    ],
)
def test_a_malformed_open_body_is_refused(body):
    with pytest.raises(CvReplacementApiRequestError):
        parse_open_request(body)


def test_a_client_can_never_name_the_profile():
    """Refused, not ignored: dropping it silently would hide the attempt."""
    with pytest.raises(CvReplacementApiRequestError):
        parse_open_request({"extraction_id": 1, "profile_id": 99})


# --------------------------------------------------------------- decisions


@pytest.mark.parametrize("decision", ["ACCEPT", "REJECT", "SKIP_BLOCKED"])
def test_an_incoming_answer_names_a_candidate(decision):
    candidate_id, fact_id, parsed, staged = parse_decision_request(
        {"candidate_id": 4, "decision": decision}
    )
    assert (candidate_id, fact_id, staged) == (4, None, None)
    assert parsed is ReviewDecision(decision)


@pytest.mark.parametrize("decision", ["KEEP", "RETIRE"])
def test_an_existing_answer_names_a_fact(decision):
    candidate_id, fact_id, parsed, staged = parse_decision_request(
        {"fact_id": 9, "decision": decision}
    )
    assert (candidate_id, fact_id, staged) == (None, 9, None)
    assert parsed is ReviewDecision(decision)


def test_a_correction_carries_the_value_it_proposes():
    candidate_id, fact_id, decision, staged = parse_decision_request(
        {
            "candidate_id": 4,
            "decision": "CORRECT",
            "staged_value": SENTINEL_CORRECTION,
        }
    )
    assert (candidate_id, fact_id) == (4, None)
    assert decision is ReviewDecision.CORRECT
    assert staged == SENTINEL_CORRECTION


def test_a_correction_is_passed_through_byte_for_byte():
    """No trim, no case folding, no collapsing: it is the person's own text."""
    typed = f"\t  {SENTINEL_CORRECTION}  \n"
    _, _, _, staged = parse_decision_request(
        {"candidate_id": 1, "decision": "CORRECT", "staged_value": typed}
    )
    assert staged == typed


def test_a_correction_at_the_limit_is_accepted_and_one_over_is_not():
    at_limit = "x" * MAX_STAGED_VALUE_LENGTH
    _, _, _, staged = parse_decision_request(
        {"candidate_id": 1, "decision": "CORRECT", "staged_value": at_limit}
    )
    assert staged == at_limit
    with pytest.raises(CvReplacementApiRequestError) as error:
        parse_decision_request(
            {
                "candidate_id": 1,
                "decision": "CORRECT",
                "staged_value": "x" * (MAX_STAGED_VALUE_LENGTH + 1),
            }
        )
    # Reported by length. The refused text itself never reaches the message.
    assert str(MAX_STAGED_VALUE_LENGTH) in str(error.value)


def test_an_oversized_correction_is_never_echoed_back():
    oversized = SENTINEL_CORRECTION * (
        MAX_STAGED_VALUE_LENGTH // len(SENTINEL_CORRECTION) + 2
    )
    with pytest.raises(CvReplacementApiRequestError) as error:
        parse_decision_request(
            {"candidate_id": 1, "decision": "CORRECT", "staged_value": oversized}
        )
    assert SENTINEL_CORRECTION not in str(error.value)
    assert SENTINEL_CORRECTION not in repr(error.value)


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "ACCEPT"},
        {"candidate_id": 1, "fact_id": 2, "decision": "ACCEPT"},
        {"candidate_id": 1},
        {"candidate_id": 1, "decision": "CORRECT"},
        {"candidate_id": 1, "decision": "CORRECT", "staged_value": ""},
        {"candidate_id": 1, "decision": "CORRECT", "staged_value": "   "},
        {"candidate_id": 1, "decision": "CORRECT", "staged_value": 5},
        {"candidate_id": 1, "decision": "ACCEPT", "staged_value": "x"},
        {"fact_id": 1, "decision": "KEEP", "staged_value": "x"},
        {"candidate_id": 1, "decision": "KEEP"},
        {"candidate_id": 1, "decision": "RETIRE"},
        {"fact_id": 1, "decision": "ACCEPT"},
        {"fact_id": 1, "decision": "CORRECT", "staged_value": "x"},
        {"candidate_id": 1, "decision": "UNDECIDED"},
        {"candidate_id": 1, "decision": "WHATEVER"},
        {"candidate_id": 1, "decision": 3},
        {"candidate_id": 1, "decision": "ACCEPT", "profile_id": 1},
        {
            "candidate_id": 1,
            "decision": "CORRECT",
            "staged_value": "x",
            "staged_normalized_value": "x",
        },
        {"candidate_id": 0, "decision": "ACCEPT"},
        {"fact_id": True, "decision": "KEEP"},
    ],
    ids=[
        "no-target",
        "both-targets",
        "no-decision",
        "correct-without-value",
        "empty-correction",
        "blank-correction",
        "non-string-correction",
        "value-outside-correct",
        "value-on-existing",
        "existing-word-on-candidate",
        "retire-on-candidate",
        "incoming-word-on-fact",
        "correct-on-fact",
        "undecided",
        "unknown-word",
        "non-string-decision",
        "profile-id",
        "client-normal-form",
        "zero-candidate",
        "boolean-fact",
    ],
)
def test_a_malformed_decision_body_is_refused(body):
    with pytest.raises(CvReplacementApiRequestError):
        parse_decision_request(body)


def test_undecided_is_never_a_decision_a_client_may_send():
    """The absence of an answer is not an answer, and the domain says so too."""
    with pytest.raises(CvReplacementApiRequestError):
        parse_decision_request({"candidate_id": 1, "decision": "UNDECIDED"})


# ---------------------------------------------------------------- activate


def test_the_activate_body_carries_one_digest():
    assert parse_activate_request({"review_digest": VALID_DIGEST}) == VALID_DIGEST


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"review_digest": VALID_DIGEST, "profile_id": 1},
        {"review_digest": VALID_DIGEST, "force": True},
        {"digest": VALID_DIGEST},
        {"review_digest": None},
        {"review_digest": 1234},
        {"review_digest": ""},
        {"review_digest": "ab" * 31},
        {"review_digest": "ab" * 33},
        {"review_digest": "AB" * 32},
        {"review_digest": "g" * 64},
        {"review_digest": f" {'a' * 63}"},
    ],
    ids=[
        "empty",
        "profile-id",
        "unknown-field",
        "wrong-name",
        "null",
        "not-a-string",
        "blank",
        "too-short",
        "too-long",
        "uppercase",
        "not-hex",
        "padded",
    ],
)
def test_a_malformed_activate_body_is_refused(body):
    with pytest.raises(CvReplacementApiRequestError):
        parse_activate_request(body)


def test_the_digest_is_passed_through_unchanged():
    """It is the token the person confirmed, not one this surface recomputes."""
    digest = "0123456789abcdef" * 4
    assert parse_activate_request({"review_digest": digest}) == digest
