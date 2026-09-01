"""Pure exact-key skill fit evaluation for Phase 4 matching."""

from __future__ import annotations

from dataclasses import dataclass

from .models import MatchingInput, MatchingProfileSkill, MatchingRequirements
from .skill_signals import (
    OpportunitySkillSignal,
    OpportunitySkillSignals,
    RequirementsState,
    SkillSignalKind,
    SkillSignalSource,
)

SKILL_FIT_VERSION = "skill-fit-v1"


class SkillFitInputError(RuntimeError):
    """Raised when upstream matching contracts are inconsistent."""


@dataclass(frozen=True)
class SkillCoverage:
    matched_count: int
    total_count: int

    @property
    def ratio(self) -> float | None:
        return self.matched_count / self.total_count if self.total_count else None


@dataclass(frozen=True)
class SkillFitEvaluation:
    canonical_key: str
    canonical_name: str
    kind: SkillSignalKind
    sources: tuple[SkillSignalSource, ...]
    matched: bool
    profile_normalizer_versions: tuple[str, ...]


@dataclass(frozen=True)
class SkillFitResult:
    profile_id: int
    opportunity_id: int
    requirements_state: RequirementsState
    requirements_extractor_version: str | None
    evaluations: tuple[SkillFitEvaluation, ...]
    required_coverage: SkillCoverage
    preferred_coverage: SkillCoverage
    context_overlap: SkillCoverage
    matching_input_version: str
    skill_signal_version: str
    skill_fit_version: str = SKILL_FIT_VERSION


def _unique_by_key(items: tuple, label: str) -> dict[str, object]:
    indexed: dict[str, object] = {}
    for item in items:
        key = item.canonical_key
        if key in indexed:
            raise SkillFitInputError(f"duplicate {label} canonical_key: {key!r}")
        indexed[key] = item
    return indexed


def _validate_signals(signals: OpportunitySkillSignals) -> None:
    if not signals.skill_signal_version:
        raise SkillFitInputError("skill_signal_version must be non-empty")
    if signals.requirements_state is RequirementsState.UNKNOWN:
        if signals.requirements_extractor_version is not None:
            raise SkillFitInputError(
                "UNKNOWN requirements must not have an extractor version"
            )
        if any(
            item.kind in (SkillSignalKind.REQUIRED, SkillSignalKind.PREFERRED)
            for item in signals.signals
        ):
            raise SkillFitInputError(
                "UNKNOWN requirements cannot contain REQUIRED or PREFERRED signals"
            )
    elif signals.requirements_state is RequirementsState.EXTRACTED:
        if not signals.requirements_extractor_version:
            raise SkillFitInputError(
                "EXTRACTED requirements must have a non-empty extractor version"
            )
    else:
        raise SkillFitInputError(
            f"unsupported requirements state: {signals.requirements_state!r}"
        )


