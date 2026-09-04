"""Unit coverage for the rules a candidature obeys, with no database in sight.

Everything asserted here is a statement about the domain's vocabulary rather
than about storage: which statuses a person may set today, which intention
advances which state, where the submission boundary lies, and how a free-text
tracking field is read.
"""

import pytest

from services.applications import (
    ACTION_STATUS,
    MAX_NEXT_ACTION_LENGTH,
    MAX_NOTES_LENGTH,
    POST_SUBMISSION_STATUSES,
    PRE_SUBMISSION_STATUSES,
    PUBLIC_STATUSES,
    ApplicationAction,
    ApplicationRequestError,
    ApplicationStatus,
    TrackingUpdate,
    action_advances,
    is_public_status,
    normalize_followup_date,
    normalize_text,
    submission_guard,
)


def test_the_official_vocabulary_is_exactly_the_eleven_statuses():
    assert {status.value for status in ApplicationStatus} == {
        "DISCOVERED",
        "SAVED",
        "PREPARING",
        "READY",
        "SUBMITTED",
        "CONFIRMED",
        "ASSESSMENT",
        "INTERVIEW",
        "REJECTED",
        "OFFER",
        "WITHDRAWN",
    }


def test_every_status_is_before_or_after_submission_and_withdrawn_is_neither():
    accounted = PRE_SUBMISSION_STATUSES | POST_SUBMISSION_STATUSES
    assert not (PRE_SUBMISSION_STATUSES & POST_SUBMISSION_STATUSES)
    assert set(ApplicationStatus) - accounted == {ApplicationStatus.WITHDRAWN}


def test_ready_and_discovered_are_the_two_statuses_this_phase_withholds():
    assert set(ApplicationStatus) - PUBLIC_STATUSES == {
        ApplicationStatus.READY,
        ApplicationStatus.DISCOVERED,
    }
    assert not is_public_status(ApplicationStatus.READY)
    assert not is_public_status(ApplicationStatus.DISCOVERED)
    for status in PUBLIC_STATUSES:
        assert is_public_status(status)


def test_the_three_actions_map_onto_the_three_statuses_they_name():
    assert ACTION_STATUS == {
        ApplicationAction.SAVE: ApplicationStatus.SAVED,
        ApplicationAction.PREPARE: ApplicationStatus.PREPARING,
        ApplicationAction.MARK_SUBMITTED: ApplicationStatus.SUBMITTED,
    }
    assert {action.value for action in ApplicationAction} == {
        "SAVE",
        "PREPARE",
        "MARK_SUBMITTED",
    }


@pytest.mark.parametrize(
    "action, status, advances",
    [
        (ApplicationAction.SAVE, ApplicationStatus.DISCOVERED, True),
        (ApplicationAction.SAVE, ApplicationStatus.SAVED, False),
        (ApplicationAction.SAVE, ApplicationStatus.PREPARING, False),
        (ApplicationAction.SAVE, ApplicationStatus.SUBMITTED, False),
        (ApplicationAction.SAVE, ApplicationStatus.INTERVIEW, False),
        (ApplicationAction.PREPARE, ApplicationStatus.SAVED, True),
        (ApplicationAction.PREPARE, ApplicationStatus.PREPARING, False),
        (ApplicationAction.PREPARE, ApplicationStatus.SUBMITTED, False),
        (ApplicationAction.MARK_SUBMITTED, ApplicationStatus.SAVED, True),
        (ApplicationAction.MARK_SUBMITTED, ApplicationStatus.PREPARING, True),
        (ApplicationAction.MARK_SUBMITTED, ApplicationStatus.SUBMITTED, False),
        (ApplicationAction.MARK_SUBMITTED, ApplicationStatus.OFFER, False),
    ],
)
def test_an_action_only_advances_a_candidature_it_reaches_past(
    action, status, advances
):
    assert action_advances(action, status) is advances


@pytest.mark.parametrize(
    "status, submitted, allowed",
    [
        (ApplicationStatus.SAVED, False, True),
        (ApplicationStatus.PREPARING, False, True),
        (ApplicationStatus.SUBMITTED, False, True),
        (ApplicationStatus.WITHDRAWN, False, True),
        (ApplicationStatus.INTERVIEW, False, False),
        (ApplicationStatus.OFFER, False, False),
        (ApplicationStatus.REJECTED, False, False),
        (ApplicationStatus.SAVED, True, False),
        (ApplicationStatus.PREPARING, True, False),
        (ApplicationStatus.SUBMITTED, True, True),
        (ApplicationStatus.INTERVIEW, True, True),
        (ApplicationStatus.OFFER, True, True),
        (ApplicationStatus.WITHDRAWN, True, True),
    ],
)
def test_the_only_guard_is_the_submission_boundary(status, submitted, allowed):
    assert submission_guard(status, submitted=submitted) is allowed


def test_no_hiring_state_machine_was_invented():
    # Every ordering a real search actually produces stays possible. This test
    # exists to fail loudly if somebody later adds a transition graph.
    for target in POST_SUBMISSION_STATUSES:
        for other in POST_SUBMISSION_STATUSES:
            assert submission_guard(target, submitted=True)
            assert submission_guard(other, submitted=True)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("\n\t", None),
        (" relancer ", "relancer"),
        ("relancer", "relancer"),
        ("a" * MAX_NEXT_ACTION_LENGTH, "a" * MAX_NEXT_ACTION_LENGTH),
    ],
)
def test_a_free_text_field_is_trimmed_and_an_empty_one_clears(value, expected):
    assert normalize_text(value, "next_action", MAX_NEXT_ACTION_LENGTH) == expected


@pytest.mark.parametrize(
    "value, maximum",
    [
        ("a" * (MAX_NEXT_ACTION_LENGTH + 1), MAX_NEXT_ACTION_LENGTH),
        ("n" * (MAX_NOTES_LENGTH + 1), MAX_NOTES_LENGTH),
        (7, MAX_NOTES_LENGTH),
        (["note"], MAX_NOTES_LENGTH),
        (True, MAX_NOTES_LENGTH),
    ],
)
def test_a_free_text_field_refuses_what_it_cannot_store(value, maximum):
    with pytest.raises(ApplicationRequestError):
        normalize_text(value, "notes", maximum)


def test_a_rejected_note_is_never_quoted_back():
    secret = "n" * (MAX_NOTES_LENGTH + 1)
    with pytest.raises(ApplicationRequestError) as refusal:
        normalize_text(secret, "notes", MAX_NOTES_LENGTH)
    assert secret not in str(refusal.value)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("2026-04-01", "2026-04-01"),
        (" 2026-04-01 ", "2026-04-01"),
        ("2024-02-29", "2024-02-29"),
    ],
)
def test_a_followup_date_is_read_as_an_iso_day(value, expected):
    assert normalize_followup_date(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "01/04/2026",
        "2026-4-1",
        "20260401",
        "2026-13-01",
        "2026-04-31",
        "2023-02-29",
        "2026-04-01T09:00:00",
        20260401,
        True,
    ],
)
def test_a_followup_date_that_is_not_a_real_day_is_refused(value):
    with pytest.raises(ApplicationRequestError):
        normalize_followup_date(value)


def test_a_tracking_update_tells_absent_apart_from_cleared():
    assert TrackingUpdate().empty
    assert not TrackingUpdate(notes=(None,)).empty
    assert TrackingUpdate(notes=(None,)).next_action is None
    assert TrackingUpdate(notes=("x",)).notes == ("x",)
