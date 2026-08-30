"""Unit coverage for the local Phase 3.4C explicit-input CLI.

Every database is created under `tmp_path` and thrown away; the operational
`.data/` database is never opened. The values below are invented locations,
domains, constraints and objectives, and the address is a `.invalid` one that
never resolves.

This command receives somebody's own words and must never hand them back: half
of these tests are about that. The summary, the structured log and the error
messages carry counters, ids, action verbs and `KNOWN`/`UNKNOWN` flags, and
nothing else.
"""

import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.preferences import cli
from services.digital_twin.preferences.repository import (
    get_profile_availability,
    get_profile_career_objectives,
    get_profile_mobility,
    get_profile_preferences,
)
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY address; no real identity is ever used or stored by the tests.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_LOCAL_PART = "student"

# TEST ONLY wordings. They must never leave the database.
TEST_ONLY_LOCATION = "Ville Exemple"
TEST_ONLY_OTHER_LOCATION = "Autre Ville"
TEST_ONLY_DOMAIN = "Domaine fictif"
TEST_ONLY_CONSTRAINT = "Contrainte inventée"
TEST_ONLY_OBJECTIVE = "Objectif inventé"
TEST_ONLY_DATE = "2030-01-15"

#: Every value a run could leak, so no test has to guess which one would.
TEST_ONLY_VALUES = (
    TEST_ONLY_LOCATION,
    TEST_ONLY_OTHER_LOCATION,
    TEST_ONLY_DOMAIN,
    TEST_ONLY_CONSTRAINT,
    TEST_ONLY_OBJECTIVE,
    TEST_ONLY_DATE,
    TEST_ONLY_EMAIL,
    TEST_ONLY_LOCAL_PART,
)


