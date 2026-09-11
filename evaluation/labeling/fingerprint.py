"""Deterministic digests for a calibration selection and for a set of labels.

Same primitive as every phase before this one — the project's single
`canonical_json` (sorted keys, no insignificant whitespace, UTF-8), digested
with SHA-256. Nothing new is invented; only the two domains are new, and both
are stated here in full because a digest whose domain is implicit is a digest
nobody can reason about.

**The selection fingerprint** answers "which small lot is this, and by what
rule was it drawn". Its exact domain:

    selector_version          the algorithm that drew the lot
    dataset_id                which frozen dataset it was drawn from
    dataset_content_fingerprint   and what that dataset actually contained
    parameters.sample_size    the effective size of the lot
    selected_opportunity_ids  the lot itself, in the order it was drawn

Outside it, and why:

* `generated_at` — the clock, as in Phase 10.1. Re-deriving the same lot
  tomorrow must produce the same identifier;
* the *requested* sample size when it exceeded the dataset — asking for 500 of
  394 records and asking for 394 produce the same lot, and a digest that
  distinguished them would say two identical selections were different. The
  requested size is kept in the artefact as provenance, outside the digest;
* the per-item strata — derived from the dataset and the selector version, so
  already implied by both.

**The labelset fingerprint** answers "which judgements are these". Its exact
domain:

    label_schema_version      the row shape
    protocol_version          the rubric the judgements were made under
    dataset_id                what was judged
    dataset_content_fingerprint
    profile_id                for whom
    profile_context_fingerprint
    selection.selector_version        which lot the judgements cover
    selection.selection_fingerprint
    labels[]                  in opportunity_id order, each contributing its
                              grade, its diagnostics, its reason tags and its
                              note

Outside it, and this is the property the slice is explicitly required to have:

* `labeled_at` — **the same judgements recorded at two different moments
  produce the same labelset fingerprint.** The digest is a statement about
  opinions, not about an annotation session. The timestamps stay in the label
  rows as provenance;
* `revision` and `relabel_reason` — the history of how an opinion was reached.
  A grade corrected from 1 to 2 and a grade given as 2 first time are the same
  opinion, and any metric computed over them computes the same number;
* the unjudged opportunities. They contribute nothing because they *are*
  nothing: an opportunity nobody judged has no row, and a digest that ranged
  over "all selected ids" would have to invent a value for the empty ones — the
  precise mistake this protocol exists to prevent.

Both digests are taken over the canonical form of a Python structure, never over
the bytes of a file, so indentation and key order cannot move them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .schema import (
    HumanRelevanceLabel,
    canonical_label_order,
    human_label_semantic_payload,
)

__all__ = [
    "calibration_selection_fingerprint",
    "canonical_calibration_selection_payload",
    "canonical_labelset_payload",
    "labelset_fingerprint",
]


def canonical_calibration_selection_payload(
    *,
    selector_version: str,
    dataset_id: str,
    dataset_content_fingerprint: str,
    sample_size: int,
    selected_opportunity_ids: Sequence[int],
) -> dict[str, Any]:
    """The selection digest domain, as a structure a test can read and assert on."""
    return {
        "selector_version": selector_version,
        "dataset_id": dataset_id,
        "dataset_content_fingerprint": dataset_content_fingerprint,
        "parameters": {"sample_size": sample_size},
        # The draw order is part of the statement: a lot drawn in a different
        # order is a different lot to work through, so it is not sorted here.
        "selected_opportunity_ids": list(selected_opportunity_ids),
    }


def calibration_selection_fingerprint(
    *,
    selector_version: str,
    dataset_id: str,
    dataset_content_fingerprint: str,
    sample_size: int,
    selected_opportunity_ids: Sequence[int],
) -> str:
    """SHA-256 of the canonical JSON of everything a selection asserts."""
    payload = canonical_calibration_selection_payload(
        selector_version=selector_version,
        dataset_id=dataset_id,
        dataset_content_fingerprint=dataset_content_fingerprint,
        sample_size=sample_size,
        selected_opportunity_ids=selected_opportunity_ids,
    )
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_labelset_payload(
    *,
    label_schema_version: str,
    protocol_version: str,
    dataset_id: str,
    dataset_content_fingerprint: str,
    profile_id: int,
    profile_context_fingerprint: str,
    selector_version: str,
    selection_fingerprint: str,
    labels: Sequence[HumanRelevanceLabel],
) -> dict[str, Any]:
    """The labelset digest domain, built from the judgements and their bindings.

    `labels` may hold at most one entry per opportunity — the effective label,
    as `storage.py` resolves it. Two rows for one opportunity would mean the
    caller passed the raw history rather than the current state, so it is
    refused here rather than digested into a number nobody can explain.
    """
    ordered = canonical_label_order(labels)
    ids = [label.opportunity_id for label in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError(
            "a labelset holds at most one effective label per opportunity"
        )
    return {
        "label_schema_version": label_schema_version,
        "protocol_version": protocol_version,
        "dataset_id": dataset_id,
        "dataset_content_fingerprint": dataset_content_fingerprint,
        "profile_id": profile_id,
        "profile_context_fingerprint": profile_context_fingerprint,
        "selection": {
            "selector_version": selector_version,
            "selection_fingerprint": selection_fingerprint,
        },
        "labels": [human_label_semantic_payload(label) for label in ordered],
    }


def labelset_fingerprint(
    *,
    label_schema_version: str,
    protocol_version: str,
    dataset_id: str,
    dataset_content_fingerprint: str,
    profile_id: int,
    profile_context_fingerprint: str,
    selector_version: str,
    selection_fingerprint: str,
    labels: Sequence[HumanRelevanceLabel],
) -> str:
    """SHA-256 of the canonical JSON of a set of human judgements."""
    payload = canonical_labelset_payload(
        label_schema_version=label_schema_version,
        protocol_version=protocol_version,
        dataset_id=dataset_id,
        dataset_content_fingerprint=dataset_content_fingerprint,
        profile_id=profile_id,
        profile_context_fingerprint=profile_context_fingerprint,
        selector_version=selector_version,
        selection_fingerprint=selection_fingerprint,
        labels=labels,
    )
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
