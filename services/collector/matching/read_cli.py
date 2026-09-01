"""Deterministic, strictly read-only CLI for persisted matching data."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from services.collector.database.connection import connect_readonly_database

from .persistence_audit import audit_matching_profile_history
from .read_model import list_matching_runs, read_current_matching, read_matching_run


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read and audit persisted matching snapshots."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("current", "history", "audit"):
        child = subparsers.add_parser(command)
        child.add_argument("--database", required=True)
        child.add_argument("--profile-id", required=True, type=_positive_integer)
    run = subparsers.add_parser("run")
    run.add_argument("--database", required=True)
    run.add_argument("--run-id", required=True, type=_positive_integer)
    return parser.parse_args(argv)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _current_payload(model: Any) -> dict[str, Any]:
    payload = {
        "profile_id": model.profile_id,
        "status": model.status,
        "persistence_version": model.persistence_version,
        "selection_version": model.selection_version,
        "history_count": model.history_count,
        "current_run_id": model.current_run_id,
    }
    if model.current_run is not None:
        run = model.current_run
        lane_counts = {
            lane: 0 for lane in ("PRIMARY", "UNCERTAIN", "OUTSIDE_PREFERENCES")
        }
        for assessment in run.assessments:
            lane_counts[assessment.lane] += 1
        payload["current_run"] = {
            "run_id": run.run_id,
            "assessment_count": run.assessment_count,
            "run_fingerprint": run.run_fingerprint,
            "batch_fingerprint": run.batch_fingerprint,
            "matching_engine_version": run.matching_engine_version,
            "matching_rules_version": run.matching_rules_version,
            "semantic_percentile_version": run.semantic_percentile_version,
            "lane_counts": lane_counts,
        }
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with connect_readonly_database(args.database) as connection:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise RuntimeError("SQLite query_only mode could not be enabled")
            if args.command == "current":
                payload = _current_payload(
                    read_current_matching(connection, args.profile_id)
                )
            elif args.command == "history":
                payload = {
                    "profile_id": args.profile_id,
                    "runs": _jsonable(list_matching_runs(connection, args.profile_id)),
                }
            elif args.command == "run":
                payload = _jsonable(read_matching_run(connection, args.run_id))
            else:
                payload = _jsonable(
                    audit_matching_profile_history(connection, args.profile_id)
                )
    except Exception as error:
        print(f"matching read failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
