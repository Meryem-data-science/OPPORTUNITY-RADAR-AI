"""Immutable input contract for Phase 4 matching.

These types contain only persisted, upstream-owned professional content.  They
do not score, infer, normalize, or retain persistence identities other than the
profile and opportunity identifiers needed by callers.
"""

from __future__ import annotations

from dataclasses import dataclass

MATCHING_INPUT_VERSION = "matching-input-v1"


@dataclass(frozen=True)
class MatchingProfileSkill:
    canonical_key: str
    canonical_name: str
    normalizer_versions: tuple[str, ...]


@dataclass(frozen=True)
class MatchingExperience:
    role_text: str | None
    description_text: str | None
    structurer_version: str


@dataclass(frozen=True)
class MatchingProject:
    title_text: str | None
    description_text: str | None
    structurer_version: str


@dataclass(frozen=True)
class MatchingEducation:
    program_text: str | None
    description_text: str | None
    structurer_version: str


@dataclass(frozen=True)
class MatchingPreferences:
    opportunity_types: tuple[str, ...]
    work_modes: tuple[str, ...]
    preferred_domains: tuple[str, ...]
    input_version: str


@dataclass(frozen=True)
class MatchingCareerObjectives:
    objectives: tuple[str, ...]
    input_version: str


@dataclass(frozen=True)
class MatchingProfileInput:
    profile_id: int
    skills: tuple[MatchingProfileSkill, ...] = ()
    experiences: tuple[MatchingExperience, ...] = ()
    projects: tuple[MatchingProject, ...] = ()
    educations: tuple[MatchingEducation, ...] = ()
    preferences: MatchingPreferences | None = None
    career_objectives: MatchingCareerObjectives | None = None


@dataclass(frozen=True)
class MatchingQualification:
    qualification: str
    primary_domain: str
    opportunity_type: str
    classifier_version: str


@dataclass(frozen=True)
class MatchingRequiredSkill:
    canonical_key: str
    canonical_name: str
    requirement: str


@dataclass(frozen=True)
class MatchingRequirementAmbiguity:
    kind: str
    reason: str


@dataclass(frozen=True)
class MatchingRequirements:
    extractor_version: str
    skills: tuple[MatchingRequiredSkill, ...] = ()
    ambiguities: tuple[MatchingRequirementAmbiguity, ...] = ()


@dataclass(frozen=True)
class MatchingOpportunityInput:
    opportunity_id: int
    canonical_title: str
    description: str | None
    remote_type: str | None
    qualification: MatchingQualification | None = None
    requirements: MatchingRequirements | None = None


@dataclass(frozen=True)
class MatchingInput:
    profile: MatchingProfileInput
    opportunity: MatchingOpportunityInput
    matching_input_version: str = MATCHING_INPUT_VERSION
