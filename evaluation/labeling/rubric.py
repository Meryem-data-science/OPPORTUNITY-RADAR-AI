"""The frozen semantic contract of `human-relevance-v1`.

This module states what a human relevance grade *means*, once and for good. It
is the outcome of the real calibration round run under
`human-relevance-calibration-v0`, and it is the definition every later Phase 10
slice must measure against.

**It is a contract, not a machine.** Nothing here reads a dataset, a label file
or a pipeline verdict; nothing here assigns a grade to anything. The predicates
below exist so that the contract can be *stated precisely and tested* rather
than only described in prose — a rubric whose central rule ("an unknown is not a
contradiction") is written only in English is a rubric that will be violated by
the first person who implements against it in a hurry.

---

## The two versions, and why both exist

`HUMAN_LABEL_PROTOCOL_VERSION` — `human-relevance-calibration-v0` — is the
protocol the label **writer** records under, and the only one the label reader
interprets. Every real label on the operator's machine carries it.

`FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION` — `human-relevance-v1` — is the
**semantic** contract frozen here. No label carries it, because none has been
made under it: freezing a definition and producing judgements under it are two
different acts, and this slice performs only the first.

They are deliberately *not* unified in this slice, and the reason is concrete.
The existing labels are an audit trail. Switching the writer to v1 would either
rewrite their recorded protocol — destroying the record of what question was
actually answered — or append v1 rows into a file of v0 rows, producing one
history holding judgements of two different rubrics with nothing to tell them
apart. `SUPPORTED_PROTOCOL_VERSIONS` therefore still contains only v0, so a v1
row is refused on read rather than mixed in, and a real v1 benchmark will need a
labelset explicitly separated from the calibration history. Doing that
separation properly is a storage question, and it is not this slice's.

## What v1 changed, and what it did not

The **scale is unchanged**: the same four integers, the same four names, the
same absolute rule that an unjudged opportunity has no label and is never a `0`.
A dataset labelled under v0 and one labelled under v1 use the same alphabet.

What the calibration sharpened is the *question*. Under v0 the rubric asked, in
effect, how close a posting looked to Data/AI. That is not the question worth
measuring. v1 asks whether the opportunity is **actually actionable for the
profile and preferences the dataset is frozen against** — and lexical proximity
to Data/AI is evidence toward that, never a substitute for it.

The second sharpening is **hard constraints**. Some requirements are not
tradeable: a posting that explicitly contradicts one is out of target however
well it matches on everything else. Which requirements those are is declared by
the profile and preferences behind `profile_context_fingerprint` — not by this
module, which names *kinds* of constraint and never a country, a city, a level
or a language. A protocol that hard-coded one profile's geography would be a
protocol for one person, and the digest that binds a label to a profile context
would be describing something the rubric had already assumed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .schema import (
    HUMAN_LABEL_PROTOCOL_VERSION,
    RELEVANCE_GRADES,
    RELEVANCE_GRADE_NAMES,
)

__all__ = [
    "CALIBRATION_V0_PROVENANCE",
    "FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION",
    "FROZEN_RELEVANCE_RUBRIC",
    "UNKNOWN_IS_NOT_FALSE",
    "CalibrationProvenance",
    "ConstraintEvidence",
    "FrozenRubricGrade",
    "HardConstraint",
    "HardConstraintKind",
    "frozen_rubric_grade",
    "hard_contradictions",
    "out_of_target_is_established",
]

#: The frozen semantic contract. Distinct from the writer's protocol version on
#: purpose — see this module's docstring. Nothing writes this string into a
#: label file, and `SUPPORTED_PROTOCOL_VERSIONS` deliberately does not contain
#: it: no judgement has been made under it yet.
FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION = "human-relevance-v1"


# --------------------------------------------------------------------------
# hard constraints
# --------------------------------------------------------------------------


class HardConstraintKind(StrEnum):
    """The *kinds* of requirement a profile can declare as non-tradeable.

    Kinds, never values. `TARGET_GEOGRAPHY` says "where the work is, is a hard
    requirement for this profile"; it does not say which places qualify. The
    places come from the profile and preferences the dataset is bound to, and
    that binding is already in every label through `profile_context_fingerprint`.

    A profile for which a given kind is *not* hard simply does not declare it,
    and evidence on that dimension then affects the grade the way any other
    evidence does — it can pull a judgement from 3 to 2 or to 1, but it cannot
    by itself make an opportunity out of target.
    """

    #: Where the work is, or is not: country, region, on-site city, whether
    #: remote from elsewhere is acceptable.
    TARGET_GEOGRAPHY = "TARGET_GEOGRAPHY"
    #: Seniority and the kind of position — a target that is strictly
    #: PFE / internship / junior declares this.
    TARGET_LEVEL = "TARGET_LEVEL"
    #: Right to work, visa, clearance, residency.
    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    #: The subject of the work itself, for a target defined by a domain such as
    #: Data/AI.
    TARGET_DOMAIN = "TARGET_DOMAIN"
    #: Anything else a profile declares as non-tradeable — a language, a start
    #: date, a contract type. Named so the vocabulary does not have to be
    #: reopened for every profile.
    OTHER_PROFILE_CONSTRAINT = "OTHER_PROFILE_CONSTRAINT"


class ConstraintEvidence(StrEnum):
    """What the frozen evidence establishes about one hard constraint.

    Three states, and keeping the third distinct from the first is the single
    most important property in this module.

    * `SATISFIED` — the posting states something that meets the requirement;
    * `CONTRADICTED` — the posting states something that **explicitly** cannot
      meet it;
    * `UNKNOWN` — the posting does not say. Not "probably fine", not "probably
      not": it does not say.

    An `UNKNOWN` is a reason for a lower grade when the rest of the evidence is
    thin, and it is never, on its own, a contradiction.
    """

    SATISFIED = "SATISFIED"
    CONTRADICTED = "CONTRADICTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class HardConstraint:
    """One non-tradeable requirement, as the profile declares it.

    `statement` is prose because it is written by whoever configured the
    evaluation, for whoever annotates it. The protocol needs to know that a
    constraint of this kind is in force; it does not need to parse its content,
    and a protocol that tried to would be a rules engine.
    """

    kind: HardConstraintKind
    statement: str


def hard_contradictions(
    evidence: Mapping[HardConstraintKind, ConstraintEvidence],
) -> tuple[HardConstraintKind, ...]:
    """The hard constraints the evidence **explicitly** contradicts, in order.

    The whole of the rule, and the whole of its restraint: only `CONTRADICTED`
    counts. A dimension the posting says nothing about does not appear in the
    result, however many such dimensions there are, because "we do not know" is
    not a finding and a pile of unknowns is not a finding either.
    """
    return tuple(
        kind
        for kind in HardConstraintKind
        if evidence.get(kind) is ConstraintEvidence.CONTRADICTED
    )


def out_of_target_is_established(
    evidence: Mapping[HardConstraintKind, ConstraintEvidence],
) -> bool:
    """Is the rubric's grade-0 condition met by this evidence?

    **This does not assign a grade.** It answers one question about the contract
    — whether at least one declared hard constraint is explicitly contradicted —
    so that the rule can be tested rather than only asserted. The grade is
    recorded by the person who judged the posting, and nothing in the labelling
    flow calls this function to derive one: a rubric predicate that started
    deciding labels would be an auto-grader wearing a contract's clothes, and
    the whole point of Phase 10 is that the human judgement is independent of
    anything the machine concluded.

    Note the asymmetry, which is deliberate. A contradiction *establishes* that
    the grade-0 condition is met; the absence of one establishes nothing, and
    certainly not that the opportunity is relevant. Grades 1, 2 and 3 are
    positive judgements that require positive evidence.
    """
    return bool(hard_contradictions(evidence))


#: The unknown-is-not-false invariant, written out case by case because this is
#: exactly the rule that erodes when somebody implements against the prose. Each
#: pair is (what is absent, what it must not be read as).
UNKNOWN_IS_NOT_FALSE: tuple[tuple[str, str], ...] = (
    ("a posting with no description", "an out-of-target posting"),
    ("a posting stating no level", "a senior posting"),
    ("a posting stating no country", "a posting outside the target geography"),
    ("a posting stating no opportunity type", "a posting of the wrong type"),
    ("evidence that is absent", "evidence that contradicts"),
    ("an opportunity nobody judged", "an opportunity graded 0"),
)


# --------------------------------------------------------------------------
# the frozen rubric
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenRubricGrade:
    """One grade of the frozen v1 rubric: what it is, and what it requires."""

    grade: int
    name: str
    definition: str
    #: What must be positively established for this grade to be defensible.
    requires: tuple[str, ...]
    #: What may remain open at this grade without disqualifying it.
    tolerates: tuple[str, ...]


FROZEN_RELEVANCE_RUBRIC: tuple[FrozenRubricGrade, ...] = (
    FrozenRubricGrade(
        grade=3,
        name="VERY_RELEVANT",
        definition=(
            "Clearly very well suited to the profile and the target. An "
            "opportunity you would want surfaced first."
        ),
        requires=(
            "the work is in a relevant Data/AI domain",
            "the level or kind of position is compatible with the target "
            "(PFE / internship / junior) where that constraint applies",
            "geography and actionability are compatible with the profile's "
            "declared constraints",
            "no known hard contradiction",
        ),
        tolerates=(),
    ),
    FrozenRubricGrade(
        grade=2,
        name="RELEVANT",
        definition=(
            "Relevant and reasonably actionable. Enough positive evidence to "
            "want the opportunity surfaced."
        ),
        requires=(
            "enough positive evidence to want the opportunity surfaced",
            "no known hard contradiction",
        ),
        tolerates=(
            "some unknowns",
            "non-blocking reservations",
            "incomplete evidence on a secondary dimension",
        ),
    ),
    FrozenRubricGrade(
        grade=1,
        name="WEAKLY_RELEVANT",
        definition=(
            "A real but weak, partial or borderline link — an adjacent domain, "
            "a level that may be too high without being stated, Data/AI only "
            "partly demonstrated, or too much uncertainty for a strong "
            "recommendation."
        ),
        requires=(
            "a real but partial link to the target",
            "no explicitly established hard contradiction, which would impose 0",
        ),
        tolerates=(
            "an adjacent rather than core domain",
            "a level that may be too high but is not stated",
            "Data/AI only partly demonstrated",
            "uncertainty too great for a strong recommendation",
        ),
    ),
    FrozenRubricGrade(
        grade=0,
        name="OUT_OF_TARGET",
        definition=(
            "A human determined the opportunity is outside the actionable "
            "target. Typically because the posting explicitly contradicts a "
            "hard constraint declared by the profile and preferences the "
            "dataset is bound to. Never a default, and never the value of an "
            "opportunity nobody judged."
        ),
        requires=(
            "a human judgement that the opportunity is outside the actionable "
            "target",
        ),
        tolerates=(),
    ),
)


def frozen_rubric_grade(grade: int) -> FrozenRubricGrade:
    """One grade of the frozen rubric, by its integer."""
    for entry in FROZEN_RELEVANCE_RUBRIC:
        if entry.grade == grade:
            return entry
    raise KeyError(grade)


#: The frozen rubric uses the same scale as the calibration one: same integers,
#: same names. Stated as an assertion at import time because a silent divergence
#: between the two would make every calibration label unreadable under v1 for a
#: reason nobody would think to look for.
assert tuple(entry.grade for entry in FROZEN_RELEVANCE_RUBRIC) == tuple(
    sorted(RELEVANCE_GRADES, reverse=True)
)
assert {entry.grade: entry.name for entry in FROZEN_RELEVANCE_RUBRIC} == dict(
    RELEVANCE_GRADE_NAMES
)


# --------------------------------------------------------------------------
# where the frozen contract came from
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationProvenance:
    """The calibration round a frozen protocol was derived from.

    Digests and counts only. No opportunity, no judgement and no personal detail
    is recorded here: this says *which* round produced the definition, so that
    the freeze can be traced, and nothing about what was in it.
    """

    protocol_version: str
    dataset_id: str
    dataset_content_fingerprint: str
    selection_fingerprint: str
    labelset_fingerprint: str
    judged_count: int
    unjudged_count: int
    recorded_label_rows: int
    grade_distribution: Mapping[str, int]
    #: How the round was conducted. Recorded because the answer changes what the
    #: round can be used to claim.
    method: str
    #: What this round is explicitly *not*, stated so nobody has to infer it.
    not_a: tuple[str, ...]


#: The real calibration round that produced `human-relevance-v1`.
#:
#: It ran on the operator's machine, against the frozen dataset and the labels
#: under `data/`, neither of which exists in any development container. The
#: values below are the digests and counts that round reported and are recorded
#: here as provenance — they are **not** a verification performed here, and no
#: code in this repository has read those labels.
#:
#: The round was **AI-assisted with final human validation**: a model proposed a
#: reading of each posting and the operator decided the grade. That is a
#: legitimate way to find out whether a rubric is usable — it is what surfaced
#: the actionability rule and the hard-constraint rule frozen above — and it is
#: emphatically not an independent human benchmark. A holdout intended to
#: *evaluate* the pipeline must not let a model propose the grade before the
#: person forms one, or the two are no longer independent and the measurement
#: is of the model's agreement with itself.
CALIBRATION_V0_PROVENANCE = CalibrationProvenance(
    protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
    dataset_id="evaluation-dataset-v3-8ed3d8fa9359f24c",
    dataset_content_fingerprint=(
        "8ed3d8fa9359f24c044c71057fa1dd5909ebf02eab11ad114679ebf27e0bc04e"
    ),
    selection_fingerprint=(
        "a467243e5629e9238e3c525378fb7008c567bb17f84b2bc8a0d9621114b6d478"
    ),
    labelset_fingerprint=(
        "721f362aad8d2d22090f253554e76f911388c5663700f578af4321168e8a501a"
    ),
    judged_count=12,
    unjudged_count=0,
    # Thirteen rows for twelve judgements: one opportunity was explicitly
    # relabelled at revision 2 after the rubric was clarified, and both rows
    # remain in the audit trail. That relabel is the calibration working.
    recorded_label_rows=13,
    grade_distribution={
        "OUT_OF_TARGET": 7,
        "WEAKLY_RELEVANT": 2,
        "RELEVANT": 3,
        "VERY_RELEVANT": 0,
    },
    method="AI-assisted reading with final human validation of every grade",
    not_a=(
        "an independent human benchmark",
        "an inter-annotator agreement study",
        "an independent gold-standard holdout",
    ),
)
