"""Integration tests for the Phase 5.3B notification sync over real SQLite."""

from dataclasses import replace
import json
import socket
import sys

import pytest

from services.collector.matching import (
    MATCHING_SELECTION_VERSION,
    MatchingBatchResult,
    matching_batch_fingerprint,
    store_matching_batch,
)
from services.collector.qualification.persistence import persist_qualifications
from services.eligibility import ELIGIBILITY_ENGINE_VERSION
from services.notifications import (
    NOTIFICATION_POLICY_VERSION,
    NOTIFICATION_TARGET_PATH,
    NotificationSyncError,
    NotificationSyncStatus,
    sync_notification_policy,
)
from services.portfolio import (
    PortfolioBucket,
    PortfolioDisposition,
    assemble_portfolio_inputs,
    build_portfolio_assessments,
)
from services.priority import PriorityCategory, sync_priority
from tests.integration.test_portfolio_persistence_sqlite import (
    persist,
    portfolio_fixture,
    refingerprint,
)
from tests.integration.test_priority_dry_run_sqlite import _batch
from tests.integration.test_priority_persistence_sqlite import DAY

URGENT, HIGH, MEDIUM, LOW = (
    PriorityCategory.URGENT,
    PriorityCategory.HIGH,
    PriorityCategory.MEDIUM,
    PriorityCategory.LOW,
)


def included(bucket=PortfolioBucket.TARGET, priority=MEDIUM):
    return dict(
        disposition=PortfolioDisposition.INCLUDED,
        bucket=bucket,
        priority_category=priority,
    )


def excluded(priority=LOW):
    return dict(
        disposition=PortfolioDisposition.EXCLUDED,
        bucket=None,
        priority_category=priority,
    )


def store_run(connection, profile_id, overrides):
    """Persist one Portfolio run whose positions the test states outright."""
    assembly = assemble_portfolio_inputs(connection, profile_id)
    assessments = tuple(
        refingerprint(item, **overrides[item.opportunity_id])
        if item.opportunity_id in overrides
        else item
        for item in build_portfolio_assessments(assembly.inputs)
    )
    return persist(
        connection,
        dict(
            profile_id=profile_id,
            priority_run_id=assembly.priority_run_id,
            priority_run_fingerprint=assembly.priority_run_fingerprint,
            matching_run_id=assembly.matching_run_id,
            matching_run_fingerprint=assembly.matching_run_fingerprint,
            assessments=assessments,
        ),
    )


def fixture(tmp_path, opportunities=3):
    connection, identity, opportunity_ids, _ = portfolio_fixture(
        tmp_path, opportunities
    )
    return connection, identity, opportunity_ids


def events(connection):
    """Every persisted event with the outbox row that must accompany it."""
    return connection.execute(
        """SELECT notification_events.event_type,notification_events.opportunity_id,
        notification_events.previous_portfolio_run_id,
        notification_events.portfolio_run_id,notification_events.policy_version,
        notification_events.payload_json,notification_outbox.id
        FROM notification_events JOIN notification_outbox
        ON notification_outbox.event_id=notification_events.id
        ORDER BY notification_events.id"""
    ).fetchall()


def announced(connection):
    return [(row[0], row[1]) for row in events(connection)]


def state(connection):
    return connection.execute(
        """SELECT profile_id,baseline_portfolio_run_id,last_processed_portfolio_run_id,
        policy_version FROM notification_policy_state ORDER BY profile_id"""
    ).fetchall()


def snapshot(connection):
    return (
        connection.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0],
        state(connection),
    )


def baseline(connection, identity, overrides):
    """Store the first Portfolio run and let it become the silent baseline."""
    first = store_run(connection, identity.profile_id, overrides)
    result = sync_notification_policy(connection, identity.profile_id)
    assert result.status is NotificationSyncStatus.BASELINE_INITIALIZED
    return first


