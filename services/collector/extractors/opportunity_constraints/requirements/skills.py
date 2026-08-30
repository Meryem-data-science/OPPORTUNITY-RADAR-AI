"""The closed rules that read the skills a posting asks for.

One sentence at a time, and the same four questions each time: does this
sentence cancel a demand, does it state one locally, does its section state one,
and did it join several technologies into a choice? `signals.py` answers the
first three, `signals.term_runs` the fourth, and this module turns the answers
into observations and then into requirements.

**What is deliberately not extracted.** A technology named in a sentence that
demands nothing produces nothing:

    Our stack includes Python, Spark and Kafka.   -> nothing
    You will build pipelines using Python.        -> nothing
    We use SQL across the company.                -> nothing
    Training in Python will be provided.          -> nothing
    No prior Python experience is required.       -> nothing

Each of those sentences contains a technology and none of them requires one.
The first describes a company, the second a responsibility, the third a habit,
the fourth a promise, the fifth the absence of a demand. A rule that read any
of them as a requirement would produce a projection whose rows a human has to
disprove one by one, which is worse than a projection that is silent.

**REQUIRED beats PREFERRED, and the loser keeps its evidence.** A posting that
names Python under "Nice to have" and again under "Requirements" requires it.
That is not a contradiction — nothing here produces one — but both mentions are
things the posting said, so both become evidence, each carrying the level *it*
stated. An audit reading the preferred row therefore never finds it claiming to
have demanded anything.
"""

from __future__ import annotations

from collections.abc import Sequence

from services.collector.extractors.opportunity_constraints.models import (
    MAX_EVIDENCE_LENGTH,
)
from services.collector.extractors.opportunity_constraints.requirements.matcher import (
    SKILL_MATCHER,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    AmbiguityReason,
    RequirementAmbiguity,
    RequirementEvidence,
    RequirementKind,
    RequirementLevel,
    RequirementSegment,
    RequirementSourceField,
    SkillRequirement,
    stronger,
)
from services.collector.extractors.opportunity_constraints.requirements.signals import (
    segment_level,
    term_runs,
)
from services.collector.extractors.opportunity_constraints.requirements.skill_catalog import (
    SKILL_CATALOG,
)
from services.collector.extractors.opportunity_constraints.text import shorten_evidence

__all__ = ["ALTERNATIVE_GROUP_RULE_ID", "read_skill_requirements"]

#: The rule that refuses to turn a choice into a list of obligations.
ALTERNATIVE_GROUP_RULE_ID = "SKILL_ALTERNATIVE_GROUP_V1"

_NAMES = {term.canonical_key: term.canonical_name for term in SKILL_CATALOG}


def read_skill_requirements(
    segments: Sequence[RequirementSegment],
) -> tuple[tuple[SkillRequirement, ...], tuple[RequirementAmbiguity, ...]]:
    """Every skill the posting asks for, and every choice it refused to store.

    Requirements come back sorted by canonical key — a stable order that does
    not depend on where in the posting a technology happened to appear — and
    ambiguities in the order the posting produced them. Their `position` is
    local to this reading; `extractor.py` renumbers them once across skills and
    languages together, so one posting has one ambiguity sequence.
    """
    observations: dict[str, list[tuple[RequirementLevel, str, str, str | None]]] = {}
    refused: list[RequirementAmbiguity] = []

    for segment in segments:
        stated = segment_level(segment.text, segment.context)
        if stated is None:
            continue
        level, rule_id = stated
        matches = SKILL_MATCHER.find(segment.text)
        if not matches:
            continue
        fragment = shorten_evidence(segment.text, MAX_EVIDENCE_LENGTH)
        for run in term_runs([(item.start, item.end) for item in matches], segment.text):
            if run.alternative:
                refused.append(
                    RequirementAmbiguity(
                        position=len(refused),
                        kind=RequirementKind.SKILL,
                        reason=AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED,
                        rule_id=ALTERNATIVE_GROUP_RULE_ID,
                        text=fragment,
                        context_heading_text=segment.heading_text,
                    )
                )
                continue
            for index in run.indexes:
                key = matches[index].key
                observations.setdefault(key, []).append(
                    (level, rule_id, fragment, segment.heading_text)
                )

    requirements: list[SkillRequirement] = []
    for key in sorted(observations):
        seen = observations[key]
        level = seen[0][0]
        for observation in seen[1:]:
            level = stronger(level, observation[0])
        requirements.append(
            SkillRequirement(
                canonical_key=key,
                canonical_name=_NAMES[key],
                requirement=level,
                evidence=tuple(
                    RequirementEvidence(
                        position=position,
                        source_field=RequirementSourceField.DESCRIPTION,
                        observed_requirement=observed,
                        rule_id=rule_id,
                        text=fragment,
                        context_heading_text=heading,
                    )
                    for position, (observed, rule_id, fragment, heading) in enumerate(seen)
                ),
            )
        )
    return tuple(requirements), tuple(refused)
