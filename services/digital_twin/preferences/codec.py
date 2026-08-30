"""Canonical JSON for the four explicit-input fact values, both ways.

A fact's `value` is text, and this module decides exactly which text. It is
canonical in the strict sense: **one statement has one encoding**, byte for
byte. Object keys are sorted, there is no insignificant whitespace, the closed
registries are written in their declaration order, and free-text lists keep the
person's order after an exact first-occurrence dedup. Nothing here reads a
clock, a counter or a row id, so encoding the same statement twice in two
processes produces the same bytes.

That property is what the service is built on. "The person restated what they
already said" is decided by comparing the canonical encoding of the new input
with the stored fact's `value` — a string comparison, not a field-by-field
diff, and not a fuzzy one. Without canonicity the same statement typed in a
different order would look like a correction, and the person's history would
fill up with changes nobody made.

Decoding is strict in the other direction, and deliberately unhelpful. A value
is accepted only if it is a JSON object with exactly the expected keys, holding
exactly the expected types, whose own re-encoding is byte-identical to what was
read. Anything else — an unknown key, a missing key, a null where a list
belongs, a legal-JSON-but-not-canonical spelling — is refused by
`ExplicitProfileInputError` rather than repaired. There is no "best effort"
reading of a preference: a preference nobody can read exactly is not a
preference, and guessing at one would put words in somebody's mouth.
"""

from __future__ import annotations

import json
from typing import Any

from services.digital_twin.preferences.models import (
    AvailabilityPreference,
    AvailabilityStatus,
    CareerObjectives,
    ConventionStatus,
    ExplicitProfileInputError,
    MobilityPreference,
    MobilityScope,
    OpportunityPreferences,
    OpportunityType,
    VisaSponsorshipRequired,
    WorkMode,
)

__all__ = [
    "canonical_json",
    "decode_availability",
    "decode_career_objectives",
    "decode_mobility",
    "decode_preferences",
    "encode_availability",
    "encode_career_objectives",
    "encode_mobility",
    "encode_preferences",
]