def _validate_requirements_alignment(
    requirements: MatchingRequirements | None,
    signals: OpportunitySkillSignals,
) -> None:
    """Ensure signals and persisted requirements describe the same snapshot."""
    if requirements is None:
        if (
            signals.requirements_state is not RequirementsState.UNKNOWN
            or signals.requirements_extractor_version is not None
        ):
            raise SkillFitInputError(
                "skill signals do not align with absent matching requirements"
            )
        return

    if signals.requirements_state is not RequirementsState.EXTRACTED:
        raise SkillFitInputError(
            "skill signals do not align with extracted matching requirements"
        )
    if signals.requirements_extractor_version != requirements.extractor_version:
        raise SkillFitInputError(
            "requirements extractor version mismatch between matching input and signals"
        )

    persisted: dict[str, tuple[str, SkillSignalKind]] = {}
    for requirement in requirements.skills:
        if requirement.canonical_key in persisted:
            raise SkillFitInputError(
                "duplicate persisted requirement canonical_key: "
                f"{requirement.canonical_key!r}"
            )
        try:
            kind = SkillSignalKind(requirement.requirement)
        except ValueError as error:
            raise SkillFitInputError(
                f"unsupported persisted requirement kind: {requirement.requirement!r}"
            ) from error
        if kind not in (SkillSignalKind.REQUIRED, SkillSignalKind.PREFERRED):
            raise SkillFitInputError(
                f"unsupported persisted requirement kind: {requirement.requirement!r}"
            )
        persisted[requirement.canonical_key] = (requirement.canonical_name, kind)

    authoritative = {
        signal.canonical_key: signal
        for signal in signals.signals
        if signal.kind in (SkillSignalKind.REQUIRED, SkillSignalKind.PREFERRED)
    }
    if set(authoritative) != set(persisted):
        raise SkillFitInputError(
            "REQUIRED/PREFERRED signals do not match persisted requirements"
        )
    for key, (canonical_name, kind) in persisted.items():
        signal = authoritative[key]
        if signal.canonical_name != canonical_name or signal.kind is not kind:
            raise SkillFitInputError(
                f"requirement signal does not match persisted requirement: {key!r}"
            )
        if SkillSignalSource.REQUIREMENTS not in signal.sources:
            raise SkillFitInputError(
                f"requirement signal is missing REQUIREMENTS source: {key!r}"
            )


def _coverage(
    evaluations: tuple[SkillFitEvaluation, ...], kind: SkillSignalKind
) -> SkillCoverage:
    selected = tuple(item for item in evaluations if item.kind is kind)
    return SkillCoverage(sum(item.matched for item in selected), len(selected))


def build_skill_fit(
    matching_input: MatchingInput,
    opportunity_signals: OpportunitySkillSignals,
) -> SkillFitResult:
    """Compare verified profile skills and opportunity signals by exact key only."""
    if not matching_input.matching_input_version:
        raise SkillFitInputError("matching_input_version must be non-empty")
    if (
        matching_input.opportunity.opportunity_id
        != opportunity_signals.opportunity_id
    ):
        raise SkillFitInputError(
            "opportunity ID mismatch between matching input and skill signals"
        )
    _validate_signals(opportunity_signals)
    _validate_requirements_alignment(
        matching_input.opportunity.requirements, opportunity_signals
    )
    profile_by_key = _unique_by_key(
        matching_input.profile.skills, "profile skill"
    )
    signal_by_key = _unique_by_key(
        opportunity_signals.signals, "opportunity signal"
    )

    evaluations = tuple(
        _evaluate(signal_by_key[key], profile_by_key.get(key))
        for key in sorted(signal_by_key)
    )
    return SkillFitResult(
        profile_id=matching_input.profile.profile_id,
        opportunity_id=matching_input.opportunity.opportunity_id,
        requirements_state=opportunity_signals.requirements_state,
        requirements_extractor_version=(
            opportunity_signals.requirements_extractor_version
        ),
        evaluations=evaluations,
        required_coverage=_coverage(evaluations, SkillSignalKind.REQUIRED),
        preferred_coverage=_coverage(evaluations, SkillSignalKind.PREFERRED),
        context_overlap=_coverage(evaluations, SkillSignalKind.CONTEXT),
        matching_input_version=matching_input.matching_input_version,
        skill_signal_version=opportunity_signals.skill_signal_version,
    )


def _evaluate(
    signal: OpportunitySkillSignal, profile_skill: object | None
) -> SkillFitEvaluation:
    skill = profile_skill if isinstance(profile_skill, MatchingProfileSkill) else None
    return SkillFitEvaluation(
        canonical_key=signal.canonical_key,
        canonical_name=signal.canonical_name,
        kind=signal.kind,
        sources=signal.sources,
        matched=skill is not None,
        profile_normalizer_versions=(
            tuple(sorted(set(skill.normalizer_versions))) if skill is not None else ()
        ),
    )
