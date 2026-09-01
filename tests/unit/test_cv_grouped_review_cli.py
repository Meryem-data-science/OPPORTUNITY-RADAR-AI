"""Unit coverage for the grouped shape of the local CV review CLI.

TEST ONLY content throughout: every CV below is invented, describes nobody, and
`.invalid` never resolves. Every database is created under `tmp_path` and thrown
away; no real CV and no real `.data/` database takes part.

The grouped review exists to ask fewer questions, and these tests are mostly
about what it still refuses to do while asking them: `a` decides only facts that
were printed, only facts of the group on screen, and only facts nobody has
decided yet; a malformed command decides nothing at all; and no command anywhere
reaches the whole CV.
"""

import dataclasses
import json
import re

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.cv import grouped_review, review_cli
from services.digital_twin.cv.grouped_review import (
    FactGroup,
    GroupAction,
    GroupCommand,
    build_review_groups,
    group_for_fact_type,
    parse_group_command,
)
from services.digital_twin.facts.models import FactStatus, ProfileFactType
from services.digital_twin.facts.repository import (
    ProfileFactError,
    decide_profile_facts,
    list_profile_facts,
)
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY address; no real identity is ever used or stored by the tests.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_LOCAL_PART = "student"
TEST_ONLY_CORRECTION = "Example Corrected Person"

# TEST ONLY CV content. The synthetic PDF below describes nobody, and carries
# one candidate of every group the review knows about.
FIRST_PAGE = [
    "Example Person",
    "Data Scientist",
    "person@example.invalid",
    "+00 0 00 00 00 00",
    "github.com/example-account",
    "linkedin.com/in/example-account",
    "FORMATION",
    "Master fictif - Example Institute",
]
SECOND_PAGE = [
    "EXPERIENCE",
    "Stage fictif - Example Company",
    "PROJETS",
    "Example Project",
    "COMPETENCES",
    "Skill-01, Skill-02, Skill-03",
    "LANGUES",
    "Francais - courant",
    "CERTIFICATIONS",
    "Example Certificate",
]
#: The fourteen candidates that CV yields, in the extractor's own order.
CANDIDATE_COUNT = 14
#: The seven groups they fall into, in the order the review walks them.
GROUP_COUNT = 7
#: The six facts of the first group: name, title, email, phone, two URLs.
IDENTITY_SIZE = 6
#: Values that must never appear outside the review prompt itself.
PERSONAL_TEST_CONTENT = (
    "Example Person",
    "person@example.invalid",
    "+00 0 00 00 00 00",
    "github.com/example-account",
    "Master fictif",
    "Stage fictif",
    "Example Certificate",
    "Skill-01",
)

#: A second TEST ONLY CV, all of whose interest is that its skills are many.
SKILL_COUNT = 42
SKILL_VALUES = tuple(f"Skill-{number:02d}" for number in range(1, SKILL_COUNT + 1))


def skills_page() -> list[str]:
    lines = ["Example Person", "person@example.invalid", "COMPETENCES"]
    for start in range(0, SKILL_COUNT, 6):
        lines.append(", ".join(SKILL_VALUES[start : start + 6]))
    return lines


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
def skills_cv_path(tmp_path, synthetic_pdf):
    path = tmp_path / "skills.pdf"
    path.write_bytes(synthetic_pdf([skills_page()]))
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


def run(path, *answers: str, grouped: bool = True) -> int:
    argv = [review_cli.REVIEW_COMMAND, str(path), "--email", TEST_ONLY_EMAIL]
    if grouped:
        argv.append("--grouped")
    return review_cli.main(argv, ask=asker(*answers))


def facts(path) -> list:
    connection = connect_database(path)
    try:
        return list(list_profile_facts(connection, 1))
    finally:
        connection.close()


def statuses(path) -> list[FactStatus]:
    return [fact.status for fact in facts(path)]


def by_type(path) -> dict[str, list]:
    collected: dict[str, list] = {}
    for fact in facts(path):
        collected.setdefault(fact.fact_type, []).append(fact)
    return collected


