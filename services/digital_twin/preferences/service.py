"""Recording what a person states about their availability, mobility and wants.

This is the write side of Phase 3.4C, and it writes **facts**, never rows. The
projection is derived from those facts by `repository.py`, so nothing here
touches `profile_availability`, `profile_mobility`, `profile_preferences` or
`profile_career_objectives`: a preference written straight into a projection
would be a claim with no provenance and no history, and the whole point of
`profile_facts` is that no such claim exists.

Each of the four domains is a **singleton**, and each `set_*` call resolves to
exactly one of three outcomes:

* `CREATED` — the person had stated nothing for that domain. One `ACCEPTED`
  fact is recorded, with its `USER_INPUT` provenance, in one transaction;
* `UNCHANGED` — the person restated exactly what they already said. Nothing is
  written at all: no second fact, no correction, no touched timestamp. This is
  decided by comparing canonical encodings, so restating the same preference
  with the opportunity types typed in another order is still a no-op;
* `CORRECTED` — the person stated something else. `correct_profile_fact`
  creates the new `ACCEPTED` fact and marks the previous one `CORRECTED`,
  pointing it at its replacement. The old value is never overwritten, so the
  chain of everything the person ever said stays readable.

A fifth possibility is refused rather than resolved: a domain already holding
two facts the person stated. See `AmbiguousExplicitInputError` — choosing
between two things somebody said is not this module's decision to make.

"Stated" has one definition, and it lives in `repository.find_stated_fact`:
`ACCEPTED`, of that type, **and carrying `USER_INPUT` provenance**. Both this
module and the projection ask through it, so what a `set_*` call decides
against is exactly what the rows will describe.

Nothing here infers anything. There is no code path that reads a CV, a skill,
an experience, an address, a nationality or the clock, and the callers below
receive a fully-formed, already-validated statement from `models.py`. What the
person typed is what gets recorded.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from services.digital_twin.facts.models import (
    FactSourceType,
    ProfileFactType,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    correct_profile_fact,
    record_verified_user_input_fact,
)
from services.digital_twin.preferences.codec import (
    encode_availability,
    encode_career_objectives,
    encode_mobility,
    encode_preferences,
)
from services.digital_twin.preferences.models import (
    EXPLICIT_PROFILE_INPUT_VERSION,
    AvailabilityPreference,
    CareerObjectives,
    MobilityPreference,
    OpportunityPreferences,
)
from services.digital_twin.preferences.repository import find_stated_fact

__all__ = [
    "ExplicitInputAction",
    "ExplicitInputOutcome",
    "set_profile_availability",
    "set_profile_career_objectives",
    "set_profile_mobility",
    "set_profile_preferences",
]


class ExplicitInputAction(StrEnum):
    """What one `set_*` call did to the facts. Three outcomes, no others."""

    #: The first statement for this domain. One new `ACCEPTED` fact.
    CREATED = "CREATED"
    #: The same statement again. Nothing was written.
    UNCHANGED = "UNCHANGED"
    #: A different statement. The previous fact is now `CORRECTED`.
    CORRECTED = "CORRECTED"


@dataclass(frozen=True)
class ExplicitInputOutcome:
    """What one `set_*` call did, as ids and a verb — never as a value.

    `fact_id` is always the fact that now holds the statement, whether this
    call created it, corrected into it, or found it unchanged.
    `previous_fact_id` is set only on a correction, and points at the fact that
    is now `CORRECTED` and still readable.
    """

    profile_id: int
    fact_type: str
    action: ExplicitInputAction
    fact_id: int
    previous_fact_id: int | None = None

    @property
    def changed(self) -> bool:
        return self.action is not ExplicitInputAction.UNCHANGED

    def as_dict(self) -> dict[str, object]:
        """The privacy-safe summary: a type, a verb and two ids.

        The flag is `fact_changed`, not `changed`: a `set-*` command prints this
        summary next to the synchronization's own, and those are two different
        questions — whether the person's statement moved, and whether the
        projection had to be rewritten. They usually agree, and the run where
        they disagree is exactly the one worth being able to read.
        """
        return {
            "fact_type": self.fact_type,
            "action": self.action.value,
            "fact_id": self.fact_id,
            "previous_fact_id": self.previous_fact_id,
            "fact_changed": self.changed,
        }


def _provenance(fact_type: ProfileFactType) -> ProvenanceInput:
    """The `USER_INPUT` evidence for one act of typing something.

    Two identities are at play in this package and they are **not** the same
    one, which is the whole reason this function exists:

    * the identity of a *value* is its canonical JSON. That is what tells
      `UNCHANGED` from a correction, and it is fully deterministic — the same
      statement always encodes to the same bytes;
    * the identity of a *proof* is this key, and a proof is an **event**: a
      person sat down and typed something on some occasion. Two occasions are
      two proofs even when the person typed the same words both times.

    Deriving the key from the canonical value conflated the two. A person who
    said A, then B, then A again has made three statements, but the first and
    third would have resolved to one key — leaving two facts of one profile
    sharing a single proof, which is exactly the state
    `AmbiguousFactEvidenceError` exists to report, and which would make
    `ensure_profile_fact_proposal` and `ensure_profile_fact_provenance` raise
    on that profile from then on. Going back to a previous answer is an
    ordinary thing to do; it must not corrupt the evidence.

    So the key names the contract and the domain, and identifies the event with
    a fresh `uuid4`. It carries no personal content — not the text, not even a
    digest of it: a provenance key is read by operators and printed in errors,
    and somebody's career objective is not an identifier. Two different events
    cannot collide, and nothing about the value can be recovered from the key.

    This is called **only** where a fact is actually written, from the
    `CREATED` and `CORRECTED` branches of `_record_singleton`. A no-op writes
    no fact, so it generates no event and no key: restating what one already
    said is not an occasion of record.
    """
    return ProvenanceInput(
        source_type=FactSourceType.USER_INPUT,
        provenance_key=(
            f"{EXPLICIT_PROFILE_INPUT_VERSION}:{fact_type.value}:{uuid4().hex}"
        ),
    )


def _record_singleton(
    connection: sqlite3.Connection,
    profile_id: int,
    fact_type: ProfileFactType,
    canonical_value: str,
) -> ExplicitInputOutcome:
    """Create, no-op or correct the one fact of this type for this profile.

    The read is `find_stated_fact`, the same one the projection uses, so the
    write side and the rows can never disagree about which fact is the person's
    current statement. It counts only `ACCEPTED` facts carrying `USER_INPUT`
    provenance, which has two consequences worth stating:

    * a `PROPOSED`, `REJECTED` or `CORRECTED` fact of the same type is history
      or somebody else's business. It neither blocks a first statement nor is
      mistaken for the current one;
    * an `ACCEPTED` fact of the same type evidenced only by a CV, a GitHub page
      or other accepted evidence is **not** this person's statement either, so
      it is not corrected in place. Correcting it would rewrite what a document
      was read as saying in order to record what somebody typed, and those are
      two different claims. The person's first statement is a fact of its own.

    Two stated facts raise rather than resolve — see `find_stated_fact`.
    """
    stated = find_stated_fact(connection, profile_id, fact_type)
    if stated is None:
        fact = record_verified_user_input_fact(
            connection,
            profile_id=profile_id,
            fact_type=fact_type,
            value=canonical_value,
            provenance=_provenance(fact_type),
        )
        return ExplicitInputOutcome(
            profile_id=profile_id,
            fact_type=fact_type.value,
            action=ExplicitInputAction.CREATED,
            fact_id=fact.id,
        )
    current_id, current_value = stated
    if current_value == canonical_value:
        # Byte-identical because both sides are canonical. Restating what one
        # already said is not a correction, and recording it as one would fill
        # somebody's history with changes they never made.
        return ExplicitInputOutcome(
            profile_id=profile_id,
            fact_type=fact_type.value,
            action=ExplicitInputAction.UNCHANGED,
            fact_id=current_id,
        )
    correction = correct_profile_fact(
        connection,
        profile_id,
        current_id,
        value=canonical_value,
        provenance=_provenance(fact_type),
    )
    return ExplicitInputOutcome(
        profile_id=profile_id,
        fact_type=fact_type.value,
        action=ExplicitInputAction.CORRECTED,
        fact_id=correction.replacement.id,
        previous_fact_id=correction.corrected.id,
    )


def set_profile_availability(
    connection: sqlite3.Connection,
    profile_id: int,
    availability: AvailabilityPreference,
) -> ExplicitInputOutcome:
    """Record when this person said they can start. Never a computed date."""
    if not isinstance(availability, AvailabilityPreference):
        raise TypeError("availability must be an AvailabilityPreference")
    return _record_singleton(
        connection,
        profile_id,
        ProfileFactType.AVAILABILITY,
        encode_availability(availability),
    )


def set_profile_mobility(
    connection: sqlite3.Connection, profile_id: int, mobility: MobilityPreference
) -> ExplicitInputOutcome:
    """Record where this person said they will go. Never a geocoded address."""
    if not isinstance(mobility, MobilityPreference):
        raise TypeError("mobility must be a MobilityPreference")
    return _record_singleton(
        connection, profile_id, ProfileFactType.MOBILITY, encode_mobility(mobility)
    )


def set_profile_preferences(
    connection: sqlite3.Connection,
    profile_id: int,
    preferences: OpportunityPreferences,
) -> ExplicitInputOutcome:
    """Record what this person said they want. Never a domain read off a CV."""
    if not isinstance(preferences, OpportunityPreferences):
        raise TypeError("preferences must be an OpportunityPreferences")
    return _record_singleton(
        connection,
        profile_id,
        ProfileFactType.PREFERENCE,
        encode_preferences(preferences),
    )


def set_profile_career_objectives(
    connection: sqlite3.Connection, profile_id: int, objectives: CareerObjectives
) -> ExplicitInputOutcome:
    """Record what this person said they aim for. Never a CV's job title."""
    if not isinstance(objectives, CareerObjectives):
        raise TypeError("objectives must be a CareerObjectives")
    return _record_singleton(
        connection,
        profile_id,
        ProfileFactType.CAREER_OBJECTIVE,
        encode_career_objectives(objectives),
    )