def canonical_json(payload: Any) -> str:
    """The one text form of a payload: sorted keys, no padding, real unicode.

    `ensure_ascii=False` keeps accented words as themselves rather than as
    `\\uXXXX` escapes — the text belongs to the person who typed it, and an
    escaped copy of it is harder to read back and no safer. It stays canonical
    because Python's encoder escapes nothing else and orders nothing else.
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _loaded_object(value: str, expected_keys: frozenset[str], label: str) -> dict[str, Any]:
    """Parse one fact value into a JSON object holding exactly those keys."""
    if not isinstance(value, str):
        raise ExplicitProfileInputError(f"{label} must be text")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ExplicitProfileInputError(f"{label} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ExplicitProfileInputError(f"{label} must be a JSON object")
    keys = frozenset(payload)
    if keys != expected_keys:
        missing = ", ".join(sorted(expected_keys - keys)) or "none"
        unknown = ", ".join(sorted(keys - expected_keys)) or "none"
        raise ExplicitProfileInputError(
            f"{label} has the wrong keys (missing: {missing}; unknown: {unknown})"
        )
    return payload


def _text_list(payload: dict[str, Any], key: str, label: str) -> list[str]:
    """One JSON array of strings, refused rather than coerced."""
    values = payload[key]
    if not isinstance(values, list):
        raise ExplicitProfileInputError(f"{label}.{key} must be a JSON array")
    for entry in values:
        if not isinstance(entry, str):
            raise ExplicitProfileInputError(f"{label}.{key} must hold text entries")
    return values


def _canonical(decoded: Any, encode, value: str, label: str):
    """Refuse a value whose own re-encoding is not byte-identical to it.

    This is the check that makes "unknown form" mean something precise. A value
    that parses, holds the right keys and the right types, but was written with
    a space after a colon, with its keys in another order, or with a duplicate
    the normalization would have dropped, is **not** what this package writes,
    so something else wrote it — and reading it as if this package had would
    quietly re-canonicalize somebody's statement on the next synchronization.
    """
    if encode(decoded) != value:
        raise ExplicitProfileInputError(f"{label} is not canonical")
    return decoded


_AVAILABILITY_KEYS = frozenset({"status", "available_from"})
_MOBILITY_KEYS = frozenset({"scope", "locations"})
_PREFERENCE_KEYS = frozenset(
    {
        "opportunity_types",
        "work_modes",
        "preferred_domains",
        "convention_status",
        "visa_sponsorship_required",
        "constraints",
    }
)
_CAREER_OBJECTIVE_KEYS = frozenset({"objectives"})


def encode_availability(availability: AvailabilityPreference) -> str:
    """`{"available_from":null,"status":"AVAILABLE_NOW"}` and its dated form."""
    if not isinstance(availability, AvailabilityPreference):
        raise ExplicitProfileInputError("availability must be an AvailabilityPreference")
    return canonical_json(
        {
            "status": availability.status.value,
            "available_from": availability.available_from,
        }
    )


def decode_availability(value: str) -> AvailabilityPreference:
    """Read one AVAILABILITY fact value. Refuses anything else."""
    payload = _loaded_object(value, _AVAILABILITY_KEYS, "availability")
    status = payload["status"]
    available_from = payload["available_from"]
    if not isinstance(status, str):
        raise ExplicitProfileInputError("availability.status must be text")
    if available_from is not None and not isinstance(available_from, str):
        raise ExplicitProfileInputError("availability.available_from must be text or null")
    decoded = AvailabilityPreference(
        status=AvailabilityStatus(status)
        if status in tuple(member.value for member in AvailabilityStatus)
        else status,
        available_from=available_from,
    )
    return _canonical(decoded, encode_availability, value, "availability")


def encode_mobility(mobility: MobilityPreference) -> str:
    """`{"locations":[...],"scope":"OPEN"}`, the locations in the typed order."""
    if not isinstance(mobility, MobilityPreference):
        raise ExplicitProfileInputError("mobility must be a MobilityPreference")
    return canonical_json(
        {"scope": mobility.scope.value, "locations": list(mobility.locations)}
    )


def decode_mobility(value: str) -> MobilityPreference:
    """Read one MOBILITY fact value. Refuses anything else."""
    payload = _loaded_object(value, _MOBILITY_KEYS, "mobility")
    scope = payload["scope"]
    if not isinstance(scope, str):
        raise ExplicitProfileInputError("mobility.scope must be text")
    decoded = MobilityPreference(
        scope=MobilityScope(scope)
        if scope in tuple(member.value for member in MobilityScope)
        else scope,
        locations=tuple(_text_list(payload, "locations", "mobility")),
    )
    return _canonical(decoded, encode_mobility, value, "mobility")


def encode_preferences(preferences: OpportunityPreferences) -> str:
    """The six-field preference object, registries in declaration order."""
    if not isinstance(preferences, OpportunityPreferences):
        raise ExplicitProfileInputError("preferences must be an OpportunityPreferences")
    return canonical_json(
        {
            "opportunity_types": [
                member.value for member in preferences.opportunity_types
            ],
            "work_modes": [member.value for member in preferences.work_modes],
            "preferred_domains": list(preferences.preferred_domains),
            "convention_status": preferences.convention_status.value,
            "visa_sponsorship_required": preferences.visa_sponsorship_required.value,
            "constraints": list(preferences.constraints),
        }
    )


def decode_preferences(value: str) -> OpportunityPreferences:
    """Read one PREFERENCE fact value. Refuses anything else."""
    payload = _loaded_object(value, _PREFERENCE_KEYS, "preferences")
    decoded = OpportunityPreferences(
        opportunity_types=tuple(
            _registry_list(payload, "opportunity_types", OpportunityType)
        ),
        work_modes=tuple(_registry_list(payload, "work_modes", WorkMode)),
        preferred_domains=tuple(_text_list(payload, "preferred_domains", "preferences")),
        convention_status=_choice(payload, "convention_status", ConventionStatus),
        visa_sponsorship_required=_choice(
            payload, "visa_sponsorship_required", VisaSponsorshipRequired
        ),
        constraints=tuple(_text_list(payload, "constraints", "preferences")),
    )
    return _canonical(decoded, encode_preferences, value, "preferences")


def _registry_list(payload: dict[str, Any], key: str, registry) -> list[Any]:
    """One JSON array read against a closed registry, unknown members refused."""
    values = _text_list(payload, key, "preferences")
    known = frozenset(member.value for member in registry)
    unknown = [entry for entry in values if entry not in known]
    if unknown:
        raise ExplicitProfileInputError(
            f"preferences.{key} names {len(unknown)} value(s) outside the registry"
        )
    return [registry(entry) for entry in values]


def _choice(payload: dict[str, Any], key: str, registry) -> Any:
    """One closed-registry field, refused rather than defaulted when unknown.

    A missing answer is spelled `UNKNOWN` and must be written; it is never
    supplied here, because a decoder silently defaulting a field would turn a
    corrupt value into a plausible one.
    """
    value = payload[key]
    if not isinstance(value, str):
        raise ExplicitProfileInputError(f"preferences.{key} must be text")
    try:
        return registry(value)
    except ValueError as error:
        raise ExplicitProfileInputError(
            f"preferences.{key} is outside the registry"
        ) from error


def encode_career_objectives(objectives: CareerObjectives) -> str:
    """`{"objectives":[...]}`, in the order the person wrote them."""
    if not isinstance(objectives, CareerObjectives):
        raise ExplicitProfileInputError("objectives must be a CareerObjectives")
    return canonical_json({"objectives": list(objectives.objectives)})


def decode_career_objectives(value: str) -> CareerObjectives:
    """Read one CAREER_OBJECTIVE fact value. Refuses anything else."""
    payload = _loaded_object(value, _CAREER_OBJECTIVE_KEYS, "objectives")
    decoded = CareerObjectives(
        objectives=tuple(_text_list(payload, "objectives", "objectives"))
    )
    return _canonical(decoded, encode_career_objectives, value, "objectives")