def add_opportunity(connection, identity, number):
    opportunity_id = connection.execute(
        """INSERT INTO opportunities
        (canonical_title,organization,published_at,deadline,discovered_at,
         first_seen_at,last_seen_at,source_url,status)
        VALUES (?,?,NULL,NULL,'2099-01-01','2099-01-01','2099-01-01',?,'new')
        RETURNING id""",
        (f"Data Engineer {number}", "Org", f"https://example.invalid/{number}"),
    ).fetchone()[0]
    connection.commit()
    persist_qualifications(connection)
    connection.execute(
        """INSERT INTO opportunity_eligibilities
        (user_id,opportunity_id,status,engine_version,input_fingerprint,satisfied_count,
         violated_count,unknown_count,not_applicable_count,not_evaluated_count,
         blocking_unknown_count,evaluated_at) VALUES (?,?,?,?,?,0,0,1,0,0,1,?)""",
        (
            identity.user_id,
            opportunity_id,
            "UNKNOWN",
            ELIGIBILITY_ENGINE_VERSION,
            f"{opportunity_id:x}".zfill(64),
            "2026-09-01",
        ),
    )
    connection.commit()
    return opportunity_id


def rebuild_upstream(connection, identity, opportunity_ids):
    """Re-run Matching and Priority over a changed cohort, as the pipeline does."""
    items = tuple(
        _batch(identity.profile_id, opportunity_id).assessments[0]
        for opportunity_id in opportunity_ids
    )
    batch = MatchingBatchResult(items, "a" * 64, "b" * 64, len(items))
    batch = replace(batch, batch_fingerprint=matching_batch_fingerprint(batch))
    store_matching_batch(
        connection,
        identity.profile_id,
        batch,
        selection_version=MATCHING_SELECTION_VERSION,
    )
    sync_priority(connection, identity.profile_id, DAY)


def test_first_activation_makes_the_current_snapshot_a_silent_baseline(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    first = store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(priority=URGENT), ids[1]: included(), ids[2]: excluded()},
    )
    result = sync_notification_policy(connection, identity.profile_id)

    assert result.status is NotificationSyncStatus.BASELINE_INITIALIZED
    assert (result.event_count, result.outbox_count) == (0, 0)
    assert result.baseline_portfolio_run_id == first.run_id
    assert result.last_processed_portfolio_run_id == first.run_id
    assert result.processed_run_ids == () and result.state_changed is True
    assert result.policy_version == NOTIFICATION_POLICY_VERSION
    # Nothing already in the Portfolio is announced: no backfill, no spam.
    assert snapshot(connection) == (
        0,
        0,
        [
            (
                identity.profile_id,
                first.run_id,
                first.run_id,
                NOTIFICATION_POLICY_VERSION,
            )
        ],
    )


def test_a_repeated_sync_without_a_new_portfolio_run_is_a_byte_noop(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: excluded(), ids[1]: included()})
    dump = tuple(connection.iterdump())
    first = sync_notification_policy(connection, identity.profile_id)
    second = sync_notification_policy(connection, identity.profile_id)

    for result in (first, second):
        assert result.status is NotificationSyncStatus.UP_TO_DATE
        assert (result.event_count, result.outbox_count) == (0, 0)
        assert result.state_changed is False and result.processed_run_ids == ()
    assert tuple(connection.iterdump()) == dump
    assert connection.in_transaction is False


def test_a_repeated_sync_after_real_events_is_also_a_byte_noop(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: excluded(), ids[1]: included()})
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    processed = sync_notification_policy(connection, identity.profile_id)
    dump = tuple(connection.iterdump())
    again = sync_notification_policy(connection, identity.profile_id)

    assert processed.event_count == 1 and processed.outbox_count == 1
    assert again.status is NotificationSyncStatus.UP_TO_DATE
    assert (again.event_count, again.outbox_count, again.duplicate_count) == (0, 0, 0)
    assert again.state_changed is False
    assert tuple(connection.iterdump()) == dump


def test_excluded_becoming_included_is_one_new_actionable_event(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    first = baseline(connection, identity, {ids[0]: excluded(), ids[1]: excluded()})
    second = store_run(
        connection, identity.profile_id, {ids[0]: included(), ids[1]: excluded()}
    )
    result = sync_notification_policy(connection, identity.profile_id)

    assert result.status is NotificationSyncStatus.PROCESSED
    assert result.processed_run_ids == (second.run_id,)
    assert (result.event_count, result.outbox_count) == (1, 1)
    assert result.last_processed_portfolio_run_id == second.run_id
    row = events(connection)[0]
    assert row[:5] == (
        "NEW_ACTIONABLE_OPPORTUNITY",
        ids[0],
        first.run_id,
        second.run_id,
        NOTIFICATION_POLICY_VERSION,
    )


def test_an_opportunity_absent_from_the_previous_run_is_a_new_actionable_event(
    tmp_path,
):
    connection, identity, ids = fixture(tmp_path, opportunities=2)
    baseline(connection, identity, {ids[0]: included(), ids[1]: included()})
    added = add_opportunity(connection, identity, 99)
    rebuild_upstream(connection, identity, [*ids, added])
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(), added: included(priority=HIGH)},
    )
    result = sync_notification_policy(connection, identity.profile_id)

    assert result.event_count == 1
    assert announced(connection) == [("NEW_ACTIONABLE_OPPORTUNITY", added)]


