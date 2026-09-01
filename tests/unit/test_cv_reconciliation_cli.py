"""Unit coverage for the local Phase 3.3C CV reconciliation CLI.

TEST ONLY content throughout: the CV below is invented and describes nobody,
and `.invalid` never resolves. Every database is created under `tmp_path` and
thrown away; no real CV and no real `.data/` database takes part.

Unlike the review command, this one shows nobody anything they have to decide,
so it prints no CV content at all. Half of these tests are about that: the
values reach neither stdout, nor the structured log, nor an error message, on
any of the three commands and on either kind of failure.
"""

import json
from dataclasses import replace

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.cv import reconciliation_cli
from services.digital_twin.cv.candidates.extractor import extract_candidates
from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    StructuredCvExtraction,
)
from services.digital_twin.cv.models import PARSER_VERSION
from services.digital_twin.cv.fact_bridge import import_cv_candidates
from services.digital_twin.cv.parser import parse_cv_pdf
from services.digital_twin.facts.models import FactStatus
from services.digital_twin.facts.repository import (
    accept_profile_fact,
    list_profile_facts,
)
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY address; no real identity is ever used or stored by the tests.
TEST_ONLY_EMAIL = "student@example.invalid"

#: The campaign the CLI reconciles away from by default. Taken from the module
#: rather than spelled out, because what these tests exercise is the default
#: itself: seed the database with the campaign the CLI will look for, run the
#: command with no version arguments, and the two have to line up. Spelling a
#: version here would only make the tests pass a bump without noticing one.
OLD_PARSER = reconciliation_cli.PREVIOUS_PARSER_VERSION
OLD_EXTRACTOR = reconciliation_cli.PREVIOUS_EXTRACTOR_VERSION

# TEST ONLY CV content. The synthetic PDF below describes nobody.
FIRST_PAGE = [
    "Jeanne Exemple",
    "Data Scientist",
    "jeanne@example.invalid",
    "FORMATION",
    "Master fictif - Universite Exemple",
]
SECOND_PAGE = [
    "EXPERIENCE",
    "Stage analyste donnees",
    "",
    "PROJETS",
    "Prototype fictif de tableau de bord",
    "COMPETENCES",
    "Python, SQL",
]
#: Values that must never appear on stdout or in the structured log.
PERSONAL_TEST_CONTENT = (
    "Jeanne Exemple",
    "jeanne@example.invalid",
    "Master fictif",
    "Stage analyste donnees",
    "Prototype fictif",
    "Python",
)

#: The shape of that document once the older campaign is replayed over it: the
#: project entry is the one reading the newer rules classify differently.
CANDIDATES = 8
UNCHANGED = 7
CHANGED = 1
SUPERSEDED = 1


