"""Unit coverage for the local Phase 3.3B CV review CLI.

TEST ONLY content throughout: the CV below is invented and describes nobody,
and `.invalid` never resolves. Every database is created under `tmp_path` and
thrown away; no real CV and no real `.data/` database takes part.

This command is the one that deliberately prints CV content, so the tests come
in two halves: the values a reviewer must see do reach the review prompt, and
they reach nothing else — not the summary, not the structured log, not an error
message.
"""

import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.cv import review_cli
from services.digital_twin.facts.models import FactStatus
from services.digital_twin.facts.repository import list_profile_facts
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY address; no real identity is ever used or stored by the tests.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_LOCAL_PART = "student"
TEST_ONLY_CORRECTION = "Jeanne Corrigee"

# TEST ONLY CV content. The synthetic PDF below describes nobody.
FIRST_PAGE = [
    "Jeanne Exemple",
    "Data Scientist",
    "jeanne@example.invalid",
    "+33 6 00 00 00 00",
    "github.com/exemple-compte",
    "FORMATION",
    "Master fictif - Universite Exemple",
]
SECOND_PAGE = [
    "EXPERIENCE",
    "Stage analyste donnees",
    "COMPETENCES",
    "Python, SQL",
]
#: The nine candidates that CV yields, in the extractor's own order.
CANDIDATE_COUNT = 9
#: Values that must never appear outside the review prompt itself.
PERSONAL_TEST_CONTENT = (
    "Jeanne Exemple",
    "jeanne@example.invalid",
    "+33 6 00 00 00 00",
    "github.com/exemple-compte",
    "Master fictif",
    "Stage analyste donnees",
)


class _SilentLogger:
    """Keeps structured events out of the stdout assertions."""

    def info(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


@pytest.fixture()
def silence_logging(monkeypatch):
    monkeypatch.setattr(review_cli, "get_logger", lambda name: _SilentLogger())


@pytest.fixture()
def cv_path(tmp_path, synthetic_pdf):
    path = tmp_path / "cv.pdf"
    path.write_bytes(synthetic_pdf([FIRST_PAGE, SECOND_PAGE]))
    return path


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """A disposable SQLite database holding one existing profile."""
    path = tmp_path / "review.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        ensure_user_profile(connection, TEST_ONLY_EMAIL)
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


def asker(*answers: str):
    """Return an `ask` callable replaying these answers, and refusing more."""
    remaining = list(answers)

    def ask(_message: str) -> str:
        assert remaining, "the review asked more questions than the test answers"
        return remaining.pop(0)

    return ask


def refuse_prompt(_message: str) -> str:
    raise AssertionError("the CLI must not ask for an address it cannot use")


def facts(path) -> list:
    connection = connect_database(path)
    try:
        return list(list_profile_facts(connection, 1))
    finally:
        connection.close()


def statuses(path) -> list[FactStatus]:
    return [fact.status for fact in facts(path)]


def provenance_source_types(path, fact_id: int) -> list[str]:
    connection = connect_database(path)
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT source_type FROM profile_fact_provenance WHERE fact_id = ?",
                (fact_id,),
            )
        ]
    finally:
        connection.close()


def summary_of(output: str) -> dict[str, str]:
    """Read back the trailing `key=value` block the command prints."""
    return dict(
        line.split("=", 1)
        for line in output.splitlines()
        if "=" in line and not line.startswith(" ")
    )


# --------------------------------------------------------------------------
# The import half
# --------------------------------------------------------------------------


def test_the_import_proposes_every_candidate_and_accepts_none(
    silence_logging, database, cv_path, capsys
) -> None:
    """Quitting straight away leaves nine proposals and no decision at all."""
    exit_code = review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        prompt=refuse_prompt,
        ask=asker("q"),
    )
    summary = summary_of(capsys.readouterr().out)

    assert exit_code == 0
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT
    assert summary["candidates"] == str(CANDIDATE_COUNT)
    assert summary["newly_proposed"] == str(CANDIDATE_COUNT)
    assert summary["already_imported"] == "0"
    assert summary["accepted_this_run"] == "0"
    assert summary["remaining_proposed"] == str(CANDIDATE_COUNT)
    assert summary["quit_requested"] == "true"