def status_of(path, fact_type: ProfileFactType) -> FactStatus:
    """The status of the one fact of that type. Fails if there is not one."""
    found = by_type(path)[fact_type.value]
    assert len(found) == 1
    return found[0].status


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


#: The closing block is `counter=value` lines, and a counter is named in lower
#: case: no CV value the review printed above can be read back as one.
_COUNTER_LINE = re.compile(r"^[a-z_]+=")


def summary_of(output: str) -> dict[str, str]:
    """Read back the trailing `key=value` block the command prints."""
    return dict(
        line.split("=", 1)
        for line in output.splitlines()
        if _COUNTER_LINE.match(line)
    )


def group_headers(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("GROUP ")]


# --------------------------------------------------------------------------
# The default review is untouched
# --------------------------------------------------------------------------


def test_the_default_review_is_still_one_fact_at_a_time(
    silence_logging, database, cv_path, capsys
) -> None:
    """Without the flag, nothing about the historical review changes."""
    asked: list[str] = []

    def ask(message: str) -> str:
        asked.append(message)
        return "a" if len(asked) == 1 else "q"

    exit_code = review_cli.main(
        [review_cli.REVIEW_COMMAND, str(cv_path), "--email", TEST_ONLY_EMAIL],
        ask=ask,
    )
    output = capsys.readouterr().out
    summary = summary_of(output)

    assert exit_code == 0
    assert f"[1/{CANDIDATE_COUNT}] NAME" in output
    assert asked == [review_cli.ACTION_PROMPT, review_cli.ACTION_PROMPT]
    assert group_headers(output) == []
    assert summary["accepted_this_run"] == "1"
    assert set(summary) == {
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
    assert statuses(database)[1:] == [FactStatus.PROPOSED] * (CANDIDATE_COUNT - 1)


def test_the_grouped_flag_adds_no_accept_all_of_any_kind() -> None:
    for flag in ("--accept-all", "--yes", "--auto-accept", "--threshold"):
        with pytest.raises(SystemExit):
            review_cli.parse_args(
                [review_cli.REVIEW_COMMAND, "cv.pdf", "--grouped", flag]
            )


# --------------------------------------------------------------------------
# Accepting, rejecting and correcting inside one printed group
# --------------------------------------------------------------------------


def test_one_explicit_action_accepts_the_whole_printed_group(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "a", "q")
    summary = summary_of(capsys.readouterr().out)
    recorded = statuses(database)

    assert summary["accepted_this_run"] == str(IDENTITY_SIZE)
    assert recorded[:IDENTITY_SIZE] == [FactStatus.ACCEPTED] * IDENTITY_SIZE
    assert recorded[IDENTITY_SIZE:] == [FactStatus.PROPOSED] * (
        CANDIDATE_COUNT - IDENTITY_SIZE
    )


def test_only_the_named_indices_are_accepted(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "a 1,3", "q")
    summary = summary_of(capsys.readouterr().out)

    assert summary["accepted_this_run"] == "2"
    assert status_of(database, ProfileFactType.NAME) is FactStatus.ACCEPTED
    assert status_of(database, ProfileFactType.EMAIL) is FactStatus.ACCEPTED
    assert (
        status_of(database, ProfileFactType.PROFESSIONAL_TITLE) is FactStatus.PROPOSED
    )
    assert summary["remaining_proposed"] == str(CANDIDATE_COUNT - 2)


def test_a_rejection_decides_exactly_the_index_it_names(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "r 2", "q")
    summary = summary_of(capsys.readouterr().out)

    assert (summary["rejected_this_run"], summary["accepted_this_run"]) == ("1", "0")
    assert (
        status_of(database, ProfileFactType.PROFESSIONAL_TITLE) is FactStatus.REJECTED
    )
    assert status_of(database, ProfileFactType.NAME) is FactStatus.PROPOSED
    assert len(facts(database)) == CANDIDATE_COUNT


def test_a_rejection_then_an_accept_answers_the_group_in_two_commands(
    silence_logging, database, cv_path, capsys
) -> None:
    """The workflow the grouped review exists for: refuse two, keep the rest."""
    run(cv_path, "r 2", "a", "q")
    summary = summary_of(capsys.readouterr().out)
    recorded = statuses(database)

    assert summary["rejected_this_run"] == "1"
    assert summary["accepted_this_run"] == str(IDENTITY_SIZE - 1)
    assert recorded[:IDENTITY_SIZE] == [
        FactStatus.ACCEPTED,
        FactStatus.REJECTED,
        FactStatus.ACCEPTED,
        FactStatus.ACCEPTED,
        FactStatus.ACCEPTED,
        FactStatus.ACCEPTED,
    ]
    assert recorded[IDENTITY_SIZE:] == [FactStatus.PROPOSED] * (
        CANDIDATE_COUNT - IDENTITY_SIZE
    )


def test_a_partial_action_stays_in_the_same_group_and_reprints_it(
    silence_logging, database, cv_path, capsys
) -> None:
    """The reader must be able to see what a partial decision did."""
    run(cv_path, "r 2", "q")
    output = capsys.readouterr().out
    headers = group_headers(output)

    assert headers == [
        f"GROUP 1/{GROUP_COUNT} — {FactGroup.IDENTITY_CONTACT.value}",
        f"GROUP 1/{GROUP_COUNT} — {FactGroup.IDENTITY_CONTACT.value}",
    ]
    assert f"{IDENTITY_SIZE} of {IDENTITY_SIZE} facts still PROPOSED" in output
    assert f"{IDENTITY_SIZE - 1} of {IDENTITY_SIZE} facts still PROPOSED" in output
    assert "— REJECTED in this run" in output


def test_a_correction_stays_individual_and_leaves_the_others_alone(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "c 1", TEST_ONLY_CORRECTION, "q")
    summary = summary_of(capsys.readouterr().out)
    recorded = facts(database)
    original = recorded[0]
    replacement = next(
        fact for fact in recorded if fact.id == original.replaced_by_fact_id
    )

    assert summary["corrected_this_run"] == "1"
    assert summary["accepted_this_run"] == "0"
    assert original.status is FactStatus.CORRECTED
    assert original.value == "Example Person"
    assert replacement.status is FactStatus.ACCEPTED
    assert replacement.value == TEST_ONLY_CORRECTION
    assert replacement.fact_type == original.fact_type
    assert provenance_source_types(database, replacement.id) == ["USER_INPUT"]
    # Nothing else in the group moved.
    assert [fact.status for fact in recorded[1:CANDIDATE_COUNT]] == [
        FactStatus.PROPOSED
    ] * (CANDIDATE_COUNT - 1)


def test_an_empty_correction_decides_nothing_and_keeps_the_group(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "c 1", "   ", "q")
    output = capsys.readouterr().out

    assert review_cli.EMPTY_CORRECTION_NOTICE in output
    assert summary_of(output)["corrected_this_run"] == "0"
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


# --------------------------------------------------------------------------
# Skipping, quitting, resuming
# --------------------------------------------------------------------------


def test_skip_leaves_the_group_proposed_and_shows_the_next_one(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "s", "q")
    output = capsys.readouterr().out
    summary = summary_of(output)

    assert group_headers(output) == [
        f"GROUP 1/{GROUP_COUNT} — {FactGroup.IDENTITY_CONTACT.value}",
        f"GROUP 2/{GROUP_COUNT} — {FactGroup.EDUCATION.value}",
    ]
    assert summary["skipped_this_run"] == str(IDENTITY_SIZE)
    assert summary["accepted_this_run"] == "0"
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_quit_keeps_what_was_decided_and_proposes_the_rest(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "a 1", "q")
    summary = summary_of(capsys.readouterr().out)

    assert summary["quit_requested"] == "true"
    assert summary["accepted_this_run"] == "1"
    assert summary["remaining_proposed"] == str(CANDIDATE_COUNT - 1)
    assert status_of(database, ProfileFactType.NAME) is FactStatus.ACCEPTED


def test_a_closed_input_is_a_quit_and_never_an_acceptance(
    silence_logging, database, cv_path, capsys
) -> None:
    def ask(_message: str) -> str:
        raise EOFError

    review_cli.main(
        [
            review_cli.REVIEW_COMMAND,
            str(cv_path),
            "--email",
            TEST_ONLY_EMAIL,
            "--grouped",
        ],
        ask=ask,
    )
    summary = summary_of(capsys.readouterr().out)

    assert summary["quit_requested"] == "true"
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_facts_decided_before_are_never_shown_or_asked_again(
    silence_logging, database, cv_path, capsys
) -> None:
    """Resuming shows the undecided remainder, and only it."""
    run(cv_path, "a 1,2", "q")
    capsys.readouterr()

    run(cv_path, "q")
    output = capsys.readouterr().out

    assert (
        f"GROUP 1/{GROUP_COUNT} — {FactGroup.IDENTITY_CONTACT.value}"
        in group_headers(output)[0]
    )
    assert (
        f"{IDENTITY_SIZE - 2} of {IDENTITY_SIZE - 2} facts still PROPOSED" in output
    )
    assert "Example Person" not in output
    assert "Data Scientist" not in output
    assert "value: person@example.invalid" in output
    assert summary_of(output)["remaining_proposed"] == str(CANDIDATE_COUNT - 2)


def test_a_second_grouped_run_re_imports_nothing(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "q")
    capsys.readouterr()

    run(cv_path, "q")
    summary = summary_of(capsys.readouterr().out)

    assert summary["newly_proposed"] == "0"
    assert summary["already_imported"] == str(CANDIDATE_COUNT)
    assert len(facts(database)) == CANDIDATE_COUNT


# --------------------------------------------------------------------------
# What a command may not do
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    [
        "a banana",
        "x",
        "a 999",
        "r 0",
        "c 1,2",
        "a 1--4",
        "a 1-",
        "a -1",
        "a 1,",
        "a  1",
        "A",
        "a ",
        " a",
        "",
        "yes",
        "r",
        "c",
        "s 1",
        "q 1",
        "a 01",
        "a 3-1",
    ],
)
def test_a_command_that_is_not_one_decides_nothing(
    silence_logging, database, cv_path, capsys, typed
) -> None:
    """No input error ends in an acceptance, or in any other mutation."""
    run(cv_path, typed, "q")
    summary = summary_of(capsys.readouterr().out)

    assert summary["accepted_this_run"] == "0"
    assert summary["rejected_this_run"] == "0"
    assert summary["corrected_this_run"] == "0"
    assert summary["skipped_this_run"] == "0"
    assert summary["remaining_proposed"] == str(CANDIDATE_COUNT)
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_a_refusal_says_why_and_asks_again(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "r", "c", "c 1,2", "a 999", "zz", "q")
    output = capsys.readouterr().out

    assert grouped_review.REJECT_NEEDS_INDICES_NOTICE in output
    assert output.count(grouped_review.CORRECT_NEEDS_ONE_INDEX_NOTICE) == 2
    assert grouped_review.UNKNOWN_INDEX_NOTICE in output
    assert grouped_review.INVALID_COMMAND_NOTICE in output
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_a_range_is_inclusive_and_mixes_with_a_list(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "a 1-3,5", "q")
    summary = summary_of(capsys.readouterr().out)
    recorded = statuses(database)

    assert summary["accepted_this_run"] == "4"
    assert recorded[:IDENTITY_SIZE] == [
        FactStatus.ACCEPTED,
        FactStatus.ACCEPTED,
        FactStatus.ACCEPTED,
        FactStatus.PROPOSED,
        FactStatus.ACCEPTED,
        FactStatus.PROPOSED,
    ]