@pytest.mark.parametrize(
    "before,after", [(MEDIUM, HIGH), (MEDIUM, URGENT), (HIGH, URGENT)]
)
def test_each_declared_escalation_of_an_actionable_opportunity_is_announced(
    tmp_path, before, after
):
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: included(priority=before)})
    store_run(connection, identity.profile_id, {ids[0]: included(priority=after)})
    result = sync_notification_policy(connection, identity.profile_id)

    assert (result.event_count, result.outbox_count) == (1, 1)
    assert announced(connection) == [("ATTENTION_ESCALATED", ids[0])]
    payload = json.loads(events(connection)[0][5])
    assert payload["transition"]["previous"]["priority_category"] == before.value
    assert payload["portfolio"]["priority_category"] == after.value


def test_becoming_actionable_and_escalating_at_once_keeps_only_the_new_event(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: excluded(priority=MEDIUM)})
    store_run(connection, identity.profile_id, {ids[0]: included(priority=URGENT)})
    result = sync_notification_policy(connection, identity.profile_id)

    assert (result.event_count, result.outbox_count) == (1, 1)
    assert announced(connection) == [("NEW_ACTIONABLE_OPPORTUNITY", ids[0])]


@pytest.mark.parametrize(
    "overrides",
    [
        {"first": included(priority=URGENT), "second": included(priority=MEDIUM)},
        {"first": included(priority=HIGH), "second": included(priority=LOW)},
        {
            "first": included(bucket=PortfolioBucket.AMBITIOUS),
            "second": included(bucket=PortfolioBucket.SAFE),
        },
        {"first": included(), "second": excluded()},
        {"first": excluded(priority=LOW), "second": excluded(priority=URGENT)},
    ],
)
def test_downgrades_bucket_moves_and_exclusions_announce_nothing(tmp_path, overrides):
    connection, identity, ids = fixture(tmp_path)
    first = baseline(connection, identity, {ids[0]: overrides["first"]})
    second = store_run(connection, identity.profile_id, {ids[0]: overrides["second"]})
    result = sync_notification_policy(connection, identity.profile_id)

    assert second.run_id != first.run_id
    assert result.status is NotificationSyncStatus.PROCESSED
    assert (result.event_count, result.outbox_count) == (0, 0)
    assert announced(connection) == []
    # The cursor still advances: the runs were processed, they just said nothing.
    assert result.last_processed_portfolio_run_id == second.run_id


def test_an_unchanged_portfolio_position_announces_nothing(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: included(), ids[1]: included()})
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(), ids[2]: excluded()},
    )
    result = sync_notification_policy(connection, identity.profile_id)

    assert (result.event_count, result.outbox_count) == (0, 0)
    assert announced(connection) == []


def test_every_unprocessed_run_is_replayed_in_order_not_only_the_latest(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    first = baseline(
        connection,
        identity,
        {ids[0]: excluded(), ids[1]: included(priority=MEDIUM), ids[2]: excluded()},
    )
    second = store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(priority=MEDIUM), ids[2]: excluded()},
    )
    third = store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(priority=URGENT), ids[2]: excluded()},
    )
    result = sync_notification_policy(connection, identity.profile_id)

    assert result.processed_run_ids == (second.run_id, third.run_id)
    assert (result.event_count, result.outbox_count) == (2, 2)
    rows = events(connection)
    assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
        ("NEW_ACTIONABLE_OPPORTUNITY", ids[0], first.run_id, second.run_id),
        ("ATTENTION_ESCALATED", ids[1], second.run_id, third.run_id),
    ]
    assert result.last_processed_portfolio_run_id == third.run_id


def test_an_intermediate_run_is_never_skipped_by_comparing_only_the_endpoints(
    tmp_path,
):
    """Cursor and current agree on the position; the run in between does not."""
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: excluded(), ids[1]: excluded()})
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: excluded()})
    store_run(connection, identity.profile_id, {ids[0]: excluded(), ids[1]: included()})
    result = sync_notification_policy(connection, identity.profile_id)

    assert result.event_count == 2
    assert announced(connection) == [
        ("NEW_ACTIONABLE_OPPORTUNITY", ids[0]),
        ("NEW_ACTIONABLE_OPPORTUNITY", ids[1]),
    ]