def test_a_second_run_re_imports_nothing(
    silence_logging, database, cv_path, capsys
) -> None:
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("q"),
    )
    capsys.readouterr()

    exit_code = review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("q"),
    )
    summary = summary_of(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["newly_proposed"] == "0"
    assert summary["already_imported"] == str(CANDIDATE_COUNT)
    assert len(facts(database)) == CANDIDATE_COUNT


# --------------------------------------------------------------------------
# The five actions
# --------------------------------------------------------------------------


def test_accept_verifies_exactly_the_fact_it_was_typed_in_front_of(
    silence_logging, database, cv_path, capsys
) -> None:
    exit_code = review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("a", "q"),
    )
    summary = summary_of(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["accepted_this_run"] == "1"
    assert summary["remaining_proposed"] == str(CANDIDATE_COUNT - 1)
    recorded = facts(database)
    assert recorded[0].status is FactStatus.ACCEPTED
    assert recorded[0].value == "Jeanne Exemple"
    assert [fact.status for fact in recorded[1:]] == [FactStatus.PROPOSED] * 8


def test_reject_keeps_the_row_and_refuses_the_claim(
    silence_logging, database, cv_path, capsys
) -> None:
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("r", "q"),
    )
    summary = summary_of(capsys.readouterr().out)

    assert summary["rejected_this_run"] == "1"
    recorded = facts(database)
    assert recorded[0].status is FactStatus.REJECTED
    assert len(recorded) == CANDIDATE_COUNT


def test_correct_keeps_the_old_value_and_writes_a_new_accepted_user_input_fact(
    silence_logging, database, cv_path, capsys
) -> None:
    """A correction adds a fact; it never overwrites the one the CV proposed."""
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("c", TEST_ONLY_CORRECTION, "q"),
    )
    summary = summary_of(capsys.readouterr().out)

    assert summary["corrected_this_run"] == "1"
    recorded = facts(database)
    original = recorded[0]
    assert original.status is FactStatus.CORRECTED
    assert original.value == "Jeanne Exemple"
    replacement = next(
        fact for fact in recorded if fact.id == original.replaced_by_fact_id
    )
    assert replacement.value == TEST_ONLY_CORRECTION
    assert replacement.status is FactStatus.ACCEPTED
    assert replacement.fact_type == original.fact_type
    assert provenance_source_types(database, replacement.id) == ["USER_INPUT"]
    assert provenance_source_types(database, original.id) == ["CV"]


def test_an_empty_correction_decides_nothing_and_asks_again(
    silence_logging, database, cv_path, capsys
) -> None:
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("c", "   ", "q"),
    )
    output = capsys.readouterr().out

    assert review_cli.EMPTY_CORRECTION_NOTICE in output
    assert summary_of(output)["corrected_this_run"] == "0"
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_skip_leaves_the_fact_proposed_and_moves_on(
    silence_logging, database, cv_path, capsys
) -> None:
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("s", "a", "q"),
    )
    summary = summary_of(capsys.readouterr().out)

    assert (summary["skipped_this_run"], summary["accepted_this_run"]) == ("1", "1")
    recorded = facts(database)
    assert recorded[0].status is FactStatus.PROPOSED
    assert recorded[1].status is FactStatus.ACCEPTED


def test_quit_stops_cleanly_and_the_next_run_resumes(
    silence_logging, database, cv_path, capsys
) -> None:
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("a", "q"),
    )
    capsys.readouterr()

    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("a", "q"),
    )
    summary = summary_of(capsys.readouterr().out)

    assert summary["newly_proposed"] == "0"
    assert summary["accepted_this_run"] == "1"
    assert summary["remaining_proposed"] == str(CANDIDATE_COUNT - 2)
    recorded = facts(database)
    assert [fact.status for fact in recorded[:2]] == [
        FactStatus.ACCEPTED,
        FactStatus.ACCEPTED,
    ]


