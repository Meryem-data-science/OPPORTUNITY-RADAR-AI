"""Deterministic content fingerprints for a frozen evaluation dataset.

Same primitive as every phase before this one — the project's single
`canonical_json` (sorted keys, no insignificant whitespace, UTF-8), digested
with SHA-256. Nothing new is invented here; only the *domain* is new.

Two levels, and both matter:

* a **record fingerprint** over one opportunity's canonical payload, so a later
  error analysis can say exactly which records moved between two datasets
  rather than only that the dataset moved;
* a **content fingerprint** over everything that makes a snapshot the snapshot
  it is.

**The exact domain of the content fingerprint**, and nothing else:

    schema_version         the record and manifest shape
    cohort.version         which universe rule selected the rows
    cohort.criteria        that rule, as written
    cohort.ordering        the canonical order the records are in
    cohort.excluded_counts one count per exclusion the rule applied
    profile_context        profile_id, user_id and the digest of what that
                           profile declared — the dataset is personalised, and
                           it is bound to the person it was built for
    upstream.*_status      whether each upstream run existed at all
    upstream.*_fingerprint the run digests the downstream signals came from
    upstream.*_version     the engine, rules and selection versions in force
    records[]              every record fingerprint, in canonical order

**What is outside it, and why**, which is as much of the contract as what is
inside:

* `generated_at` — the clock. Two extractions a day apart over an unchanged
  database must agree, or the digest describes the run instead of the data.
  This exclusion is only safe because a written dataset is immutable: see
  `storage.py`, which refuses to rewrite an existing dataset directory, so the
  first manifest written under a `dataset_id` keeps its `generated_at` for good;
* `dataset_id` — derived *from* the digest, so including it is circular;
* `git_commit`, the database path, its size, its file digest and its applied
  migration list — they say where and by what the data was read, not what it
  says. Copying a database to a second path, or reading it from a second
  checkout, must not produce a second dataset;
* the run **ids** of the upstream Matching and Recommendation runs —
  autoincrement row identity. Re-persisting an identical run under a new id
  changes nothing a measurement can see. The run *fingerprints* are in, because
  those are content;
* `record_count` — already determined by the list of record fingerprints, and a
  count that could disagree with the list it counts is worse than no count.

The rule the two lists obey: **two artefacts carrying the same `dataset_id`
represent the same semantic snapshot.** Everything left outside is execution
metadata — where, when and by which checkout the read happened — and none of it
can mutate a snapshot that has already been frozen.

Serialization is normalized rather than trusted. Indentation, key order and the
JSON writer's mood cannot move a digest, because the digest is taken over the
canonical form of a Python structure and never over the bytes of a file.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .schema import (
    EvaluationCohortDefinition,
    EvaluationDatasetError,
    EvaluationOpportunityRecord,
    EvaluationProfileContext,
    EvaluationUpstreamProvenance,
    evaluation_cohort_payload,
    evaluation_profile_context_payload,
    evaluation_record_payload,
    evaluation_upstream_payload,
)

__all__ = [
    "canonical_evaluation_content_payload",
    "evaluation_content_fingerprint",
    "evaluation_record_fingerprint",
    "manifest_fingerprint_domain",
]

#: The manifest fields the digest covers, named once. A reader that has only a
#: stored `manifest.json` — the writer verifying an already-frozen dataset — and
#: a writer that has only the in-memory objects must agree exactly on this set,
#: so both go through `manifest_fingerprint_domain` below and neither spells the
#: domain out a second time.
_COHORT_FINGERPRINTED_KEYS = ("version", "criteria", "ordering", "excluded_counts")
_PROFILE_CONTEXT_FINGERPRINTED_KEYS = ("profile_id", "user_id", "fingerprint")
#: Statuses, run digests and engine versions — and deliberately not
#: `matching_run_id` or `recommendation_run_id`, which are autoincrement row
#: identity: re-persisting an identical run under a new id changes nothing a
#: measurement can see.
_UPSTREAM_FINGERPRINTED_KEYS = (
    "matching_status",
    "matching_run_fingerprint",
    "matching_engine_version",
    "matching_rules_version",
    "matching_selection_version",
    "recommendation_status",
    "recommendation_run_fingerprint",
    "recommendation_engine_version",
    "recommendation_rules_version",
)


def _section(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = payload.get(name)
    if not isinstance(section, Mapping):
        raise EvaluationDatasetError(f"manifest section {name!r} is not an object")
    return section


def _project(
    payload: Mapping[str, Any], name: str, keys: tuple[str, ...]
) -> dict[str, Any]:
    """Keep exactly `keys` from one manifest section, refusing a missing one.

    A key that is absent is an error rather than a `None`: a manifest written by
    another version of this contract must be refused, not silently read as one
    stating nothing.
    """
    section = _section(payload, name)
    missing = [key for key in keys if key not in section]
    if missing:
        raise EvaluationDatasetError(
            f"manifest section {name!r} is missing {', '.join(sorted(missing))}"
        )
    return {key: section[key] for key in keys}


def manifest_fingerprint_domain(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project a manifest payload onto the half of it the digest covers.

    The **one** definition of that half. It is applied to the manifest this
    process just built when computing a fingerprint, and to a manifest read back
    off disk when verifying an already-frozen dataset, so the two can never
    drift into disagreeing about which fields are semantic.
    """
    if "schema_version" not in payload:
        raise EvaluationDatasetError("manifest states no schema_version")
    return {
        "schema_version": payload["schema_version"],
        "cohort": _project(payload, "cohort", _COHORT_FINGERPRINTED_KEYS),
        "profile_context": _project(
            payload, "profile_context", _PROFILE_CONTEXT_FINGERPRINTED_KEYS
        ),
        "upstream": _project(payload, "upstream", _UPSTREAM_FINGERPRINTED_KEYS),
    }


def evaluation_record_fingerprint(record: EvaluationOpportunityRecord) -> str:
    """SHA-256 of the same payload the record is written to JSONL as."""
    payload = evaluation_record_payload(record)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_evaluation_content_payload(
    *,
    schema_version: str,
    cohort: EvaluationCohortDefinition,
    profile_context: EvaluationProfileContext,
    upstream: EvaluationUpstreamProvenance,
    records: Sequence[EvaluationOpportunityRecord],
) -> dict[str, Any]:
    """The digest domain, as a plain structure a test can read and assert on.

    Built by laying the three blocks out in their manifest shape and then
    projecting them through `manifest_fingerprint_domain`, which is what keeps
    "what is digested" and "what the writer verifies on disk" one definition
    rather than two that agree today.
    """
    domain = manifest_fingerprint_domain(
        {
            "schema_version": schema_version,
            "cohort": evaluation_cohort_payload(cohort),
            "profile_context": evaluation_profile_context_payload(profile_context),
            "upstream": evaluation_upstream_payload(upstream),
        }
    )
    # In canonical order, and not sorted here: the order *is* part of the
    # statement, and the snapshot builder is the one component entitled to
    # decide it.
    domain["records"] = [
        evaluation_record_fingerprint(record) for record in records
    ]
    return domain


def evaluation_content_fingerprint(
    *,
    schema_version: str,
    cohort: EvaluationCohortDefinition,
    profile_context: EvaluationProfileContext,
    upstream: EvaluationUpstreamProvenance,
    records: Sequence[EvaluationOpportunityRecord],
) -> str:
    """SHA-256 of the canonical JSON of everything this dataset asserts."""
    payload = canonical_evaluation_content_payload(
        schema_version=schema_version,
        cohort=cohort,
        profile_context=profile_context,
        upstream=upstream,
        records=records,
    )
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