def test_a_repeated_index_decides_once(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "a 1,1,2", "q")
    summary = summary_of(capsys.readouterr().out)

    assert summary["accepted_this_run"] == "2"
    assert summary["batch_actions"] == "1"
    assert statuses(database)[:2] == [FactStatus.ACCEPTED, FactStatus.ACCEPTED]


def test_an_index_of_an_already_decided_fact_is_refused(
    silence_logging, database, cv_path, capsys
) -> None:
    """A stale index cannot move a fact a second time."""
    run(cv_path, "r 2", "a 2", "q")
    output = capsys.readouterr().out
    summary = summary_of(output)

    assert grouped_review.UNKNOWN_INDEX_NOTICE in output
    assert summary["accepted_this_run"] == "0"
    assert summary["rejected_this_run"] == "1"
    assert (
        status_of(database, ProfileFactType.PROFESSIONAL_TITLE) is FactStatus.REJECTED
    )


def test_an_accept_never_reaches_a_group_that_was_not_printed(
    silence_logging, database, cv_path, capsys
) -> None:
    """`a` in the second group decides the second group, and nothing else."""
    run(cv_path, "s", "a", "q")
    summary = summary_of(capsys.readouterr().out)

    assert summary["accepted_this_run"] == "1"
    assert status_of(database, ProfileFactType.EDUCATION) is FactStatus.ACCEPTED
    assert statuses(database)[:IDENTITY_SIZE] == [FactStatus.PROPOSED] * IDENTITY_SIZE


