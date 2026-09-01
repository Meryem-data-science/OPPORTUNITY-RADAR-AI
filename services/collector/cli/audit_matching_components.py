"""Aggregate-only CLI for the matching component audit."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from services.collector.matching.component_audit import audit_matching_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit matching components without scores or writes"
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--profile-id", required=True, type=int)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    return parser


def _payload(report: object) -> dict[str, object]:
    payload = asdict(report)  # type: ignore[arg-type]
    payload.pop("observations", None)
    return payload


def main() -> None:
    args = build_parser().parse_args()
    payload = _payload(audit_matching_database(args.database, args.profile_id))
    if args.format == "json":
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print("Matching component audit (STRICTLY READ-ONLY; aggregate facts only)")
        print(json.dumps(payload, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