def test_an_unknown_answer_decides_nothing_and_asks_again(
    silence_logging, database, cv_path, capsys
) -> None:
    """A stray key must never accept somebody's CV for them."""
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("x", "", "yes", "q"),
    )
    output = capsys.readouterr().out
    summary = summary_of(output)

    assert output.count(review_cli.INVALID_ACTION_NOTICE) == 3
    assert summary["accepted_this_run"] == "0"
    assert summary["rejected_this_run"] == "0"
    assert summary["corrected_this_run"] == "0"
    assert summary["skipped_this_run"] == "0"
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_an_action_is_matched_exactly_and_never_tidied_up(
    silence_logging, database, cv_path, capsys
) -> None:
    """A capital or a stray space is not the answer `a`, and accepts nothing.

    Trimming or lowercasing the answer would be the command guessing what
    somebody meant, on the one prompt where guessing is the whole thing to
    avoid. The exact answer typed afterwards still works normally.
    """
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("A", " a ", "a", "q"),
    )
    output = capsys.readouterr().out
    summary = summary_of(output)

    # The two near-misses were refused, and only the exact answer decided.
    assert output.count(review_cli.INVALID_ACTION_NOTICE) == 2
    assert summary["accepted_this_run"] == "1"
    assert summary["rejected_this_run"] == "0"
    assert summary["corrected_this_run"] == "0"
    assert summary["skipped_this_run"] == "0"
    recorded = facts(database)
    assert recorded[0].status is FactStatus.ACCEPTED
    assert [fact.status for fact in recorded[1:]] == [FactStatus.PROPOSED] * 8


@pytest.mark.parametrize(
    "near_miss", ["A", "R", "C", "S", "Q", " a", "a ", " a ", "", "yes", "ac"]
)
def test_no_near_miss_of_any_action_decides_anything(
    silence_logging, database, cv_path, capsys, near_miss
) -> None:
    """Every one of these is refused, including a capital of each action."""
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker(near_miss, "q"),
    )
    output = capsys.readouterr().out
    summary = summary_of(output)

    assert review_cli.INVALID_ACTION_NOTICE in output
    assert summary["accepted_this_run"] == "0"
    assert summary["rejected_this_run"] == "0"
    assert summary["corrected_this_run"] == "0"
    assert summary["skipped_this_run"] == "0"
    assert summary["remaining_proposed"] == str(CANDIDATE_COUNT)
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_a_decided_fact_is_never_offered_again(
    silence_logging, database, cv_path, capsys
) -> None:
    """The second run starts at the second candidate, not the first."""
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("a", "r", "q"),
    )
    capsys.readouterr()

    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("q"),
    )
    output = capsys.readouterr().out

    # Seven left to review, and the two already decided are not among them.
    assert "[1/7]" in output
    assert "Jeanne Exemple" not in output
    assert "Data Scientist" not in output
    # The third candidate was never decided, so it is still offered.
    assert "value: jeanne@example.invalid" in output
    assert summary_of(output)["remaining_proposed"] == "7"


def test_running_out_of_input_stops_without_deciding(
    silence_logging, database, cv_path, capsys
) -> None:
    """A closed stdin is a quit, never an implicit acceptance."""

    def ask(_message: str) -> str:
        raise EOFError

    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=ask,
    )
    summary = summary_of(capsys.readouterr().out)

    assert summary["quit_requested"] == "true"
    assert summary["remaining_proposed"] == str(CANDIDATE_COUNT)
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_there_is_no_accept_all_of_any_kind() -> None:
    parser_error = SystemExit
    for flag in ("--accept-all", "--yes", "--auto-accept", "--threshold"):
        with pytest.raises(parser_error):
            review_cli.parse_args([review_cli.REVIEW_COMMAND, "cv.pdf", flag])


# --------------------------------------------------------------------------
# What the reviewer sees, and what nothing else does
# --------------------------------------------------------------------------


def test_the_review_prompt_shows_enough_to_decide(
    silence_logging, database, cv_path, capsys
) -> None:
    """This command prints CV content on purpose: that is what a review is."""
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("s", "s", "s", "s", "s", "s", "s", "s", "s"),
    )
    output = capsys.readouterr().out

    assert review_cli.SENSITIVE_OUTPUT_NOTICE in output
    assert "[1/9] NAME" in output
    assert "value: Jeanne Exemple" in output
    assert "value: jeanne@example.invalid" in output
    assert "pages=1 section=UNCLASSIFIED#0 rule=EMAIL_PATTERN" in output
    assert "pages=2 section=SKILLS#3 rule=SKILLS_SEPARATED_LIST_LINE" in output