def test_a_batch_is_refused_outright_when_the_group_was_not_displayed(
    silence_logging, database, cv_path, capsys
) -> None:
    """The invariant is in the code, not only in the order calls happen in.

    Nothing in the loop can reach `_decide_batch` without a printed group, so
    the guard is exercised directly: a session that has printed nothing decides
    nothing, whatever it is handed.
    """
    run(cv_path, "q")
    capsys.readouterr()
    connection = connect_database(database)
    try:
        session = review_cli._GroupedSession(
            connection=connection, profile_id=1, ask=asker()
        )
        item = review_cli._GroupItem(
            entry=_first_entry(connection), index=1
        )
        decided = session._decide_batch(GroupAction.ACCEPT, [item])
    finally:
        connection.close()
    output = capsys.readouterr().out

    assert decided is False
    assert review_cli.NOT_DISPLAYED_NOTICE in output
    assert session.outcome.accepted == 0
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def _first_entry(connection):
    """One `ImportedCandidate`-shaped stand-in for the first proposed fact."""
    from services.digital_twin.cv.fact_bridge import ImportedCandidate

    fact = list(list_profile_facts(connection, 1))[0]
    return ImportedCandidate(candidate=None, fact=fact, created=False)


# --------------------------------------------------------------------------
# The skills group, which is what this shape of review is for
# --------------------------------------------------------------------------


