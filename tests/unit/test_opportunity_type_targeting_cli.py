"""Unit coverage for the Phase 7B.1 opportunity-type targeting CLI.

Every database is created under `tmp_path` and thrown away; the operational
database is never opened. Every posting and every person is invented, and
`.invalid` never resolves.

This command reads listings **and** a person's stated preferences, so most of
these tests are about what it must not say: no posting's words, no free-text
preference and not even the address the command was given reaches stdout or the
structured log. Counters, closed-registry values, rule ids and versions are the
whole output.

The rest are about what it must refuse: a database that has not been migrated,
a person who does not exist, a command with nobody to audit, an `explain` with
no posting to explain, and a non-SQLite backend — the last of these before it
opens any connection at all. And about what it must not do at all: write.
"""

import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)
from services.digital_twin.preferences.models import (
    OpportunityPreferences,
    OpportunityType,
    WorkMode,
)
from services.digital_twin.preferences.repository import (
    synchronize_profile_preferences,
)
from services.digital_twin.preferences.service import set_profile_preferences
from services.digital_twin.repository import ensure_user_profile
from services.targeting.opportunity_type import cli
from services.targeting.opportunity_type.models import TARGETING_VERSION

TEST_ONLY_EMAIL = "test-only-opportunity-type@example.invalid"
TEST_ONLY_ORG = "TEST ONLY Org"
TEST_ONLY_PFE_TITLE = "TEST ONLY Stage PFE Data Engineering"
TEST_ONLY_JUNIOR_TITLE = "TEST ONLY Junior Data Analyst"
TEST_ONLY_SILENT_TITLE = "TEST ONLY Opening"
TEST_ONLY_DOMAIN = "TEST ONLY preferred domain"
TEST_ONLY_CONSTRAINT = "TEST ONLY personal constraint"

#: Every word a run could leak, so no test has to guess which one would. The
#: address is in here too: an operator who typed it knows it, and a log file
#: that collects it holds personal data for no reason. `PFE` is not a leak —
#: it is a value of the closed registry both sides of the comparison speak.
TEST_ONLY_WORDS = (
    TEST_ONLY_EMAIL,
    TEST_ONLY_ORG,
    TEST_ONLY_PFE_TITLE,
    TEST_ONLY_JUNIOR_TITLE,
    TEST_ONLY_SILENT_TITLE,
    TEST_ONLY_DOMAIN,
    TEST_ONLY_CONSTRAINT,
    "Junior Data Analyst",
    "Stage PFE",
)


class _RecordingLogger:
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


def _insert(connection, title: str, index: int) -> int:
    row = connection.execute(
        """INSERT INTO opportunities (
               canonical_title, organization, description, discovered_at,
               first_seen_at, last_seen_at, source_url, status
           ) VALUES (?, ?, 'TEST ONLY description.', 't', 't', 't', ?, 'new')
           RETURNING id""",
        (title, TEST_ONLY_ORG, f"https://example.invalid/{index}"),
    ).fetchone()
    return int(row[0])


def _seed(path, *, read_phase_3_5: bool = True, with_twin: bool = True) -> dict:
    connection = connect_database(path)
    ids: dict[str, int] = {}
    try:
        apply_migrations(connection)
        for index, (key, title) in enumerate(
            (
                ("pfe", TEST_ONLY_PFE_TITLE),
                ("junior", TEST_ONLY_JUNIOR_TITLE),
                ("silent", TEST_ONLY_SILENT_TITLE),
            )
        ):
            ids[key] = _insert(connection, title, index)
        connection.commit()
        if read_phase_3_5:
            synchronize_opportunity_constraints(connection)
        if with_twin:
            profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
            set_profile_preferences(
                connection,
                profile_id,
                OpportunityPreferences(
                    opportunity_types=(
                        OpportunityType.PFE,
                        OpportunityType.INTERNSHIP,
                    ),
                    work_modes=(WorkMode.ON_SITE,),
                    preferred_domains=(TEST_ONLY_DOMAIN,),
                    constraints=(TEST_ONLY_CONSTRAINT,),
                ),
            )
            synchronize_profile_preferences(connection, profile_id)
    finally:
        connection.close()
    return ids


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """Three invented postings, read by Phase 3.5, and one invented twin."""
    path = tmp_path / "targeting.db"
    ids = _seed(path)
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return {"path": path, **ids}


