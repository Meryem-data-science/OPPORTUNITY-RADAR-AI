"""The vocabulary of Phase 7B.1: a declared target set, and three verdicts.

Two questions live in this package and they are kept apart on purpose:

    *what kind of thing is this posting?*        a property of the posting
    *is that a kind I am looking for?*           a property of a person, today

The first already has an answer and an owner: `opportunity_constraints.
opportunity_type`, extracted by Phase 3.5A from the employer's own words and
stored there. **This package does not extract, re-read, re-classify or correct
it**, and it holds no type vocabulary of its own — `OpportunityType` below is
imported from `services/digital_twin/preferences/models.py`, the one closed
registry the profile side and the offer side already share. A third taxonomy is
exactly what would let the two sides disagree.

The second depends on a preference the person stated and can restate this
afternoon, so it is computed on demand and **stored nowhere**. Nothing below
carries a verdict column, a `profile_id + opportunity_id` pair, an
`is_target_type` flag or a score, and Phase 7B.1 adds no migration.

`UNKNOWN` is never `FALSE`, on either side. A posting whose type no closed rule
could read is a question about the posting, not a statement that it is
something else; and a profile that never said what it is looking for has not
said it is looking for nothing.

`TargetVerdict` here deliberately mirrors, and does not import,
`services.geography.models.TargetVerdict`. The two answer different questions
about different evidence, Phase 7A.1 is closed, and merging two closed
vocabularies is the business of the slice that first has to show both answers
at once — not of the one that adds the second.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.digital_twin.preferences.models import OpportunityType

#: The version of the whole type-targeting contract: the target resolver's
#: rules, the evaluator's rules, and the rule ids both emit. It labels audit
#: output so two runs of two versions cannot be compared by accident. Nothing
#: stores it, because nothing here stores anything.
TARGETING_VERSION = "opportunity-type-targeting-v1"

__all__ = [
    "OpportunityType",
    "ProfileTypeTarget",
    "TARGETING_VERSION",
    "TargetVerdict",
    "TypeTargetAssessment",
    "TypeTargetingError",
]


class TypeTargetingError(ValueError):
    """Raised when a targeting value is malformed. Nothing here ever writes."""


class TargetVerdict(StrEnum):
    """Whether a posting's structured type is one this profile is looking for.

    `MATCH`          the posting's type is one the profile explicitly named.
    `OUT_OF_TARGET`  the posting's type is known, and is not one of them.
    `UNKNOWN`        the posting's type is unknown, or the profile named none.

    There is no fourth answer and no number. A posting is not 60% a PFE, and a
    type nobody could read is not a near miss.

    Nothing is implicitly accepted. A profile targeting `PFE` and `INTERNSHIP`
    is not thereby targeting `ALTERNANCE`, `FIRST_JOB` or `JUNIOR_ROLE`, and no
    rule in this package widens a declared set to the values that look adjacent
    to it: the person named a set, and the set is what they named.
    """

    MATCH = "MATCH"
    OUT_OF_TARGET = "OUT_OF_TARGET"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProfileTypeTarget:
    """The kinds of opportunity a profile explicitly said it is looking for.

    `opportunity_types` is empty exactly when the target is UNKNOWN, and
    `rule_id` says which case it was. It is **derived**: `profile_preferences`
    keeps what the person stated, and this value is never written back — a
    derived set stored beside its source is a set that can disagree with it.

    Never inferred. Nothing here reads a CV, a diploma, a skill, a project, a
    career objective, an availability or a mobility to guess what somebody is
    looking for; having done an internship is not wanting another one.
    """

    profile_id: int
    opportunity_types: tuple[OpportunityType, ...]
    rule_id: str

    def __post_init__(self) -> None:
        if not self.rule_id.strip() or self.rule_id != self.rule_id.strip():
            raise TypeTargetingError("a target names one trimmed rule id")
        if len(set(self.opportunity_types)) != len(self.opportunity_types):
            raise TypeTargetingError("a target set names each type once")
        if any(
            not isinstance(value, OpportunityType) for value in self.opportunity_types
        ):
            raise TypeTargetingError(
                "a target set holds OpportunityType members of the shared registry"
            )

    @property
    def known(self) -> bool:
        return bool(self.opportunity_types)

    @property
    def count(self) -> int:
        return len(self.opportunity_types)


@dataclass(frozen=True)
class TypeTargetAssessment:
    """One posting judged against one declared target set, and why.

    Computed on demand and stored nowhere: the left-hand side of this
    comparison is a preference, and a preference changes. It carries no
    `opportunity_id` and no `profile_id` for the same reason the geographic
    assessment carries none — it is the answer to a comparison, not a row.
    """

    verdict: TargetVerdict
    rule_id: str
    #: What the posting's structured type was, or None when it had none. Copied
    #: from `opportunity_constraints`, never decided here.
    opportunity_type: OpportunityType | None
    target_type_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "rule_id": self.rule_id,
            "opportunity_type": (
                self.opportunity_type.value
                if self.opportunity_type is not None
                else "UNKNOWN"
            ),
            "target_type_count": self.target_type_count,
        }