def test_a_large_skills_group_is_answered_by_a_rejection_and_one_accept(
    silence_logging, database, skills_cv_path, capsys
) -> None:
    """Forty-two skills, three commands, and every one of them was printed."""
    run(skills_cv_path, "s", "r 17,21", "a", review_cli.CONFIRM_ANSWER)
    output = capsys.readouterr().out
    summary = summary_of(output)
    skills = by_type(database)[ProfileFactType.SKILL.value]

    assert len(skills) == SKILL_COUNT
    assert summary["rejected_this_run"] == "2"
    assert summary["accepted_this_run"] == str(SKILL_COUNT - 2)
    assert summary["batch_actions"] == "2"
    assert [fact.status for fact in skills] == [
        FactStatus.REJECTED
        if position in (17, 21)
        else FactStatus.ACCEPTED
        for position in range(1, SKILL_COUNT + 1)
    ]
    # Every skill was on screen before any of them was decided.
    for value in SKILL_VALUES:
        assert f"value: {value}" in output


def test_a_large_acceptance_asks_once_more_and_defaults_to_no(
    silence_logging, database, skills_cv_path, capsys
) -> None:
    asked: list[str] = []
    answers = iter(("s", "a", "", "q"))

    def ask(message: str) -> str:
        asked.append(message)
        return next(answers)

    review_cli.main(
        [
            review_cli.REVIEW_COMMAND,
            str(skills_cv_path),
            "--email",
            TEST_ONLY_EMAIL,
            "--grouped",
        ],
        ask=ask,
    )
    output = capsys.readouterr().out
    summary = summary_of(output)

    assert (
        review_cli.CONFIRM_LARGE_ACCEPT_PROMPT.format(count=SKILL_COUNT) in asked
    )
    assert review_cli.CONFIRMATION_DECLINED_NOTICE in output
    assert summary["accepted_this_run"] == "0"
    assert [fact.status for fact in by_type(database)[ProfileFactType.SKILL.value]] == [
        FactStatus.PROPOSED
    ] * SKILL_COUNT


