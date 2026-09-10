"""Where a frozen dataset lands on disk, and in what exact bytes.

Two files per dataset, under the project's local data space:

    data/evaluation/datasets/<dataset_id>/manifest.json
    data/evaluation/datasets/<dataset_id>/opportunities.jsonl

**Nothing here touches the operational database.** Phase 10.1 adds no migration,
no evaluation table and no column: an evaluation artefact that lived inside the
operational schema would invert the dependency this slice exists to establish,
and the next production read would be one join away from depending on it.

**The bytes are stable.** `opportunities.jsonl` is one canonical JSON object per
record, in the dataset's canonical order, so two extractions over an unchanged
database produce files that are identical byte for byte and `diff` says nothing.
`manifest.json` is written indented because a person reads it, and sorted keys
with a fixed separator make that indentation just as reproducible. Neither
file's formatting reaches the fingerprint, which is taken over the payload
structure and never over the bytes.

**The directory name is the content.** `dataset_id` is derived from the content
fingerprint, so re-running an extraction over unchanged data overwrites its own
directory with the same bytes instead of accumulating near-identical snapshots,
and a change in the data lands somewhere new without disturbing the old one.

**Nothing written here is ever committed.** These files hold real opportunity
data — organizations, URLs, and a specific person's eligibility decisions — so
`data/` is Git-ignored in full.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from services.collector.matching.fingerprint import canonical_json

from .schema import (
    EvaluationDataset,
    EvaluationDatasetError,
    evaluation_manifest_payload,
    evaluation_record_payload,
)

__all__ = [
    "DEFAULT_EVALUATION_DATASET_ROOT",
    "EvaluationDatasetPaths",
    "write_evaluation_dataset",
]

#: Relative on purpose, and under `data/` like the operational SQLite database
#: it is taken from: both are local, both are ignored by Git, and neither
#: belongs to a deployment.
DEFAULT_EVALUATION_DATASET_ROOT = Path("data/evaluation/datasets")

MANIFEST_FILENAME = "manifest.json"
RECORDS_FILENAME = "opportunities.jsonl"


@dataclass(frozen=True)
class EvaluationDatasetPaths:
    """Where a dataset was written."""

    directory: Path
    manifest: Path
    records: Path


def write_evaluation_dataset(
    dataset: EvaluationDataset,
    root: str | Path = DEFAULT_EVALUATION_DATASET_ROOT,
) -> EvaluationDatasetPaths:
    """Write the manifest and the records, and return where they went."""
    directory = Path(root) / dataset.manifest.dataset_id
    manifest_path = directory / MANIFEST_FILENAME
    records_path = directory / RECORDS_FILENAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        records_path.write_text(
            "".join(
                canonical_json(evaluation_record_payload(record)) + "\n"
                for record in dataset.records
            ),
            encoding="utf-8",
        )
        manifest_path.write_text(
            json.dumps(
                evaluation_manifest_payload(dataset.manifest),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise EvaluationDatasetError(
            f"cannot write the evaluation dataset to {directory}"
        ) from error
    return EvaluationDatasetPaths(
        directory=directory, manifest=manifest_path, records=records_path
    )