def test_the_closing_summary_repeats_no_value(
    silence_logging, database, cv_path, capsys
) -> None:
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("a", "a", "q"),
    )
    output = capsys.readouterr().out
    closing = output[output.rindex("candidates=") :]

    for secret in PERSONAL_TEST_CONTENT:
        assert secret not in closing
    assert set(summary_of(closing)) == {
        "candidates",
        "newly_proposed",
        "already_imported",
        "accepted_this_run",
        "rejected_this_run",
        "corrected_this_run",
        "skipped_this_run",
        "remaining_proposed",
        "quit_requested",
    }


def test_the_cv_path_is_never_echoed_back(
    silence_logging, database, cv_path, capsys
) -> None:
    review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker("q"),
    )
    missing = review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path.parent / "absent.pdf"),
         "--email", TEST_ONLY_EMAIL],
        ask=asker(),
    )
    output = capsys.readouterr().out

    assert missing == 1
    assert str(cv_path) not in output
    assert "cv.pdf" not in output
    assert "absent.pdf" not in output


def test_structured_events_carry_counters_and_never_the_cv(
    database, cv_path, capsys
) -> None:
    """The events are read off the shared JSON handler, where they really go.

    The command shares the `services.collector` logger with the Phase 3.2A
    parser it calls, and that parser reconfigures the handler onto the current
    `sys.stdout`. Reading the JSON lines back out of the captured stdout tests
    what an operator would actually see, rather than a stream only this test
    holds.
    """
    assert (
        review_cli.main(
            [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
            ask=asker("a", "r", "q"),
        )
        == 0
    )
    output = capsys.readouterr().out
    events = [
        json.loads(line)
        for line in output.splitlines()
        if line.startswith("{") and line.endswith("}")
    ]
    rendered = "\n".join(
        line for line in output.splitlines() if line.startswith("{")
    )

    succeeded = next(
        event for event in events if event["event"] == "cv_review_command_succeeded"
    )
    assert succeeded["context"] == {
        "database_backend": "sqlite",
        "parser_version": "cv-parser-v2",
        "extractor_version": "cv-candidates-v2",
        "candidates": CANDIDATE_COUNT,
        "newly_proposed": CANDIDATE_COUNT,
        "already_imported": 0,
        "accepted_this_run": 1,
        "rejected_this_run": 1,
        "corrected_this_run": 0,
        "skipped_this_run": 0,
        "remaining_proposed": CANDIDATE_COUNT - 2,
        "quit_requested": True,
    }
    for secret in PERSONAL_TEST_CONTENT:
        assert secret not in rendered
    assert TEST_ONLY_LOCAL_PART not in rendered
    assert str(cv_path) not in rendered


# --------------------------------------------------------------------------
# Refusals before anything is touched
# --------------------------------------------------------------------------


def test_a_non_sqlite_backend_is_refused_before_any_connection(
    silence_logging, monkeypatch, cv_path, capsys
) -> None:
    secret = "TEST_ONLY_TURSO_AUTH_TOKEN_DO_NOT_EXPOSE"
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "https://test-only.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", secret)

    def refuse(_settings):
        raise AssertionError("the CLI must not connect to a remote backend")

    monkeypatch.setattr(review_cli, "connect_configured_database", refuse)

    exit_code = review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path)],
        prompt=refuse_prompt,
        ask=asker(),
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert review_cli.NON_SQLITE_BACKEND_ERROR in output
    assert secret not in output


def test_a_missing_profile_is_reported_and_never_created(
    silence_logging, monkeypatch, tmp_path, cv_path, capsys
) -> None:
    """Reviewing a CV is not a way to bring a profile into existence."""
    path = tmp_path / "empty.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))

    exit_code = review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=asker(),
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert review_cli.NO_PROFILE_ERROR in output
    assert TEST_ONLY_LOCAL_PART not in output
    connection = connect_database(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0] == 0
        )
    finally:
        connection.close()


def test_the_address_can_be_given_without_the_shell_history(
    silence_logging, database, cv_path, capsys
) -> None:
    asked: list[str] = []

    def prompt(message: str) -> str:
        asked.append(message)
        return TEST_ONLY_EMAIL

    exit_code = review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path)], prompt=prompt, ask=asker("q")
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert asked == [review_cli.EMAIL_PROMPT]
    assert TEST_ONLY_LOCAL_PART not in summary_of(output).keys()
