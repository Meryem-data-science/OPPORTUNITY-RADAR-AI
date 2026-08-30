"""The closed rules that read the skills a posting asks for.

One clause at a time, and the same four questions each time: does this clause
cancel a demand, does it state one locally, does its section state one, and did
it join several technologies into a choice? `signals.py` answers the first
three, `signals.term_runs` the fourth, and this module turns the answers into
observations and then into requirements.

**A clause, not a sentence**, and that distinction is `v2`. One sentence can
hold two demands of different strength — "Python required and Spark preferred",
"Python preferred and SQL required" — and scoring the markers over the whole
sentence collapsed both into one level, demoting a real requirement in the first
case. Worse, one skill's cancelling clause deleted a demand about another:
"No Python experience required, but SQL is required" produced nothing at all.
`signals.term_clauses` cuts the sentence at the connectors between terms, so
each technology is scored against its own words. Terms joined by a bare
connector stay in one clause, which is why "Python and SQL required" is still
two requirements.

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

**A slash is not an `or`, and it is not an `and` either.** `AI/ML engineering`
names one field; refusing it as a choice describes an offer the posting never
made, and storing both halves invents two obligations out of one noun phrase.
A closed registry names the compounds this happens to be true of, and every
other slash — `Python/R`, `TensorFlow/PyTorch`, `C/C++` — stays refused as a
choice. Either way, nothing is stored: see `_refusal`.

**REQUIRED beats PREFERRED, and the loser keeps its evidence.** This is about
one technology named **twice**, and it is a different question from the one
above: two mentions of Python resolve to the stronger; two different
technologies in one sentence keep their own levels. A posting that
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
    clause_level,
    term_clauses,
    term_runs,
)
from services.collector.extractors.opportunity_constraints.requirements.skill_catalog import (
    SKILL_CATALOG,
    is_compound_expression,
)
from services.collector.extractors.opportunity_constraints.text import shorten_evidence

__all__ = [
    "ALTERNATIVE_GROUP_RULE_ID",
    "COMPOUND_EXPRESSION_RULE_ID",
    "read_skill_requirements",
]

#: The rule that refuses to turn a choice into a list of obligations.
#:
#: `_V2` because its responsibility narrowed: it used to cover every bare slash
#: as well, and a stored `_V1` row could therefore be a compound expression
#: mislabelled as a choice the posting never offered.
ALTERNATIVE_GROUP_RULE_ID = "SKILL_ALTERNATIVE_GROUP_V2"

#: The rule that refuses to split a registered slashed expression in half —
#: and equally refuses to read it as a choice between the halves.
COMPOUND_EXPRESSION_RULE_ID = "SKILL_COMPOUND_EXPRESSION_UNSUPPORTED_V1"

_NAMES = {term.canonical_key: term.canonical_name for term in SKILL_CATALOG}


def _refusal(run, keys) -> tuple[AmbiguityReason, str] | None:
    """Why this run cannot be stored as requirements, or None to store it.

    Three outcomes, and only the middle one is new in `v3`:

    * a run the posting joined with a written `or` is a choice, refused as one;
    * a run joined only by a bare `/` whose members are a **registered**
      compound is one expression, refused as one — `AI/ML` is neither a choice
      between AI and ML nor a demand for both;
    * any other bare slash stays the conservative reading: `Python/R` and
      `TensorFlow/PyTorch` are refused as choices, because reading a slash as a
      conjunction is exactly the OR-becomes-AND mistake this package exists to
      avoid.
    """
    if run.alternative:
        return (
            AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED,
            ALTERNATIVE_GROUP_RULE_ID,
        )
    if not run.slashed:
        return None
    if is_compound_expression(keys):
        return (
            AmbiguityReason.COMPOUND_SKILL_EXPRESSION_UNSUPPORTED,
            COMPOUND_EXPRESSION_RULE_ID,
        )
    return AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED, ALTERNATIVE_GROUP_RULE_ID


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
        matches = SKILL_MATCHER.find(segment.text)
        if not matches:
            continue
        spans = [(item.start, item.end) for item in matches]
        runs = term_runs(spans, segment.text)
        fragment = shorten_evidence(segment.text, MAX_EVIDENCE_LENGTH)
        for run, (start, end) in zip(
            runs, term_clauses(spans, segment.text, runs), strict=True
        ):
            stated = clause_level(segment.text[start:end], segment.context)
            if stated is None:
                # This clause demanded nothing — or cancelled its own demand.
                # Neither is a refusal, so no ambiguity is recorded, and a
                # neighbouring clause that *did* demand something is untouched.
                continue
            level, rule_id = stated
            refusal = _refusal(run, [matches[index].key for index in run.indexes])
            if refusal is not None:
                reason, refusing_rule = refusal
                refused.append(
                    RequirementAmbiguity(
                        position=len(refused),
                        kind=RequirementKind.SKILL,
                        reason=reason,
                        rule_id=refusing_rule,
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
