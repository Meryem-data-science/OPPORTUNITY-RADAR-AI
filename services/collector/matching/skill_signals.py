"""Pure, deterministic opportunity-side skill signals for matching."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from services.collector.extractors.opportunity_constraints.requirements.matcher import (
    SKILL_MATCHER,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    SectionContext,
)
from services.collector.extractors.opportunity_constraints.requirements.sections import (
    parse_requirement_segments,
    strip_bullet,
)
from services.collector.extractors.opportunity_constraints.requirements.signals import (
    cancels_requirement,
    term_clauses,
    term_runs,
)
from services.collector.extractors.opportunity_constraints.requirements.skill_catalog import (
    SKILL_CATALOG,
)
from services.collector.matching.models import MatchingOpportunityInput

SKILL_SIGNAL_VERSION = "skill-signals-v1"


class RequirementsState(StrEnum):
    UNKNOWN = "UNKNOWN"
    EXTRACTED = "EXTRACTED"


class SkillSignalKind(StrEnum):
    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"
    CONTEXT = "CONTEXT"


class SkillSignalSource(StrEnum):
    REQUIREMENTS = "REQUIREMENTS"
    TITLE = "TITLE"
    ROLE_DESCRIPTION = "ROLE_DESCRIPTION"
    RESPONSIBILITIES = "RESPONSIBILITIES"
    TECH_STACK = "TECH_STACK"


@dataclass(frozen=True)
class OpportunitySkillSignal:
    canonical_key: str
    canonical_name: str
    kind: SkillSignalKind
    sources: tuple[SkillSignalSource, ...]


@dataclass(frozen=True)
class OpportunitySkillSignals:
    opportunity_id: int
    requirements_state: RequirementsState
    requirements_extractor_version: str | None
    signals: tuple[OpportunitySkillSignal, ...]
    skill_signal_version: str = SKILL_SIGNAL_VERSION


_ROLE_DESCRIPTION_HEADINGS = frozenset(
    ("about the role", "about this role", "about the job", "the role")
)
_RESPONSIBILITIES_HEADINGS = frozenset(
    (
        "responsibilities", "responsibility", "key responsibilities",
        "your responsibilities", "what you'll do", "what you will do",
        "what you'll be doing", "what you will be doing", "your mission",
        "mission", "missions", "vos missions",
    )
)
_TECH_STACK_HEADINGS = frozenset(
    (
        "our stack", "tech stack", "technical stack", "technology stack",
        "technologies", "technology", "our technologies",
        "environnement technique", "stack technique",
    )
)
_SOURCE_ORDER = {source: index for index, source in enumerate(SkillSignalSource)}
_KIND_PRIORITY = {
    SkillSignalKind.CONTEXT: 0,
    SkillSignalKind.PREFERRED: 1,
    SkillSignalKind.REQUIRED: 2,
}
_CATALOG_BY_KEY = {term.canonical_key: term for term in SKILL_CATALOG}


def _heading_source(heading: str | None) -> SkillSignalSource | None:
    if heading is None:
        return None
    value = strip_bullet(unicodedata.normalize("NFKC", heading)).strip()
    value = value[:-1].rstrip() if value.endswith((":", "：")) else value
    folded = " ".join(value.split()).casefold()
    if folded in _ROLE_DESCRIPTION_HEADINGS:
        return SkillSignalSource.ROLE_DESCRIPTION
    if folded in _RESPONSIBILITIES_HEADINGS:
        return SkillSignalSource.RESPONSIBILITIES
    if folded in _TECH_STACK_HEADINGS:
        return SkillSignalSource.TECH_STACK
    return None


def build_opportunity_skill_signals(
    opportunity: MatchingOpportunityInput,
) -> OpportunitySkillSignals:
    """Build signals without persistence, scoring, or profile inspection."""
    requirements = opportunity.requirements
    state = RequirementsState.UNKNOWN if requirements is None else RequirementsState.EXTRACTED
    extractor_version = None if requirements is None else requirements.extractor_version
    observations: dict[str, tuple[str, SkillSignalKind, set[SkillSignalSource]]] = {}

    def observe(key: str, name: str, kind: SkillSignalKind, source: SkillSignalSource) -> None:
        current = observations.get(key)
        if current is None:
            observations[key] = (name, kind, {source})
            return
        current_name, current_kind, sources = current
        sources.add(source)
        if _KIND_PRIORITY[kind] > _KIND_PRIORITY[current_kind]:
            observations[key] = (name, kind, sources)
        else:
            observations[key] = (current_name, current_kind, sources)

    if requirements is not None:
        for skill in requirements.skills:
            try:
                kind = SkillSignalKind(skill.requirement)
            except ValueError as error:
                raise ValueError(
                    f"unsupported persisted requirement {skill.requirement!r} "
                    f"for skill {skill.canonical_key!r}"
                ) from error
            if kind is SkillSignalKind.CONTEXT:
                raise ValueError(
                    f"unsupported persisted requirement {skill.requirement!r} "
                    f"for skill {skill.canonical_key!r}"
                )
            observe(skill.canonical_key, skill.canonical_name, kind, SkillSignalSource.REQUIREMENTS)

    for match in SKILL_MATCHER.find(opportunity.canonical_title):
        term = _CATALOG_BY_KEY[match.key]
        observe(match.key, term.canonical_name, SkillSignalKind.CONTEXT, SkillSignalSource.TITLE)

    if requirements is not None:
        for segment in parse_requirement_segments(opportunity.description):
            if segment.context is not SectionContext.NEUTRAL:
                continue
            source = _heading_source(segment.heading_text)
            if source is None:
                continue
            matches = SKILL_MATCHER.find(segment.text)
            spans = tuple((match.start, match.end) for match in matches)
            runs = term_runs(spans, segment.text)
            clauses = term_clauses(spans, segment.text, runs)
            for run, (start, end) in zip(runs, clauses, strict=True):
                if cancels_requirement(segment.text[start:end]):
                    continue
                for index in run.indexes:
                    match = matches[index]
                    term = _CATALOG_BY_KEY[match.key]
                    observe(match.key, term.canonical_name, SkillSignalKind.CONTEXT, source)

    signals = tuple(
        OpportunitySkillSignal(
            canonical_key=key,
            canonical_name=name,
            kind=kind,
            sources=tuple(sorted(sources, key=_SOURCE_ORDER.__getitem__)),
        )
        for key, (name, kind, sources) in sorted(observations.items())
    )
    return OpportunitySkillSignals(
        opportunity_id=opportunity.opportunity_id,
        requirements_state=state,
        requirements_extractor_version=extractor_version,
        signals=signals,
    )
