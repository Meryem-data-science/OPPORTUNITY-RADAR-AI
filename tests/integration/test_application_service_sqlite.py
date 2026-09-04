"""Integration coverage for the operations a person performs on a candidature.

The two properties these tests exist for are idempotence and an honest
history. Everything else — which status may follow which, when the submission
instant is written, what a tracking update leaves alone — is a consequence of
those two being taken seriously.
"""

import sqlite3

import pytest

from services.applications import (
    ApplicationAction,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationRequestError,
    ApplicationStatus,
    OpportunityNotFoundError,
    TrackingUpdate,
    apply_action,
    change_status,
    read_application_detail,
    read_applications,
    update_tracking,
)
from services.applications.repository import preflight
from services.applications.models import ApplicationError
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from tests.integration.test_application_tracking_migration_sqlite import (
    migrations_below,
    opportunity,
    tracking_fixture,
)


def events(connection, application_id):
    return connection.execute(
        "SELECT event_type, from_status, to_status, actor_type"
        " FROM application_events WHERE application_id = ? ORDER BY id",
        (application_id,),
    ).fetchall()


def save(connection, profile_id, opportunity_id):
    return apply_action(
        connection,
        profile_id=profile_id,
        opportunity_id=opportunity_id,
        action=ApplicationAction.SAVE,
    )


def act(connection, profile_id, opportunity_id, action):
    return apply_action(
        connection,
        profile_id=profile_id,
        opportunity_id=opportunity_id,
        action=action,
    )


def test_each_action_creates_the_candidature_it_names(tmp_path):
    connection, profile_id, _, first = tracking_fixture(tmp_path)
    try:
        second = opportunity(connection, "https://boards.example.invalid/jobs/b")
        third = opportunity(connection, "https://boards.example.invalid/jobs/c")
        expected = (
            (first, ApplicationAction.SAVE, ApplicationStatus.SAVED),
            (second, ApplicationAction.PREPARE, ApplicationStatus.PREPARING),
            (third, ApplicationAction.MARK_SUBMITTED, ApplicationStatus.SUBMITTED),
        )
        for opportunity_id, action, status in expected:
            outcome = act(connection, profile_id, opportunity_id, action)
            assert outcome.created and outcome.changed
            assert outcome.application.status is status
            assert outcome.application.last_status_change == outcome.application.created_at
            assert events(connection, outcome.application.id) == [
                ("APPLICATION_CREATED", None, status.value, "USER")
            ]
        submitted = act(
            connection, profile_id, third, ApplicationAction.MARK_SUBMITTED
        ).application
        assert submitted.submitted_at == submitted.created_at
    finally:
        connection.close()