@pytest.fixture()
def stranger_database(monkeypatch, tmp_path):
    """Postings that were read, and nobody to audit them for."""
    path = tmp_path / "stranger.db"
    _seed(path, with_twin=False)
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


@pytest.fixture()
def unmigrated_database(monkeypatch, tmp_path):
    path = tmp_path / "empty.db"
    connect_database(path).close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


def summary_of(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def run(*argv) -> int:
    return cli.main(list(argv))


def _rows(path, sql: str) -> list[tuple]:
    connection = connect_database(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


# --------------------------------------------------------------------------
# What each command does
# --------------------------------------------------------------------------


def test_audit_reports_the_three_verdicts_and_the_derived_target(
    logger, database, capsys
) -> None:
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["profile_target_known"] == "true"
    assert summary["target_types"] == "PFE,INTERNSHIP"
    assert summary["target_type_count"] == "2"
    assert summary["target_rule_id"] == "declared-opportunity-types-v1"
    assert summary["total_opportunities"] == "3"
    assert summary["targeting_version"] == TARGETING_VERSION
    counted = (
        int(summary["match"]) + int(summary["out_of_target"]) + int(summary["unknown"])
    )
    assert counted == 3


def test_audit_reports_the_distribution_of_structured_types(
    logger, database, capsys
) -> None:
    """Every registry value gets a key, at zero or not, and UNKNOWN is one."""
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 0
    summary = summary_of(capsys.readouterr().out)
    for member in OpportunityType:
        assert f"type_{member.value}" in summary
    assert "type_UNKNOWN" in summary
    distributed = sum(
        int(value) for key, value in summary.items() if key.startswith("type_")
    )
    assert distributed == int(summary["total_opportunities"])


def test_audit_counts_a_posting_with_no_structured_type_as_unknown(
    logger, database, capsys
) -> None:
    """A posting nobody could read is not a posting of another kind."""
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 0
    summary = summary_of(capsys.readouterr().out)
    assert int(summary["type_UNKNOWN"]) >= 1
    assert int(summary["unknown"]) >= 1
    assert int(summary["opportunity_type_unknown"]) == int(summary["type_UNKNOWN"])


def test_explain_names_the_rule_behind_one_posting(
    logger, database, capsys
) -> None:
    assert (
        run(
            "explain",
            "--email",
            TEST_ONLY_EMAIL,
            "--opportunity-id",
            str(database["silent"]),
        )
        == 0
    )
    summary = summary_of(capsys.readouterr().out)
    assert summary["opportunity_id"] == str(database["silent"])
    assert summary["verdict"] == "UNKNOWN"
    assert summary["rule_id"] == "unknown-opportunity-type-v1"
    assert summary["opportunity_type"] == "UNKNOWN"
    assert summary["target_types"] == "PFE,INTERNSHIP"


def test_explain_reports_a_posting_whose_stored_type_is_targeted(
    logger, database, capsys
) -> None:
    """Whatever Phase 3.5A stored is reported, and never re-decided here."""
    assert (
        run(
            "explain",
            "--email",
            TEST_ONLY_EMAIL,
            "--opportunity-id",
            str(database["pfe"]),
        )
        == 0
    )
    summary = summary_of(capsys.readouterr().out)
    assert summary["constraints_read"] == "true"
    assert summary["verdict"] in {"MATCH", "OUT_OF_TARGET", "UNKNOWN"}
    assert summary["opportunity_id"] == str(database["pfe"])


def test_limit_bounds_the_postings_read(logger, database, capsys) -> None:
    assert run("audit", "--email", TEST_ONLY_EMAIL, "--limit", "1") == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["total_opportunities"] == "1"


# --------------------------------------------------------------------------
# What it must never say
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["audit", "explain"])
def test_no_posting_and_no_person_reaches_stdout_or_the_log(
    logger, database, capsys, command
) -> None:
    argv = [command, "--email", TEST_ONLY_EMAIL]
    if command == "explain":
        argv += ["--opportunity-id", str(database["pfe"])]
    assert run(*argv) == 0
    printed = capsys.readouterr().out
    logged = logger.rendered()
    for word in TEST_ONLY_WORDS:
        assert word not in printed, word
        assert word not in logged, word


def test_a_failure_quotes_neither_the_address_nor_a_posting(
    logger, stranger_database, capsys
) -> None:
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 1
    printed = capsys.readouterr().out
    logged = logger.rendered()
    for word in TEST_ONLY_WORDS:
        assert word not in printed, word
        assert word not in logged, word


# --------------------------------------------------------------------------
# What it must refuse
# --------------------------------------------------------------------------


def test_audit_without_an_email_is_refused(logger, database, capsys) -> None:
    assert run("audit") == 1
    assert cli.MISSING_EMAIL_ERROR in capsys.readouterr().out


def test_explain_without_an_opportunity_is_refused(
    logger, database, capsys
) -> None:
    assert run("explain", "--email", TEST_ONLY_EMAIL) == 1
    assert cli.MISSING_OPPORTUNITY_ERROR in capsys.readouterr().out


def test_an_unknown_person_is_refused(logger, stranger_database, capsys) -> None:
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 1
    assert cli.UNKNOWN_USER_ERROR in capsys.readouterr().out


def test_an_absent_opportunity_is_refused(logger, database, capsys) -> None:
    assert (
        run("explain", "--email", TEST_ONLY_EMAIL, "--opportunity-id", "999999") == 1
    )
    assert "LookupError" in capsys.readouterr().out


def test_an_unmigrated_database_is_refused(
    logger, unmigrated_database, capsys
) -> None:
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 1
    assert capsys.readouterr().out.strip() != ""


def test_a_non_sqlite_backend_is_refused_before_connecting(
    logger, monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "test-only-token")

    def _refuse(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the command connected to a non-SQLite backend")

    monkeypatch.setattr(cli, "connect_configured_database", _refuse)
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 1
    assert cli.NON_SQLITE_BACKEND_ERROR in capsys.readouterr().out


def test_an_unknown_command_is_refused(logger, database) -> None:
    with pytest.raises(SystemExit):
        run("sync", "--email", TEST_ONLY_EMAIL)


# --------------------------------------------------------------------------
# What it must not do at all
# --------------------------------------------------------------------------


def test_no_command_writes_anything(logger, database, capsys) -> None:
    """Both commands read. Nothing in this slice projects a verdict."""
    before = _rows(
        database["path"],
        "SELECT profile_id, opportunity_types_json, work_modes_json, fact_id "
        "FROM profile_preferences ORDER BY profile_id",
    )
    constraints_before = _rows(
        database["path"],
        "SELECT opportunity_id, opportunity_type, extractor_version, "
        "source_fingerprint FROM opportunity_constraints ORDER BY opportunity_id",
    )
    tables_before = _rows(
        database["path"], "SELECT name FROM sqlite_master WHERE type='table'"
    )

    assert run("audit", "--email", TEST_ONLY_EMAIL) == 0
    assert (
        run(
            "explain",
            "--email",
            TEST_ONLY_EMAIL,
            "--opportunity-id",
            str(database["pfe"]),
        )
        == 0
    )
    capsys.readouterr()

    assert (
        _rows(
            database["path"],
            "SELECT profile_id, opportunity_types_json, work_modes_json, fact_id "
            "FROM profile_preferences ORDER BY profile_id",
        )
        == before
    )
    assert (
        _rows(
            database["path"],
            "SELECT opportunity_id, opportunity_type, extractor_version, "
            "source_fingerprint FROM opportunity_constraints ORDER BY opportunity_id",
        )
        == constraints_before
    )
    assert (
        _rows(database["path"], "SELECT name FROM sqlite_master WHERE type='table'")
        == tables_before
    )