@pytest.mark.parametrize("answer", ["Y", "yes", "n", " y", "y "])
def test_only_the_exact_confirmation_accepts_a_large_group(
    silence_logging, database, skills_cv_path, capsys, answer
) -> None:
    run(skills_cv_path, "s", "a", answer, "q")
    summary = summary_of(capsys.readouterr().out)

    assert summary["accepted_this_run"] == "0"
    assert summary["remaining_proposed"] == str(SKILL_COUNT + 2)


def test_a_small_group_is_accepted_without_a_second_question(
    silence_logging, database, cv_path, capsys
) -> None:
    """Six facts read in a glance do not deserve a confirmation prompt."""
    run(cv_path, "a", "q")
    output = capsys.readouterr().out

    assert IDENTITY_SIZE <= review_cli.LARGE_ACCEPT_CONFIRMATION_SIZE
    assert "[y/N]" not in output
    assert summary_of(output)["accepted_this_run"] == str(IDENTITY_SIZE)


# --------------------------------------------------------------------------
# Groups: what they are, and in which order
# --------------------------------------------------------------------------


def test_the_groups_are_shown_in_a_fixed_order(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "s", "s", "s", "s", "s", "s", "s")
    output = capsys.readouterr().out

    assert group_headers(output) == [
        f"GROUP {position}/{GROUP_COUNT} — {group.value}"
        for position, group in enumerate(
            (
                FactGroup.IDENTITY_CONTACT,
                FactGroup.EDUCATION,
                FactGroup.EXPERIENCE,
                FactGroup.PROJECTS,
                FactGroup.SKILLS,
                FactGroup.CERTIFICATIONS,
                FactGroup.LANGUAGES,
            ),
            start=1,
        )
    ]
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_every_mapped_fact_type_lands_in_a_group_the_review_walks() -> None:
    for fact_type in ProfileFactType:
        assert group_for_fact_type(fact_type.value) in grouped_review.GROUP_ORDER


def test_a_fact_type_this_version_does_not_know_lands_in_other(
    silence_logging, database, cv_path, capsys
) -> None:
    """A future type is grouped rather than dropped: it must still be reviewed."""
    assert group_for_fact_type("A_TYPE_FROM_A_LATER_VERSION") is FactGroup.OTHER

    run(cv_path, "q")
    capsys.readouterr()
    connection = connect_database(database)
    try:
        entries = [_first_entry(connection)]
    finally:
        connection.close()
    future = dataclasses.replace(
        entries[0],
        fact=dataclasses.replace(
            entries[0].fact, fact_type="A_TYPE_FROM_A_LATER_VERSION"
        ),
    )
    groups = build_review_groups([*entries, future])

    assert [group.group for group in groups] == [
        FactGroup.IDENTITY_CONTACT,
        FactGroup.OTHER,
    ]
    assert sum(len(group) for group in groups) == 2


def test_grouping_keeps_every_entry_and_its_document_order(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "q")
    capsys.readouterr()
    connection = connect_database(database)
    try:
        from services.digital_twin.cv.candidates.extractor import extract_candidates
        from services.digital_twin.cv.fact_bridge import import_cv_candidates
        from services.digital_twin.cv.parser import parse_cv_pdf

        result = import_cv_candidates(
            connection,
            profile_id=1,
            extraction=extract_candidates(parse_cv_pdf(str(cv_path))),
        )
    finally:
        connection.close()
    groups = build_review_groups(result.pending_review)
    regrouped = [entry for group in groups for entry in group.entries]

    assert len(regrouped) == CANDIDATE_COUNT
    assert {entry.fact.id for entry in regrouped} == {
        entry.fact.id for entry in result.pending_review
    }
    skills = next(group for group in groups if group.group is FactGroup.SKILLS)
    assert [entry.fact.value for entry in skills.entries] == [
        "Skill-01",
        "Skill-02",
        "Skill-03",
    ]


