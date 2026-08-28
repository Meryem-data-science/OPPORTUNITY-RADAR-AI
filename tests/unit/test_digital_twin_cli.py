"""Unit coverage for the local Digital Twin profile CLI."""

from io import StringIO
import json

from services.collector import logging_config
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin import cli

# TEST ONLY address; no real identity is ever used or stored by the tests.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_LOCAL_PART = "student"


class _SilentLogger:
    """Keeps structured events out of the stdout assertions."""

    def info(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


def _silence_logging(monkeypatch) -> None:
    monkeypatch.setattr(cli, "get_logger", lambda name: _SilentLogger())


def _sqlite_environment(monkeypatch, tmp_path):
    path = tmp_path / "cli.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


def _counts(path) -> tuple[int, int]:
    connection = connect_database(path)
    try:
        users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        profiles = connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    finally:
        connection.close()
    return int(users), int(profiles)


def test_init_profile_is_idempotent_and_never_echoes_the_address(
    monkeypatch, tmp_path, capsys
) -> None:
    _silence_logging(monkeypatch)
    path = _sqlite_environment(monkeypatch, tmp_path)

    first = cli.main(["init-profile", "--email", TEST_ONLY_EMAIL])
    created_output = capsys.readouterr().out
    second = cli.main(["init-profile", "--email", "  Student@Example.invalid  "])
    existing_output = capsys.readouterr().out

    assert (first, second) == (0, 0)
    assert created_output == "profile created user_id=1 profile_id=1\n"
    assert existing_output == "profile existing user_id=1 profile_id=1\n"
    assert TEST_ONLY_LOCAL_PART not in created_output + existing_output
    assert _counts(path) == (1, 1)


def test_interactive_prompt_avoids_the_shell_history(
    monkeypatch, tmp_path, capsys
) -> None:
    _silence_logging(monkeypatch)
    path = _sqlite_environment(monkeypatch, tmp_path)
    asked: list[str] = []

    def prompt(message: str) -> str:
        asked.append(message)
        return TEST_ONLY_EMAIL

    exit_code = cli.main(["init-profile"], prompt=prompt)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert asked == [cli.PROMPT]
    assert TEST_ONLY_LOCAL_PART not in output
    assert _counts(path) == (1, 1)


def test_show_profile_reads_back_and_reports_a_missing_profile(
    monkeypatch, tmp_path, capsys
) -> None:
    _silence_logging(monkeypatch)
    _sqlite_environment(monkeypatch, tmp_path)

    missing = cli.main(["show-profile", "--email", TEST_ONLY_EMAIL])
    missing_output = capsys.readouterr().out
    cli.main(["init-profile", "--email", TEST_ONLY_EMAIL])
    capsys.readouterr()
    found = cli.main(["show-profile", "--email", TEST_ONLY_EMAIL])
    found_output = capsys.readouterr().out

    assert missing == 1
    assert missing_output == "no profile exists for that address\n"
    assert found == 0
    assert found_output == "profile existing user_id=1 profile_id=1\n"
    assert TEST_ONLY_LOCAL_PART not in missing_output + found_output


def test_non_sqlite_backend_is_refused_without_any_remote_connection(
    monkeypatch, tmp_path, capsys
) -> None:
    _silence_logging(monkeypatch)
    secret = "TEST_ONLY_TURSO_AUTH_TOKEN_DO_NOT_EXPOSE"
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "https://test-only.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", secret)

    def refuse(_settings):
        raise AssertionError("the CLI must not connect to a remote backend")

    def refuse_prompt(_message: str) -> str:
        raise AssertionError("the CLI must not ask for an address it cannot store")

    monkeypatch.setattr(cli, "connect_configured_database", refuse)

    exit_code = cli.main(["init-profile", "--email", TEST_ONLY_EMAIL], prompt=refuse_prompt)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert cli.NON_SQLITE_BACKEND_ERROR in output
    assert secret not in output
    assert TEST_ONLY_LOCAL_PART not in output


def test_structured_events_never_carry_the_address_or_a_secret(
    monkeypatch, tmp_path, capsys
) -> None:
    stream = StringIO()
    monkeypatch.setattr(
        cli, "get_logger", lambda name: logging_config.get_logger(name, stream=stream)
    )
    _sqlite_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST_ONLY_TURSO_AUTH_TOKEN_DO_NOT_EXPOSE")

    assert cli.main(["init-profile", "--email", TEST_ONLY_EMAIL]) == 0
    assert cli.main(["show-profile", "--email", "not-an-address"]) == 1
    rendered = stream.getvalue()
    capsys.readouterr()

    records = [json.loads(line) for line in rendered.splitlines()]
    succeeded = next(
        record
        for record in records
        if record["event"] == "digital_twin_profile_command_succeeded"
    )
    failed = next(
        record
        for record in records
        if record["event"] == "digital_twin_profile_command_failed"
    )

    assert succeeded["context"] == {
        "database_backend": "sqlite",
        "profile_created": True,
    }
    assert failed["context"] == {
        "database_backend": "sqlite",
        "error_type": "InvalidEmailError",
    }
    assert TEST_ONLY_LOCAL_PART not in rendered
    assert "TEST_ONLY_TURSO_AUTH_TOKEN_DO_NOT_EXPOSE" not in rendered
