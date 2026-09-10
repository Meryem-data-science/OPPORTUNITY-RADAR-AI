"""Where a frozen dataset lands on disk, and why it never moves again.

Two files per dataset, under the project's local data space:

    data/evaluation/datasets/<dataset_id>/manifest.json
    data/evaluation/datasets/<dataset_id>/opportunities.jsonl

**Nothing here touches the operational database.** Phase 10.1 adds no migration,
no evaluation table and no column: an evaluation artefact that lived inside the
operational schema would invert the dependency this slice exists to establish,
and the next production read would be one join away from depending on it.

**Frozen means frozen.** `dataset_id` is derived from the content fingerprint,
so re-extracting unchanged data resolves to a directory that already exists.
Rewriting it would replace the stored manifest — and with it `generated_at`,
which is outside the fingerprint precisely because it is allowed to differ
between two extractions of the same data. A snapshot whose recorded moment kept
sliding forward would not be a frozen dataset, and a human label attached to it
in Phase 10.2 would be attached to something that had since been rewritten. So
an existing dataset is verified and left alone:

    directory absent              write both files, report CREATED
    present and identical         write nothing, report UNCHANGED, and return
                                  the `generated_at` the snapshot was frozen at
    present and different         raise, and change nothing

That last case is the one that matters, and "different" is decided
**semantically**: every field the digest covers is compared field by field
against the stored manifest, along with `dataset_id` and `record_count`, rather
than the stored `content_fingerprint` being taken at its word. A manifest can be
edited while keeping the digest it claims for itself, and such a file must never
pass as UNCHANGED. There is no lock here — that is out of scope for this slice —
but there is also no silent overwrite and no trusted self-report, which is what
the absence of a lock would otherwise cost.

**The bytes are stable.** `opportunities.jsonl` is one canonical JSON object per
record, in the dataset's canonical order, so two extractions over an unchanged
database produce files that are identical byte for byte and `diff` says nothing.
`manifest.json` is written indented because a person reads it, and sorted keys
with a fixed separator make that indentation just as reproducible. Neither
file's formatting reaches the fingerprint, which is taken over the payload
structure and never over the bytes.

**Nothing written here is ever committed.** These files hold real opportunity
data — organizations, URLs, and a specific person's eligibility decisions — so
`data/` is Git-ignored in full.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from services.collector.matching.fingerprint import canonical_json

from .fingerprint import manifest_fingerprint_domain
from .schema import (
    EvaluationDataset,
    EvaluationDatasetError,
    evaluation_manifest_payload,
    evaluation_record_payload,
)

__all__ = [
    "DEFAULT_EVALUATION_DATASET_ROOT",
    "EvaluationDatasetPaths",
    "MANIFEST_FILENAME",
    "RECORDS_FILENAME",
    "WRITE_STATUS_CREATED",
    "WRITE_STATUS_UNCHANGED",
    "write_evaluation_dataset",
]

#: Relative on purpose, and under `data/` like the operational SQLite database
#: it is taken from: both are local, both are ignored by Git, and neither
#: belongs to a deployment.
DEFAULT_EVALUATION_DATASET_ROOT = Path("data/evaluation/datasets")

MANIFEST_FILENAME = "manifest.json"
RECORDS_FILENAME = "opportunities.jsonl"

#: This extraction froze the dataset.
WRITE_STATUS_CREATED = "CREATED"
#: The dataset was already frozen, by this extraction or an earlier one, and was
#: verified rather than rewritten.
WRITE_STATUS_UNCHANGED = "UNCHANGED"


@dataclass(frozen=True)
class EvaluationDatasetPaths:
    """Where a dataset lives, and what this call did about it.

    `frozen_generated_at` is read back from the manifest on disk, so a caller
    that finds an existing dataset learns when it was actually frozen instead of
    reporting the moment of the extraction that merely re-derived it.
    """

    directory: Path
    manifest: Path
    records: Path
    status: str
    frozen_generated_at: str


def _render(dataset: EvaluationDataset) -> tuple[str, str]:
    """The exact bytes of both files, as text."""
    records = "".join(
        canonical_json(evaluation_record_payload(record)) + "\n"
        for record in dataset.records
    )
    manifest = (
        json.dumps(
            evaluation_manifest_payload(dataset.manifest),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return manifest, records


def _stored_manifest(
    dataset_id: str, directory: Path, manifest_path: Path
) -> dict:
    """Read back the manifest of an existing dataset, or refuse the directory."""
    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EvaluationDatasetError(
            f"evaluation dataset {dataset_id} exists at {directory} but its "
            "manifest cannot be read; remove the directory to re-freeze it"
        ) from error
    if not isinstance(stored, dict):
        raise EvaluationDatasetError(
            f"the manifest of evaluation dataset {dataset_id} is not an object"
        )
    return stored


def _verify_existing(
    dataset: EvaluationDataset,
    directory: Path,
    manifest_path: Path,
    records_path: Path,
    records: str,
) -> str:
    """Confirm a directory already holds this exact snapshot, or refuse.

    Returns the `generated_at` the existing manifest was frozen at.

    The check is **semantic, not just a fingerprint comparison**. A stored
    `content_fingerprint` is a claim the manifest makes about itself, and a
    manifest that was edited — by hand, by a partial restore, by a script — can
    keep that claim while its `profile_context`, its cohort, its upstream
    provenance or its `dataset_id` now say something else entirely. Trusting the
    digest alone would let such a manifest pass as UNCHANGED and be handed to
    Phase 10.2 as though it described this dataset.

    So everything the digest covers is compared field by field, through
    `manifest_fingerprint_domain` — the one definition of that domain, shared
    with the code that computes the digest in the first place — together with
    the identity and size fields the digest deliberately leaves out
    (`dataset_id`, `record_count`) and the record bytes themselves.

    What is allowed to differ, because it is execution metadata and nothing a
    measurement can see: `generated_at`, `git_commit`, every field of `source`
    (path, size, digest, WAL flags, applied migrations) and the autoincrement
    `matching_run_id` / `recommendation_run_id`. A second extraction differing
    only in those is UNCHANGED and rewrites nothing.
    """
    dataset_id = dataset.manifest.dataset_id
    if not manifest_path.is_file() or not records_path.is_file():
        raise EvaluationDatasetError(
            f"evaluation dataset {dataset_id} exists at {directory} but is "
            "incomplete; remove the directory to re-freeze it"
        )
    stored = _stored_manifest(dataset_id, directory, manifest_path)
    expected = evaluation_manifest_payload(dataset.manifest)

    # Identity. The directory name is structurally the id this call derived, so
    # it can only disagree if a caller pointed somewhere else; the manifest's own
    # `dataset_id` can disagree because a file was edited.
    for label, found in (
        ("directory name", directory.name),
        ("dataset_id", stored.get("dataset_id")),
    ):
        if found != dataset_id:
            raise EvaluationDatasetError(
                f"evaluation dataset at {directory} states {label} {found!r}, "
                f"not {dataset_id!r}; refusing to overwrite a frozen dataset"
            )

    # Size, which the digest leaves out because the record list already fixes
    # it — and which an edited manifest can therefore contradict freely.
    if stored.get("record_count") != expected["record_count"]:
        raise EvaluationDatasetError(
            f"evaluation dataset {dataset_id} states record_count "
            f"{stored.get('record_count')!r}, not {expected['record_count']!r}; "
            "refusing to overwrite a frozen dataset"
        )

    # Everything the digest covers, compared as values rather than trusted
    # through the digest the manifest claims for itself.
    stored_domain = manifest_fingerprint_domain(stored)
    expected_domain = manifest_fingerprint_domain(expected)
    if canonical_json(stored_domain) != canonical_json(expected_domain):
        differing = sorted(
            key
            for key in expected_domain
            if canonical_json(stored_domain.get(key)) != canonical_json(
                expected_domain[key]
            )
        )
        raise EvaluationDatasetError(
            f"evaluation dataset {dataset_id} already exists at {directory} "
            f"and its {', '.join(differing)} do not match this snapshot; "
            "refusing to overwrite a frozen dataset"
        )

    stored_fingerprint = stored.get("content_fingerprint")
    if stored_fingerprint != dataset.manifest.content_fingerprint:
        raise EvaluationDatasetError(
            f"evaluation dataset {dataset_id} already exists at {directory} "
            f"with content fingerprint {stored_fingerprint!r}, not "
            f"{dataset.manifest.content_fingerprint!r}; refusing to overwrite "
            "a frozen dataset"
        )

    try:
        stored_records = records_path.read_text(encoding="utf-8")
    except OSError as error:
        raise EvaluationDatasetError(
            f"cannot read the records of evaluation dataset {dataset_id}"
        ) from error
    if stored_records != records:
        # The fingerprints agreed and the bytes did not, so one of the two is
        # lying. Neither is trusted over the other here.
        raise EvaluationDatasetError(
            f"evaluation dataset {dataset_id} already exists at {directory} "
            "with the same fingerprint but different records; refusing to "
            "overwrite a frozen dataset"
        )

    frozen_at = stored.get("generated_at")
    if not isinstance(frozen_at, str) or not frozen_at:
        raise EvaluationDatasetError(
            f"the manifest of evaluation dataset {dataset_id} states no "
            "generated_at"
        )
    return frozen_at


def write_evaluation_dataset(
    dataset: EvaluationDataset,
    root: str | Path = DEFAULT_EVALUATION_DATASET_ROOT,
) -> EvaluationDatasetPaths:
    """Freeze the dataset if it is new, verify it if it is not, never overwrite."""
    directory = Path(root) / dataset.manifest.dataset_id
    manifest_path = directory / MANIFEST_FILENAME
    records_path = directory / RECORDS_FILENAME
    manifest, records = _render(dataset)

    if directory.exists():
        frozen_at = _verify_existing(
            dataset, directory, manifest_path, records_path, records
        )
        return EvaluationDatasetPaths(
            directory=directory,
            manifest=manifest_path,
            records=records_path,
            status=WRITE_STATUS_UNCHANGED,
            frozen_generated_at=frozen_at,
        )

    try:
        directory.mkdir(parents=True)
        # Records first, manifest last: the manifest is the marker that says the
        # dataset is complete, so an interrupted write leaves a directory
        # `_verify_existing` refuses rather than one a later read would trust.
        records_path.write_text(records, encoding="utf-8")
        manifest_path.write_text(manifest, encoding="utf-8")
    except OSError as error:
        raise EvaluationDatasetError(
            f"cannot write the evaluation dataset to {directory}"
        ) from error
    return EvaluationDatasetPaths(
        directory=directory,
        manifest=manifest_path,
        records=records_path,
        status=WRITE_STATUS_CREATED,
        frozen_generated_at=dataset.manifest.generated_at,
    )
