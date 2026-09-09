"""Canonical operational identity for persisted matching runs.

Two fingerprints describe a stored matching run and they are deliberately not
the same thing:

    batch fingerprint   content identity — what was computed, with no
                        opportunity id anywhere in it
    run fingerprint     operational identity — *this* profile, over *these*
                        postings, with *this* content

The semantic binding provenance belongs to the second. A corpus whose documents
were exchanged between two postings has identical content and a different
meaning, so it must produce a different operational identity while leaving the
content identity alone. Adding it to the batch fingerprint instead would
silently convert a content digest into an identity, which is exactly what the
two layers exist to keep apart.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from .fingerprint import canonical_json

MATCHING_PERSISTENCE_VERSION = "matching-persistence-v1"


def canonical_matching_run_payload(
    *,
    profile_id: int,
    selection_version: str,
    matching_engine_version: str,
    matching_rules_version: str,
    semantic_percentile_version: str,
    corpus_fingerprint: str,
    tfidf_model_fingerprint: str,
    batch_fingerprint: str,
    assessments: Iterable[tuple[int, str]],
    persistence_version: str = MATCHING_PERSISTENCE_VERSION,
    semantic_binding_version: str | None = None,
    semantic_binding_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Return operational content, including IDs, in canonical order.

    The two binding arguments are optional and additive. Passing neither yields
    the **exact** payload this function produced before they existed, byte for
    byte, so every run fingerprint already stored keeps verifying. Passing both
    adds them. Passing one is refused: half a provenance is not a provenance,
    and quietly ignoring the half that arrived would make two different runs
    share an identity.
    """
    ordered = sorted(
        (
            {"opportunity_id": identifier, "assessment_fingerprint": fingerprint}
            for identifier, fingerprint in assessments
        ),
        key=lambda item: item["opportunity_id"],
    )
    payload = {
        "persistence_version": persistence_version,
        "selection_version": selection_version,
        "profile_id": profile_id,
        "matching_engine_version": matching_engine_version,
        "matching_rules_version": matching_rules_version,
        "semantic_percentile_version": semantic_percentile_version,
        "corpus_fingerprint": corpus_fingerprint,
        "tfidf_model_fingerprint": tfidf_model_fingerprint,
        "batch_fingerprint": batch_fingerprint,
        "assessment_count": len(ordered),
        "assessments": ordered,
    }
    present = (semantic_binding_version is not None, semantic_binding_fingerprint is not None)
    if any(present) and not all(present):
        raise ValueError(
            "semantic binding provenance needs both a version and a fingerprint"
        )
    if all(present):
        payload["semantic_binding_version"] = semantic_binding_version
        payload["semantic_binding_fingerprint"] = semantic_binding_fingerprint
    return payload


def matching_run_fingerprint(**values: Any) -> str:
    payload = canonical_matching_run_payload(**values)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