def test_a_candidature_is_never_created_by_anything_but_an_intention(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        # The opportunity exists and is visible. Nothing has been decided about
        # it, so nothing tracks it.
        assert read_applications(connection, profile_id=profile_id) == []
        assert connection.execute(
            "SELECT COUNT(*) FROM applications"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_saving_three_times_leaves_one_candidature_and_one_event(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        first = save(connection, profile_id, opportunity_id)
        second = save(connection, profile_id, opportunity_id)
        third = save(connection, profile_id, opportunity_id)
        assert first.created and first.changed
        assert not second.created and not second.changed
        assert not third.created and not third.changed
        assert second.application == first.application
        assert third.application == first.application
        assert connection.execute("SELECT COUNT(*) FROM applications").fetchone() == (1,)
        assert events(connection, first.application.id) == [
            ("APPLICATION_CREATED", None, "SAVED", "USER")
        ]
    finally:
        connection.close()


def test_preparing_advances_once_and_then_does_nothing(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id)
        prepared = act(connection, profile_id, opportunity_id, ApplicationAction.PREPARE)
        assert prepared.changed and not prepared.created
        assert prepared.application.status is ApplicationStatus.PREPARING
        repeated = act(connection, profile_id, opportunity_id, ApplicationAction.PREPARE)
        assert not repeated.changed
        assert repeated.application == prepared.application
        assert events(connection, created.application.id) == [
            ("APPLICATION_CREATED", None, "SAVED", "USER"),
            ("STATUS_CHANGED", "SAVED", "PREPARING", "USER"),
        ]
    finally:
        connection.close()


def test_marking_submitted_writes_the_instant_exactly_once(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id)
        assert created.application.submitted_at is None
        submitted = act(
            connection, profile_id, opportunity_id, ApplicationAction.MARK_SUBMITTED
        )
        assert submitted.changed
        assert submitted.application.status is ApplicationStatus.SUBMITTED
        assert submitted.application.submitted_at is not None
        assert (
            submitted.application.last_status_change
            == submitted.application.submitted_at
        )
        repeated = act(
            connection, profile_id, opportunity_id, ApplicationAction.MARK_SUBMITTED
        )
        assert not repeated.changed
        assert repeated.application.submitted_at == submitted.application.submitted_at
        assert events(connection, created.application.id) == [
            ("APPLICATION_CREATED", None, "SAVED", "USER"),
            ("STATUS_CHANGED", "SAVED", "SUBMITTED", "USER"),
        ]
    finally:
        connection.close()


def test_the_submission_instant_survives_every_later_movement(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = act(
            connection, profile_id, opportunity_id, ApplicationAction.MARK_SUBMITTED
        ).application
        submitted_at = created.submitted_at
        assert submitted_at is not None
        for status in (
            ApplicationStatus.CONFIRMED,
            ApplicationStatus.ASSESSMENT,
            ApplicationStatus.INTERVIEW,
            ApplicationStatus.OFFER,
        ):
            outcome = change_status(
                connection,
                profile_id=profile_id,
                application_id=created.id,
                status=status,
            )
            assert outcome.changed
            assert outcome.application.submitted_at == submitted_at
    finally:
        connection.close()


def test_an_action_never_walks_a_candidature_backwards(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = act(
            connection, profile_id, opportunity_id, ApplicationAction.MARK_SUBMITTED
        ).application
        for action in (ApplicationAction.SAVE, ApplicationAction.PREPARE):
            outcome = act(connection, profile_id, opportunity_id, action)
            assert not outcome.changed
            assert outcome.application.status is ApplicationStatus.SUBMITTED
        assert len(events(connection, created.id)) == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "terminal",
    [
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
        ApplicationStatus.WITHDRAWN,
    ],
)
def test_a_concluded_candidature_refuses_the_create_actions(tmp_path, terminal):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = act(
            connection, profile_id, opportunity_id, ApplicationAction.MARK_SUBMITTED
        ).application
        change_status(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            status=terminal,
        )
        before = events(connection, created.id)
        for action in ApplicationAction:
            with pytest.raises(ApplicationConflictError):
                act(connection, profile_id, opportunity_id, action)
        assert events(connection, created.id) == before
        assert (
            connection.execute(
                "SELECT status FROM applications WHERE id = ?", (created.id,)
            ).fetchone()[0]
            == terminal.value
        )
    finally:
        connection.close()


def test_an_opportunity_that_does_not_exist_is_never_invented(tmp_path):
    connection, profile_id, _, _ = tracking_fixture(tmp_path)
    try:
        before = connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()
        with pytest.raises(OpportunityNotFoundError):
            save(connection, profile_id, 4242)
        assert connection.execute("SELECT COUNT(*) FROM opportunities").fetchone() == before
        assert connection.execute("SELECT COUNT(*) FROM applications").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "start, target",
    [
        (ApplicationStatus.SAVED, ApplicationStatus.PREPARING),
        (ApplicationStatus.PREPARING, ApplicationStatus.SUBMITTED),
        (ApplicationStatus.SUBMITTED, ApplicationStatus.INTERVIEW),
        (ApplicationStatus.SUBMITTED, ApplicationStatus.REJECTED),
        (ApplicationStatus.SUBMITTED, ApplicationStatus.CONFIRMED),
        (ApplicationStatus.INTERVIEW, ApplicationStatus.OFFER),
        (ApplicationStatus.SAVED, ApplicationStatus.WITHDRAWN),
        (ApplicationStatus.PREPARING, ApplicationStatus.WITHDRAWN),
    ],
)
def test_the_transitions_a_real_search_makes_are_allowed(tmp_path, start, target):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        # Reach `start` the way a real candidature would: a status that only
        # exists after a submission is reached through one.
        path = []
        if start is not ApplicationStatus.SAVED:
            if start not in {ApplicationStatus.PREPARING, ApplicationStatus.SUBMITTED}:
                path.append(ApplicationStatus.SUBMITTED)
            path.append(start)
        for step in path:
            change_status(
                connection,
                profile_id=profile_id,
                application_id=created.id,
                status=step,
            )
        outcome = change_status(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            status=target,
        )
        assert outcome.changed
        assert outcome.application.status is target
        assert events(connection, created.id)[-1] == (
            "STATUS_CHANGED",
            start.value,
            target.value,
            "USER",
        )
    finally:
        connection.close()


def test_setting_the_status_a_candidature_already_has_changes_nothing(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        outcome = change_status(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            status=ApplicationStatus.SAVED,
        )
        assert not outcome.changed
        assert outcome.application == created
        assert len(events(connection, created.id)) == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "refused", [ApplicationStatus.READY, ApplicationStatus.DISCOVERED]
)
def test_ready_and_discovered_cannot_be_set_in_this_phase(tmp_path, refused):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = act(
            connection, profile_id, opportunity_id, ApplicationAction.PREPARE
        ).application
        with pytest.raises(ApplicationRequestError):
            change_status(
                connection,
                profile_id=profile_id,
                application_id=created.id,
                status=refused,
            )
        assert len(events(connection, created.id)) == 1
        assert (
            connection.execute(
                "SELECT status FROM applications WHERE id = ?", (created.id,)
            ).fetchone()[0]
            == "PREPARING"
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "target", [ApplicationStatus.SAVED, ApplicationStatus.PREPARING]
)
def test_a_submitted_candidature_cannot_go_back_to_being_drafted(tmp_path, target):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = act(
            connection, profile_id, opportunity_id, ApplicationAction.MARK_SUBMITTED
        ).application
        change_status(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            status=ApplicationStatus.INTERVIEW,
        )
        with pytest.raises(ApplicationConflictError):
            change_status(
                connection,
                profile_id=profile_id,
                application_id=created.id,
                status=target,
            )
        assert (
            connection.execute(
                "SELECT status, submitted_at FROM applications WHERE id = ?",
                (created.id,),
            ).fetchone()[0]
            == "INTERVIEW"
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "target",
    [
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.ASSESSMENT,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
    ],
)
def test_a_candidature_that_was_never_sent_cannot_be_past_sending(tmp_path, target):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        with pytest.raises(ApplicationConflictError):
            change_status(
                connection,
                profile_id=profile_id,
                application_id=created.id,
                status=target,
            )
        assert len(events(connection, created.id)) == 1
    finally:
        connection.close()


def test_another_profiles_candidature_is_simply_not_there(tmp_path):
    connection, profile_id, other_profile_id, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        with pytest.raises(ApplicationNotFoundError):
            change_status(
                connection,
                profile_id=other_profile_id,
                application_id=created.id,
                status=ApplicationStatus.PREPARING,
            )
        with pytest.raises(ApplicationNotFoundError):
            update_tracking(
                connection,
                profile_id=other_profile_id,
                application_id=created.id,
                update=TrackingUpdate(notes=("theirs",)),
            )
        assert (
            read_application_detail(
                connection,
                profile_id=other_profile_id,
                application_id=created.id,
            )
            is None
        )
        assert read_applications(connection, profile_id=other_profile_id) == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    "update",
    [
        TrackingUpdate(notes=("Relancer le recruteur",)),
        TrackingUpdate(next_action=("Préparer l'entretien",)),
        TrackingUpdate(followup_date=("2026-03-15",)),
    ],
)
def test_every_tracking_field_produces_one_tracking_event(tmp_path, update):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        outcome = update_tracking(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            update=update,
        )
        assert outcome.changed
        assert events(connection, created.id)[-1] == (
            "TRACKING_UPDATED",
            None,
            None,
            "USER",
        )
        # A note is not a movement.
        assert outcome.application.last_status_change == created.last_status_change
        assert outcome.application.status is created.status
        assert outcome.application.submitted_at == created.submitted_at
    finally:
        connection.close()


def test_writing_the_same_tracking_values_again_changes_nothing(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        update = TrackingUpdate(
            notes=("Candidature envoyée via le site",),
            next_action=("Relancer",),
            followup_date=("2026-03-15",),
        )
        first = update_tracking(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            update=update,
        )
        second = update_tracking(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            update=update,
        )
        assert first.changed and not second.changed
        assert second.application == first.application
        assert len(events(connection, created.id)) == 2
    finally:
        connection.close()


def test_a_tracking_field_can_be_cleared_and_an_unmentioned_one_is_left_alone(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        update_tracking(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            update=TrackingUpdate(
                notes=("garder",), next_action=("relancer",), followup_date=("2026-03-15",)
            ),
        )
        cleared = update_tracking(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            update=TrackingUpdate(next_action=(None,)),
        )
        assert cleared.changed
        assert cleared.application.next_action is None
        assert cleared.application.notes == "garder"
        assert cleared.application.followup_date == "2026-03-15"
    finally:
        connection.close()


def test_a_tracking_update_that_names_no_field_is_refused(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        with pytest.raises(ApplicationRequestError):
            update_tracking(
                connection,
                profile_id=profile_id,
                application_id=created.id,
                update=TrackingUpdate(),
            )
        assert len(events(connection, created.id)) == 1
    finally:
        connection.close()


def test_tracking_updates_touch_updated_at_and_a_status_change_touches_both(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        # Timestamps come from SQLite and have one-second resolution, so the
        # test moves the stored ones back rather than waiting a second.
        connection.execute(
            "UPDATE applications SET created_at = ?, updated_at = ?,"
            " last_status_change = ? WHERE id = ?",
            ("2020-01-01 00:00:00",) * 3 + (created.id,),
        )
        connection.commit()
        tracked = update_tracking(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            update=TrackingUpdate(notes=("une note",)),
        ).application
        assert tracked.updated_at > "2020-01-01 00:00:00"
        assert tracked.last_status_change == "2020-01-01 00:00:00"
        moved = change_status(
            connection,
            profile_id=profile_id,
            application_id=created.id,
            status=ApplicationStatus.PREPARING,
        ).application
        assert moved.last_status_change == moved.updated_at
        assert moved.created_at == "2020-01-01 00:00:00"
    finally:
        connection.close()


def test_a_failed_change_leaves_neither_state_nor_event_behind(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        with pytest.raises(ApplicationConflictError):
            change_status(
                connection,
                profile_id=profile_id,
                application_id=created.id,
                status=ApplicationStatus.OFFER,
            )
        assert not connection.in_transaction
        stored = connection.execute(
            "SELECT status, submitted_at, updated_at FROM applications WHERE id = ?",
            (created.id,),
        ).fetchone()
        assert stored == (created.status.value, None, created.updated_at)
        assert len(events(connection, created.id)) == 1
    finally:
        connection.close()


def test_a_database_below_0023_is_refused_before_anything_is_written(tmp_path):
    connection = connect_database(tmp_path / "below.db")
    try:
        apply_migrations(connection, migrations_below(tmp_path, "0023"))
        with pytest.raises(ApplicationError):
            preflight(connection)
        with pytest.raises(ApplicationError):
            save(connection, 1, 1)
        assert not connection.in_transaction
    finally:
        connection.close()


def test_the_read_model_carries_the_real_opportunity_and_its_link(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        created = save(connection, profile_id, opportunity_id).application
        views = read_applications(connection, profile_id=profile_id)
        assert len(views) == 1
        view = views[0]
        assert view.application.id == created.id
        assert view.opportunity.id == opportunity_id
        assert view.opportunity.canonical_title == "Data Engineer"
        assert view.opportunity.organization == "Example Org"
        assert view.opportunity.location == "Paris"
        assert view.opportunity.original_url == "https://boards.example.invalid/jobs/1"
        detail = read_application_detail(
            connection, profile_id=profile_id, application_id=created.id
        )
        assert detail is not None
        assert detail.opportunity == view.opportunity
        assert [event.event_type.value for event in detail.events] == [
            "APPLICATION_CREATED"
        ]
    finally:
        connection.close()


def test_the_read_model_prefers_the_official_observation_link(tmp_path):
    connection, profile_id, _, opportunity_id = tracking_fixture(tmp_path)
    try:
        connection.execute(
            "INSERT INTO sources (id, type, status) VALUES ('board', 'greenhouse', 'active')"
        )
        connection.execute(
            """INSERT INTO opportunity_sources
               (opportunity_id, source_id, source_url, application_url, discovered_at)
               VALUES (?, 'board', ?, ?, '2026-02-01 09:00:00')""",
            (
                opportunity_id,
                "https://boards.example.invalid/jobs/1",
                "https://careers.example.invalid/apply/1",
            ),
        )
        connection.commit()
        save(connection, profile_id, opportunity_id)
        view = read_applications(connection, profile_id=profile_id)[0]
        assert view.opportunity.original_url == "https://careers.example.invalid/apply/1"
    finally:
        connection.close()


def test_the_list_is_ordered_by_the_most_recent_movement(tmp_path):
    connection, profile_id, _, first = tracking_fixture(tmp_path)
    try:
        second = opportunity(connection, "https://boards.example.invalid/jobs/b")
        older = save(connection, profile_id, first).application
        newer = save(connection, profile_id, second).application
        connection.execute(
            "UPDATE applications SET last_status_change = ? WHERE id = ?",
            ("2020-01-01 00:00:00", older.id),
        )
        connection.commit()
        ordered = [
            view.application.id
            for view in read_applications(connection, profile_id=profile_id)
        ]
        assert ordered == [newer.id, older.id]
    finally:
        connection.close()
