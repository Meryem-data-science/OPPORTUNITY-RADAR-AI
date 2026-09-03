"""Integration tests for the Phase 5.4A Gmail digest over real SQLite.

Every test here runs against a genuinely migrated database holding a real
audited Portfolio snapshot: the digest's whole promise is that it repeats what
the Portfolio persisted, so nothing about the Portfolio is stubbed out.
"""

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from services.collector.database.connection import connect_database
from services.digital_twin.repository import ensure_user_profile
from services.gmail_digest import (
    DIGEST_VERSION,
    UNKNOWN_LOCATION_LABEL,
    DigestMaterializationStatus,
    GmailDigestError,
    build_digest_candidate,
    canonical_digest_payload,
    digest_cli,
    materialize_daily_digest,
    read_digest_status,
    recipient_fingerprint,
)
from services.portfolio import PortfolioBucket
from services.priority import PriorityCategory
from tests.integration.test_notification_policy_sync_sqlite import (
    excluded,
    fixture,
    included,
    store_run,
)

RECIPIENT = "digest-recipient@example.invalid"
OTHER_RECIPIENT = "digest-other@example.invalid"
TIMEZONE = "Africa/Casablanca"
DAY_ONE = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)
TARGET, SAFE, AMBITIOUS = (
    PortfolioBucket.TARGET,
    PortfolioBucket.SAFE,
    PortfolioBucket.AMBITIOUS,
)
URGENT, HIGH, MEDIUM, LOW = (
    PriorityCategory.URGENT,
    PriorityCategory.HIGH,
    PriorityCategory.MEDIUM,
    PriorityCategory.LOW,
)


def digest_fixture(tmp_path, opportunities=3):
    """A migrated database with a profile, opportunities and no digest yet."""
    connection, identity, opportunity_ids = fixture(tmp_path, opportunities)
    return connection, identity, opportunity_ids


def database_path(connection):
    return connection.execute("PRAGMA database_list").fetchone()[2]


def file_digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def digest_bytes(connection):
    connection.commit()
    return file_digest(database_path(connection))


def rows(connection):
    return connection.execute(
        """SELECT id,profile_id,digest_date,timezone,digest_version,content_fingerprint,
        recipient_fingerprint,portfolio_run_id,item_count,status,attempt_count,subject
        FROM gmail_digest_outbox ORDER BY id"""
    ).fetchall()


def mark_sent(connection, outbox_id, moment="2026-09-03 12:00:00"):
    """Do to one row exactly what Phase 5.4B's delivery will do to it."""
    connection.execute(
        """UPDATE gmail_digest_outbox SET status='SENT',next_attempt_at=NULL,
        sent_at=?,gmail_message_id='18f0a1b2c3d4e5f6',attempt_count=1,last_attempt_at=?
        WHERE id=?""",
        (moment, moment, outbox_id),
    )
    connection.commit()


def set_opportunity(connection, opportunity_id, **columns):
    assignments = ",".join(f"{name}=?" for name in columns)
    connection.execute(
        f"UPDATE opportunities SET {assignments} WHERE id=?",
        (*columns.values(), opportunity_id),
    )
    connection.commit()


def observe_from_greenhouse(connection, opportunity_id, application_url):
    connection.execute(
        """INSERT OR IGNORE INTO sources (id,type,status)
        VALUES ('gh','greenhouse','active')"""
    )
    connection.execute(
        """INSERT INTO opportunity_sources
        (opportunity_id,source_id,source_url,application_url,discovered_at)
        VALUES (?,'gh',?,?,'2026-09-01')""",
        (opportunity_id, "https://board.example.invalid/listing", application_url),
    )
    connection.commit()


