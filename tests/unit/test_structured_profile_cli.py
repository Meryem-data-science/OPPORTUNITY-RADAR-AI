"""Unit coverage for the local Phase 3.4B1 structured projection CLI.

Every database is created under `tmp_path` and thrown away; the operational
`.data/` database is never opened. The values below are invented roles,
organizations and project titles, and the address is a `.invalid` one that
never resolves.

This command needs nobody to read a value, so it prints none. Half of these
tests are about that — the summary, the structured log and the error messages
carry counters, versions and rule tallies and nothing else.
"""

import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.facts.models import (
    FactSourceType,
    ProfileFactType,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    accept_profile_fact,
    propose_profile_fact,
)
from services.digital_twin.repository import ensure_user_profile
from services.digital_twin.structured_profile import cli
from services.digital_twin.structured_profile.repository import (
    list_profile_experiences,
    list_profile_projects,
)

# TEST ONLY address; no real identity is ever used or stored by the tests.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_LOCAL_PART = "student"

#: Invented wordings. They must never leave the database.
TEST_ONLY_FACTS = (
    (ProfileFactType.EXPERIENCE, "Data Analyst | ACME | 2022 - 2024\nPremière ligne"),
    (ProfileFactType.EXPERIENCE, "Analyste de données pour ACME entre 2022 et 2024"),
    (ProfileFactType.PROJECT, "• Analyse RH (2023 - 2024) : segmentation"),
    (ProfileFactType.PROJECT, "Analyse RH des effectifs sur deux ans"),
)

#: Every fragment a projected row could hold, so no test has to guess which one
#: a leak would expose.
TEST_ONLY_FRAGMENTS = (
    "Data Analyst",
    "ACME",
    "2022 - 2024",
    "Première ligne",
    "Analyse RH",
    "segmentation",
)


