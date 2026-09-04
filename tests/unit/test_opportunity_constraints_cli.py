"""Unit coverage for the Phase 3.5A constraint CLI.

Every database is created under `tmp_path` and thrown away; the operational
`.data/` database is never opened. Every posting is invented.

This command reads listings and must never hand their words back: half of these
tests are about that. Counters, rule counts and versions are the whole output.
"""

import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.extractors.opportunity_constraints import cli
from services.collector.extractors.opportunity_constraints.repository import (
    read_opportunity_constraints,
)

# TEST ONLY postings, invented for these tests.
TEST_ONLY_TITLE = "PFE Data Engineer"
TEST_ONLY_LOCATION = "Ville Exemple"
TEST_ONLY_COUNTRY = "Pays Exemple"
TEST_ONLY_ORG = "TEST ONLY Org"
TEST_ONLY_DESCRIPTION = (
    "&lt;ul&gt;&lt;li&gt;Minimum 3 years of experience required&lt;/li&gt;"
    "&lt;li&gt;Education: Bac+5 minimum&lt;/li&gt;"
    "&lt;li&gt;Visa sponsorship available&lt;/li&gt;"
    "&lt;li&gt;Convention de stage obligatoire&lt;/li&gt;&lt;/ul&gt;"
)

#: Every word a run could leak, so no test has to guess which one would.
#:
#: Deliberately only words the *postings* use. `sponsorship`, `experience` and
#: `convention` are also substrings of the counter names — `known_visa_
#: sponsorship`, `known_convention` — and a canary that fires on the schema's
#: own vocabulary would be a test that can never pass rather than a leak.
TEST_ONLY_WORDS = (
    TEST_ONLY_TITLE, TEST_ONLY_LOCATION, TEST_ONLY_COUNTRY, TEST_ONLY_ORG,
    "Bac+5", "Convention de stage", "Minimum 3 years", "Visa sponsorship available",
    "Data Engineer", "Senior Data Scientist",
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


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """A disposable database holding two invented postings."""
    path = tmp_path / "constraints.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        for title, description, location in (
            (TEST_ONLY_TITLE, TEST_ONLY_DESCRIPTION, TEST_ONLY_LOCATION),
            ("Senior Data Scientist", "We build good products.", None),
        ):
            connection.execute(
                """INSERT INTO opportunities (
                       canonical_title, organization, location, country, description,
                       discovered_at, first_seen_at, last_seen_at, source_url, status
                   ) VALUES (?, ?, ?, ?, ?, 't', 't', 't',
                             'https://example.invalid/1', 'new')""",
                (title, TEST_ONLY_ORG, location, TEST_ONLY_COUNTRY, description),
            )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