def materialize(connection, profile_id, *, now=DAY_ONE, recipient=RECIPIENT, **extra):
    return materialize_daily_digest(
        connection,
        profile_id,
        timezone=extra.pop("timezone", TIMEZONE),
        recipient=recipient,
        now=now,
        **extra,
    )


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def test_only_included_opportunities_reach_the_digest(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path)
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: excluded(), ids[2]: included(SAFE, HIGH)},
    )

    candidate = build_digest_candidate(connection, identity.profile_id)

    assert [item.opportunity_id for item in candidate.content.items] == [ids[0], ids[2]]
    excluded_url = connection.execute(
        "SELECT source_url FROM opportunities WHERE id=?", (ids[1],)
    ).fetchone()[0]
    assert "Data Engineer 1" not in candidate.content.body_text
    assert "Data Engineer 1" not in candidate.content.body_html
    assert excluded_url not in candidate.content.body_text
    assert excluded_url not in candidate.content.body_html


def test_the_digest_link_is_the_one_the_portfolio_surface_would_show(tmp_path):
    """The official ATS observation beats the opportunity's own stored link."""
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included()})
    observe_from_greenhouse(
        connection, ids[0], "https://boards.greenhouse.io/org/jobs/42"
    )

    candidate = build_digest_candidate(connection, identity.profile_id)

    assert candidate.content.items[0].url == "https://boards.greenhouse.io/org/jobs/42"
    assert "https://boards.greenhouse.io/org/jobs/42" in candidate.content.body_text
    assert (
        'href="https://boards.greenhouse.io/org/jobs/42"' in candidate.content.body_html
    )


def test_items_are_ordered_by_bucket_then_priority_then_opportunity_id(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 5)
    store_run(
        connection,
        identity.profile_id,
        {
            ids[0]: included(AMBITIOUS, URGENT),
            ids[1]: included(TARGET, MEDIUM),
            ids[2]: included(SAFE, HIGH),
            ids[3]: included(TARGET, URGENT),
            ids[4]: included(TARGET, MEDIUM),
        },
    )

    candidate = build_digest_candidate(connection, identity.profile_id)

    assert [item.opportunity_id for item in candidate.content.items] == [
        ids[3],
        ids[1],
        ids[4],
        ids[2],
        ids[0],
    ]
    text = candidate.content.body_text
    assert (
        text.index("TARGET (3)") < text.index("SAFE (1)") < text.index("AMBITIOUS (1)")
    )


def test_persisted_verdicts_are_repeated_and_unknown_stays_unknown(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included(SAFE, URGENT)})

    item = build_digest_candidate(connection, identity.profile_id).content.items[0]
    stored = connection.execute(
        """SELECT bucket,priority_category,eligibility_status FROM portfolio_assessments
        WHERE opportunity_id=? ORDER BY run_id DESC LIMIT 1""",
        (ids[0],),
    ).fetchone()

    assert stored == ("SAFE", "URGENT", "UNKNOWN")
    assert (
        item.bucket.value,
        item.priority_category.value,
        item.eligibility_status.value,
    ) == stored


def test_a_missing_location_is_rendered_as_an_absence_not_invented(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    set_opportunity(connection, ids[0], location="Casablanca, Maroc")
    set_opportunity(connection, ids[1], location="   ")

    content = build_digest_candidate(connection, identity.profile_id).content

    assert content.items[0].location == "Casablanca, Maroc"
    assert content.items[1].location is None
    assert content.body_text.count(UNKNOWN_LOCATION_LABEL) == 1
    assert content.body_html.count(UNKNOWN_LOCATION_LABEL) == 1
    assert "Casablanca, Maroc" in content.body_text
    assert "Casablanca, Maroc" in content.body_html


@pytest.mark.parametrize(
    "url", ["/relative/path", "javascript:alert(1)", "example.invalid/job", "   "]
)
def test_an_unresolvable_link_fails_the_whole_digest_closed(tmp_path, url):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    set_opportunity(connection, ids[1], source_url=url)

    with pytest.raises(GmailDigestError):
        build_digest_candidate(connection, identity.profile_id)
    before = digest_bytes(connection)
    with pytest.raises(GmailDigestError):
        materialize(connection, identity.profile_id)
    assert rows(connection) == []
    assert digest_bytes(connection) == before


def test_html_injection_from_the_opportunity_authority_is_escaped(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included()})
    set_opportunity(
        connection,
        ids[0],
        canonical_title='<script>alert("pwn")</script>',
        organization='" onload="steal()',
        location="<img src=x onerror=alert(1)>",
    )

    content = build_digest_candidate(connection, identity.profile_id).content

    assert "<script>" not in content.body_html
    assert "&lt;script&gt;" in content.body_html
    assert "<img" not in content.body_html.lower()
    assert 'onload="steal()' not in content.body_html
    assert "onerror" not in content.body_html.lower().replace("onerror=alert", "")


