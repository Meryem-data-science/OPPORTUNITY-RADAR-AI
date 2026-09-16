"""Phase 11.2B CLI: print read-only evidence about the persisted upstream chain.

    DATABASE_BACKEND=sqlite \\
    SQLITE_DATABASE_PATH=<existing SQLite file> \\
    OPPORTUNITY_RADAR_PROFILE_ID=<positive integer> \\
    python -m services.final_validation.upstream_cli

Nothing is defaulted: the backend, the path and the profile must all be stated.
The source registry is the project's own `config/sources.yaml`, read relative to
the working directory exactly as the collector reads it. The evidence goes to
stdout as deterministic JSON.

Exit codes:
    0  validation ran and every check's integrity passed
    1  validation ran and at least one check's integrity failed
    2  configuration was refused, or validation could not run safely

NOT_DEMONSTRATED, NOT_ASSESSED, NEVER_RUN, STALE and UNKNOWN are facts reported
in the evidence; none of them changes the exit code. Errors are reported as a
stable code on stderr, never as a traceback, an exception message or a path.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import TextIO

from services.collector.config import ConfigurationError, DatabaseBackend, load_settings
from services.final_validation.operational_state import PASS, OperationalStateError, parse_profile_id, serialize_evidence
from services.final_validation.upstream_state import SCHEMA_VERSION, validate_upstream_state

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


def _error(stream: TextIO, code: str) -> int:
    stream.write(json.dumps({"error_code": code, "result": "ERROR", "schema_version": SCHEMA_VERSION}, sort_keys=True) + "\n")
    return EXIT_ERROR


def main(
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    validate: Callable[..., dict] = validate_upstream_state,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    if os.environ.get("DATABASE_BACKEND", "").strip() != DatabaseBackend.SQLITE.value:
        return _error(stderr, "SQLITE_BACKEND_REQUIRED")
    if not os.environ.get("SQLITE_DATABASE_PATH", "").strip():
        return _error(stderr, "SQLITE_DATABASE_PATH_REQUIRED")
    if "OPPORTUNITY_RADAR_PROFILE_ID" not in os.environ:
        return _error(stderr, "PROFILE_ID_REQUIRED")
    try:
        settings = load_settings()
    except ConfigurationError:
        return _error(stderr, "INVALID_CONFIGURATION")
    if settings.database_backend is not DatabaseBackend.SQLITE or settings.sqlite_database_path is None:
        return _error(stderr, "SQLITE_BACKEND_REQUIRED")
    try:
        profile_id = parse_profile_id(os.environ["OPPORTUNITY_RADAR_PROFILE_ID"])
        evidence = validate(settings.sqlite_database_path, profile_id)
    except OperationalStateError as error:
        return _error(stderr, error.code)

    stdout.write(serialize_evidence(evidence) + "\n")
    return EXIT_PASS if evidence["integrity_result"] == PASS else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
