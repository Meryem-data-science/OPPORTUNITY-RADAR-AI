"""Canonical, identity-independent matching input fingerprint."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import MatchingInput, MatchingOpportunityInput, MatchingProfileInput


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort structured nullable values without comparing unlike Python types."""
    return sorted(items, key=canonical_json)


def _profile_payload(profile: MatchingProfileInput) -> dict[str, Any]:
    preferences = profile.preferences
    objectives = profile.career_objectives
    return {
        "skills": _sorted(
            [
                {
                    "canonical_key": item.canonical_key,
                    "canonical_name": item.canonical_name,
                    "normalizer_versions": sorted(item.normalizer_versions),
                }
                for item in profile.skills
            ]
        ),
        "experiences": _sorted(
            [
                {
                    "role_text": item.role_text,
                    "description_text": item.description_text,
                    "structurer_version": item.structurer_version,
                }
                for item in profile.experiences
            ]
        ),
        "projects": _sorted(
            [
                {
                    "title_text": item.title_text,
                    "description_text": item.description_text,
                    "structurer_version": item.structurer_version,
                }
                for item in profile.projects
            ]
        ),
        "educations": _sorted(
            [
                {
                    "program_text": item.program_text,
                    "description_text": item.description_text,
                    "structurer_version": item.structurer_version,
                }
                for item in profile.educations
            ]
        ),
        "preferences": (
            None
            if preferences is None
            else {
                "opportunity_types": sorted(preferences.opportunity_types),
                "work_modes": sorted(preferences.work_modes),
                "preferred_domains": list(preferences.preferred_domains),
                "input_version": preferences.input_version,
            }
        ),
        "career_objectives": (
            None
            if objectives is None
            else {
                "objectives": list(objectives.objectives),
                "input_version": objectives.input_version,
            }
        ),
    }


def _opportunity_payload(opportunity: MatchingOpportunityInput) -> dict[str, Any]:
    qualification = opportunity.qualification
    requirements = opportunity.requirements
    return {
        "canonical_title": opportunity.canonical_title,
        "description": opportunity.description,
        "remote_type": opportunity.remote_type,
        "qualification": (
            None
            if qualification is None
            else {
                "qualification": qualification.qualification,
                "primary_domain": qualification.primary_domain,
                "opportunity_type": qualification.opportunity_type,
                "classifier_version": qualification.classifier_version,
            }
        ),
        "requirements": (
            None
            if requirements is None
            else {
                "extractor_version": requirements.extractor_version,
                "skills": _sorted(
                    [
                        {
                            "canonical_key": item.canonical_key,
                            "canonical_name": item.canonical_name,
                            "requirement": item.requirement,
                        }
                        for item in requirements.skills
                    ]
                ),
                "ambiguities": _sorted(
                    [
                        {"kind": item.kind, "reason": item.reason}
                        for item in requirements.ambiguities
                    ]
                ),
            }
        ),
    }


def canonical_matching_payload(inputs: MatchingInput) -> dict[str, Any]:
    """Return every content value, excluding technical profile/opportunity IDs."""
    return {
        "matching_input_version": inputs.matching_input_version,
        "profile": _profile_payload(inputs.profile),
        "opportunity": _opportunity_payload(inputs.opportunity),
    }


def matching_input_fingerprint(inputs: MatchingInput) -> str:
    serialized = canonical_json(canonical_matching_payload(inputs)).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()