def test_text_and_html_carry_the_same_business_information(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 3)
    store_run(
        connection,
        identity.profile_id,
        {
            ids[0]: included(TARGET, URGENT),
            ids[1]: included(SAFE, HIGH),
            ids[2]: included(AMBITIOUS, MEDIUM),
        },
    )
    set_opportunity(connection, ids[0], location="Rabat")

    content = build_digest_candidate(connection, identity.profile_id).content

    for item in content.items:
        for value in (
            item.title,
            item.organization,
            item.url,
            item.bucket.value,
            item.priority_category.value,
            item.eligibility_status.value,
        ):
            assert value in content.body_text
            assert value in content.body_html
    assert content.body_text.count("Lieu :") == len(content.items)
    assert content.body_html.count("Lieu :") == len(content.items)


def test_repeated_assembly_of_one_snapshot_is_byte_identical(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 3)
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(SAFE), ids[1]: included(), ids[2]: excluded()},
    )

    first = build_digest_candidate(connection, identity.profile_id).content
    second = build_digest_candidate(connection, identity.profile_id).content

    assert (first.subject, first.body_text, first.body_html) == (
        second.subject,
        second.body_text,
        second.body_html,
    )
    assert first.content_fingerprint == second.content_fingerprint


# --------------------------------------------------------------------------
# Content fingerprint
# --------------------------------------------------------------------------


def test_the_fingerprint_payload_holds_only_visible_content(tmp_path):
    """No date, no row id, no run id, no run fingerprint, no timestamp."""
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included()})
    items = build_digest_candidate(connection, identity.profile_id).content.items

    payload = canonical_digest_payload(items)

    assert set(payload) == {"digest_version", "items"}
    assert set(payload["items"][0]) == {
        "opportunity_id",
        "title",
        "organization",
        "location",
        "url",
        "bucket",
        "priority_category",
        "eligibility_status",
    }


def test_a_different_day_alone_does_not_change_the_content_fingerprint(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})

    first = materialize(connection, identity.profile_id, now=DAY_ONE)
    second = materialize(connection, identity.profile_id, now=DAY_TWO)

    assert first.digest_date != second.digest_date
    assert first.content_fingerprint == second.content_fingerprint
    assert (first.status, second.status) == (
        DigestMaterializationStatus.CREATED,
        DigestMaterializationStatus.CREATED,
    )


def test_a_change_to_excluded_items_alone_leaves_the_digest_identical(tmp_path):
    """A new Portfolio run, a new run fingerprint — and nothing new to read."""
    connection, identity, ids = digest_fixture(tmp_path, 3)
    first_run = store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(), ids[2]: excluded(LOW)},
    )
    before = build_digest_candidate(connection, identity.profile_id)

    second_run = store_run(
        connection,
        identity.profile_id,
        {
            ids[0]: included(),
            ids[1]: included(),
            ids[2]: excluded(PriorityCategory.IGNORE),
        },
    )
    after = build_digest_candidate(connection, identity.profile_id)

    assert second_run.run_id != first_run.run_id
    assert second_run.run_fingerprint != first_run.run_fingerprint
    assert after.portfolio_run_id != before.portfolio_run_id
    assert after.portfolio_run_fingerprint != before.portfolio_run_fingerprint
    assert after.content.content_fingerprint == before.content.content_fingerprint


