"""Unit coverage for the Phase 3.6 eligibility CLI.

Every database is created under `tmp_path` and thrown away; the operational
database is never opened. Every posting and every person is invented.

This command reads listings **and** a person's Digital Twin, so it has two ways
to leak instead of one, and most of these tests are about that: no posting's
words, no skill or language name, no CV line, and not even the address the
command was given reaches stdout or the structured log. Counters, ids, versions
and reason codes are the whole output.

The rest are about what the command must refuse: a database that has not been
migrated, a corpus Phase 3.5 has not read, a person who does not exist, and a
non-SQLite backend — the last of these before it opens any connection at all.
"""

import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.extractors.opportunity_constraints.requirements.service import (
    synchronize_opportunity_requirements,
)
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)
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
from services.digital_twin.skills.repository import synchronize_profile_skills
from services.digital_twin.structured_profile.repository import (
    synchronize_structured_profile_entries,
)
from services.eligibility import cli
from services.eligibility.models import ELIGIBILITY_ENGINE_VERSION

TEST_ONLY_EMAIL = "test-only-cli@example.invalid"
TEST_ONLY_TITLE = "TEST ONLY Data Role"
TEST_ONLY_ORG = "TEST ONLY Org"
TEST_ONLY_DESCRIPTION = (
    "<h3>Required Qualifications</h3><ul>"
    "<li>Strong Python skills</li>"
    "<li>English B2 required</li>"
    "<li>Convention de stage obligatoire</li>"
    "</ul>"
    "<h3>Nice to have</h3><ul><li>Airflow</li></ul>"
)
SILENT_DESCRIPTION = "We build good products with a great team."
TEST_ONLY_LANGUAGE_FACT = "Anglais : C1"

#: Every word a run could leak, so no test has to guess which one would. The
#: address is in here too: an operator who typed it knows it, and a log file
#: that collects it holds personal data for no reason.
TEST_ONLY_WORDS = (
    TEST_ONLY_TITLE,
    TEST_ONLY_ORG,
    TEST_ONLY_EMAIL,
    "Strong Python skills",
    "Python",
    "English",
    "Anglais",
    "Airflow",
    "Convention de stage",
    "Required Qualifications",
    "Nice to have",
    "good products",
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


def _seed(path, *, read_phase_3_5: bool, with_twin: bool = True):
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
        if read_phase_3_5:
            synchronize_opportunity_constraints(connection)
            synchronize_opportunity_requirements(connection)
        if with_twin:
            found = ensure_user_profile(connection, TEST_ONLY_EMAIL)
            fact = propose_profile_fact(
                connection,
                profile_id=found.profile.id,
                fact_type=ProfileFactType.LANGUAGE,
                value=TEST_ONLY_LANGUAGE_FACT,
                provenance=ProvenanceInput(
                    source_type=FactSourceType.CV, provenance_key="TEST-ONLY-CLI"
                ),
            )
            accept_profile_fact(connection, found.profile.id, fact.id)
            synchronize_profile_skills(connection, found.profile.id)
            synchronize_structured_profile_entries(connection, found.profile.id)
    finally:
        connection.close()


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """Two invented postings, read by Phase 3.5, and one invented Digital Twin."""
    path = tmp_path / "eligibility.db"
    _seed(path, read_phase_3_5=True)
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


@pytest.fixture()
def unread_database(monkeypatch, tmp_path):
    """The same, without the Phase 3.5 reading Phase 3.6 depends on."""
    path = tmp_path / "unread.db"
    _seed(path, read_phase_3_5=False)
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


@pytest.fixture()
def stranger_database(monkeypatch, tmp_path):
    """Postings that were read, and nobody to decide about."""
    path = tmp_path / "stranger.db"
    _seed(path, read_phase_3_5=True, with_twin=False)
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


def summary_of(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def run(*argv) -> int:
    return cli.main(list(argv))


# --------------------------------------------------------------------------
# What each command does
# --------------------------------------------------------------------------


def test_status_before_any_decision_reports_what_is_in_scope(
    logger, database, capsys
) -> None:
    assert run("status", "--email", TEST_ONLY_EMAIL) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["in_scope"] == "2"
    assert summary["requirements_read"] == "2"
    assert summary["decisions_stored"] == "0"
    assert summary["engine_version"] == ELIGIBILITY_ENGINE_VERSION


def test_sync_decides_every_posting_and_reports_counters(
    logger, database, capsys
) -> None:
    assert run("sync", "--email", TEST_ONLY_EMAIL) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["processed"] == "2"
    assert summary["created"] == "2"
    assert summary["replaced"] == "0"
    assert summary["unchanged"] == "0"
    assert summary["changed"] == "true"


def test_a_second_sync_reports_unchanged_and_writes_nothing(
    logger, database, capsys
) -> None:
    assert run("sync", "--email", TEST_ONLY_EMAIL) == 0
    capsys.readouterr()
    assert run("sync", "--email", TEST_ONLY_EMAIL) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["created"] == "0"
    assert summary["replaced"] == "0"
    assert summary["unchanged"] == "2"
    assert summary["changed"] == "false"


def test_status_after_a_sync_counts_the_stored_verdicts(
    logger, database, capsys
) -> None:
    run("sync", "--email", TEST_ONLY_EMAIL)
    capsys.readouterr()
    assert run("status", "--email", TEST_ONLY_EMAIL) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["decisions_stored"] == "2"
    total = sum(
        int(summary[name]) for name in ("eligible", "unknown", "ineligible")
    )
    assert total == 2


def test_status_writes_nothing(logger, database, capsys) -> None:
    run("status", "--email", TEST_ONLY_EMAIL)
    connection = connect_database(database)
    try:
        stored = connection.execute(
            "SELECT COUNT(*) FROM opportunity_eligibilities"
        ).fetchone()[0]
    finally:
        connection.close()
    assert stored == 0


def test_audit_reports_the_invariants_and_finds_none_broken(
    logger, database, capsys
) -> None:
    run("sync", "--email", TEST_ONLY_EMAIL)
    capsys.readouterr()
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 0
    output = capsys.readouterr().out
    summary = summary_of(output)
    assert summary["decisions"] == "2"
    violations = summary["invariant_violations"]
    assert violations
    assert ": 0" in violations or violations == "{}"
    assert "': 1" not in violations


def test_the_limit_bounds_how_many_postings_are_decided(
    logger, database, capsys
) -> None:
    assert run("sync", "--email", TEST_ONLY_EMAIL, "--limit", "1") == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["processed"] == "1"
    assert summary["created"] == "1"


def test_show_reasons_lists_codes_and_references_only(
    logger, database, capsys
) -> None:
    run("sync", "--email", TEST_ONLY_EMAIL)
    capsys.readouterr()
    assert run("audit", "--email", TEST_ONLY_EMAIL, "--show-reasons", "5") == 0
    output = capsys.readouterr().out
    assert "unknown_reason_sample" in output
    for forbidden in TEST_ONLY_WORDS:
        assert forbidden not in output


# --------------------------------------------------------------------------
# What no command may print
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("status", "--email", TEST_ONLY_EMAIL),
        ("sync", "--email", TEST_ONLY_EMAIL),
        ("audit", "--email", TEST_ONLY_EMAIL),
    ],
)
def test_no_command_prints_a_posting_s_words_or_a_person_s(
    logger, database, capsys, argv
) -> None:
    run("sync", "--email", TEST_ONLY_EMAIL)
    capsys.readouterr()
    assert run(*argv) == 0
    output = capsys.readouterr().out
    for forbidden in TEST_ONLY_WORDS:
        assert forbidden not in output, forbidden


