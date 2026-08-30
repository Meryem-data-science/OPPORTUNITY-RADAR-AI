"""Unit coverage for the Phase 3.5B requirement CLI.

Every database is created under `tmp_path` and thrown away; the operational
`.data/` database is never opened. Every posting is invented.

This command reads listings and must never hand their words back — not a
description, not a heading, not an evidence fragment, and not even the name of
a technology or a language a posting asked for. Most of these tests are about
that. Counters, ids and versions are the whole output.
"""

import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.extractors.opportunity_constraints.requirements import cli
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)

# TEST ONLY postings, invented for these tests.
TEST_ONLY_TITLE = "TEST ONLY Data Role"
TEST_ONLY_ORG = "TEST ONLY Org"
TEST_ONLY_DESCRIPTION = (
    "&lt;h3&gt;Required Qualifications&lt;/h3&gt;&lt;ul&gt;"
    "&lt;li&gt;Strong Python skills&lt;/li&gt;"
    "&lt;li&gt;Fluent English&lt;/li&gt;"
    "&lt;li&gt;Spark or Flink&lt;/li&gt;&lt;/ul&gt;"
    "&lt;h3&gt;Nice to have&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Airflow&lt;/li&gt;&lt;/ul&gt;"
)
SILENT_DESCRIPTION = "We build good products with a great team."

#: Every word a run could leak, so no test has to guess which one would.
#:
#: Deliberately only words the *postings* use, and the names of the things they
#: asked for. A counter name such as `required_skill_rows` is the schema's own
#: vocabulary, not a posting's.
TEST_ONLY_WORDS = (
    TEST_ONLY_TITLE, TEST_ONLY_ORG, "Strong Python skills", "Fluent English",
    "Python", "English", "Spark", "Flink", "Airflow", "Required Qualifications",
    "Nice to have", "good products",
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
    """A disposable database holding two invented postings, already through 3.5A."""
    path = tmp_path / "requirements.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        for title, description in (
            (TEST_ONLY_TITLE, TEST_ONLY_DESCRIPTION),
            ("TEST ONLY Quiet Role", SILENT_DESCRIPTION),
        ):
            connection.execute(
                """INSERT INTO opportunities (
                       canonical_title, organization, description, discovered_at,
                       first_seen_at, last_seen_at, source_url, status
                   ) VALUES (?, ?, ?, 't', 't', 't',
                             'https://example.invalid/1', 'new')""",
                (title, TEST_ONLY_ORG, description),
            )
        connection.commit()
        synchronize_opportunity_constraints(connection)
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


@pytest.fixture()
def unprojected_database(monkeypatch, tmp_path):
    """The same, without the Phase 3.5A projection 3.5B depends on."""
    path = tmp_path / "unprojected.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        connection.execute(
            """INSERT INTO opportunities (
                   canonical_title, organization, description, discovered_at,
                   first_seen_at, last_seen_at, source_url, status
               ) VALUES (?, ?, ?, 't', 't', 't',
                         'https://example.invalid/1', 'new')""",
            (TEST_ONLY_TITLE, TEST_ONLY_ORG, TEST_ONLY_DESCRIPTION),
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