@pytest.mark.parametrize(
    "mutate",
    [
        lambda connection, ids: set_opportunity(
            connection, ids[0], canonical_title="Staff Data Engineer"
        ),
        lambda connection, ids: set_opportunity(
            connection, ids[0], organization="Another Org"
        ),
        lambda connection, ids: set_opportunity(connection, ids[0], location="Rabat"),
        lambda connection, ids: set_opportunity(
            connection, ids[0], source_url="https://example.invalid/moved"
        ),
    ],
)
def test_a_change_to_an_included_item_changes_the_content_fingerprint(tmp_path, mutate):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    before = build_digest_candidate(
        connection, identity.profile_id
    ).content.content_fingerprint

    mutate(connection, ids)

    assert (
        build_digest_candidate(
            connection, identity.profile_id
        ).content.content_fingerprint
        != before
    )


@pytest.mark.parametrize(
    "position", [included(SAFE, MEDIUM), included(TARGET, URGENT), included(AMBITIOUS)]
)
def test_a_changed_portfolio_position_changes_the_content_fingerprint(
    tmp_path, position
):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    before = build_digest_candidate(
        connection, identity.profile_id
    ).content.content_fingerprint

    store_run(connection, identity.profile_id, {ids[0]: position, ids[1]: included()})

    assert (
        build_digest_candidate(
            connection, identity.profile_id
        ).content.content_fingerprint
        != before
    )


# --------------------------------------------------------------------------
# Daily policy
# --------------------------------------------------------------------------


def test_the_first_digest_summarizes_todays_portfolio_rather_than_staying_silent(
    tmp_path,
):
    connection, identity, ids = digest_fixture(tmp_path, 3)
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(SAFE, HIGH), ids[2]: excluded()},
    )

    result = materialize(connection, identity.profile_id)

    assert result.status is DigestMaterializationStatus.CREATED
    assert (result.created, result.item_count) == (True, 2)
    stored = rows(connection)
    assert len(stored) == 1
    assert stored[0][2:] == (
        "2026-09-03",
        TIMEZONE,
        DIGEST_VERSION,
        result.content_fingerprint,
        recipient_fingerprint(RECIPIENT),
        result.portfolio_run_id,
        2,
        "PENDING",
        0,
        "Opportunity Radar — 2 opportunités à examiner",
    )
    assert connection.execute(
        """SELECT claim_token,claimed_at,sent_at,gmail_message_id,last_attempt_at,
        last_error_code FROM gmail_digest_outbox"""
    ).fetchone() == (None, None, None, None, None, None)


def test_a_portfolio_with_nothing_included_writes_nothing_at_all(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: excluded(), ids[1]: excluded()})
    before = digest_bytes(connection)

    result = materialize(connection, identity.profile_id)

    assert result.status is DigestMaterializationStatus.EMPTY
    assert (result.created, result.item_count, result.outbox_id) == (False, 0, None)
    assert result.content_fingerprint is None
    assert rows(connection) == []
    assert digest_bytes(connection) == before


