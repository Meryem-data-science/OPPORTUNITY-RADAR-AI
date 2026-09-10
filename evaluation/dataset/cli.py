"""Strictly read-only CLI that freezes one evaluation dataset.

    python -m evaluation.dataset.cli \
        --database data/opportunity-radar.db \
        --profile-id 1

Same shape as every read-only CLI in this project — `--database`, an explicit
`--profile-id`, a JSON report on stdout — and the same connection helper:
`connect_readonly_database` opens a `mode=ro` SQLite URI, so a mistyped path
fails instead of leaving an empty database behind, and no statement this process
runs can write to the operational database even by mistake.

`--dry-run` builds the dataset and reports it without writing a single file,
which is what a determinism check wants: the fingerprint is a property of the
data, so two dry runs must agree before anything is stored.

This is the minimum needed to use the feature. It is deliberately not an
experiment runner: baselines, metrics and comparisons belong to the later Phase
10 slices, and adding a hook for them here would be building 10.5 in 10.1.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from services.collector.database.connection import connect_readonly_database

from .schema import EvaluationDataset, EvaluationDatasetError
from .snapshot import build_evaluation_dataset, resolve_git_commit
from .storage import DEFAULT_EVALUATION_DATASET_ROOT, write_evaluation_dataset


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Freeze one evaluation dataset.")
    parser.add_argument("--database", required=True)
    parser.add_argument("--profile-id", required=True, type=_positive_integer)
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_EVALUATION_DATASET_ROOT),
        help="directory the dataset directory is created under",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and report the dataset without writing any file",
    )
    return parser.parse_args(argv)


def summarize(dataset: EvaluationDataset) -> dict:
    """The distributions a reader needs to check the cohort at a glance.

    Counted from the records themselves rather than re-queried, so the summary
    describes the dataset that was just built and cannot disagree with it. The
    `null` keys are the interesting ones: an opportunity in the cohort with no
    recommendation is one the engine never ranked.
    """
    manifest = dataset.manifest
    qualifications = Counter(
        record.qualification.qualification for record in dataset.records
    )
    eligibility = Counter(
        "null" if record.eligibility is None else record.eligibility.status
        for record in dataset.records
    )
    matching = Counter(
        "null" if record.matching is None else record.matching.lane
        for record in dataset.records
    )
    recommendation = Counter(
        "null" if record.recommendation is None else record.recommendation.disposition
        for record in dataset.records
    )
    return {
        "dataset_id": manifest.dataset_id,
        "schema_version": manifest.schema_version,
        "generated_at": manifest.generated_at,
        "git_commit": manifest.git_commit,
        "profile_id": manifest.profile_id,
        "user_id": manifest.user_id,
        "record_count": manifest.record_count,
        "content_fingerprint": manifest.content_fingerprint,
        "source": {
            "database_path": manifest.source.database_path,
            "database_bytes": manifest.source.database_bytes,
            "database_sha256": manifest.source.database_sha256,
            "wal_present": manifest.source.wal_present,
            "shm_present": manifest.source.shm_present,
        },
        "cohort": {
            "version": manifest.cohort.version,
            "ordering": manifest.cohort.ordering,
            "excluded_counts": dict(manifest.cohort.excluded_counts),
        },
        "upstream": {
            "matching_status": manifest.upstream.matching_status,
            "matching_run_id": manifest.upstream.matching_run_id,
            "recommendation_status": manifest.upstream.recommendation_status,
            "recommendation_run_id": manifest.upstream.recommendation_run_id,
        },
        "distribution": {
            "qualification": dict(sorted(qualifications.items())),
            "eligibility_status": dict(sorted(eligibility.items())),
            "matching_lane": dict(sorted(matching.items())),
            "recommendation_disposition": dict(sorted(recommendation.items())),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with connect_readonly_database(args.database) as connection:
            dataset = build_evaluation_dataset(
                connection,
                profile_id=args.profile_id,
                database_path=args.database,
                git_commit=resolve_git_commit(),
            )
    except EvaluationDatasetError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    payload = summarize(dataset)
    payload["written"] = not args.dry_run
    if not args.dry_run:
        try:
            paths = write_evaluation_dataset(dataset, args.output_root)
        except EvaluationDatasetError as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
            return 2
        payload["paths"] = {
            "directory": str(paths.directory),
            "manifest": str(paths.manifest),
            "records": str(paths.records),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