def test_payload_carries_only_persisted_authorities_and_the_transition(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: excluded(priority=LOW)})
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(bucket=PortfolioBucket.SAFE, priority=HIGH)},
    )
    sync_notification_policy(connection, identity.profile_id)

    raw = events(connection)[0][5]
    payload = json.loads(raw)
    opportunity = connection.execute(
        "SELECT canonical_title,organization,source_url FROM opportunities WHERE id=?",
        (ids[0],),
    ).fetchone()
    assert raw == raw.strip()
    assert payload["policy_version"] == NOTIFICATION_POLICY_VERSION
    assert payload["opportunity"] == {
        "opportunity_id": ids[0],
        "title": opportunity[0],
        "organization": opportunity[1],
        "original_url": opportunity[2],
        "target_url": NOTIFICATION_TARGET_PATH,
    }
    assert payload["portfolio"]["bucket"] == "SAFE"
    assert payload["portfolio"]["priority_category"] == "HIGH"
    assert payload["transition"] == {
        "previous": {
            "disposition": "EXCLUDED",
            "bucket": None,
            "priority_category": "LOW",
        },
        "current": {
            "disposition": "INCLUDED",
            "bucket": "SAFE",
            "priority_category": "HIGH",
        },
    }
    persisted = connection.execute(
        """SELECT assessment_payload_json FROM portfolio_assessments
        WHERE opportunity_id=? ORDER BY id DESC LIMIT 1""",
        (ids[0],),
    ).fetchone()[0]
    assert (
        payload["portfolio"]["reason_codes"]
        == (json.loads(persisted)["result"]["reason_codes"])
    )


def test_a_corrupt_run_inside_the_chain_rolls_back_and_leaves_the_cursor(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    first = baseline(connection, identity, {ids[0]: excluded(), ids[1]: excluded()})
    middle = store_run(
        connection, identity.profile_id, {ids[0]: included(), ids[1]: excluded()}
    )
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    connection.execute(
        "UPDATE portfolio_runs SET run_payload_json=? WHERE id=?",
        ('{"tampered":true}', middle.run_id),
    )
    connection.commit()
    before = snapshot(connection)

    with pytest.raises(NotificationSyncError, match="corrupt"):
        sync_notification_policy(connection, identity.profile_id)

    assert (
        snapshot(connection)
        == before
        == (
            0,
            0,
            [
                (
                    identity.profile_id,
                    first.run_id,
                    first.run_id,
                    NOTIFICATION_POLICY_VERSION,
                )
            ],
        )
    )
    assert connection.in_transaction is False


def test_a_corrupt_current_portfolio_state_is_refused_before_anything_is_derived(
    tmp_path,
):
    connection, identity, ids = fixture(tmp_path)
    first = baseline(connection, identity, {ids[0]: excluded()})
    current = store_run(connection, identity.profile_id, {ids[0]: included()})
    connection.execute(
        "UPDATE portfolio_assessments SET assessment_fingerprint=? WHERE run_id=?"
        " AND opportunity_id=?",
        ("f" * 64, current.run_id, ids[0]),
    )
    connection.commit()
    before = snapshot(connection)

    with pytest.raises(NotificationSyncError, match="corrupt"):
        sync_notification_policy(connection, identity.profile_id)

    assert snapshot(connection) == before
    assert before[2] == [
        (identity.profile_id, first.run_id, first.run_id, NOTIFICATION_POLICY_VERSION)
    ]


def test_a_failure_after_the_events_are_written_rolls_the_whole_sync_back(
    tmp_path, monkeypatch
):
    connection, identity, ids = fixture(tmp_path)
    first = baseline(connection, identity, {ids[0]: excluded(), ids[1]: excluded()})
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    before = snapshot(connection)
    module = sys.modules["services.notifications.sync"]
    real_store = module.store_notification_events

    def store_then_fail(handle, records):
        stored = real_store(handle, records)
        assert stored.created_count == 2
        assert (
            handle.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0]
            == 2
        )
        raise RuntimeError("persistence failed after the events were written")

    monkeypatch.setattr(module, "store_notification_events", store_then_fail)
    with pytest.raises(RuntimeError, match="persistence failed"):
        sync_notification_policy(connection, identity.profile_id)

    assert snapshot(connection) == before
    assert before[2][0][2] == first.run_id
    assert connection.in_transaction is False