def test_a_second_pass_on_the_same_day_is_a_byte_level_no_op(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    first = materialize(connection, identity.profile_id)
    before = digest_bytes(connection)

    second = materialize(
        connection, identity.profile_id, now=DAY_ONE + timedelta(hours=6)
    )

    assert second.status is DigestMaterializationStatus.ALREADY_MATERIALIZED
    assert (second.created, second.outbox_id) == (False, first.outbox_id)
    assert second.content_fingerprint == first.content_fingerprint
    assert len(rows(connection)) == 1
    assert digest_bytes(connection) == before


def test_a_later_day_with_content_already_sent_stays_silent(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    first = materialize(connection, identity.profile_id, now=DAY_ONE)
    mark_sent(connection, first.outbox_id)
    before = digest_bytes(connection)

    second = materialize(connection, identity.profile_id, now=DAY_TWO)

    assert second.status is DigestMaterializationStatus.UNCHANGED
    assert second.created is False
    assert second.content_fingerprint == first.content_fingerprint
    assert len(rows(connection)) == 1
    assert digest_bytes(connection) == before


def test_a_materialized_but_unsent_digest_never_silences_a_later_day(tmp_path):
    """Silence is earned by delivery, not by having written a row."""
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    first = materialize(connection, identity.profile_id, now=DAY_ONE)

    second = materialize(connection, identity.profile_id, now=DAY_TWO)

    assert second.status is DigestMaterializationStatus.CREATED
    assert second.content_fingerprint == first.content_fingerprint
    assert len(rows(connection)) == 2


def test_a_later_day_with_changed_content_is_a_new_pending_digest(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 3)
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(), ids[2]: excluded()},
    )
    first = materialize(connection, identity.profile_id, now=DAY_ONE)
    mark_sent(connection, first.outbox_id)

    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(), ids[2]: included(AMBITIOUS, HIGH)},
    )
    second = materialize(connection, identity.profile_id, now=DAY_TWO)

    assert second.status is DigestMaterializationStatus.CREATED
    assert second.content_fingerprint != first.content_fingerprint
    assert [row[9] for row in rows(connection)] == ["SENT", "PENDING"]
    assert [row[8] for row in rows(connection)] == [2, 3]


def test_the_unchanged_decision_is_made_per_recipient(tmp_path):
    """A different mailbox has been sent nothing, so it is not up to date."""
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    first = materialize(connection, identity.profile_id, now=DAY_ONE)
    mark_sent(connection, first.outbox_id)

    other = materialize(
        connection, identity.profile_id, now=DAY_TWO, recipient=OTHER_RECIPIENT
    )

    assert other.status is DigestMaterializationStatus.CREATED
    assert other.recipient_fingerprint == recipient_fingerprint(OTHER_RECIPIENT)
    assert other.recipient_fingerprint != first.recipient_fingerprint
    assert other.content_fingerprint == first.content_fingerprint


def test_a_not_synced_portfolio_produces_no_digest_and_no_row(tmp_path):
    connection, _, _ = digest_fixture(tmp_path, 1)
    connection.commit()
    other = ensure_user_profile(connection, "second-owner@example.invalid")
    before = digest_bytes(connection)

    with pytest.raises(GmailDigestError, match="not synced"):
        materialize(connection, other.profile_id)

    assert rows(connection) == []
    assert digest_bytes(connection) == before


def test_a_corrupt_portfolio_produces_no_digest_and_no_row(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    stored = store_run(
        connection, identity.profile_id, {ids[0]: included(), ids[1]: included()}
    )
    connection.execute(
        "UPDATE portfolio_runs SET run_payload_json='{}' WHERE id=?", (stored.run_id,)
    )
    connection.commit()
    before = digest_bytes(connection)

    with pytest.raises(GmailDigestError, match="corrupt"):
        materialize(connection, identity.profile_id)

    assert rows(connection) == []
    assert digest_bytes(connection) == before


def test_no_raw_recipient_reaches_a_result_a_log_or_the_database(tmp_path, caplog):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})

    with caplog.at_level("DEBUG"):
        result = materialize(connection, identity.profile_id)

    assert RECIPIENT not in repr(result)
    assert result.recipient_fingerprint == recipient_fingerprint(RECIPIENT)
    assert RECIPIENT not in caplog.text and "digest-recipient" not in caplog.text
    connection.commit()
    with open(database_path(connection), "rb") as handle:
        assert RECIPIENT.encode() not in handle.read()