def test_the_log_carries_the_command_counters_and_no_address(
    logger, database, capsys
) -> None:
    run("sync", "--email", TEST_ONLY_EMAIL)
    rendered = logger.rendered()
    assert "eligibility_succeeded" in rendered
    assert ELIGIBILITY_ENGINE_VERSION in rendered
    for forbidden in TEST_ONLY_WORDS:
        assert forbidden not in rendered, forbidden


def test_the_output_carries_no_score_and_no_ranking(
    logger, database, capsys
) -> None:
    """The answer is one of three words; there is nothing to build a percent
    from and no flag that would render one."""
    run("sync", "--email", TEST_ONLY_EMAIL)
    output = capsys.readouterr().out
    for forbidden in ("score", "percent", "%", "rank", "match", "priority"):
        assert forbidden not in output.casefold()


def test_there_is_no_flag_that_would_dump_the_reading() -> None:
    parser_actions = {
        option
        for action in cli.parse_args(
            ["status", "--email", TEST_ONLY_EMAIL]
        ).__dict__
        for option in (action,)
    }
    assert parser_actions == {"command", "email", "limit", "show_reasons"}


# --------------------------------------------------------------------------
# What each command refuses
# --------------------------------------------------------------------------


def test_a_corpus_phase_3_5_has_not_read_is_refused(
    logger, unread_database, capsys
) -> None:
    assert run("sync", "--email", TEST_ONLY_EMAIL) == 1
    output = capsys.readouterr().out
    assert "failed" in output
    for forbidden in TEST_ONLY_WORDS:
        assert forbidden not in output


def test_an_unknown_person_is_refused_rather_than_created(
    logger, stranger_database, capsys
) -> None:
    assert run("sync", "--email", TEST_ONLY_EMAIL) == 1
    assert cli.UNKNOWN_USER_ERROR in capsys.readouterr().out
    connection = connect_database(stranger_database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    finally:
        connection.close()


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["decide-everything", "--email", TEST_ONLY_EMAIL])


def test_the_address_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["sync"])


def test_a_non_sqlite_backend_is_refused_before_connecting(
    logger, monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST-ONLY")

    def refuse(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the command connected to a non-SQLite backend")

    monkeypatch.setattr(cli, "connect_configured_database", refuse)
    assert run("sync", "--email", TEST_ONLY_EMAIL) == 1
    assert cli.NON_SQLITE_BACKEND_ERROR in capsys.readouterr().out


def test_an_unmigrated_database_is_refused(
    logger, monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(tmp_path / "empty.db"))
    assert run("sync", "--email", TEST_ONLY_EMAIL) == 1
    assert "failed" in capsys.readouterr().out


def test_the_command_runs_no_migration(logger, monkeypatch, tmp_path) -> None:
    """`0014` is applied by the ordinary migration command, like every other."""
    source = (cli.__file__ and open(cli.__file__, encoding="utf-8").read()) or ""
    assert "apply_migrations" not in source