class _RecordingLogger:
    """Keeps what was logged, so a test can read it back."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def info(self, message, *args, **kwargs) -> None:
        self.records.append((message, kwargs.get("extra", {})))

    def error(self, message, *args, **kwargs) -> None:
        self.records.append((message, kwargs.get("extra", {})))

    def rendered(self) -> str:
        return json.dumps(self.records, default=str, ensure_ascii=False)


@pytest.fixture()
def logger(monkeypatch) -> _RecordingLogger:
    recorder = _RecordingLogger()
    monkeypatch.setattr(cli, "get_logger", lambda name: recorder)
    return recorder


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """A disposable SQLite database holding one profile and no statement."""
    path = tmp_path / "preferences.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        ensure_user_profile(connection, TEST_ONLY_EMAIL)
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


def refuse_prompt(_message: str) -> str:
    raise AssertionError("the CLI must not ask for an address it cannot use")


def summary_of(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def profile_of(path):
    connection = connect_database(path)
    try:
        yield connection
    finally:
        connection.close()


AVAILABILITY_ARGS = (
    cli.SET_AVAILABILITY_COMMAND,
    "--email", TEST_ONLY_EMAIL,
    "--status", "AVAILABLE_FROM",
    "--available-from", TEST_ONLY_DATE,
)
MOBILITY_ARGS = (
    cli.SET_MOBILITY_COMMAND,
    "--email", TEST_ONLY_EMAIL,
    "--scope", "RESTRICTED",
    "--location", TEST_ONLY_LOCATION,
    "--location", TEST_ONLY_OTHER_LOCATION,
)
PREFERENCES_ARGS = (
    cli.SET_PREFERENCES_COMMAND,
    "--email", TEST_ONLY_EMAIL,
    "--opportunity-type", "PFE",
    "--opportunity-type", "ALTERNANCE",
    "--work-mode", "HYBRID",
    "--work-mode", "REMOTE",
    "--preferred-domain", TEST_ONLY_DOMAIN,
    "--constraint", TEST_ONLY_CONSTRAINT,
    "--convention-status", "AVAILABLE",
    "--visa-sponsorship-required", "NO",
)
OBJECTIVES_ARGS = (
    cli.SET_CAREER_OBJECTIVES_COMMAND,
    "--email", TEST_ONLY_EMAIL,
    "--objective", TEST_ONLY_OBJECTIVE,
)


def run(argv) -> int:
    return cli.main(list(argv), prompt=refuse_prompt)


# --------------------------------------------------------------------------
# What each set-* command does
# --------------------------------------------------------------------------


def test_set_availability_records_and_projects(logger, database, capsys) -> None:
    exit_code = run(AVAILABILITY_ARGS)
    summary = summary_of(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["fact_type"] == "AVAILABILITY"
    assert summary["action"] == "CREATED"
    assert summary["availability_rows"] == "1"
    assert summary["created"] == "1"
    assert summary["input_version"] == "explicit-profile-input-v1"

    connection = connect_database(database)
    try:
        row = get_profile_availability(connection, 1)
        assert row.value.status.value == "AVAILABLE_FROM"
        assert row.value.available_from == TEST_ONLY_DATE
    finally:
        connection.close()


def test_set_mobility_records_the_locations_that_were_typed(
    logger, database, capsys
) -> None:
    assert run(MOBILITY_ARGS) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["fact_type"] == "MOBILITY"
    assert summary["mobility_rows"] == "1"

    connection = connect_database(database)
    try:
        row = get_profile_mobility(connection, 1)
        assert row.value.scope.value == "RESTRICTED"
        assert row.value.locations == (TEST_ONLY_LOCATION, TEST_ONLY_OTHER_LOCATION)
    finally:
        connection.close()


def test_set_preferences_records_every_field(logger, database, capsys) -> None:
    assert run(PREFERENCES_ARGS) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["fact_type"] == "PREFERENCE"
    assert summary["preferences_rows"] == "1"

    connection = connect_database(database)
    try:
        value = get_profile_preferences(connection, 1).value
        assert [member.value for member in value.opportunity_types] == ["PFE", "ALTERNANCE"]
        assert [member.value for member in value.work_modes] == ["HYBRID", "REMOTE"]
        assert value.preferred_domains == (TEST_ONLY_DOMAIN,)
        assert value.constraints == (TEST_ONLY_CONSTRAINT,)
        assert value.convention_status.value == "AVAILABLE"
        assert value.visa_sponsorship_required.value == "NO"
    finally:
        connection.close()


def test_preferences_default_convention_and_visa_to_unknown(
    logger, database, capsys
) -> None:
    assert run(
        (
            cli.SET_PREFERENCES_COMMAND,
            "--email", TEST_ONLY_EMAIL,
            "--opportunity-type", "PFE",
            "--work-mode", "REMOTE",
        )
    ) == 0
    capsys.readouterr()

    connection = connect_database(database)
    try:
        value = get_profile_preferences(connection, 1).value
        # Never a NO, and never inferred from anything.
        assert value.convention_status.value == "UNKNOWN"
        assert value.visa_sponsorship_required.value == "UNKNOWN"
    finally:
        connection.close()


def test_set_career_objectives_records_what_was_written(
    logger, database, capsys
) -> None:
    assert run(OBJECTIVES_ARGS) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["fact_type"] == "CAREER_OBJECTIVE"
    assert summary["career_objectives_rows"] == "1"

    connection = connect_database(database)
    try:
        assert get_profile_career_objectives(connection, 1).value.objectives == (
            TEST_ONLY_OBJECTIVE,
        )
    finally:
        connection.close()


def test_restating_the_same_thing_is_reported_as_unchanged(
    logger, database, capsys
) -> None:
    assert run(AVAILABILITY_ARGS) == 0
    capsys.readouterr()

    assert run(AVAILABILITY_ARGS) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["action"] == "UNCHANGED"
    assert summary["fact_changed"] == "false"
    assert summary["changed"] == "false"
    assert summary["created"] == "0"
    assert summary["removed"] == "0"


def test_a_new_value_is_reported_as_a_correction(logger, database, capsys) -> None:
    assert run(AVAILABILITY_ARGS) == 0
    first = summary_of(capsys.readouterr().out)

    assert run(
        (
            cli.SET_AVAILABILITY_COMMAND,
            "--email", TEST_ONLY_EMAIL,
            "--status", "AVAILABLE_NOW",
        )
    ) == 0
    second = summary_of(capsys.readouterr().out)

    assert second["action"] == "CORRECTED"
    assert second["previous_fact_id"] == first["fact_id"]
    assert second["created"] == "1"
    assert second["removed"] == "1"


def test_sync_alone_states_nothing_and_is_idempotent(
    logger, database, capsys
) -> None:
    assert run(MOBILITY_ARGS) == 0
    capsys.readouterr()

    assert run((cli.SYNC_COMMAND, "--email", TEST_ONLY_EMAIL)) == 0
    summary = summary_of(capsys.readouterr().out)

    assert "action" not in summary
    assert summary["created"] == "0"
    assert summary["removed"] == "0"
    assert summary["changed"] == "false"


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def test_status_reports_unknown_for_a_profile_that_stated_nothing(
    logger, database, capsys
) -> None:
    assert run((cli.STATUS_COMMAND, "--email", TEST_ONLY_EMAIL)) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["availability"] == "UNKNOWN"
    assert summary["mobility"] == "UNKNOWN"
    assert summary["preferences"] == "UNKNOWN"
    assert summary["career_objectives"] == "UNKNOWN"
    assert summary["mobility_location_count"] == "0"
    assert summary["career_objective_count"] == "0"
    # UNKNOWN is never printed as a FALSE, a NO or a 0 on its own.
    assert "FALSE" not in {value.upper() for value in summary.values()}


def test_status_reports_the_documented_report(logger, database, capsys) -> None:
    for argv in (AVAILABILITY_ARGS, MOBILITY_ARGS, PREFERENCES_ARGS):
        assert run(argv) == 0
    capsys.readouterr()

    assert run((cli.STATUS_COMMAND, "--email", TEST_ONLY_EMAIL)) == 0
    output = capsys.readouterr().out

    assert output.splitlines() == [
        "availability=KNOWN",
        "mobility=KNOWN",
        "preferences=KNOWN",
        "career_objectives=UNKNOWN",
        "mobility_location_count=2",
        "opportunity_type_count=2",
        "work_mode_count=2",
        "preferred_domain_count=1",
        "constraint_count=1",
        "career_objective_count=0",
    ]


# --------------------------------------------------------------------------
# What is never printed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    (AVAILABILITY_ARGS, MOBILITY_ARGS, PREFERENCES_ARGS, OBJECTIVES_ARGS),
)
def test_no_set_command_prints_back_a_value(logger, database, capsys, argv) -> None:
    assert run(argv) == 0
    output = capsys.readouterr().out

    for value in TEST_ONLY_VALUES:
        assert value not in output
    for value in TEST_ONLY_VALUES:
        assert value not in logger.rendered()


def test_status_prints_no_value(logger, database, capsys) -> None:
    for argv in (AVAILABILITY_ARGS, MOBILITY_ARGS, PREFERENCES_ARGS, OBJECTIVES_ARGS):
        assert run(argv) == 0
    capsys.readouterr()
    logger.records.clear()

    assert run((cli.STATUS_COMMAND, "--email", TEST_ONLY_EMAIL)) == 0
    output = capsys.readouterr().out

    for value in TEST_ONLY_VALUES:
        assert value not in output
        assert value not in logger.rendered()


def test_the_log_carries_the_command_and_counters_only(
    logger, database, capsys
) -> None:
    assert run(MOBILITY_ARGS) == 0
    capsys.readouterr()

    message, extra = logger.records[-1]
    assert extra["event"] == "profile_explicit_input_succeeded"
    assert extra["command"] == cli.SET_MOBILITY_COMMAND
    assert extra["database_backend"] == "sqlite"
    assert extra["summary"]["mobility_rows"] == 1
    assert "locations" not in json.dumps(extra, default=str)


# --------------------------------------------------------------------------
# What is refused
# --------------------------------------------------------------------------


def test_a_restricted_mobility_naming_nowhere_is_refused_before_any_write(
    logger, database, capsys
) -> None:
    exit_code = run(
        (cli.SET_MOBILITY_COMMAND, "--email", TEST_ONLY_EMAIL, "--scope", "RESTRICTED")
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "failed" in output
    connection = connect_database(database)
    try:
        assert get_profile_mobility(connection, 1) is None
    finally:
        connection.close()


def test_an_available_now_carrying_a_date_is_refused(logger, database, capsys) -> None:
    exit_code = run(
        (
            cli.SET_AVAILABILITY_COMMAND,
            "--email", TEST_ONLY_EMAIL,
            "--status", "AVAILABLE_NOW",
            "--available-from", TEST_ONLY_DATE,
        )
    )
    capsys.readouterr()
    assert exit_code == 1


def test_an_available_from_without_a_date_is_refused(logger, database, capsys) -> None:
    exit_code = run(
        (
            cli.SET_AVAILABILITY_COMMAND,
            "--email", TEST_ONLY_EMAIL,
            "--status", "AVAILABLE_FROM",
        )
    )
    capsys.readouterr()
    assert exit_code == 1


def test_a_malformed_date_is_refused_without_printing_it(
    logger, database, capsys
) -> None:
    exit_code = run(
        (
            cli.SET_AVAILABILITY_COMMAND,
            "--email", TEST_ONLY_EMAIL,
            "--status", "AVAILABLE_FROM",
            "--available-from", "15/01/2030",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "15/01/2030" not in output
    assert "15/01/2030" not in logger.rendered()


@pytest.mark.parametrize(
    "argv",
    (
        (cli.SET_PREFERENCES_COMMAND, "--email", TEST_ONLY_EMAIL, "--work-mode", "REMOTE"),
        (
            cli.SET_PREFERENCES_COMMAND, "--email", TEST_ONLY_EMAIL,
            "--opportunity-type", "PFE",
        ),
        (cli.SET_CAREER_OBJECTIVES_COMMAND, "--email", TEST_ONLY_EMAIL),
        (cli.SET_MOBILITY_COMMAND, "--email", TEST_ONLY_EMAIL),
        (cli.SET_AVAILABILITY_COMMAND, "--email", TEST_ONLY_EMAIL),
    ),
)
def test_a_missing_required_argument_is_refused(database, argv) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(list(argv))


@pytest.mark.parametrize(
    "argv",
    (
        (
            cli.SET_PREFERENCES_COMMAND, "--email", TEST_ONLY_EMAIL,
            "--opportunity-type", "CDI", "--work-mode", "REMOTE",
        ),
        (
            cli.SET_PREFERENCES_COMMAND, "--email", TEST_ONLY_EMAIL,
            "--opportunity-type", "PFE", "--work-mode", "FULL_REMOTE",
        ),
        (cli.SET_MOBILITY_COMMAND, "--email", TEST_ONLY_EMAIL, "--scope", "ANYWHERE"),
        (
            cli.SET_AVAILABILITY_COMMAND, "--email", TEST_ONLY_EMAIL,
            "--status", "MAYBE_LATER",
        ),
    ),
)
def test_a_value_outside_a_closed_registry_is_refused(database, argv) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(list(argv))


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["set-everything", "--email", TEST_ONLY_EMAIL])


def test_a_non_sqlite_backend_is_refused_before_connecting(
    logger, database, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST-ONLY-TOKEN")

    exit_code = run((cli.STATUS_COMMAND, "--email", TEST_ONLY_EMAIL))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "SQLITE" in output.upper()


def test_an_absent_profile_is_refused_rather_than_created(
    logger, database, capsys, monkeypatch, tmp_path
) -> None:
    empty = tmp_path / "empty.db"
    connection = connect_database(empty)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(empty))

    exit_code = run(AVAILABILITY_ARGS)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert TEST_ONLY_EMAIL not in output
    connection = connect_database(empty)
    try:
        assert int(connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]) == 0
    finally:
        connection.close()


def test_the_address_is_prompted_without_echo_when_it_is_not_given(
    logger, database, capsys
) -> None:
    asked: list[str] = []

    def prompt(message: str) -> str:
        asked.append(message)
        return TEST_ONLY_EMAIL

    exit_code = cli.main([cli.STATUS_COMMAND], prompt=prompt)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert asked == [cli.EMAIL_PROMPT]
    assert TEST_ONLY_EMAIL not in output