def test_status_before_any_extraction_reports_nothing_read(
    logger, database, capsys
) -> None:
    assert cli.main([cli.STATUS_COMMAND]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["total_opportunities"] == "2"
    assert summary["constraint_projected"] == "2"
    assert summary["requirements_extracted"] == "0"
    assert summary["not_extracted"] == "2"
    assert summary["extractor_version"] == "opportunity-requirements-v1"


def test_sync_reads_the_postings_and_reports_counters(logger, database, capsys) -> None:
    assert cli.main([cli.SYNC_COMMAND]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["processed"] == "2"
    assert summary["created"] == "2"
    assert summary["required_skill_rows"] == "1"
    assert summary["preferred_skill_rows"] == "1"
    assert summary["required_language_rows"] == "1"
    assert summary["ambiguity_rows"] == "1"
    assert summary["new_skill_vocabulary_rows"] == "2"
    assert summary["changed"] == "true"


def test_a_second_sync_reports_unchanged(logger, database, capsys) -> None:
    assert cli.main([cli.SYNC_COMMAND]) == 0
    capsys.readouterr()

    assert cli.main([cli.SYNC_COMMAND]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["unchanged"] == "2"
    assert summary["created"] == "0"
    assert summary["replaced"] == "0"
    assert summary["changed"] == "false"


def test_status_after_a_sync_counts_what_is_stored(logger, database, capsys) -> None:
    cli.main([cli.SYNC_COMMAND])
    capsys.readouterr()

    assert cli.main([cli.STATUS_COMMAND]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["requirements_extracted"] == "2"
    assert summary["not_extracted"] == "0"
    assert summary["required_skill_rows"] == "1"


def test_status_writes_nothing(logger, database, capsys) -> None:
    """`status` reads. It does not extract, and it does not store."""
    assert cli.main([cli.STATUS_COMMAND]) == 0
    capsys.readouterr()

    connection = connect_database(database)
    try:
        stored = int(
            connection.execute(
                "SELECT COUNT(*) FROM opportunity_requirement_extraction_state"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert stored == 0


def test_extract_one_reads_a_single_posting(logger, database, capsys) -> None:
    assert cli.main([cli.EXTRACT_ONE_COMMAND, "--opportunity-id", "1"]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["opportunity_id"] == "1"
    assert summary["written"] == "true"
    assert summary["extractor_version"] == "opportunity-requirements-v1"
    assert summary["required_skill_rows"] == "1"


def test_the_limit_bounds_how_many_postings_are_read(logger, database, capsys) -> None:
    assert cli.main([cli.SYNC_COMMAND, "--limit", "1"]) == 0
    summary = summary_of(capsys.readouterr().out)

    assert summary["total_opportunities"] == "1"
    assert summary["processed"] == "1"


# --------------------------------------------------------------------------
# Privacy: counters out, never a posting's words
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    (
        ["status"],
        ["sync"],
        ["extract-one", "--opportunity-id", "1"],
    ),
)
def test_no_command_prints_a_posting_s_words(logger, database, capsys, argv) -> None:
    assert cli.main(argv) == 0
    output = capsys.readouterr().out

    for word in TEST_ONLY_WORDS:
        assert word not in output


def test_status_after_a_sync_still_prints_no_skill_or_language_name(
    logger, database, capsys
) -> None:
    cli.main([cli.SYNC_COMMAND])
    capsys.readouterr()
    cli.main([cli.STATUS_COMMAND])
    output = capsys.readouterr().out

    for word in TEST_ONLY_WORDS:
        assert word not in output


def test_the_log_carries_the_command_and_counters_only(
    logger, database, capsys
) -> None:
    cli.main([cli.SYNC_COMMAND])
    rendered = logger.rendered()

    for word in TEST_ONLY_WORDS:
        assert word not in rendered
    assert "opportunity_requirements_succeeded" in rendered


def test_the_output_names_no_verdict_about_anybody(logger, database, capsys) -> None:
    cli.main([cli.SYNC_COMMAND])
    output = capsys.readouterr().out.casefold() + logger.rendered().casefold()

    for forbidden in (
        "eligible", "eligibility", "candidate", "match_score", "cosine", "tfidf",
        "ranking", "recommendation", "notification", "skill_gap", "missing_skill",
    ):
        assert forbidden not in output


def test_there_is_no_flag_that_would_dump_the_reading() -> None:
    """The detailed reading is a read-only query, not a routine command's output."""
    parser_options = cli.parse_args([cli.STATUS_COMMAND])

    assert set(vars(parser_options)) == {"command", "opportunity_id", "limit"}


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_database_without_the_35a_projection_is_refused(
    logger, unprojected_database, capsys
) -> None:
    assert cli.main([cli.SYNC_COMMAND]) == 1
    output = capsys.readouterr().out

    assert "3.5A" in output
    for word in TEST_ONLY_WORDS:
        assert word not in output


def test_extract_one_without_an_id_is_refused(logger, database, capsys) -> None:
    assert cli.main([cli.EXTRACT_ONE_COMMAND]) == 1

    assert "requires --opportunity-id" in capsys.readouterr().out


def test_extract_one_on_an_absent_posting_is_refused(logger, database, capsys) -> None:
    assert cli.main([cli.EXTRACT_ONE_COMMAND, "--opportunity-id", "4242"]) == 1

    assert "absent" in capsys.readouterr().out


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["dump-everything"])


def test_a_non_sqlite_backend_is_refused_before_connecting(
    logger, database, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST-ONLY-not-a-real-token")

    assert cli.main([cli.SYNC_COMMAND]) == 1

    assert cli.NON_SQLITE_BACKEND_ERROR in capsys.readouterr().out


def test_an_unmigrated_database_is_refused(logger, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(tmp_path / "empty.db"))

    assert cli.main([cli.SYNC_COMMAND]) == 1

    assert "0013" in capsys.readouterr().out
