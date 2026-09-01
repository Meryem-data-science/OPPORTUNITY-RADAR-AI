"""Identity-free fingerprints for matching component audit facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Mapping, Sequence


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_component_observation_payload(
    observation: object, *, audit_version: str = "component-audit-v1"
) -> dict[str, Any]:
    payload = asdict(observation)  # type: ignore[arg-type]
    payload.pop("profile_id", None)
    payload.pop("opportunity_id", None)
    return {"component_audit_version": audit_version, **payload}


def component_observation_fingerprint(
    observation: object, *, audit_version: str = "component-audit-v1"
) -> str:
    return _digest(
        canonical_component_observation_payload(
            observation, audit_version=audit_version
        )
    )


def canonical_component_audit_payload(
    *,
    corpus_fingerprint: str,
    model_fingerprint: str,
    observation_fingerprints: Sequence[str],
    audit_version: str = "component-audit-v1",
    selection_version: str = "component-audit-selection-v1",
) -> dict[str, Any]:
    return {
        "component_audit_version": audit_version,
        "selection_version": selection_version,
        "corpus_fingerprint": corpus_fingerprint,
        "tfidf_model_fingerprint": model_fingerprint,
        "observation_count": len(observation_fingerprints),
        "observation_fingerprints": sorted(observation_fingerprints),
    }


def component_audit_fingerprint(**kwargs: Any) -> str:
    return _digest(canonical_component_audit_payload(**kwargs))