def test_the_status_report_describes_the_outbox_without_a_body(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    first = materialize(connection, identity.profile_id, now=DAY_ONE)
    mark_sent(connection, first.outbox_id)

    report = read_digest_status(connection, profile_id=identity.profile_id)

    assert report.total_count == 1
    assert report.status_counts == (("SENT", 1),)
    assert report.latest.outbox_id == first.outbox_id
    assert report.latest_sent.outbox_id == first.outbox_id
    assert "body" not in repr(report).lower()
    assert first.content_fingerprint in repr(report)


# --------------------------------------------------------------------------
# Timezone
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "Mars/Olympus_Mons", "UTC+1", None])
def test_materialization_requires_a_real_iana_timezone(tmp_path, bad):
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included()})
    before = digest_bytes(connection)

    with pytest.raises(GmailDigestError):
        materialize(connection, identity.profile_id, timezone=bad)

    assert rows(connection) == []
    assert digest_bytes(connection) == before


def test_the_local_day_boundary_decides_which_day_a_digest_belongs_to(tmp_path):
    """One second apart in UTC, two different Casablanca days, two digests."""
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included()})
    midnight = datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc)

    before_midnight = materialize(
        connection, identity.profile_id, now=midnight - timedelta(seconds=1)
    )
    after_midnight = materialize(connection, identity.profile_id, now=midnight)

    assert before_midnight.digest_date == "2026-09-03"
    assert after_midnight.digest_date == "2026-09-04"
    assert [row[2] for row in rows(connection)] == ["2026-09-03", "2026-09-04"]
    assert {row[3] for row in rows(connection)} == {TIMEZONE}


def test_the_same_instant_is_a_different_day_in_a_different_timezone(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included()})
    instant = datetime(2026, 9, 3, 23, 30, tzinfo=timezone.utc)

    casablanca = materialize(connection, identity.profile_id, now=instant)
    new_york = materialize(
        connection, identity.profile_id, now=instant, timezone="America/New_York"
    )

    assert (casablanca.digest_date, new_york.digest_date) == (
        "2026-09-04",
        "2026-09-03",
    )
    assert [row[3] for row in rows(connection)] == [TIMEZONE, "America/New_York"]


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


def test_two_concurrent_materializers_produce_exactly_one_durable_row(tmp_path):
    """Two real SQLite connections, the same profile, the same local day."""
    connection, identity, ids = digest_fixture(tmp_path, 3)
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(SAFE, HIGH), ids[2]: included(AMBITIOUS)},
    )
    connection.commit()
    path = database_path(connection)
    connection.close()

    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def worker():
        worker_connection = connect_database(path)
        worker_connection.execute("PRAGMA busy_timeout = 10000")
        try:
            barrier.wait(timeout=10)
            outcome = materialize_daily_digest(
                worker_connection,
                identity.profile_id,
                timezone=TIMEZONE,
                recipient=RECIPIENT,
                now=DAY_ONE,
            )
        except BaseException as error:  # recorded, then asserted on below
            outcome = error
        finally:
            worker_connection.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(outcomes) == 2
    assert not any(isinstance(outcome, BaseException) for outcome in outcomes)
    statuses = sorted(outcome.status.value for outcome in outcomes)
    assert statuses == ["ALREADY_MATERIALIZED", "CREATED"]
    assert [outcome.created for outcome in outcomes].count(True) == 1
    verify = connect_database(path)
    try:
        stored = rows(verify)
        assert len(stored) == 1
        assert stored[0][9] == "PENDING"
        assert {outcome.outbox_id for outcome in outcomes} == {stored[0][0]}
        assert verify.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        verify.close()


def test_the_unique_day_constraint_backs_the_policy_up_in_the_database(tmp_path):
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included()})
    first = materialize(connection, identity.profile_id)
    row = connection.execute(
        "SELECT * FROM gmail_digest_outbox WHERE id=?", (first.outbox_id,)
    ).fetchone()

    names = [
        column[0]
        for column in connection.execute(
            "SELECT * FROM gmail_digest_outbox LIMIT 0"
        ).description
    ]
    values = dict(zip(names, row))
    values.pop("id")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"INSERT INTO gmail_digest_outbox ({','.join(values)})"
            f" VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cli(argv):
    return digest_cli.main(argv)


