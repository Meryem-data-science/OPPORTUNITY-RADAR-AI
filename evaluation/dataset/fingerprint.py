"""Deterministic content fingerprints for a frozen evaluation dataset.

Same primitive as every phase before this one — the project's single
`canonical_json` (sorted keys, no insignificant whitespace, UTF-8), digested
with SHA-256. Nothing new is invented here; only the *domain* is new.

Two levels, and both matter:

* a **record fingerprint** over one opportunity's canonical payload, so a later
  error analysis can say exactly which records moved between two datasets
  rather than only that the dataset moved;
* a **content fingerprint** over the schema version, the cohort rule, the
  upstream engine identities, and the record fingerprints *in canonical order*.

What is deliberately outside the content fingerprint is as much of the contract
as what is inside it:

* `generated_at` — the clock. Two extractions a day apart over an unchanged
  database must agree, or the digest describes the run instead of the data;
* `dataset_id` — derived *from* the digest, so including it is circular;
* `git_commit`, the database path, its size and its file digest — they say
  where the data was read, not what it says. Copying a database to a second
  path must not produce a second dataset;
* the run **ids** of the upstream Matching and Recommendation runs — autoincrement
  row identity. Re-persisting an identical run under a new id changes nothing a
  measurement can see. The run *fingerprints* are in, because those are content;
* the cohort's `excluded_counts` — they describe what the database holds
  *outside* this dataset. A newly collected OUT_OF_SCOPE posting is not a change
  to this dataset's content.

And what is inside it changes it: any field of any record, the order of the
records, the cohort rule, the schema version, or the identity of the upstream
runs the downstream signals were taken from.

Serialization is normalized rather than trusted. Indentation, key order and the
JSON writer's mood cannot move a digest, because the digest is taken over the
canonical form of a Python structure and never over the bytes of a file.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .schema import (
    EvaluationCohortDefinition,
    EvaluationOpportunityRecord,
    EvaluationUpstreamProvenance,
    evaluation_record_payload,
)

__all__ = [
    "canonical_evaluation_content_payload",
    "evaluation_content_fingerprint",
    "evaluation_record_fingerprint",
]


def evaluation_record_fingerprint(record: EvaluationOpportunityRecord) -> str:
    """SHA-256 of the same payload the record is written to JSONL as."""
    payload = evaluation_record_payload(record)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_evaluation_content_payload(
    *,
    schema_version: str,
    cohort: EvaluationCohortDefinition,
    upstream: EvaluationUpstreamProvenance,
    records: Sequence[EvaluationOpportunityRecord],
) -> dict[str, Any]:
    """The digest domain, as a plain structure a test can read and assert on."""
    return {
        "schema_version": schema_version,
        "cohort": {
            "version": cohort.version,
            # The criteria keep the order they are stated in: they are a
            # written rule, and a rule reads the way it was written.
            "criteria": list(cohort.criteria),
            "ordering": cohort.ordering,
        },
        "upstream": {
            "matching_status": upstream.matching_status,
            "matching_run_fingerprint": upstream.matching_run_fingerprint,
            "matching_engine_version": upstream.matching_engine_version,
            "matching_rules_version": upstream.matching_rules_version,
            "matching_selection_version": upstream.matching_selection_version,
            "recommendation_status": upstream.recommendation_status,
            "recommendation_run_fingerprint": (
                upstream.recommendation_run_fingerprint
            ),
            "recommendation_engine_version": (
                upstream.recommendation_engine_version
            ),
            "recommendation_rules_version": upstream.recommendation_rules_version,
        },
        # In canonical order, and not sorted here: the order *is* part of the
        # statement, and the snapshot builder is the one component entitled to
        # decide it.
        "records": [evaluation_record_fingerprint(record) for record in records],
    }


def evaluation_content_fingerprint(
    *,
    schema_version: str,
    cohort: EvaluationCohortDefinition,
    upstream: EvaluationUpstreamProvenance,
    records: Sequence[EvaluationOpportunityRecord],
) -> str:
    """SHA-256 of the canonical JSON of everything this dataset asserts."""
    payload = canonical_evaluation_content_payload(
        schema_version=schema_version,
        cohort=cohort,
        upstream=upstream,
        records=records,
    )
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
