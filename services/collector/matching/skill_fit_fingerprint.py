"""Canonical, identity-independent fingerprint for exact skill fit."""

from __future__ import annotations

import hashlib
from typing import Any

from .fingerprint import canonical_json
from .skill_fit import SkillFitResult


def canonical_skill_fit_payload(result: SkillFitResult) -> dict[str, Any]:
    """Return only semantic fit content, excluding technical identities."""
    return {
        "skill_fit_version": result.skill_fit_version,
        "matching_input_version": result.matching_input_version,
        "skill_signal_version": result.skill_signal_version,
        "requirements_state": result.requirements_state.value,
        "requirements_extractor_version": result.requirements_extractor_version,
        "evaluations": [
            {
                "canonical_key": item.canonical_key,
                "canonical_name": item.canonical_name,
                "kind": item.kind.value,
                "sources": [source.value for source in item.sources],
                "matched": item.matched,
                "profile_normalizer_versions": list(
                    item.profile_normalizer_versions
                ),
            }
            for item in result.evaluations
        ],
    }


def skill_fit_fingerprint(result: SkillFitResult) -> str:
    serialized = canonical_json(canonical_skill_fit_payload(result)).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()