class _RecordingLogger:
    """Keeps the structured events out of stdout, and readable by the tests."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def info(self, message, *args, **kwargs) -> None:
        self.events.append({"message": message, **kwargs.get("extra", {})})

    def error(self, message, *args, **kwargs) -> None:
        self.events.append({"message": message, **kwargs.get("extra", {})})


@pytest.fixture()
def logged(monkeypatch):
    logger = _RecordingLogger()
    monkeypatch.setattr(reconciliation_cli, "get_logger", lambda name: logger)
    return logger


@pytest.fixture()
def cv_path(tmp_path, synthetic_pdf):
    path = tmp_path / "cv.pdf"
    path.write_bytes(synthetic_pdf([FIRST_PAGE, SECOND_PAGE]))
    return path


def historical_extraction(extraction: StructuredCvExtraction):
    """The same document as an older campaign read it.

    Every candidate is the current one relabelled with the older versions,
    except the project entry, which those older rules called an experience.
    That single difference is the whole scenario: seven readings come back
    identical and one comes back under another type.
    """
    candidates = tuple(
        replace(
            candidate,
            candidate_type=(
                CandidateType.EXPERIENCE_ENTRY
                if candidate.candidate_type is CandidateType.PROJECT_ENTRY
                else candidate.candidate_type
            ),
            parser_version=OLD_PARSER,
            extractor_version=OLD_EXTRACTOR,
        )
        for candidate in extraction.candidates
    )
    return StructuredCvExtraction(
        extractor_version=OLD_EXTRACTOR,
        parser_version=OLD_PARSER,
        cv_sha256=extraction.cv_sha256,
        candidates=candidates,
        warnings=(),
    )


@pytest.fixture()
def database(monkeypatch, tmp_path, cv_path):
    """A disposable database holding one profile and one older campaign."""
    path = tmp_path / "reconciliation.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
        result = import_cv_candidates(
            connection,
            profile_id=profile_id,
            extraction=historical_extraction(
                extract_candidates(parse_cv_pdf(cv_path))
            ),
        )
        assert len(result.imported) == CANDIDATES
        for entry in result.imported:
            accept_profile_fact(connection, profile_id, entry.fact.id)
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


def open_database(path):
    return connect_database(path)


def run(command, cv_path, *extra):
    return reconciliation_cli.main(
        [command, str(cv_path), "--email", TEST_ONLY_EMAIL, *extra]
    )


def reported(capsys) -> dict[str, str]:
    """The `key=value` lines the command printed, as a mapping."""
    printed = capsys.readouterr().out
    values: dict[str, str] = {}
    for line in printed.splitlines():
        if "=" in line:
            key, _, value = line.strip().partition("=")
            values.setdefault(key, value)
    return values


def accept_the_new_readings(path):
    """Do by hand what the review CLI would do between prepare and finalize."""
    connection = open_database(path)
    try:
        profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
        for fact in list_profile_facts(connection, profile_id):
            if fact.status is FactStatus.PROPOSED:
                accept_profile_fact(connection, profile_id, fact.id)
    finally:
        connection.close()


def test_plan_reports_the_split_and_writes_nothing(
    cv_path, database, logged, capsys
):
    connection = open_database(database)
    try:
        before = list_profile_facts(connection, 1)
    finally:
        connection.close()

    assert run("plan", cv_path) == 0

    values = reported(capsys)
    assert values["new_candidates"] == str(CANDIDATES)
    assert values["unchanged_candidates"] == str(UNCHANGED)
    assert values["changed_candidates"] == str(CHANGED)
    assert values["superseded_old_facts"] == str(SUPERSEDED)
    assert values["old_parser_version"] == OLD_PARSER
    connection = open_database(database)
    try:
        assert list_profile_facts(connection, 1) == before
    finally:
        connection.close()


def test_prepare_attaches_the_new_evidence_and_proposes_the_rest(
    cv_path, database, logged, capsys
):
    assert run("prepare", cv_path) == 0

    values = reported(capsys)
    assert values["provenance_attached"] == str(UNCHANGED)
    assert values["newly_proposed"] == str(CHANGED)
    assert values["pending_review"] == str(CHANGED)
    connection = open_database(database)
    try:
        facts = list_profile_facts(connection, 1)
    finally:
        connection.close()
    assert len(facts) == CANDIDATES + CHANGED
    assert [fact.status for fact in facts].count(FactStatus.PROPOSED) == CHANGED


def test_prepare_twice_writes_nothing_the_second_time(
    cv_path, database, logged, capsys
):
    assert run("prepare", cv_path) == 0
    connection = open_database(database)
    try:
        after_first = list_profile_facts(connection, 1)
    finally:
        connection.close()
    capsys.readouterr()

    assert run("prepare", cv_path) == 0

    values = reported(capsys)
    assert values["provenance_attached"] == "0"
    assert values["provenance_already_present"] == str(UNCHANGED)
    assert values["newly_proposed"] == "0"
    connection = open_database(database)
    try:
        assert list_profile_facts(connection, 1) == after_first
    finally:
        connection.close()


def test_finalize_refuses_while_the_new_reading_is_undecided(
    cv_path, database, logged, capsys
):
    assert run("prepare", cv_path) == 0
    capsys.readouterr()

    assert run("finalize", cv_path) == 1

    printed = capsys.readouterr().out
    assert "unresolved_changed_readings=1" in printed
    assert "reason=PROPOSED" in printed
    assert reconciliation_cli.UNRESOLVED_NOTICE in printed
    connection = open_database(database)
    try:
        assert not [
            fact
            for fact in list_profile_facts(connection, 1)
            if fact.status is FactStatus.REJECTED
        ]
    finally:
        connection.close()


def test_finalize_retires_the_old_reading_once_the_new_one_is_accepted(
    cv_path, database, logged, capsys
):
    assert run("prepare", cv_path) == 0
    accept_the_new_readings(database)
    capsys.readouterr()

    assert run("finalize", cv_path) == 0

    values = reported(capsys)
    assert values["newly_rejected"] == str(SUPERSEDED)
    assert values["changed_anything"] == "true"
    connection = open_database(database)
    try:
        rejected = [
            fact
            for fact in list_profile_facts(connection, 1)
            if fact.status is FactStatus.REJECTED
        ]
    finally:
        connection.close()
    assert len(rejected) == SUPERSEDED
    assert rejected[0].fact_type == "EXPERIENCE"


def test_a_second_finalize_rejects_nothing(cv_path, database, logged, capsys):
    assert run("prepare", cv_path) == 0
    accept_the_new_readings(database)
    assert run("finalize", cv_path) == 0
    capsys.readouterr()

    assert run("finalize", cv_path) == 0

    values = reported(capsys)
    assert values["newly_rejected"] == "0"
    assert values["already_rejected"] == str(SUPERSEDED)
    assert values["changed_anything"] == "false"


def test_the_old_campaign_can_be_named_explicitly(cv_path, database, logged, capsys):
    assert (
        run(
            "plan",
            cv_path,
            "--old-parser-version",
            "cv-parser-v0",
            "--old-extractor-version",
            "cv-candidates-v0",
        )
        == 0
    )

    values = reported(capsys)
    # No fact was read from that campaign, so nothing is unchanged and nothing
    # is superseded: every candidate is a reading of its own.
    assert values["historical_facts"] == "0"
    assert values["unchanged_candidates"] == "0"
    assert values["changed_candidates"] == str(CANDIDATES)
    assert values["superseded_old_facts"] == "0"


def test_reconciling_the_current_campaign_with_itself_is_refused(
    cv_path, database, logged, capsys
):
    assert (
        run(
            "plan",
            cv_path,
            "--old-parser-version",
            PARSER_VERSION,
            "--old-extractor-version",
            CANDIDATE_EXTRACTOR_VERSION,
        )
        == 1
    )

    printed = capsys.readouterr().out
    assert "CvReconciliationVersionError" in printed


def test_a_stale_extraction_is_refused_before_the_database_is_opened(
    monkeypatch, cv_path, database, logged, capsys
):
    monkeypatch.setattr(
        reconciliation_cli, "extraction_is_current", lambda extraction: False
    )

    def refuse(settings):
        raise AssertionError("no database must be opened for a stale extraction")

    monkeypatch.setattr(reconciliation_cli, "connect_configured_database", refuse)

    assert run("plan", cv_path) == 1

    assert reconciliation_cli.STALE_EXTRACTION_ERROR in capsys.readouterr().out


def test_a_non_sqlite_backend_is_refused_before_anything_is_parsed(
    monkeypatch, cv_path, database, logged, capsys
):
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST-ONLY-token")

    assert run("plan", cv_path) == 1

    assert reconciliation_cli.NON_SQLITE_BACKEND_ERROR in capsys.readouterr().out


def test_an_absent_profile_is_an_absence(cv_path, database, logged, capsys):
    assert (
        reconciliation_cli.main(
            ["plan", str(cv_path), "--email", "nobody@example.invalid"]
        )
        == 1
    )

    assert reconciliation_cli.NO_PROFILE_ERROR in capsys.readouterr().out


def test_the_address_is_prompted_without_echo_when_it_is_not_given(
    cv_path, database, logged, capsys
):
    asked: list[str] = []

    def prompt(message: str) -> str:
        asked.append(message)
        return TEST_ONLY_EMAIL

    assert (
        reconciliation_cli.main(["plan", str(cv_path)], prompt=prompt) == 0
    )

    assert asked == [reconciliation_cli.EMAIL_PROMPT]
    printed = capsys.readouterr().out
    assert TEST_ONLY_EMAIL not in printed


@pytest.mark.parametrize("command", ["plan", "prepare", "finalize"])
def test_no_command_ever_prints_cv_content(
    command, cv_path, database, logged, capsys
):
    if command == "finalize":
        run("prepare", cv_path)
        accept_the_new_readings(database)
        capsys.readouterr()

    run(command, cv_path)

    printed = capsys.readouterr().out
    for value in PERSONAL_TEST_CONTENT:
        assert value not in printed
    assert TEST_ONLY_EMAIL not in printed
    # Nor the path of the document, which usually carries the person's name.
    assert str(cv_path) not in printed


@pytest.mark.parametrize("command", ["plan", "prepare", "finalize"])
def test_no_command_ever_logs_cv_content(command, cv_path, database, logged, capsys):
    if command == "finalize":
        run("prepare", cv_path)
        accept_the_new_readings(database)

    run(command, cv_path)

    rendered = json.dumps(logged.events, default=str)
    for value in PERSONAL_TEST_CONTENT:
        assert value not in rendered
    assert TEST_ONLY_EMAIL not in rendered
    assert str(cv_path) not in rendered


def test_a_refusal_prints_no_cv_content_either(cv_path, database, logged, capsys):
    run("prepare", cv_path)
    capsys.readouterr()

    assert run("finalize", cv_path) == 1

    printed = capsys.readouterr().out
    rendered = json.dumps(logged.events, default=str)
    for value in PERSONAL_TEST_CONTENT:
        assert value not in printed
        assert value not in rendered


def test_a_failure_prints_no_cv_content_either(cv_path, database, logged, capsys):
    missing = cv_path.parent / "TEST-ONLY-absent.pdf"

    assert (
        reconciliation_cli.main(
            ["plan", str(missing), "--email", TEST_ONLY_EMAIL]
        )
        == 1
    )

    printed = capsys.readouterr().out
    assert "cv reconciliation failed" in printed
    assert str(missing) not in printed


def test_the_command_names_are_the_three_documented_ones():
    assert reconciliation_cli.COMMANDS == ("plan", "prepare", "finalize")
    with pytest.raises(SystemExit):
        reconciliation_cli.parse_args(["accept", "cv.pdf"])
