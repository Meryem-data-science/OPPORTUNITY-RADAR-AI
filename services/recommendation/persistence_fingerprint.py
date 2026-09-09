"""Canonical operational identity for persisted recommendation runs.

Two digests describe a stored recommendation run and they are deliberately not
the same thing:

    batch fingerprint   content identity — Phase 9A's own digest. It holds the
                        versions, the size and the ranked assessment digests,
                        and it holds **no** profile id, **no** opportunity id
                        and **no** run id, on purpose.
    run fingerprint     operational identity — *this* profile, over *this*
                        matching snapshot, over *these* postings, in *this*
                        rank order.

`fingerprint.py` owns the first and this module owns the second. Neither may be
used in the other's place: persisting under a content digest would let two
genuinely different runs — the same assessments attached to different postings,
or produced for a different person — collide into one stored row, and adding the
ids to the content digest would silently convert a content statement into an
identity and invalidate every 9A fingerprint already validated.

One thing separates this from `matching`'s equivalent. Matching sorts its
operational assessments by `opportunity_id`, because a matching batch has no
order of its own. A recommendation batch **does**: the ranking is what Phase 9A
produces, and two runs that rank the same postings differently are two different
runs. So `ranked_assessments` is kept in the batch's order, never sorted, and
each entry carries the 1-based `rank_position` it occupies.

Nothing here is allowed to see a timestamp, a run id, a database path or any
other runtime value: the same batch, persisted tomorrow, must digest identically.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from services.collector.matching.fingerprint import canonical_json

__all__ = [
    "RECOMMENDATION_PERSISTENCE_VERSION",
    "canonical_recommendation_run_payload",
    "recommendation_run_fingerprint",
]

#: The storage contract of this phase: the tables, the columns, the payloads and
#: the operational identity below. Moving any of them moves this version, and it
#: is independent of the engine, rules and assembly versions Phase 9A owns.
RECOMMENDATION_PERSISTENCE_VERSION = "recommendation-persistence-v1"


def canonical_recommendation_run_payload(
    *,
    profile_id: int,
    source_matching_run_id: int,
    source_matching_run_fingerprint: str,
    input_assembly_version: str,
    recommendation_engine_version: str,
    recommendation_rules_version: str,
    batch_fingerprint: str,
    ranked_assessments: Iterable[tuple[int, str]],
    persistence_version: str = RECOMMENDATION_PERSISTENCE_VERSION,
) -> dict[str, Any]:
    """Return the operational statement of one run, ids and ranking included.

    ``ranked_assessments`` is consumed **in the order it arrives**, which is the
    batch's ranking, and each pair becomes one entry carrying its 1-based
    position, its opportunity id and its assessment digest. It is never sorted:
    the same content re-ranked is a different run and must digest differently.
    """
    ranked = [
        {
            "rank_position": position,
            "opportunity_id": opportunity_id,
            "assessment_fingerprint": fingerprint,
        }
        for position, (opportunity_id, fingerprint) in enumerate(
            ranked_assessments, start=1
        )
    ]
    return {
        "persistence_version": persistence_version,
        "input_assembly_version": input_assembly_version,
        "profile_id": profile_id,
        "source_matching_run_id": source_matching_run_id,
        "source_matching_run_fingerprint": source_matching_run_fingerprint,
        "recommendation_engine_version": recommendation_engine_version,
        "recommendation_rules_version": recommendation_rules_version,
        "batch_fingerprint": batch_fingerprint,
        "assessment_count": len(ranked),
        "ranked_assessments": ranked,
    }


def recommendation_run_fingerprint(**values: Any) -> str:
    """SHA-256 of the canonical run payload, and of nothing else."""
    payload = canonical_recommendation_run_payload(**values)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
