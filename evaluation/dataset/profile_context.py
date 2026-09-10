"""What the profile said, canonicalized so a dataset can be bound to it.

Three of the four downstream blocks in a record — eligibility, matching,
recommendation — are personalised, and the human labels a later slice attaches
to a dataset are judgements made **for one person, against one profile state**.
A dataset that did not record which state that was could be compared against a
label set produced under a different one, silently.

**No primitive in the repository fingerprints exactly this.** Each phase digests
its own input: `matching_input_fingerprint` spans a profile *and* an
opportunity, `eligibility_fingerprint` spans a profile *and* a posting's
demands, and the profile halves of both are private payload builders inside
those modules. So the canonicalization is here — and it is only a
canonicalization. No rule, no score, no weight, no threshold and no
interpretation: the three loaders below are the production readers, called
read-only, and this module serializes exactly what they return.

    services.collector.matching.inputs.load_profile_matching_input
        skills, experiences, projects, educations, stated opportunity
        preferences and career objectives — the profile side Matching scores,
        and the same preferences Recommendation reads back

    services.eligibility.inputs.load_profile_input
        the projected languages, the projected skill keys, the sponsorship need
        and the convention capability — the profile side every eligibility rule
        reads

    services.geography.profile_target.resolve_profile_target
        the country the person restricted themselves to, derived from their
        stated mobility — the profile side of every geographic verdict

**What is deliberately outside the domain.** Availability dates drive Priority,
which this dataset carries no signal from, so they are not here; a person's
name, contact details and links are not parameters of anything the dataset
records, and putting them in a digest would only copy personal data into an
artefact for no gain.

**The serialization follows the dataclasses rather than restating them.** The
three loaders return frozen dataclasses whose collections are already read under
an explicit `ORDER BY`, so their order is deterministic and is preserved. A
field added to any of those inputs later — a new personalisation signal — enters
this digest automatically, which is the property that matters for a *provenance*
fingerprint: it can never silently omit something the pipeline started reading.

Nothing here is copied into the manifest. Only the digest is, so the manifest
identifies a profile state without republishing it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from services.collector.matching.fingerprint import canonical_json
from services.collector.matching.inputs import (
    MatchingInputError,
    load_profile_matching_input,
)
from services.eligibility.inputs import EligibilityInputError, load_profile_input
from services.geography.profile_target import resolve_profile_target

from .schema import EvaluationDatasetError, EvaluationProfileContext

__all__ = [
    "canonical_profile_context_payload",
    "profile_context_fingerprint",
    "read_profile_context",
]

#: The version of *this* canonicalization: which loaders are consulted and how
#: their output is laid out. It is in the digest, so a later slice that widens
#: the domain cannot produce a colliding fingerprint from a different one.
PROFILE_CONTEXT_VERSION = "evaluation-profile-context-v1"


def _jsonable(value: Any) -> Any:
    """Frozen dataclasses, enums and tuples as plain JSON-serializable values.

    The same shape the project's read-only CLIs use to project a dataclass, kept
    here rather than imported because those live behind CLI entry points. Field
    order comes from the dataclass and is irrelevant: `canonical_json` sorts
    keys, so only the *values* and the order of genuine sequences matter.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Refused rather than coerced with `str()`: a value this function cannot
    # represent would otherwise enter the digest as its `repr`, which can carry
    # a memory address and would make the fingerprint non-deterministic.
    raise EvaluationDatasetError(
        f"cannot canonicalize a profile context value of type {type(value).__name__}"
    )


def canonical_profile_context_payload(
    connection, profile_id: int
) -> dict[str, Any]:
    """Read the three profile inputs and lay them out, changing nothing.

    Every call below is one of production's own read-only loaders, so "what the
    digest covers" and "what personalises the dataset" are the same set by
    construction rather than by discipline.
    """
    try:
        matching = load_profile_matching_input(connection, profile_id)
        eligibility = load_profile_input(connection, profile_id)
        target = resolve_profile_target(connection, profile_id)
    except (MatchingInputError, EligibilityInputError) as error:
        raise EvaluationDatasetError(
            f"cannot read the profile context of profile {profile_id}"
        ) from error
    payload = {
        "version": PROFILE_CONTEXT_VERSION,
        "matching_profile": _jsonable(matching),
        "eligibility_profile": _jsonable(eligibility),
        "geographic_target": _jsonable(target),
    }
    # `profile_id` names the row, not what the row said, and each loader already
    # carries it. It is dropped here and stated once, beside the digest, in
    # `EvaluationProfileContext` — where it is part of the dataset's identity
    # rather than part of what the person declared.
    for section in ("matching_profile", "eligibility_profile", "geographic_target"):
        payload[section].pop("profile_id", None)
    return payload


def profile_context_fingerprint(connection, profile_id: int) -> str:
    """SHA-256 of the canonical JSON of everything that personalises a dataset."""
    payload = canonical_profile_context_payload(connection, profile_id)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def read_profile_context(
    connection, profile_id: int, user_id: int
) -> EvaluationProfileContext:
    """The whole binding: whose dataset this is, and what their profile said."""
    return EvaluationProfileContext(
        profile_id=profile_id,
        user_id=user_id,
        fingerprint=profile_context_fingerprint(connection, profile_id),
    )