def test_the_cli_dry_run_leaves_the_database_byte_identical(tmp_path, capsys):
    connection, identity, ids = digest_fixture(tmp_path, 3)
    store_run(
        connection,
        identity.profile_id,
        {ids[0]: included(), ids[1]: included(SAFE, HIGH), ids[2]: excluded()},
    )
    connection.commit()
    path = database_path(connection)
    connection.close()
    before = file_digest(path)

    code = cli(
        [
            "--database",
            path,
            "--profile-id",
            str(identity.profile_id),
            "--timezone",
            TIMEZONE,
            "--recipient",
            RECIPIENT,
            "--dry-run",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["mode"] == "dry-run"
    assert payload["status"] == "CREATED"
    assert payload["item_count"] == 2
    assert payload["created"] is False
    assert payload["outbox_id"] is None
    assert payload["recipient_fingerprint"] == recipient_fingerprint(RECIPIENT)
    assert file_digest(path) == before
    verify = connect_database(path)
    try:
        assert rows(verify) == []
    finally:
        verify.close()


def test_the_cli_materializes_once_and_prints_nothing_sensitive(tmp_path, capsys):
    connection, identity, ids = digest_fixture(tmp_path, 2)
    store_run(connection, identity.profile_id, {ids[0]: included(), ids[1]: included()})
    connection.commit()
    path = database_path(connection)
    connection.close()
    argv = [
        "--database",
        path,
        "--profile-id",
        str(identity.profile_id),
        "--timezone",
        TIMEZONE,
        "--recipient",
        RECIPIENT,
    ]

    assert cli(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli(argv) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["status"] == "CREATED" and first["created"] is True
    assert second["status"] == "ALREADY_MATERIALIZED" and second["created"] is False
    assert second["outbox_id"] == first["outbox_id"]
    for payload in (first, second):
        printed = json.dumps(payload)
        assert RECIPIENT not in printed and "digest-recipient" not in printed
        assert "body_text" not in payload and "body_html" not in payload
        assert "subject" not in payload
        assert payload["recipient_fingerprint"] == recipient_fingerprint(RECIPIENT)
    verify = connect_database(path)
    try:
        assert len(rows(verify)) == 1
    finally:
        verify.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"--timezone": "Mars/Olympus_Mons"},
        {"--recipient": "not-an-address"},
    ],
)
def test_the_cli_fails_closed_without_leaking_a_message(tmp_path, capsys, changes):
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included()})
    connection.commit()
    path = database_path(connection)
    connection.close()
    before = file_digest(path)
    arguments = {
        "--database": path,
        "--profile-id": str(identity.profile_id),
        "--timezone": TIMEZONE,
        "--recipient": RECIPIENT,
    } | changes

    code = cli([value for pair in arguments.items() for value in pair])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert captured.err.strip() == "gmail digest failed: GmailDigestError"
    assert file_digest(path) == before


def test_the_cli_refuses_a_database_that_has_not_reached_0022(tmp_path, capsys):
    connection, identity, ids = digest_fixture(tmp_path, 1)
    store_run(connection, identity.profile_id, {ids[0]: included()})
    connection.execute("DROP TABLE gmail_digest_outbox")
    connection.execute("DELETE FROM schema_migrations WHERE version='0022'")
    connection.commit()
    path = database_path(connection)
    connection.close()

    code = cli(
        [
            "--database",
            path,
            "--profile-id",
            str(identity.profile_id),
            "--timezone",
            TIMEZONE,
            "--recipient",
            RECIPIENT,
            "--dry-run",
        ]
    )

    assert code == 1
    assert capsys.readouterr().err.strip() == "gmail digest failed: GmailDigestError"
