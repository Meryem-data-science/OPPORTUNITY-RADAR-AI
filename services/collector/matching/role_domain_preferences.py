"""Deterministic alignment of persisted role, domain, and work preferences."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from services.collector.qualification.taxonomy import Domain
from services.collector.qualification.taxonomy import OpportunityType as QualifiedType
from services.digital_twin.preferences.models import OpportunityType as PreferredType
from services.digital_twin.preferences.models import WorkMode

from .models import MatchingInput, MatchingPreferences, MatchingQualification

ROLE_DOMAIN_PREFERENCES_VERSION = "role-domain-preferences-v2"
OPPORTUNITY_TYPE_BRIDGE_VERSION = "opportunity-type-bridge-v2"
DOMAIN_PREFERENCE_BRIDGE_VERSION = "domain-preference-bridge-v2"
WORK_MODE_BRIDGE_VERSION = "work-mode-bridge-v1"


class AlignmentStatus(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class AlignmentReason(StrEnum):
    PROFILE_PREFERENCES_ABSENT = "PROFILE_PREFERENCES_ABSENT"
    OPPORTUNITY_QUALIFICATION_ABSENT = "OPPORTUNITY_QUALIFICATION_ABSENT"
    OPPORTUNITY_TYPE_UNKNOWN = "OPPORTUNITY_TYPE_UNKNOWN"
    OPPORTUNITY_TYPE_UNMAPPED = "OPPORTUNITY_TYPE_UNMAPPED"
    OPPORTUNITY_TYPE_INCOMPATIBLE_WITH_PREFERENCES = (
        "OPPORTUNITY_TYPE_INCOMPATIBLE_WITH_PREFERENCES"
    )
    OPPORTUNITY_TYPE_ALLOWED = "OPPORTUNITY_TYPE_ALLOWED"
    OPPORTUNITY_TYPE_NOT_ALLOWED = "OPPORTUNITY_TYPE_NOT_ALLOWED"
    PRIMARY_DOMAIN_UNKNOWN = "PRIMARY_DOMAIN_UNKNOWN"
    PRIMARY_DOMAIN_NON_TARGET = "PRIMARY_DOMAIN_NON_TARGET"
    DOMAIN_PREFERENCES_EMPTY = "DOMAIN_PREFERENCES_EMPTY"
    DOMAIN_PREFERENCE_MATCHED = "DOMAIN_PREFERENCE_MATCHED"
    DOMAIN_PREFERENCE_NOT_LISTED = "DOMAIN_PREFERENCE_NOT_LISTED"
    DOMAIN_PREFERENCES_PARTIALLY_UNMAPPED = "DOMAIN_PREFERENCES_PARTIALLY_UNMAPPED"
    REMOTE_TYPE_ABSENT = "REMOTE_TYPE_ABSENT"
    REMOTE_TYPE_UNMAPPED = "REMOTE_TYPE_UNMAPPED"
    WORK_MODE_ALLOWED = "WORK_MODE_ALLOWED"
    WORK_MODE_NOT_ALLOWED = "WORK_MODE_NOT_ALLOWED"


class RoleDomainPreferencesInputError(ValueError):
    """Raised when a projected closed-taxonomy value is impossible."""


@dataclass(frozen=True)
class OpportunityTypeAlignment:
    status: AlignmentStatus
    reason: AlignmentReason
    opportunity_type: str | None
    mapped_profile_type: str | None


@dataclass(frozen=True)
class DomainPreferenceAlignment:
    status: AlignmentStatus
    reason: AlignmentReason
    opportunity_domain: str | None
    preferred_rank: int | None
    mapped_preference_count: int
    unmapped_preference_count: int


@dataclass(frozen=True)
class WorkModeAlignment:
    status: AlignmentStatus
    reason: AlignmentReason
    opportunity_mode: str | None


@dataclass(frozen=True)
class RoleDomainPreferencesResult:
    profile_id: int
    opportunity_id: int
    opportunity_type: OpportunityTypeAlignment
    domain: DomainPreferenceAlignment
    work_mode: WorkModeAlignment
    matching_input_version: str
    preferences_input_version: str | None
    qualification_classifier_version: str | None
    role_domain_preferences_version: str = ROLE_DOMAIN_PREFERENCES_VERSION
    opportunity_type_bridge_version: str = OPPORTUNITY_TYPE_BRIDGE_VERSION
    domain_preference_bridge_version: str = DOMAIN_PREFERENCE_BRIDGE_VERSION
    work_mode_bridge_version: str = WORK_MODE_BRIDGE_VERSION


_TYPE_BRIDGE = {
    QualifiedType.PFE: PreferredType.PFE,
    QualifiedType.INTERNSHIP: PreferredType.INTERNSHIP,
    QualifiedType.APPRENTICESHIP: PreferredType.ALTERNANCE,
}
_JOB_LIKE_PREFERRED_TYPES = {
    PreferredType.FIRST_JOB,
    PreferredType.JUNIOR_ROLE,
}


def _normalize_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()


_DOMAIN_LABELS = {
    Domain.DATA_ENGINEERING: (
        "Data Engineering",
        "Big Data / Data Platforms",
        "Big Data/Data Platforms",
        "DATA_ENGINEERING",
    ),
    Domain.DATA_SCIENCE: ("Data Science", "DATA_SCIENCE"),
    Domain.MACHINE_LEARNING_AI: (
        "ML/AI",
        "ML / AI",
        "Machine Learning/AI",
        "Machine Learning / AI",
        "Machine Learning / Deep Learning",
        "Machine Learning/Deep Learning",
        "Artificial Intelligence",
        "MACHINE_LEARNING_AI",
    ),
    Domain.GENAI_LLM: (
        "GenAI/LLM",
        "GenAI / LLM",
        "Generative AI/LLM",
        "Generative AI / LLM",
        "Generative AI / LLM / RAG",
        "Generative AI/LLM/RAG",
        "GENAI_LLM",
    ),
    Domain.MLOPS_ML_PLATFORM: (
        "MLOps/ML Platform",
        "MLOps / ML Platform",
        "MLOps / ML Engineering",
        "MLOps/ML Engineering",
        "MLOPS_ML_PLATFORM",
    ),
    Domain.BI_ANALYTICS: (
        "BI/Analytics",
        "BI / Analytics",
        "Business Intelligence/Analytics",
        "Business Intelligence / Analytics",
        "Data Analytics / Business Intelligence",
        "Data Analytics/Business Intelligence",
        "BI_ANALYTICS",
    ),
    Domain.DATA_QUALITY_GOVERNANCE: (
        "Data Quality/Governance",
        "Data Quality / Governance",
        "DATA_QUALITY_GOVERNANCE",
    ),
    Domain.OTHER_DATA_AI: ("Other Data/AI", "Other Data / AI", "OTHER_DATA_AI"),
}
_DOMAIN_BRIDGE = {
    _normalize_label(label): domain
    for domain, labels in _DOMAIN_LABELS.items()
    for label in labels
}

# Values explicitly recognized by the upstream collected-field normalizer.
_WORK_MODE_BRIDGE = {
    "remote": WorkMode.REMOTE,
    "fully_remote": WorkMode.REMOTE,
    "full_remote": WorkMode.REMOTE,
    "telework": WorkMode.REMOTE,
    "hybrid": WorkMode.HYBRID,
    "hybride": WorkMode.HYBRID,
    "on_site": WorkMode.ON_SITE,
    "onsite": WorkMode.ON_SITE,
    "on-site": WorkMode.ON_SITE,
    "sur_site": WorkMode.ON_SITE,
}


def _closed(value: str, enum: type[StrEnum], field: str) -> StrEnum:
    try:
        return enum(value)
    except (TypeError, ValueError) as error:
        raise RoleDomainPreferencesInputError(
            f"invalid {field} closed-taxonomy value: {value!r}"
        ) from error


def _validate_preferences(preferences: MatchingPreferences | None) -> None:
    if preferences is None:
        return
    for value in preferences.opportunity_types:
        _closed(value, PreferredType, "profile opportunity type")
    for value in preferences.work_modes:
        _closed(value, WorkMode, "profile work mode")


def _validate_qualification(
    qualification: MatchingQualification | None,
) -> tuple[QualifiedType, Domain] | None:
    if qualification is None:
        return None
    return (
        _closed(qualification.opportunity_type, QualifiedType, "opportunity type"),
        _closed(qualification.primary_domain, Domain, "primary domain"),
    )  # type: ignore[return-value]


def _type_alignment(preferences, qualification, validated):
    if preferences is None:
        return OpportunityTypeAlignment(
            AlignmentStatus.UNKNOWN,
            AlignmentReason.PROFILE_PREFERENCES_ABSENT,
            None if qualification is None else qualification.opportunity_type,
            None,
        )
    if qualification is None:
        return OpportunityTypeAlignment(
            AlignmentStatus.UNKNOWN,
            AlignmentReason.OPPORTUNITY_QUALIFICATION_ABSENT,
            None,
            None,
        )
    opportunity_type = validated[0]
    if opportunity_type is QualifiedType.UNKNOWN:
        return OpportunityTypeAlignment(
            AlignmentStatus.UNKNOWN,
            AlignmentReason.OPPORTUNITY_TYPE_UNKNOWN,
            opportunity_type.value,
            None,
        )
    mapped = _TYPE_BRIDGE.get(opportunity_type)
    if mapped is None:
        if opportunity_type is QualifiedType.JOB and not any(
            preferred.value in preferences.opportunity_types
            for preferred in _JOB_LIKE_PREFERRED_TYPES
        ):
            return OpportunityTypeAlignment(
                AlignmentStatus.MISMATCH,
                AlignmentReason.OPPORTUNITY_TYPE_INCOMPATIBLE_WITH_PREFERENCES,
                opportunity_type.value,
                None,
            )
        return OpportunityTypeAlignment(
            AlignmentStatus.UNKNOWN,
            AlignmentReason.OPPORTUNITY_TYPE_UNMAPPED,
            opportunity_type.value,
            None,
        )
    allowed = mapped.value in preferences.opportunity_types
    return OpportunityTypeAlignment(
        AlignmentStatus.MATCH if allowed else AlignmentStatus.MISMATCH,
        AlignmentReason.OPPORTUNITY_TYPE_ALLOWED
        if allowed
        else AlignmentReason.OPPORTUNITY_TYPE_NOT_ALLOWED,
        opportunity_type.value,
        mapped.value,
    )


def _domain_alignment(preferences, qualification, validated):
    if preferences is None:
        return DomainPreferenceAlignment(
            AlignmentStatus.UNKNOWN,
            AlignmentReason.PROFILE_PREFERENCES_ABSENT,
            None if qualification is None else qualification.primary_domain,
            None,
            0,
            0,
        )
    if qualification is None:
        return DomainPreferenceAlignment(
            AlignmentStatus.UNKNOWN,
            AlignmentReason.OPPORTUNITY_QUALIFICATION_ABSENT,
            None,
            None,
            0,
            0,
        )
    domain = validated[1]
    if domain is Domain.UNKNOWN:
        return DomainPreferenceAlignment(
            AlignmentStatus.UNKNOWN,
            AlignmentReason.PRIMARY_DOMAIN_UNKNOWN,
            domain.value,
            None,
            0,
            0,
        )
    if domain is Domain.NON_TARGET:
        return DomainPreferenceAlignment(
            AlignmentStatus.UNKNOWN,
            AlignmentReason.PRIMARY_DOMAIN_NON_TARGET,
            domain.value,
            None,
            0,
            0,
        )
    if not preferences.preferred_domains:
        return DomainPreferenceAlignment(
            AlignmentStatus.UNKNOWN,
            AlignmentReason.DOMAIN_PREFERENCES_EMPTY,
            domain.value,
            None,
            0,
            0,
        )
    ranks: dict[Domain, int] = {}
    unmapped = 0
    considered = 0
    for rank, label in enumerate(preferences.preferred_domains, 1):
        considered += 1
        mapped = _DOMAIN_BRIDGE.get(_normalize_label(label))
        if mapped is None:
            unmapped += 1
        else:
            ranks.setdefault(mapped, rank)
            # A suffix cannot change either the explicit match or its rank.
            if mapped is domain:
                break
    if domain in ranks:
        status, reason = (
            AlignmentStatus.MATCH,
            AlignmentReason.DOMAIN_PREFERENCE_MATCHED,
        )
    elif unmapped:
        status, reason = (
            AlignmentStatus.UNKNOWN,
            AlignmentReason.DOMAIN_PREFERENCES_PARTIALLY_UNMAPPED,
        )
    else:
        status, reason = (
            AlignmentStatus.MISMATCH,
            AlignmentReason.DOMAIN_PREFERENCE_NOT_LISTED,
        )
    return DomainPreferenceAlignment(
        status, reason, domain.value, ranks.get(domain), considered - unmapped, unmapped
    )


def _work_mode_alignment(preferences, remote_type):
    if preferences is None:
        return WorkModeAlignment(
            AlignmentStatus.UNKNOWN, AlignmentReason.PROFILE_PREFERENCES_ABSENT, None
        )
    if remote_type is None or not remote_type.strip():
        return WorkModeAlignment(
            AlignmentStatus.UNKNOWN, AlignmentReason.REMOTE_TYPE_ABSENT, None
        )
    normalized = remote_type.strip().casefold().replace(" ", "_")
    mapped = _WORK_MODE_BRIDGE.get(normalized)
    if mapped is None:
        return WorkModeAlignment(
            AlignmentStatus.UNKNOWN, AlignmentReason.REMOTE_TYPE_UNMAPPED, None
        )
    allowed = mapped.value in preferences.work_modes
    return WorkModeAlignment(
        AlignmentStatus.MATCH if allowed else AlignmentStatus.MISMATCH,
        AlignmentReason.WORK_MODE_ALLOWED
        if allowed
        else AlignmentReason.WORK_MODE_NOT_ALLOWED,
        mapped.value,
    )


def build_role_domain_preference_signals(
    matching_input: MatchingInput,
) -> RoleDomainPreferencesResult:
    """Build three independent alignments without inference or side effects."""
    preferences = matching_input.profile.preferences
    qualification = matching_input.opportunity.qualification
    _validate_preferences(preferences)
    validated = _validate_qualification(qualification)
    return RoleDomainPreferencesResult(
        profile_id=matching_input.profile.profile_id,
        opportunity_id=matching_input.opportunity.opportunity_id,
        opportunity_type=_type_alignment(preferences, qualification, validated),
        domain=_domain_alignment(preferences, qualification, validated),
        work_mode=_work_mode_alignment(
            preferences, matching_input.opportunity.remote_type
        ),
        matching_input_version=matching_input.matching_input_version,
        preferences_input_version=None
        if preferences is None
        else preferences.input_version,
        qualification_classifier_version=None
        if qualification is None
        else qualification.classifier_version,
    )
