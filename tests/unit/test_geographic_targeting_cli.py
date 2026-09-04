"""Unit coverage for the Phase 7A.1 geographic CLI.

Every database is created under `tmp_path` and thrown away; the operational
database is never opened. Every posting and every person is invented, and
`.invalid` never resolves.

This command reads listings **and** a person's declared mobility, so most of
these tests are about what it must not say: no posting's words, no raw location
string, no mobility entry and not even the address the command was given
reaches stdout or the structured log. Counters, ISO country codes, rule ids and
versions are the whole output.

The rest are about what it must refuse: a database that has not been migrated,
a person who does not exist, an `audit` with nobody to audit, and a non-SQLite
backend — the last of these before it opens any connection at all.
"""

import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)
from services.digital_twin.preferences.models import (
    MobilityPreference,
    MobilityScope,
)
from services.digital_twin.preferences.repository import (
    synchronize_profile_preferences,
)
from services.digital_twin.preferences.service import set_profile_mobility
from services.digital_twin.repository import ensure_user_profile
from services.geography import cli
from services.geography.models import RESOLVER_VERSION

TEST_ONLY_EMAIL = "test-only-geography@example.invalid"
TEST_ONLY_TITLE = "TEST ONLY Data Internship"
TEST_ONLY_ORG = "TEST ONLY Org"
TEST_ONLY_MOROCCAN_LOCATION = "Capital Tower, Casablanca, Maroc"
TEST_ONLY_FRENCH_LOCATION = "Paris, France"
TEST_ONLY_MOBILITY = "Maroc"

#: Every word a run could leak, so no test has to guess which one would. The
#: address is in here too: an operator who typed it knows it, and a log file
#: that collects it holds personal data for no reason. `Maroc` is the person's
#: own spelling of their mobility; `MA` — the derived code — is not a leak.
TEST_ONLY_WORDS = (
    TEST_ONLY_TITLE,
    TEST_ONLY_ORG,
    TEST_ONLY_EMAIL,
    TEST_ONLY_MOROCCAN_LOCATION,
    TEST_ONLY_FRENCH_LOCATION,
    "Capital Tower",
    "Casablanca",
    "Paris",
    TEST_ONLY_MOBILITY,
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


def _seed(path, *, read_phase_3_5: bool = True, with_twin: bool = True) -> None:
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        for index, location in enumerate(
            (TEST_ONLY_MOROCCAN_LOCATION, TEST_ONLY_FRENCH_LOCATION)
        ):
            connection.execute(
                """INSERT INTO opportunities (
                       canonical_title, organization, location, description,
                       discovered_at, first_seen_at, last_seen_at, source_url,
                       status
                   ) VALUES (?, ?, ?, 'TEST ONLY description.', 't', 't', 't',
                             ?, 'new')""",
                (
                    TEST_ONLY_TITLE,
                    TEST_ONLY_ORG,
                    location,
                    f"https://example.invalid/{index}",
                ),
            )
        connection.commit()
        if read_phase_3_5:
            synchronize_opportunity_constraints(connection)
        if with_twin:
            profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
            set_profile_mobility(
                connection,
                profile_id,
                MobilityPreference(
                    scope=MobilityScope.RESTRICTED,
                    locations=(TEST_ONLY_MOBILITY,),
                ),
            )
            synchronize_profile_preferences(connection, profile_id)
    finally:
        connection.close()


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """Two invented postings, read by Phase 3.5, and one invented Digital Twin."""
    path = tmp_path / "geography.db"
    _seed(path)
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


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


# --------------------------------------------------------------------------
# What each command does
# --------------------------------------------------------------------------


def test_status_before_any_sync_reports_what_is_pending(
    logger, database, capsys
) -> None:
    assert run("status") == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["total_opportunities"] == "2"
    assert summary["source_location_rows"] == "2"
    assert summary["source_location_rows_resolved"] == "0"
    assert summary["source_location_rows_pending"] == "2"
    assert summary["segments"] == "0"
    assert summary["resolver_version"] == RESOLVER_VERSION


def test_sync_materializes_the_projection(logger, database, capsys) -> None:
    assert run("sync") == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["created"] == "2"
    assert summary["segments"] == "2"
    assert summary["resolved"] == "2"
    assert summary["known_country"] == "2"
    assert summary["changed"] == "true"


def test_a_second_sync_writes_nothing(logger, database, capsys) -> None:
    assert run("sync") == 0
    capsys.readouterr()
    assert run("sync") == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["unchanged"] == "2"
    assert summary["created"] == "0"
    assert summary["replaced"] == "0"
    assert summary["changed"] == "false"


def test_audit_reports_the_three_verdicts_and_the_derived_target(
    logger, database, capsys
) -> None:
    assert run("sync") == 0
    capsys.readouterr()
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["target_country_code"] == "MA"
    assert summary["total_opportunities"] == "2"
    assert summary["segments"] == "2"
    assert summary["resolved"] == "2"
    assert summary["ambiguous"] == "0"
    assert summary["unknown"] == "0"
    assert summary["resolved_target_country"] == "1"
    assert summary["resolved_other_country"] == "1"
    assert summary["match"] == "1"
    assert summary["out_of_target"] == "1"
    assert summary["unknown_target"] == "0"


def test_audit_before_a_sync_reports_unknown_rather_than_out_of_target(
    logger, database, capsys
) -> None:
    """An empty projection is a question about every posting, never a refusal."""
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["segments"] == "0"
    assert summary["unknown_target"] == "2"
    assert summary["out_of_target"] == "0"


def test_a_limit_bounds_the_postings_read(logger, database, capsys) -> None:
    assert run("sync", "--limit", "1") == 0
    summary = summary_of(capsys.readouterr().out)
    assert summary["total_opportunities"] == "1"
    assert summary["source_location_rows"] == "1"
    assert summary["created"] == "1"


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_audit_without_an_address_is_refused(logger, database, capsys) -> None:
    assert run("audit") == 1
    assert cli.MISSING_EMAIL_ERROR in capsys.readouterr().out


def test_an_unknown_person_is_refused(logger, stranger_database, capsys) -> None:
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 1
    assert cli.UNKNOWN_USER_ERROR in capsys.readouterr().out


def test_an_unmigrated_database_is_refused(
    logger, unmigrated_database, capsys
) -> None:
    assert run("sync") == 1
    assert "0024" in capsys.readouterr().out


def test_a_non_sqlite_backend_is_refused_before_connecting(
    logger, monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST-ONLY-TOKEN")
    assert run("sync") == 1
    assert cli.NON_SQLITE_BACKEND_ERROR in capsys.readouterr().out


def test_an_unknown_command_is_refused(logger, database) -> None:
    with pytest.raises(SystemExit):
        run("resolve-everything")


# --------------------------------------------------------------------------
# What it never prints
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("status",),
        ("sync",),
        ("audit", "--email", TEST_ONLY_EMAIL),
    ],
)
def test_no_command_prints_a_posting_or_a_person(
    logger, database, capsys, argv
) -> None:
    assert run(*argv) == 0
    printed = capsys.readouterr().out + logger.rendered()
    for word in TEST_ONLY_WORDS:
        assert word not in printed


def test_a_failure_names_the_failure_and_not_the_address(
    logger, stranger_database, capsys
) -> None:
    assert run("audit", "--email", TEST_ONLY_EMAIL) == 1
    printed = capsys.readouterr().out + logger.rendered()
    assert TEST_ONLY_EMAIL not in printed
    assert "LookupError" in printed