def test_sync_refuses_a_profile_without_a_portfolio_and_a_broken_schema(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    dump = tuple(connection.iterdump())
    result = sync_notification_policy(connection, identity.profile_id)

    assert result.status is NotificationSyncStatus.NOT_SYNCED
    assert (result.event_count, result.outbox_count) == (0, 0)
    assert result.state_changed is False and result.baseline_portfolio_run_id is None
    assert tuple(connection.iterdump()) == dump

    store_run(connection, identity.profile_id, {ids[0]: included()})
    connection.execute("DELETE FROM schema_migrations WHERE version='0019'")
    connection.commit()
    with pytest.raises(NotificationSyncError, match="schema is not ready"):
        sync_notification_policy(connection, identity.profile_id)
    assert connection.in_transaction is False


def test_sync_rejects_an_invalid_profile_and_an_open_transaction(tmp_path):
    connection, identity, _ = fixture(tmp_path)
    for profile_id in (0, -1, True, "1"):
        with pytest.raises(NotificationSyncError, match="positive integer"):
            sync_notification_policy(connection, profile_id)
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(NotificationSyncError, match="active transaction"):
        sync_notification_policy(connection, identity.profile_id)
    connection.execute("ROLLBACK")


def test_sync_never_opens_a_socket_and_never_touches_push_subscriptions(
    tmp_path, monkeypatch
):
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: excluded()})
    store_run(connection, identity.profile_id, {ids[0]: included(priority=URGENT)})

    def refuse(*arguments, **keywords):
        raise AssertionError("notification sync must not reach the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    result = sync_notification_policy(connection, identity.profile_id)

    assert result.event_count == 1
    assert "pywebpush" not in sys.modules
    assert (
        connection.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] == 0
    )


def test_notifications_never_write_to_portfolio_priority_or_matching(tmp_path):
    connection, identity, ids = fixture(tmp_path)
    baseline(connection, identity, {ids[0]: excluded(), ids[1]: included()})
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(priority=URGENT), ids[1]: included(priority=URGENT)},
    )
    upstream = {
        table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        for table in (
            "portfolio_runs",
            "portfolio_assessments",
            "priority_runs",
            "priority_assessments",
            "matching_runs",
            "matching_assessments",
            "opportunities",
        )
    }
    pointers = connection.execute(
        "SELECT * FROM portfolio_profile_state ORDER BY profile_id"
    ).fetchall()
    result = sync_notification_policy(connection, identity.profile_id)

    assert result.event_count == 2
    assert {
        table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        for table in upstream
    } == upstream
    assert (
        connection.execute(
            "SELECT * FROM portfolio_profile_state ORDER BY profile_id"
        ).fetchall()
        == pointers
    )


def test_a_portfolio_that_returns_to_an_earlier_snapshot_is_processed_once(tmp_path):
    """A→B→A→B: the movement is announced once, and the cursor still tracks it.

    Portfolio runs are reused by semantic fingerprint, so coming back to an
    earlier snapshot points the profile at a *lower* run id. The chain has to
    cope with that, and the event fingerprint has to recognise the repeat.
    """
    connection, identity, ids = fixture(tmp_path)
    first = baseline(connection, identity, {ids[0]: excluded()})
    second = store_run(connection, identity.profile_id, {ids[0]: included()})
    announced_once = sync_notification_policy(connection, identity.profile_id)

    back = store_run(connection, identity.profile_id, {ids[0]: excluded()})
    went_back = sync_notification_policy(connection, identity.profile_id)

    forward = store_run(connection, identity.profile_id, {ids[0]: included()})
    again = sync_notification_policy(connection, identity.profile_id)

    assert (back.run_id, forward.run_id) == (first.run_id, second.run_id)
    assert (back.created, forward.created) == (False, False)
    assert announced_once.event_count == 1
    assert went_back.processed_run_ids == (first.run_id,)
    assert (went_back.event_count, went_back.duplicate_count) == (0, 0)
    assert again.processed_run_ids == (second.run_id,)
    assert (again.event_count, again.outbox_count) == (0, 0)
    assert again.duplicate_count == 1
    assert announced(connection) == [("NEW_ACTIONABLE_OPPORTUNITY", ids[0])]
    assert again.last_processed_portfolio_run_id == second.run_id