# --------------------------------------------------------------------------
# The command grammar, read on its own
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("a", GroupCommand(GroupAction.ACCEPT)),
        ("a 1", GroupCommand(GroupAction.ACCEPT, (1,))),
        ("a 1,3,5", GroupCommand(GroupAction.ACCEPT, (1, 3, 5))),
        ("a 1-5", GroupCommand(GroupAction.ACCEPT, (1, 2, 3, 4, 5))),
        ("a 1,3-5,9", GroupCommand(GroupAction.ACCEPT, (1, 3, 4, 5, 9))),
        ("a 1,1,2", GroupCommand(GroupAction.ACCEPT, (1, 2))),
        ("a 3,1", GroupCommand(GroupAction.ACCEPT, (1, 3))),
        ("a 2-2", GroupCommand(GroupAction.ACCEPT, (2,))),
        ("r 2", GroupCommand(GroupAction.REJECT, (2,))),
        ("r 2,7", GroupCommand(GroupAction.REJECT, (2, 7))),
        ("r 2-5", GroupCommand(GroupAction.REJECT, (2, 3, 4, 5))),
        ("c 4", GroupCommand(GroupAction.CORRECT, (4,))),
        ("s", GroupCommand(GroupAction.SKIP)),
        ("q", GroupCommand(GroupAction.QUIT)),
        ("?", GroupCommand(GroupAction.HELP)),
    ],
)
def test_the_grammar_reads_what_it_documents(typed, expected) -> None:
    assert parse_group_command(typed) == expected


@pytest.mark.parametrize(
    "typed",
    [
        "",
        " ",
        "a ",
        " a",
        "a  1",
        "A",
        "A 1",
        "aa",
        "a1",
        "a banana",
        "a 1--4",
        "a 1-",
        "a -1",
        "a 1,",
        "a ,1",
        "a 3-1",
        "a 01",
        "a +1",
        "a 1 3",
        "r",
        "c",
        "c 1,2",
        "c 1-3",
        "s 1",
        "q 1",
        "? 1",
        "y",
        "accept",
    ],
)
def test_the_grammar_refuses_everything_else(typed) -> None:
    assert parse_group_command(typed) is None


def test_the_grammar_has_no_command_beyond_one_group() -> None:
    """There is no letter for the whole CV, and no flag that adds one."""
    for typed in ("aa", "A", "all", "a all", "a *", "a -", "*"):
        assert parse_group_command(typed) is None
    assert set(GroupAction) == {
        GroupAction.ACCEPT,
        GroupAction.REJECT,
        GroupAction.CORRECT,
        GroupAction.SKIP,
        GroupAction.QUIT,
        GroupAction.HELP,
    }


def test_the_help_says_what_accept_covers(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "?", "q")
    output = capsys.readouterr().out

    assert "accept every fact of THIS group" in output
    assert "There is no command that accepts the whole CV." in output
    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


# --------------------------------------------------------------------------
# Batches are atomic, and the summary and the log say nothing about the person
# --------------------------------------------------------------------------