class _SilentLogger:
    def info(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


@pytest.fixture()
def silence_logging(monkeypatch):
    monkeypatch.setattr(cli, "get_logger", lambda name: _SilentLogger())


def _seed(path, facts=TEST_ONLY_FACTS) -> None:
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
        for index, (fact_type, value) in enumerate(facts):
            fact = propose_profile_fact(
                connection,
                profile_id=profile_id,
                fact_type=fact_type,
                value=value,
                provenance=ProvenanceInput(
                    source_type=FactSourceType.CV,
                    provenance_key=f"TEST-ONLY-{index}",
                ),
            )
            accept_profile_fact(connection, profile_id, fact.id)
    finally:
        connection.close()


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """A disposable SQLite database holding one profile and its facts."""
    path = tmp_path / "structured.db"
    _seed(path)
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


def refuse_prompt(_message: str) -> str:
    raise AssertionError("the CLI must not ask for an address it cannot use")


def summary_of(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def rows_of(path) -> tuple[int, int]:
    connection = connect_database(path)
    try:
        return (
            len(list_profile_experiences(connection, 1)),
            len(list_profile_projects(connection, 1)),
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# What one run does
# --------------------------------------------------------------------------


def test_sync_projects_the_accepted_facts_and_reports_counters(
    silence_logging, database, capsys
) -> None:
    exit_code = cli.main(
        [cli.SYNC_COMMAND, "--email", TEST_ONLY_EMAIL], prompt=refuse_prompt
    )
    summary = summary_of(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["accepted_experience_facts"] == "2"
    assert summary["accepted_project_facts"] == "2"
    assert summary["experience_rows"] == "2"
    assert summary["project_rows"] == "2"
    assert summary["structured_experiences"] == "1"
    assert summary["unparsed_experiences"] == "1"
    assert summary["structured_projects"] == "1"
    assert summary["unparsed_projects"] == "1"
    assert summary["created"] == "4"
    assert summary["removed"] == "0"
    assert summary["structurer_version"] == "structured-profile-v1"
    assert summary["changed"] == "true"
    assert rows_of(database) == (2, 2)


def test_a_second_run_reports_no_change(silence_logging, database, capsys) -> None:
    cli.main([cli.SYNC_COMMAND, "--email", TEST_ONLY_EMAIL])
    capsys.readouterr()

    exit_code = cli.main([cli.SYNC_COMMAND, "--email", TEST_ONLY_EMAIL])
    summary = summary_of(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["changed"] == "false"
    assert summary["created"] == "0"
    assert summary["removed"] == "0"


def test_the_address_is_prompted_without_echo_when_it_is_not_given(
    silence_logging, database, capsys
) -> None:
    asked: list[str] = []

    def prompt(message: str) -> str:
        asked.append(message)
        return TEST_ONLY_EMAIL

    exit_code = cli.main([cli.SYNC_COMMAND], prompt=prompt)

    assert exit_code == 0
    assert asked == [cli.EMAIL_PROMPT]
    assert TEST_ONLY_LOCAL_PART not in capsys.readouterr().out


# --------------------------------------------------------------------------
# Nothing personal leaves the database
# --------------------------------------------------------------------------


def test_the_summary_names_no_role_organization_period_or_title(
    silence_logging, database, capsys
) -> None:
    cli.main([cli.SYNC_COMMAND, "--email", TEST_ONLY_EMAIL])
    output = capsys.readouterr().out

    for fragment in TEST_ONLY_FRAGMENTS:
        assert fragment not in output, fragment
    for _, value in TEST_ONLY_FACTS:
        assert value not in output
    assert TEST_ONLY_LOCAL_PART not in output


def test_structured_events_carry_counters_and_never_a_value(database, capsys) -> None:
    assert cli.main([cli.SYNC_COMMAND, "--email", TEST_ONLY_EMAIL]) == 0
    output = capsys.readouterr().out
    events = [
        json.loads(line)
        for line in output.splitlines()
        if line.startswith("{") and line.endswith("}")
    ]

    succeeded = next(
        event
        for event in events
        if event["event"] == "structured_profile_sync_succeeded"
    )
    assert succeeded["context"]["database_backend"] == "sqlite"
    assert succeeded["context"]["summary"] == {
        "accepted_experience_facts": 2,
        "accepted_project_facts": 2,
        "experience_rows": 2,
        "project_rows": 2,
        "structured_experiences": 1,
        "unparsed_experiences": 1,
        "structured_projects": 1,
        "unparsed_projects": 1,
        "created": 4,
        "removed": 0,
        "structurer_version": "structured-profile-v1",
        "changed": True,
    }
    rendered = "\n".join(line for line in output.splitlines() if line.startswith("{"))
    for fragment in TEST_ONLY_FRAGMENTS:
        assert fragment not in rendered, fragment
    assert TEST_ONLY_LOCAL_PART not in rendered


def test_the_command_offers_no_flag_that_would_print_a_value() -> None:
    """The privacy of the output is a property of the command, not of a default."""
    parsed = cli.parse_args([cli.SYNC_COMMAND]).__dict__

    assert set(parsed) == {"command", "email"}


# --------------------------------------------------------------------------
# Refusals before anything is touched
# --------------------------------------------------------------------------


def test_a_non_sqlite_backend_is_refused_before_any_connection(
    silence_logging, monkeypatch, capsys
) -> None:
    secret = "TEST_ONLY_TURSO_AUTH_TOKEN_DO_NOT_EXPOSE"
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "https://test-only.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", secret)

    def refuse(_settings):
        raise AssertionError("the CLI must not connect to a remote backend")

    monkeypatch.setattr(cli, "connect_configured_database", refuse)

    exit_code = cli.main([cli.SYNC_COMMAND], prompt=refuse_prompt)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert cli.NON_SQLITE_BACKEND_ERROR in output
    assert secret not in output


def test_a_missing_profile_is_reported_and_never_created(
    silence_logging, monkeypatch, tmp_path, capsys
) -> None:
    """Synchronizing is not a way to bring a profile into existence."""
    path = tmp_path / "empty.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))

    exit_code = cli.main([cli.SYNC_COMMAND, "--email", TEST_ONLY_EMAIL])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert cli.NO_PROFILE_ERROR in output
    assert TEST_ONLY_LOCAL_PART not in output

    connection = connect_database(path)
    try:
        for table in ("users", "profiles", "profile_experiences", "profile_projects"):
            assert (
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
    finally:
        connection.close()


def test_the_command_runs_no_migration(
    silence_logging, monkeypatch, tmp_path, capsys
) -> None:
    """`0009` is applied by the ordinary explicit migration command, like the rest."""
    path = tmp_path / "unmigrated.db"
    connect_database(path).close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))

    exit_code = cli.main([cli.SYNC_COMMAND, "--email", TEST_ONLY_EMAIL])

    assert exit_code == 1
    connection = connect_database(path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert tables == set()
    assert TEST_ONLY_LOCAL_PART not in capsys.readouterr().out


def test_only_the_configured_database_is_written(
    silence_logging, monkeypatch, tmp_path, capsys
) -> None:
    """A second database beside the configured one stays exactly as it was."""
    configured = tmp_path / "configured.db"
    untouched = tmp_path / "untouched.db"
    _seed(configured)
    _seed(untouched)
    before = untouched.read_bytes()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(configured))

    assert cli.main([cli.SYNC_COMMAND, "--email", TEST_ONLY_EMAIL]) == 0

    assert untouched.read_bytes() == before
    assert rows_of(untouched) == (0, 0)
    assert rows_of(configured) == (2, 2)


def test_an_unknown_command_is_refused(silence_logging) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["publish"])
