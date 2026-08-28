"""Local CLI to initialise and read the real Digital Twin profile.

Nothing here prints, logs, or stores the address beyond the `users` row it is
meant to create: the operator's email is the input, `user_id` and `profile_id`
are the output. The interactive prompt exists so a real person can avoid
leaving their address in the shell history; `--email` stays available for tests
and automation.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from getpass import getpass

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.digital_twin.repository import (
    ensure_user_profile,
    get_user_profile_by_email,
)

INIT_COMMAND = "init-profile"
SHOW_COMMAND = "show-profile"
PROMPT = "Email (not echoed, not logged): "
NON_SQLITE_BACKEND_ERROR = (
    "the Digital Twin profile root is local SQLite only; "
    "set DATABASE_BACKEND=sqlite"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialise or read the local Digital Twin user profile."
    )
    parser.add_argument("command", choices=(INIT_COMMAND, SHOW_COMMAND))
    parser.add_argument(
        "--email",
        default=None,
        help=(
            "Address to use. Omit it to be prompted without echo, so the "
            "address never enters the shell history."
        ),
    )
    return parser.parse_args(argv)


def _read_email(provided: str | None, prompt: Callable[[str], str]) -> str:
    return provided if provided is not None else prompt(PROMPT)


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Callable[[str], str] = getpass,
) -> int:
    """Run one profile command against the configured local SQLite database."""
    # Child of the configured "services.collector" logger, as the read-only API
    # service already does, so events use the shared sanitized JSON handler.
    logger = get_logger("services.collector.digital_twin.cli")
    backend = "unconfigured"
    try:
        args = parse_args(argv)
        settings = load_settings()
        backend = settings.database_backend.value
        if settings.database_backend is not DatabaseBackend.SQLITE:
            # Refused before connecting: no remote database is contacted.
            raise PermissionError(NON_SQLITE_BACKEND_ERROR)
        email = _read_email(args.email, prompt)
        connection = connect_configured_database(settings)
        try:
            if args.command == INIT_COMMAND:
                result = ensure_user_profile(connection, email)
            else:
                result = get_user_profile_by_email(connection, email)
        finally:
            connection.close()
    except Exception as error:
        # The message may quote configuration, never the address itself.
        print(f"profile command failed: {type(error).__name__}: {error}")
        logger.error(
            "Digital Twin profile command failed.",
            extra={
                "event": "digital_twin_profile_command_failed",
                "database_backend": backend,
                "error_type": type(error).__name__,
            },
        )
        return 1

    if result is None:
        print("no profile exists for that address")
        logger.info(
            "Digital Twin profile not found.",
            extra={
                "event": "digital_twin_profile_not_found",
                "database_backend": backend,
            },
        )
        return 1

    state = "created" if result.created else "existing"
    print(f"profile {state} user_id={result.user_id} profile_id={result.profile_id}")
    logger.info(
        "Digital Twin profile command succeeded.",
        extra={
            "event": "digital_twin_profile_command_succeeded",
            "database_backend": backend,
            "profile_created": result.created,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