def test_a_batch_that_fails_partway_leaves_nothing_decided(
    silence_logging, database, cv_path, capsys
) -> None:
    """Either the whole group moves or none of it does."""
    run(cv_path, "q")
    capsys.readouterr()
    ids = [fact.id for fact in facts(database)[:IDENTITY_SIZE]]
    connection = connect_database(database)
    try:
        seen: list[int] = []

        def fail_on_the_third(fact_id: int) -> None:
            seen.append(fact_id)
            if len(seen) == 3:
                raise RuntimeError("TEST ONLY failure partway through the batch")

        with pytest.raises(RuntimeError):
            decide_profile_facts(
                connection,
                1,
                ids,
                FactStatus.ACCEPTED,
                after_fact=fail_on_the_third,
            )
    finally:
        connection.close()

    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_a_batch_naming_one_fact_twice_is_refused(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "q")
    capsys.readouterr()
    first = facts(database)[0].id
    connection = connect_database(database)
    try:
        with pytest.raises(ProfileFactError):
            decide_profile_facts(connection, 1, [first, first], FactStatus.ACCEPTED)
        with pytest.raises(ProfileFactError):
            decide_profile_facts(connection, 1, [first], FactStatus.CORRECTED)
        assert decide_profile_facts(connection, 1, (), FactStatus.ACCEPTED) == ()
    finally:
        connection.close()

    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_a_batch_stops_at_a_fact_of_another_profile_without_deciding_any(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "q")
    capsys.readouterr()
    ids = [fact.id for fact in facts(database)[:3]]
    connection = connect_database(database)
    try:
        with pytest.raises(Exception):
            decide_profile_facts(
                connection, 1, [*ids, 10_000], FactStatus.ACCEPTED
            )
    finally:
        connection.close()

    assert statuses(database) == [FactStatus.PROPOSED] * CANDIDATE_COUNT


def test_the_grouped_summary_counts_the_grouped_actions(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "a 1,3", "r 2", "s", "q")
    summary = summary_of(capsys.readouterr().out)

    assert summary["accepted_this_run"] == "2"
    assert summary["rejected_this_run"] == "1"
    assert summary["corrected_this_run"] == "0"
    assert summary["skipped_this_run"] == str(IDENTITY_SIZE - 3)
    assert summary["batch_actions"] == "2"
    assert summary["groups_shown"] == "2"
    assert summary["groups_completed"] == "0"
    assert summary["quit_requested"] == "true"
    assert summary["remaining_proposed"] == str(CANDIDATE_COUNT - 3)


def test_a_group_answered_in_full_is_counted_as_completed(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "a", "a", "q")
    summary = summary_of(capsys.readouterr().out)

    assert summary["groups_completed"] == "2"
    assert summary["groups_shown"] == "3"


def test_the_closing_summary_repeats_no_value(
    silence_logging, database, cv_path, capsys
) -> None:
    run(cv_path, "a", "q")
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
        "groups_shown",
        "groups_completed",
        "batch_actions",
    }


def test_structured_events_carry_counters_and_never_the_cv(
    database, cv_path, capsys
) -> None:
    """The grouped run logs counters, versions and nothing a CV said."""
    assert run(cv_path, "a", "r 1", "c 1", TEST_ONLY_CORRECTION, "q") == 0
    output = capsys.readouterr().out
    events = [
        json.loads(line)
        for line in output.splitlines()
        if line.startswith("{") and line.endswith("}")
    ]
    rendered = "\n".join(line for line in output.splitlines() if line.startswith("{"))

    succeeded = next(
        event for event in events if event["event"] == "cv_review_command_succeeded"
    )
    assert succeeded["context"] == {
        "database_backend": "sqlite",
        "parser_version": "cv-parser-v5",
        "extractor_version": "cv-candidates-v7",
        "candidates": CANDIDATE_COUNT,
        "newly_proposed": CANDIDATE_COUNT,
        "already_imported": 0,
        "accepted_this_run": IDENTITY_SIZE,
        "rejected_this_run": 1,
        "corrected_this_run": 1,
        "skipped_this_run": 0,
        "remaining_proposed": CANDIDATE_COUNT - IDENTITY_SIZE - 2,
        "quit_requested": True,
        "groups_shown": 4,
        "groups_completed": 3,
        "batch_actions": 2,
    }
    for secret in (*PERSONAL_TEST_CONTENT, TEST_ONLY_CORRECTION):
        assert secret not in rendered
    assert TEST_ONLY_LOCAL_PART not in rendered
    assert str(cv_path) not in rendered
