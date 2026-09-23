"""One token for one review: what the person actually confirmed.

A review is answered on a screen and activated by a later call. Between the two,
an answer can change — the person reconsiders, or a second window is open. If
the activation applied whatever the database happened to hold at that moment, it
would apply decisions nobody confirmed, and it would do so silently.

So a complete, still-current review is given a **token**: one digest over the
whole ordered decision set. `mark_ready_to_activate` stores it,
`record_review_decision` clears it the moment anything changes, and an
activation is required to hand it back. An activation carrying a token the
review no longer has is refused rather than applied.

What the token covers, and why each part is in it:

* the digest version, so changing what a token means invalidates every open
  review instead of quietly comparing two different things;
* `profile_id`, `replacement_id` and `extraction_id`, so a token belongs to one
  attempt of one person over one reading campaign and to nothing else;
* every decision, ordered: its role, its target, its classification, the answer,
  and the `state_digest` of the plan entry it answered;
* the staged values, **as SHA-256 digests**.

That last point is the rule this module exists under: no CV text enters the
token, the stored digest, a log line or an error message. The plaintext is read
inside `repository.list_decision_digest_inputs`, hashed there, and never travels
further. `None` — no staged value — is encoded with an explicit marker that is
not the digest of the empty string, so "nothing typed" and "an empty string
typed" can never collide.
"""

from __future__ import annotations

import hashlib
import sqlite3

from services.digital_twin.cv.replacement.repository import (
    DecisionDigestInput,
    list_decision_digest_inputs,
)

__all__ = [
    "AGGREGATE_REVIEW_DIGEST_VERSION",
    "aggregate_review_digest",
    "compute_review_digest",
]

#: Named so that changing what a token covers is a visible change of contract.
#: Every stored token stops matching, and every open review is confirmed again
#: rather than activated against something nobody checked.
AGGREGATE_REVIEW_DIGEST_VERSION = "cv-review-aggregate-v1"

#: The separators `ProvenanceInput` and the manifest chain already use.
_FIELD = "\x1f"
_RECORD = "\x1e"

#: "No staged value", written as something no SHA-256 digest can equal: a digest
#: is 64 hexadecimal characters, and this is neither. The empty string has a
#: digest of its own, so the two stay distinguishable.
_ABSENT = "\x00"


def _marker(digest: str | None) -> str:
    return _ABSENT if digest is None else digest


def _sort_key(entry: DecisionDigestInput) -> tuple:
    """Absent ids sort before present ones, and never compare against them."""
    return (
        entry.role,
        entry.candidate_id is not None,
        entry.candidate_id or 0,
        entry.fact_id is not None,
        entry.fact_id or 0,
    )


def aggregate_review_digest(
    *,
    profile_id: int,
    replacement_id: int,
    extraction_id: int,
    decisions: tuple[DecisionDigestInput, ...],
) -> str:
    """The token for this exact review. A pure function of its arguments.

    Deterministic: the same review yields the same token whatever order the
    decisions were written in, on any machine, with no clock and no row id
    taking part.
    """
    header = _FIELD.join(
        (
            AGGREGATE_REVIEW_DIGEST_VERSION,
            str(profile_id),
            str(replacement_id),
            str(extraction_id),
            str(len(decisions)),
        )
    )
    records = [
        _FIELD.join(
            (
                entry.role,
                _ABSENT if entry.candidate_id is None else str(entry.candidate_id),
                _ABSENT if entry.fact_id is None else str(entry.fact_id),
                entry.difference,
                entry.decision,
                entry.review_state_digest,
                _marker(entry.staged_value_digest),
                _marker(entry.staged_normalized_value_digest),
            )
        )
        for entry in sorted(decisions, key=_sort_key)
    ]
    payload = _RECORD.join([header, *records])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_review_digest(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    replacement_id: int,
    extraction_id: int,
) -> str:
    """Read this review's decisions and return its token. Writes nothing.

    Safe on a `mode=ro` connection and safe inside a transaction the caller
    owns: it runs one `SELECT` and nothing else.
    """
    return aggregate_review_digest(
        profile_id=profile_id,
        replacement_id=replacement_id,
        extraction_id=extraction_id,
        decisions=list_decision_digest_inputs(
            connection, profile_id=profile_id, replacement_id=replacement_id
        ),
    )
