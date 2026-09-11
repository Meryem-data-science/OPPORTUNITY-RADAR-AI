"""The integrity gate: reading a frozen Phase 10.1 dataset without trusting it.

A labelling session is a person spending real attention on real postings. The
one thing that must never happen is for that attention to be spent on a dataset
that is not the dataset it claims to be — a manifest edited by hand, a JSONL
half-copied, a directory renamed, a record dropped from the middle. Every one of
those still *parses*: JSON always parses. So nothing here is taken on trust.

What is verified before a single opportunity is shown, in this order:

    manifest.json exists, is readable and is a JSON object
    schema_version is one this build understands   (Phase 10.1's own check)
    the directory name equals manifest.dataset_id
    the cohort ordering is the canonical order Phase 10.1 states
    dataset_id is consistent with content_fingerprint
    opportunities.jsonl exists and every line is a JSON object
    every record carries exactly the fields the Phase 10.1 record contract has
    the number of records equals manifest.record_count
    opportunity ids are integers, and unique
    the records are in strictly ascending opportunity_id order
    the content fingerprint recomputed from manifest + records matches the
        one the manifest claims for itself

The last check is the one that makes the others more than paperwork, and it is
computed **through Phase 10.1's own primitives**: `manifest_fingerprint_domain`
projects the stored manifest onto exactly the half of it the digest covers, and
each record's digest is the SHA-256 of `canonical_json` of the very line the
file holds. There is no second definition of the domain here — a divergent copy
would eventually disagree with the writer and the disagreement would surface as
a corrupt dataset that is not corrupt, or worse, the reverse.

The record contract itself is not re-listed either: the required field set is
read off `EvaluationOpportunityRecord`'s dataclass fields, so a Phase 10.1
record that gains a field makes this reader require it rather than silently
accept files written under two different shapes.

**No SQLite is opened.** A frozen dataset is a pair of files; the operational
database is not consulted, not needed, and not reachable from this module. That
is what makes a labelling session reproducible on a machine that has the dataset
and nothing else.

Records are kept as the mappings the file holds rather than re-parsed into the
Phase 10.1 dataclasses. Two reasons: the digest is taken over exactly what was
read, with no parse-and-re-serialize step that could normalize a difference
away; and the annotator's evidence — a description, a URL — reaches the blind
view as the bytes the dataset froze, never as something this package rebuilt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from evaluation.dataset import (
    EVALUATION_CANONICAL_ORDER,
    EVALUATION_DATASET_SCHEMA_VERSION,
    EvaluationOpportunityRecord,
    require_supported_schema_version,
)
from evaluation.dataset.fingerprint import manifest_fingerprint_domain
from evaluation.dataset.storage import MANIFEST_FILENAME, RECORDS_FILENAME

from .schema import HumanLabelError

__all__ = [
    "EVALUATION_RECORD_CONTRACT_FIELDS",
    "FrozenDatasetIntegrityError",
    "FrozenEvaluationDataset",
    "read_frozen_dataset",
]


class FrozenDatasetIntegrityError(HumanLabelError):
    """Raised when a frozen dataset on disk cannot be trusted as read."""


#: The record contract, derived from Phase 10.1 rather than restated. The
#: JSONL payload uses exactly the dataclass field names — a property this
#: package's tests assert, so the derivation cannot rot quietly.
EVALUATION_RECORD_CONTRACT_FIELDS: tuple[str, ...] = tuple(
    field.name for field in fields(EvaluationOpportunityRecord)
)


@dataclass(frozen=True)
class FrozenEvaluationDataset:
    """A verified frozen dataset, read-only, with its identity to hand.

    Everything a label has to be bound to is on this object, which is why the
    labelling code takes a `FrozenEvaluationDataset` and never a path: a caller
    that has one has already passed the integrity gate, and a caller that has a
    path has not.
    """

    directory: Path
    schema_version: str
    dataset_id: str
    content_fingerprint: str
    record_count: int
    profile_id: int
    user_id: int
    profile_context_fingerprint: str
    ordering: str
    #: In the dataset's canonical order, exactly as the file holds them.
    records: tuple[Mapping[str, Any], ...]

    @property
    def opportunity_ids(self) -> tuple[int, ...]:
        return tuple(int(record["opportunity_id"]) for record in self.records)

    def record(self, opportunity_id: int) -> Mapping[str, Any]:
        """One record by id, or a refusal naming the dataset it is missing from."""
        for record in self.records:
            if int(record["opportunity_id"]) == opportunity_id:
                return record
        raise FrozenDatasetIntegrityError(
            f"opportunity {opportunity_id} is not in dataset {self.dataset_id}"
        )

    def contains(self, opportunity_id: int) -> bool:
        return any(
            int(record["opportunity_id"]) == opportunity_id
            for record in self.records
        )


def _read_manifest(directory: Path) -> dict[str, Any]:
    path = directory / MANIFEST_FILENAME
    if not path.is_file():
        raise FrozenDatasetIntegrityError(
            f"{directory} holds no {MANIFEST_FILENAME}; it is not a frozen "
            "evaluation dataset"
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FrozenDatasetIntegrityError(
            f"the manifest of {directory} cannot be read: {error}"
        ) from error
    if not isinstance(manifest, dict):
        raise FrozenDatasetIntegrityError(
            f"the manifest of {directory} is not a JSON object"
        )
    return manifest


def _read_records(directory: Path) -> tuple[dict[str, Any], ...]:
    path = directory / RECORDS_FILENAME
    if not path.is_file():
        raise FrozenDatasetIntegrityError(
            f"{directory} holds no {RECORDS_FILENAME}; it is not a frozen "
            "evaluation dataset"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise FrozenDatasetIntegrityError(
            f"the records of {directory} cannot be read: {error}"
        ) from error

    records: list[dict[str, Any]] = []
    expected = set(EVALUATION_RECORD_CONTRACT_FIELDS)
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            # Not tolerated. A blank line is a symptom — a truncated write, a
            # concatenation, an editor — and a reader that skips it would make
            # a damaged file look intact.
            raise FrozenDatasetIntegrityError(
                f"{path} line {number} is blank; refusing a damaged dataset"
            )
        try:
            record = json.loads(line)
        except ValueError as error:
            raise FrozenDatasetIntegrityError(
                f"{path} line {number} is not valid JSON: {error}"
            ) from error
        if not isinstance(record, dict):
            raise FrozenDatasetIntegrityError(
                f"{path} line {number} is not a JSON object"
            )
        found = set(record)
        if found != expected:
            missing = ", ".join(sorted(expected - found))
            unexpected = ", ".join(sorted(found - expected))
            raise FrozenDatasetIntegrityError(
                f"{path} line {number} does not match the evaluation record "
                f"contract (missing: {missing or 'none'}; "
                f"unexpected: {unexpected or 'none'})"
            )
        records.append(record)
    if not records:
        raise FrozenDatasetIntegrityError(f"{path} holds no record")
    return tuple(records)


def _opportunity_ids(records: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    ids: list[int] = []
    for position, record in enumerate(records, start=1):
        value = record["opportunity_id"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise FrozenDatasetIntegrityError(
                f"record {position} states a non-integer opportunity_id "
                f"{value!r}"
            )
        ids.append(value)
    if len(set(ids)) != len(ids):
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        raise FrozenDatasetIntegrityError(
            f"the dataset repeats opportunity ids: {duplicates}"
        )
    return tuple(ids)


def _string(manifest: Mapping[str, Any], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise FrozenDatasetIntegrityError(f"the manifest states no {key}")
    return value


def _profile_context(manifest: Mapping[str, Any]) -> tuple[int, int, str]:
    context = manifest.get("profile_context")
    if not isinstance(context, Mapping):
        raise FrozenDatasetIntegrityError(
            "the manifest states no profile_context; a dataset with no profile "
            "binding cannot carry a personalised human judgement"
        )
    profile_id = context.get("profile_id")
    user_id = context.get("user_id")
    fingerprint = context.get("fingerprint")
    if isinstance(profile_id, bool) or not isinstance(profile_id, int):
        raise FrozenDatasetIntegrityError(
            f"the manifest states a non-integer profile_id {profile_id!r}"
        )
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise FrozenDatasetIntegrityError(
            f"the manifest states a non-integer user_id {user_id!r}"
        )
    if not isinstance(fingerprint, str) or not fingerprint:
        raise FrozenDatasetIntegrityError(
            "the manifest states no profile context fingerprint"
        )
    return profile_id, user_id, fingerprint


def _recompute_content_fingerprint(
    manifest: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> str:
    """Phase 10.1's digest, recomputed from what the two files actually say.

    `manifest_fingerprint_domain` is imported rather than reimplemented; the
    record digests are taken over the canonical form of the parsed lines, which
    is the same domain `evaluation_record_fingerprint` covers because the line
    *is* `evaluation_record_payload`.
    """
    domain = manifest_fingerprint_domain(manifest)
    domain["records"] = [
        hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()
        for record in records
    ]
    return hashlib.sha256(canonical_json(domain).encode("utf-8")).hexdigest()


def read_frozen_dataset(directory: str | Path) -> FrozenEvaluationDataset:
    """Read and verify a frozen Phase 10.1 dataset, or refuse it.

    Read-only in the strongest sense available: two `read_text` calls, no write,
    no database, no repair. Anything that does not add up raises
    `FrozenDatasetIntegrityError` *before* an annotator is shown a single
    posting, because a judgement made against a corrupt dataset is worse than no
    judgement — it looks like evidence.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise FrozenDatasetIntegrityError(f"{directory} is not a directory")

    manifest = _read_manifest(directory)
    schema_version = require_supported_schema_version(manifest)
    dataset_id = _string(manifest, "dataset_id")
    if directory.name != dataset_id:
        raise FrozenDatasetIntegrityError(
            f"{directory} holds dataset_id {dataset_id!r}; a frozen dataset "
            "lives in the directory named after it, and a mismatch means the "
            "directory or the manifest was moved or edited"
        )

    content_fingerprint = _string(manifest, "content_fingerprint")
    # Phase 10.1 names a dataset after the digest of its own content. Checking
    # that here costs one comparison and catches a manifest whose id and content
    # were edited apart.
    expected_id = f"{EVALUATION_DATASET_SCHEMA_VERSION}-{content_fingerprint[:16]}"
    if dataset_id != expected_id:
        raise FrozenDatasetIntegrityError(
            f"dataset_id {dataset_id!r} is not the identifier its content "
            f"fingerprint implies ({expected_id!r})"
        )

    cohort = manifest.get("cohort")
    if not isinstance(cohort, Mapping):
        raise FrozenDatasetIntegrityError("the manifest states no cohort")
    ordering = cohort.get("ordering")
    if ordering != EVALUATION_CANONICAL_ORDER:
        raise FrozenDatasetIntegrityError(
            f"the dataset states ordering {ordering!r}; this build only reads "
            f"datasets ordered {EVALUATION_CANONICAL_ORDER!r}"
        )

    record_count = manifest.get("record_count")
    if isinstance(record_count, bool) or not isinstance(record_count, int):
        raise FrozenDatasetIntegrityError(
            f"the manifest states a non-integer record_count {record_count!r}"
        )

    records = _read_records(directory)
    if len(records) != record_count:
        raise FrozenDatasetIntegrityError(
            f"the manifest states record_count {record_count} but "
            f"{RECORDS_FILENAME} holds {len(records)} records"
        )

    ids = _opportunity_ids(records)
    if list(ids) != sorted(ids):
        raise FrozenDatasetIntegrityError(
            "the records are not in the canonical order "
            f"({EVALUATION_CANONICAL_ORDER}); refusing a reordered dataset"
        )

    recomputed = _recompute_content_fingerprint(manifest, records)
    if recomputed != content_fingerprint:
        raise FrozenDatasetIntegrityError(
            f"dataset {dataset_id} claims content fingerprint "
            f"{content_fingerprint} but its manifest and records digest to "
            f"{recomputed}; refusing to label a dataset that is not what it "
            "says it is"
        )

    profile_id, user_id, profile_fingerprint = _profile_context(manifest)
    return FrozenEvaluationDataset(
        directory=directory,
        schema_version=schema_version,
        dataset_id=dataset_id,
        content_fingerprint=content_fingerprint,
        record_count=record_count,
        profile_id=profile_id,
        user_id=user_id,
        profile_context_fingerprint=profile_fingerprint,
        ordering=ordering,
        records=records,
    )
