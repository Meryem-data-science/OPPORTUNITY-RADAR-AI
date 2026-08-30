"""The pure reading: one posting in, one set of constraints out.

Nothing in this module opens a database, issues a query, reads a clock or
consults the network. It is handed an `OpportunitySource` — the already
collected values, and only those — and returns an `ExtractedConstraints`. That
is what makes the extraction testable in memory against real postings without
writing anything anywhere, and what makes the fingerprint below meaningful:
the inputs are exactly the fields of that value object, so "did the source
change" is a decidable question rather than a guess.

Two decisions live here rather than in the rules.

One relation is applied before a disagreement is declared, and only one: the
four specific internship kinds are `INTERNSHIP` said more precisely, so a
posting naming both keeps the precise one. See `SPECIFIC_INTERNSHIP_TYPES`;
two specific kinds still conflict.

**Conflict resolution, which is mostly a refusal to resolve.** Rules produce
hits; hits land in slots; a slot holding two different values is a
contradiction in the posting, and a contradiction is recorded rather than
settled. The unit is the **slot**, not the kind: a posting can disagree with
itself about how much experience it wants and, separately, about whether it
insists, and those are two contradictions with two answers rather than one row
mixing `36-` with `REQUIRED`. Picking the first match, the last match or the longest one would be
this package deciding what an employer meant, and the field would then read as
a fact. It stays UNKNOWN and a conflict row says which values disagreed and
which rules produced them.

The one exception is deliberate and follows a precedent already in this
repository: for the opportunity type, the title outranks the description,
exactly as the Phase 2 classifier decides it. A posting titled "PFE Data
Engineer" whose body says "6-month internship" is describing one thing at two
grains, not contradicting itself. Fields are tried in
`TYPE_FIELD_PRECEDENCE` order and the first that says anything settles it;
inside one field, two different answers are a real conflict.

**The fingerprint**, which is what makes synchronization idempotent. It is a
SHA-256 over canonical JSON of the source fields, sorted by key, with no
clock, no row id, no counter and no dict-ordering dependence in it. Two
processes reading the same posting compute the same digest, and a posting
whose description changed computes a different one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from services.collector.extractors.opportunity_constraints.models import (
    EXTRACTOR_VERSION,
    ConstraintConflict,
    ConstraintKind,
    ConventionRequirement,
    DurationRequirement,
    EducationRequirement,
    ExtractedConstraints,
    OpportunitySource,
    OpportunityType,
    Slot,
    SourceField,
    more_specific_internship,
    StartRequirement,
    VisaSponsorship,
    WorkAuthorization,
    WorkMode,
)
from services.collector.extractors.opportunity_constraints.rules import (
    TYPE_FIELD_PRECEDENCE,
    RuleHit,
    read_posting,
)

__all__ = [
    "FINGERPRINT_FIELDS",
    "extract_opportunity_constraints",
    "source_fingerprint",
]

#: Exactly the fields the extractor reads, in the order they are documented.
#: The fingerprint covers these and nothing else, so adding an input to the
#: extractor without adding it here would let a changed source look unchanged.
#: A test compares this tuple to `OpportunitySource`'s own fields.
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "canonical_title",
    "country",
    "description",
    "location",
    "qualification_type",
    "remote_type",
)


def source_fingerprint(source: OpportunitySource) -> str:
    """The SHA-256 of the canonical JSON of everything the extractor reads.

    Canonical means sorted keys and no insignificant whitespace, so the digest
    depends on the values and not on how a dict happened to be built. It
    deliberately excludes `opportunity_id`: the identity of the row is not part
    of what was read, and including it would make two postings with identical
    text look like different sources.
    """
    payload = {name: getattr(source, name) for name in FINGERPRINT_FIELDS}
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _by_slot(hits: Sequence[RuleHit], slot: Slot) -> tuple[RuleHit, ...]:
    return tuple(hit for hit in hits if hit.slot is slot)


def _settle(hits: Sequence[RuleHit], slot: Slot):
    """One slot's value, or None plus a conflict when the posting disagrees.

    Returns `(value, kept_hits, conflict)`. `kept_hits` are the pieces of
    evidence worth storing: the ones that supported the value that was
    asserted, or — when nothing was — every hit that took part in the
    disagreement, because a conflict a reader cannot inspect is not auditable.
    """
    relevant = _by_slot(hits, slot)
    if not relevant:
        return None, (), None
    distinct = {hit.value_key: hit.value for hit in relevant}
    if len(distinct) == 1:
        return relevant[0].value, relevant, None
    conflict = ConstraintConflict(
        slot=slot,
        values=tuple(sorted(distinct)),
        rule_ids=tuple(sorted({hit.rule_id for hit in relevant})),
    )
    return None, relevant, conflict


def _settle_opportunity_type(hits: Sequence[RuleHit]):
    """The type, decided field by field in precedence order. See the docstring."""
    relevant = _by_slot(hits, Slot.OPPORTUNITY_TYPE)
    if not relevant:
        return None, (), None
    for source_field in TYPE_FIELD_PRECEDENCE:
        tier = tuple(hit for hit in relevant if hit.source_field is source_field)
        if not tier:
            continue
        distinct = {hit.value_key: hit.value for hit in tier}
        if len(distinct) == 1:
            return tier[0].value, tier, None
        # A posting calling itself a `stage` and, elsewhere, a `PFE` is not
        # disagreeing with itself: the second name is the first one said more
        # precisely. Only that one relation is applied, and only when exactly
        # one specific kind is in play — two of them is a real disagreement.
        specific = more_specific_internship(set(distinct.values()))
        if specific is not None:
            # The evidence kept is the evidence for what was asserted, so an
            # auditor never reads a row whose value the projection does not
            # hold.
            return specific, tuple(hit for hit in tier if hit.value is specific), None
        return None, tier, ConstraintConflict(
            slot=Slot.OPPORTUNITY_TYPE,
            values=tuple(sorted(distinct)),
            rule_ids=tuple(sorted({hit.rule_id for hit in tier})),
        )
    return None, (), None


def _multi_valued(hits: Sequence[RuleHit], slot: Slot):
    """Every distinct value one multi-valued slot holds, and its evidence.

    Several education levels or several places are several answers, never a
    disagreement, so nothing is settled here and no conflict can arise. The
    deduplication is exact and keeps the first occurrence, and it drops the
    losing hit with its value — otherwise a posting whose `location` and
    `country` were written identically would project one place and explain it
    twice.
    """
    values: list[object] = []
    kept: list[RuleHit] = []
    seen: set[str] = set()
    for hit in _by_slot(hits, slot):
        if hit.value_key in seen:
            continue
        seen.add(hit.value_key)
        values.append(hit.value)
        kept.append(hit)
    return tuple(values), tuple(kept)


def extract_opportunity_constraints(
    source: OpportunitySource,
) -> ExtractedConstraints:
    """Read one posting. Deterministic, offline, and silent where unsure.

    Every field it cannot justify with a named rule and an explicit fragment
    comes back UNKNOWN. That is the whole contract: this package would rather
    say nothing than say something a posting did not.
    """
    if not isinstance(source, OpportunitySource):
        raise TypeError("source must be an OpportunitySource")

    hits = read_posting(source)
    kept: list[RuleHit] = []
    conflicts: list[ConstraintConflict] = []

    def settle(slot: Slot):
        value, evidence, conflict = _settle(hits, slot)
        kept.extend(evidence)
        if conflict is not None:
            conflicts.append(conflict)
        return value

    opportunity_type, type_evidence, type_conflict = _settle_opportunity_type(hits)
    kept.extend(type_evidence)
    if type_conflict is not None:
        conflicts.append(type_conflict)

    education, education_evidence = _multi_valued(hits, Slot.EDUCATION)
    kept.extend(education_evidence)
    locations, location_evidence = _multi_valued(hits, Slot.LOCATION)
    kept.extend(location_evidence)

    experience, experience_evidence = _multi_valued(hits, Slot.EXPERIENCE)
    kept.extend(experience_evidence)

    duration_bounds = settle(Slot.DURATION)
    duration = DurationRequirement(
        min_months=None if duration_bounds is None else duration_bounds[0],
        max_months=None if duration_bounds is None else duration_bounds[1],
    )

    start = settle(Slot.START) or StartRequirement()
    work_mode = settle(Slot.WORK_MODE)
    sponsorship = settle(Slot.VISA_SPONSORSHIP)
    authorization = settle(Slot.WORK_AUTHORIZATION)
    convention = settle(Slot.CONVENTION)

    return ExtractedConstraints(
        opportunity_id=source.opportunity_id,
        source_fingerprint=source_fingerprint(source),
        extractor_version=EXTRACTOR_VERSION,
        opportunity_type=opportunity_type,
        education=education,
        experience=tuple(experience),
        duration=duration,
        start=start,
        locations=tuple(str(value) for value in locations),
        work_mode=work_mode,
        visa_sponsorship=sponsorship or VisaSponsorship.UNKNOWN,
        work_authorization=authorization or WorkAuthorization.UNKNOWN,
        convention=convention or ConventionRequirement.UNKNOWN,
        evidence=tuple(hit.evidence() for hit in kept),
        conflicts=tuple(sorted(conflicts, key=lambda item: item.slot.value)),
    )
