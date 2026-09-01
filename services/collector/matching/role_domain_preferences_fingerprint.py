"""Canonical fingerprint for role/domain/preference alignments."""

from __future__ import annotations

import hashlib
import json

from .role_domain_preferences import RoleDomainPreferencesResult


def canonical_role_domain_preferences_payload(
    result: RoleDomainPreferencesResult,
) -> dict[str, object]:
    return {
        "role_domain_preferences_version": result.role_domain_preferences_version,
        "opportunity_type_bridge_version": result.opportunity_type_bridge_version,
        "domain_preference_bridge_version": result.domain_preference_bridge_version,
        "work_mode_bridge_version": result.work_mode_bridge_version,
        "matching_input_version": result.matching_input_version,
        "preferences_input_version": result.preferences_input_version,
        "qualification_classifier_version": result.qualification_classifier_version,
        "opportunity_type": {
            "status": result.opportunity_type.status.value,
            "reason": result.opportunity_type.reason.value,
            "opportunity_type": result.opportunity_type.opportunity_type,
            "mapped_profile_type": result.opportunity_type.mapped_profile_type,
        },
        "domain": {
            "status": result.domain.status.value,
            "reason": result.domain.reason.value,
            "opportunity_domain": result.domain.opportunity_domain,
            "preferred_rank": result.domain.preferred_rank,
            "mapped_preference_count": result.domain.mapped_preference_count,
            "unmapped_preference_count": result.domain.unmapped_preference_count,
        },
        "work_mode": {
            "status": result.work_mode.status.value,
            "reason": result.work_mode.reason.value,
            "opportunity_mode": result.work_mode.opportunity_mode,
        },
    }


def role_domain_preferences_fingerprint(result: RoleDomainPreferencesResult) -> str:
    payload = canonical_role_domain_preferences_payload(result)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
