"""Phase 11.2A CLI: print read-only evidence about the operational SQLite state.

    DATABASE_BACKEND=sqlite \\
    SQLITE_DATABASE_PATH=<existing SQLite file> \\
    OPPORTUNITY_RADAR_PROFILE_ID=<positive integer> \\
    python -m services.final_validation.cli

Nothing is defaulted: the backend, the path and the profile must all be stated.
The evidence goes to stdout as deterministic JSON.

Exit codes:
    0  validation ran and every critical check passed
    1  validation ran and at least one critical check failed
    2  configuration was refused, or validation could not run safely

Errors are reported as a stable code on stderr, never as a traceback, an
exception message or a path.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import TextIO

from services.collector.config import ConfigurationError, DatabaseBackend, load_settings
from services.final_validation.operational_state import (
    PASS,
    SCHEMA_VERSION,
    OperationalStateError,
    parse_profile_id,
    serialize_evidence,
    validate_operational_state,
)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


def _error(stream: TextIO, code: str) -> int:
    stream.write(json.dumps({"error_code": code, "result": "ERROR", "schema_version": SCHEMA_VERSION}, sort_keys=True) + "\n")
    return EXIT_ERROR


def main(
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    validate: Callable[..., dict] = validate_operational_state,
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
    return EXIT_PASS if evidence["result"] == PASS else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