def summary_of(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


# --------------------------------------------------------------------------
# What each command does
# --------------------------------------------------------------------------


def test_status_on_an_unprojected_database_reports_nothing_known(
    logger, database, capsys
) -> None:
    assert cli.main([cli.STATUS_COMMAND]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["total_opportunities"] == "2"
    assert summary["projected"] == "0"
    assert summary["not_projected"] == "2"
    assert summary["known_visa_sponsorship"] == "0"
    assert summary["extractor_version"] == "opportunity-constraints-v4"


def test_sync_projects_the_postings_and_reports_counters(
    logger, database, capsys
) -> None:
    assert cli.main([cli.SYNC_COMMAND]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["processed"] == "2"
    assert summary["created"] == "2"
    assert summary["unchanged"] == "0"
    assert summary["known_visa_sponsorship"] == "1"
    assert summary["known_convention"] == "1"
    assert summary["changed"] == "true"


def test_a_second_sync_reports_unchanged(logger, database, capsys) -> None:
    assert cli.main([cli.SYNC_COMMAND]) == 0
    capsys.readouterr()

    assert cli.main([cli.SYNC_COMMAND]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["unchanged"] == "2"
    assert summary["created"] == "0"
    assert summary["changed"] == "false"


def test_status_after_a_sync_counts_what_is_stored(logger, database, capsys) -> None:
    assert cli.main([cli.SYNC_COMMAND]) == 0
    capsys.readouterr()

    assert cli.main([cli.STATUS_COMMAND]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["projected"] == "2"
    assert summary["not_projected"] == "0"
    assert summary["known_opportunity_type"] == "1"


def test_extract_one_reads_a_single_posting(logger, database, capsys) -> None:
    assert cli.main([cli.EXTRACT_ONE_COMMAND, "--opportunity-id", "1"]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["opportunity_id"] == "1"
    assert summary["written"] == "true"
    assert int(summary["evidence_count"]) > 0

    connection = connect_database(database)
    try:
        assert read_opportunity_constraints(connection, 1) is not None
        # The command touched only the posting it was asked about.
        assert read_opportunity_constraints(connection, 2) is None
    finally:
        connection.close()


def test_the_limit_bounds_how_many_postings_are_read(logger, database, capsys) -> None:
    assert cli.main([cli.SYNC_COMMAND, "--limit", "1"]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["total_opportunities"] == "1"
    assert summary["created"] == "1"


# --------------------------------------------------------------------------
# What is never printed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    (
        [cli.STATUS_COMMAND],
        [cli.SYNC_COMMAND],
        [cli.EXTRACT_ONE_COMMAND, "--opportunity-id", "1"],
    ),
)
def test_no_command_prints_a_posting_s_words(logger, database, capsys, argv) -> None:
    assert cli.main(argv) == 0
    output = capsys.readouterr().out

    for word in TEST_ONLY_WORDS:
        assert word not in output
        assert word not in logger.rendered()


def test_status_after_a_sync_still_prints_no_evidence_text(
    logger, database, capsys
) -> None:
    assert cli.main([cli.SYNC_COMMAND]) == 0
    capsys.readouterr()
    logger.records.clear()

    assert cli.main([cli.STATUS_COMMAND]) == 0
    output = capsys.readouterr().out

    for word in TEST_ONLY_WORDS:
        assert word not in output
        assert word not in logger.rendered()


def test_the_log_carries_the_command_and_counters_only(
    logger, database, capsys
) -> None:
    assert cli.main([cli.SYNC_COMMAND]) == 0
    capsys.readouterr()

    _message, extra = logger.records[-1]
    assert extra["event"] == "opportunity_constraints_succeeded"
    assert extra["command"] == cli.SYNC_COMMAND
    assert extra["database_backend"] == "sqlite"
    assert extra["summary"]["processed"] == 2
    assert "evidence_text" not in json.dumps(extra, default=str)


def test_the_output_names_no_verdict_about_anybody(logger, database, capsys) -> None:
    """A constraint is what a posting asks for. There is nobody to compare."""
    assert cli.main([cli.SYNC_COMMAND]) == 0
    output = capsys.readouterr().out.casefold()

    for forbidden in ("match_score", "priority_score", "ranking", "recommendation"):
        assert forbidden not in output


# --------------------------------------------------------------------------
# What is refused
# --------------------------------------------------------------------------


def test_extract_one_without_an_id_is_refused(logger, database, capsys) -> None:
    exit_code = cli.main([cli.EXTRACT_ONE_COMMAND])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "--opportunity-id" in output


def test_extract_one_on_an_absent_posting_is_refused(logger, database, capsys) -> None:
    exit_code = cli.main([cli.EXTRACT_ONE_COMMAND, "--opportunity-id", "9999"])
    capsys.readouterr()

    assert exit_code == 1


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["extract-everything"])


def test_a_non_sqlite_backend_is_refused_before_connecting(
    logger, database, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST-ONLY-TOKEN")

    exit_code = cli.main([cli.SYNC_COMMAND])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "SQLITE" in output.upper()


def test_an_unmigrated_database_is_refused(logger, monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "empty.db"
    connect_database(path).close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))

    exit_code = cli.main([cli.SYNC_COMMAND])
    capsys.readouterr()

    assert exit_code == 1
