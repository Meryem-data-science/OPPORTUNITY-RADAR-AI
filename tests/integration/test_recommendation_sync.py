"""Phase 9B.3 synchronization, on disposable SQLite databases these tests build.

Every database here is created under `tmp_path` and thrown away, the profile is
an `.invalid` address, every posting is invented, and **the operational database
is never opened**: nothing below reads or writes anything a person uses.

Nothing is faked out either. The upstream world comes from
`test_recommendation_sqlite.build_corpus`, which drives each phase through its
own entry point — qualification, constraints, requirements, skills, preferences,
geography, eligibility and Matching — so what is synchronized here is a real
cohort and not a fixture's idea of one. The readiness verdict is the real
`assemble_recommendation_inputs`, the ranking is the real
`build_recommendation_batch`, and the write is the real Recommendation
persistence. Where a test needs a failure or a pause, it wraps a real function
rather than replacing it: no stubbed assembly, no stubbed engine, no stubbed
store.

Staleness is produced the only honest way — by changing an upstream fact and
**not** running the phase that owns the repair. Editing a posting's stored
location without a geography resynchronization is exactly the situation the
INCOMPLETE state exists for, and it is reversible, which is what lets one test
prove that restoring the upstream world reuses the very same recommendation run
rather than appending a second one.

The properties this module exists to hold:

    a published recommendation describes a world that existed — one
    `BEGIN IMMEDIATE`, opened before the first business read and closed at one
    `COMMIT`, so no upstream write can land between the freshness decision and
    the publication built on it. Proved from SQLite's own statement trace, and
    again against a second connection that really is kept out.

    INCOMPLETE moves a pointer, never a history — the runs a profile made stay
    exactly as they were, and only `current_run_id` stops naming one.

    a dry run is a dry run — real assembly, real engine, and not one byte
    written, on a `mode=ro` connection included.
"""

import ast
import json
import sqlite3

import pytest

from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.matching import sync_matching
from services.collector.matching.fingerprint import canonical_json
from services.collector.qualification.persistence import persist_qualifications
from services.recommendation import (
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RECOMMENDATION_PERSISTENCE_VERSION,
    RecommendationReadinessIssueCode,
    RecommendationReadinessStatus,
    RecommendationSyncError,
    assemble_recommendation_inputs,
    audit_recommendation_profile_history,
    build_recommendation_batch,
    read_current_recommendation,
    store_recommendation_batch,
    sync_recommendations,
)
from services.recommendation import sync as sync_module
from tests.integration.test_recommendation_sqlite import CORPUS, build_corpus

#: Every shape the public identifier must be refused for. `True` and `False`
#: come first on purpose: both are `int` in Python and `True == 1`, so a profile
#: that happens to be id 1 must not be reachable by asking for `True`.
INVALID_IDENTIFIERS = (True, False, 0, -1, 1.0, "1", None)

#: The tables a synchronization may touch, and the only ones it may.
AUDITED_TABLES = (
    "recommendation_runs",
    "recommendation_assessments",
    "recommendation_profile_state",
)

#: The recommendation persistence tables, in an order the foreign keys allow
#: them to be dropped in.
DROPPABLE = (
    "recommendation_profile_state",
    "recommendation_assessments",
    "recommendation_runs",
)


@pytest.fixture
def corpus(tmp_path):
    """One profile, five postings, and a real upstream world behind them."""
    connection, path, identity, ids, matching = build_corpus(tmp_path)
    yield connection, path, identity, ids, matching
    connection.close()


def rows_of(connection):
    return {
        table: connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in AUDITED_TABLES
    }


def state_row(connection, profile_id):
    return connection.execute(
        """SELECT state,current_run_id,persistence_version,input_assembly_version,
           readiness_issues_json FROM recommendation_profile_state
           WHERE profile_id=?""",
        (profile_id,),
    ).fetchone()


def codes(issues):
    return [issue.code for issue in issues]


def make_stale(connection, opportunity_id, location="Paris, France"):
    """Edit one posting's stored location and run no geography resynchronization.

    The projection still describes the old text, so answering off it would be
    answering about a string nobody wrote. This is real staleness produced by a
    real edit, and — because the projection is untouched — putting the text back
    restores the upstream world exactly.
    """
    connection.execute(
        "UPDATE opportunity_constraint_locations SET location_text=?"
        " WHERE opportunity_id=?",
        (location, opportunity_id),
    )
    connection.commit()


def restore(connection, opportunity_id, location="Casablanca"):
    connection.execute(
        "UPDATE opportunity_constraint_locations SET location_text=?"
        " WHERE opportunity_id=?",
        (location, opportunity_id),
    )
    connection.commit()


def drop_persistence_schema(connection):
    """Leave the database migrated for everything except recommendations."""
    for table in DROPPABLE:
        connection.execute(f"DROP TABLE {table}")
    connection.execute("DELETE FROM schema_migrations WHERE version='0026'")
    connection.commit()


# --------------------------------------------------------------------------
# arguments, existence, and the transaction the sync must own
# --------------------------------------------------------------------------


@pytest.mark.parametrize("persist", [True, False])
@pytest.mark.parametrize("profile_id", INVALID_IDENTIFIERS)
def test_a_profile_id_that_is_not_a_positive_integer_is_refused(
    corpus, profile_id, persist
):
    connection, _, identity, _, _ = corpus
    # The reachable id really is reachable, so nothing below passes by absence.
    assert identity.profile_id == 1

    with pytest.raises(RecommendationSyncError, match="positive integer"):
        sync_recommendations(connection, profile_id, persist=persist)

    assert connection.in_transaction is False
    assert sync_recommendations(connection, 1, persist=False).profile_id == 1


@pytest.mark.parametrize("persist", [True, False])
def test_a_profile_that_does_not_exist_is_a_failed_sync_not_an_incomplete_state(
    corpus, persist
):
    """`PROFILE_NOT_FOUND` has no row to be written on, so it is never written.

    `recommendation_profile_state.profile_id` references `profiles(id)`. An
    absent profile could not carry that readiness even if publishing it were
    desirable, so the synchronization fails instead of attempting a row a
    foreign key would refuse.
    """
    connection, _, _, _, _ = corpus
    before = rows_of(connection)

    with pytest.raises(RecommendationSyncError, match="profile 99999 does not exist"):
        sync_recommendations(connection, 99999, persist=persist)

    assert rows_of(connection) == before
    assert connection.in_transaction is False


def test_a_persisted_sync_refuses_to_borrow_a_callers_transaction(corpus):
    """The atomicity is this function's promise, so the transaction is its own.

    Inside a caller's transaction, when the publication commits — and whether it
    commits at all — would be the caller's decision, and the guarantee that a
    recommendation describes the world its readiness was decided in would stop
    being something this module can make.
    """
    connection, _, identity, _, _ = corpus
    before = rows_of(connection)
    connection.execute("BEGIN")
    connection.execute("INSERT INTO users(email) VALUES ('pending@example.invalid')")

    with pytest.raises(RecommendationSyncError, match="must own its transaction"):
        sync_recommendations(connection, identity.profile_id, persist=True)

    # The caller's transaction is exactly as it was: still open, still holding
    # its own uncommitted work, neither committed nor rolled back by the sync.
    assert connection.in_transaction is True
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM users WHERE email='pending@example.invalid'"
        ).fetchone()[0]
        == 1
    )
    assert rows_of(connection) == before
    connection.rollback()
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM users WHERE email='pending@example.invalid'"
        ).fetchone()[0]
        == 0
    )


def test_a_dry_run_borrows_a_callers_transaction_and_never_finishes_it(corpus):
    """The caller's own uncommitted write is the probe.

    If the dry run had committed, the rollback afterwards could not undo it; if
    it had rolled back, the row would be gone before the rollback ran.
    """
    connection, _, identity, _, _ = corpus
    connection.execute("BEGIN")
    connection.execute("INSERT INTO users(email) VALUES ('pending@example.invalid')")

    result = sync_recommendations(connection, identity.profile_id, persist=False)

    assert result.state is RecommendationReadinessStatus.READY
    assert connection.in_transaction is True
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM users WHERE email='pending@example.invalid'"
        ).fetchone()[0]
        == 1
    )
    connection.rollback()
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM users WHERE email='pending@example.invalid'"
        ).fetchone()[0]
        == 0
    )


def test_a_persisted_sync_refuses_a_database_that_cannot_store_the_result(corpus):
    """Verified, never applied: a synchronization does not migrate a database."""
    connection, _, identity, _, _ = corpus
    drop_persistence_schema(connection)

    with pytest.raises(
        RecommendationSyncError, match="persistence schema is not ready"
    ):
        sync_recommendations(connection, identity.profile_id, persist=True)

    assert connection.in_transaction is False


def test_a_dry_run_needs_no_recommendation_persistence_schema_at_all(corpus):
    """It computes an answer; it does not need somewhere to put one."""
    connection, _, identity, _, _ = corpus
    expected = sync_recommendations(connection, identity.profile_id, persist=False)
    drop_persistence_schema(connection)

    result = sync_recommendations(connection, identity.profile_id, persist=False)

    assert result.state is RecommendationReadinessStatus.READY
    assert result.assessment_count == expected.assessment_count
    assert result.batch_fingerprint == expected.batch_fingerprint
    assert result.persisted is False


# --------------------------------------------------------------------------
# the transaction, as SQLite itself saw it
# --------------------------------------------------------------------------


def traced(connection):
    """Every statement SQLite executed on this connection, in order."""
    statements: list[str] = []
    connection.set_trace_callback(
        lambda statement: statements.append(" ".join(statement.split()).upper())
    )
    return statements


def transactions(statements):
    return [
        item
        for item in statements
        if item.startswith(("BEGIN", "COMMIT", "ROLLBACK"))
    ]


def test_a_persisted_sync_opens_begin_immediate_before_any_business_read(corpus):
    """From SQLite's own trace, not from reading the source.

    Two things are proved together and neither is enough alone. `BEGIN
    IMMEDIATE` must be **first** — a deferred `BEGIN` takes no write lock until
    its first write, which is exactly the window another connection could commit
    an upstream change into — and it must be the **only** transaction opened, so
    nothing nests a second one inside the publication.
    """
    connection, _, identity, _, _ = corpus
    statements = traced(connection)

    result = sync_recommendations(connection, identity.profile_id, persist=True)
    connection.set_trace_callback(None)

    assert result.state is RecommendationReadinessStatus.READY
    assert statements, "no SQL traced; the callback is not seeing the connection"
    # Nothing whatsoever precedes the write transaction.
    assert statements[0] == "BEGIN IMMEDIATE"
    assert transactions(statements) == ["BEGIN IMMEDIATE", "COMMIT"]
    # And a business read really did happen inside it, so the ordering above is
    # about a transaction that actually contains the work.
    assert any(item.startswith("SELECT") for item in statements[1:])


def test_a_dry_run_opens_one_deferred_snapshot_and_releases_it(corpus):
    """Deferred, because a reader has nothing to reserve the database for."""
    connection, _, identity, _, _ = corpus
    statements = traced(connection)

    sync_recommendations(connection, identity.profile_id, persist=False)
    connection.set_trace_callback(None)

    assert statements[0] == "BEGIN"
    assert transactions(statements) == ["BEGIN", "ROLLBACK"]
    assert "BEGIN IMMEDIATE" not in statements
    assert connection.in_transaction is False


def test_the_public_store_still_opens_its_own_write_transaction(corpus):
    """The 9B.3 refactor did not move `store_recommendation_batch`'s ownership.

    The database work is now reachable as a transaction-aware primitive so that
    a synchronization can publish inside its own transaction. The public entry
    point must be unchanged by that: it still opens `BEGIN IMMEDIATE` itself and
    still commits exactly once.
    """
    connection, _, identity, _, matching = corpus
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    batch = build_recommendation_batch(
        identity.profile_id,
        [record.recommendation_input for record in assembly.records],
    )
    statements = traced(connection)

    stored = store_recommendation_batch(
        connection,
        identity.profile_id,
        batch,
        source_matching_run_id=matching.run_id,
        source_matching_run_fingerprint=matching.run_fingerprint,
    )
    connection.set_trace_callback(None)

    assert stored.created is True
    assert transactions(statements) == ["BEGIN IMMEDIATE", "COMMIT"]


# --------------------------------------------------------------------------
# the dry run
# --------------------------------------------------------------------------


def test_a_ready_dry_run_uses_the_real_assembly_and_engine_and_writes_nothing(corpus):
    connection, _, identity, _, matching = corpus
    before_rows = rows_of(connection)
    before_changes = connection.total_changes
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    expected = build_recommendation_batch(
        identity.profile_id,
        [record.recommendation_input for record in assembly.records],
    )

    result = sync_recommendations(connection, identity.profile_id, persist=False)

    assert result.state is RecommendationReadinessStatus.READY
    assert result.persisted is False
    assert result.issues == ()
    assert result.profile_id == identity.profile_id
    assert result.input_assembly_version == RECOMMENDATION_INPUT_ASSEMBLY_VERSION
    assert result.source_matching_run_id == matching.run_id
    # The real engine's own numbers, not a recount of anything.
    assert result.assessment_count == expected.assessment_count == len(CORPUS)
    assert result.batch_fingerprint == expected.batch_fingerprint
    # Nothing about a stored run, because nothing was stored.
    assert (result.created, result.run_id, result.run_fingerprint) == (None, None, None)
    assert rows_of(connection) == before_rows
    assert connection.total_changes == before_changes
    assert connection.in_transaction is False


def test_an_incomplete_dry_run_reports_the_issues_and_writes_nothing(corpus):
    connection, _, identity, ids, _ = corpus
    make_stale(connection, ids["strong"])
    before_rows = rows_of(connection)
    before_changes = connection.total_changes

    result = sync_recommendations(connection, identity.profile_id, persist=False)

    assert result.state is RecommendationReadinessStatus.INCOMPLETE
    assert result.persisted is False
    assert codes(result.issues) == [
        RecommendationReadinessIssueCode.GEOGRAPHY_PROJECTION_STALE
    ]
    assert result.issues[0].opportunity_id == ids["strong"]
    assert result.assessment_count == 0
    assert (result.created, result.run_id, result.run_fingerprint) == (None, None, None)
    assert result.batch_fingerprint is None
    assert rows_of(connection) == before_rows
    assert connection.total_changes == before_changes
    assert state_row(connection, identity.profile_id) is None


def test_a_dry_run_works_over_a_read_only_connection(corpus):
    """`mode=ro` is the strongest available proof that nothing is attempted."""
    connection, path, identity, _, _ = corpus
    expected = sync_recommendations(connection, identity.profile_id, persist=False)
    connection.close()

    read_only = connect_readonly_database(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            read_only.execute("INSERT INTO users(email) VALUES ('x@example.invalid')")
        read_only.rollback()
        assert read_only.in_transaction is False

        result = sync_recommendations(read_only, identity.profile_id, persist=False)

        assert result.state is RecommendationReadinessStatus.READY
        assert result.batch_fingerprint == expected.batch_fingerprint
        assert result.assessment_count == expected.assessment_count
        assert read_only.total_changes == 0
        assert read_only.in_transaction is False
    finally:
        read_only.close()


# --------------------------------------------------------------------------
# the READY publication
# --------------------------------------------------------------------------


def test_a_ready_sync_stores_the_run_its_assessments_and_the_ready_state(corpus):
    connection, _, identity, _, matching = corpus
    dry = sync_recommendations(connection, identity.profile_id, persist=False)

    result = sync_recommendations(connection, identity.profile_id, persist=True)

    assert result.state is RecommendationReadinessStatus.READY
    assert result.persisted is True and result.created is True
    assert result.issues == ()
    assert result.source_matching_run_id == matching.run_id
    # What the dry run said it would publish is what it published.
    assert result.batch_fingerprint == dry.batch_fingerprint
    assert result.assessment_count == dry.assessment_count
    assert len(result.run_fingerprint) == 64

    run = connection.execute(
        """SELECT id,profile_id,source_matching_run_id,persistence_version,
           input_assembly_version,source_matching_run_fingerprint,batch_fingerprint,
           run_fingerprint,assessment_count FROM recommendation_runs""",
    ).fetchall()
    assert len(run) == 1
    assert run[0] == (
        result.run_id,
        identity.profile_id,
        matching.run_id,
        RECOMMENDATION_PERSISTENCE_VERSION,
        RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        matching.run_fingerprint,
        result.batch_fingerprint,
        result.run_fingerprint,
        result.assessment_count,
    )
    positions = connection.execute(
        "SELECT rank_position FROM recommendation_assessments WHERE run_id=?"
        " ORDER BY rank_position",
        (result.run_id,),
    ).fetchall()
    assert [row[0] for row in positions] == list(range(1, result.assessment_count + 1))
    assert state_row(connection, identity.profile_id) == (
        "READY",
        result.run_id,
        RECOMMENDATION_PERSISTENCE_VERSION,
        RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        canonical_json([]),
    )


def test_a_ready_sync_is_what_the_read_model_then_reads(corpus):
    connection, _, identity, _, _ = corpus
    result = sync_recommendations(connection, identity.profile_id)

    current = read_current_recommendation(connection, identity.profile_id)

    assert current.status == "READY"
    assert current.current_run_id == result.run_id
    assert current.history_count == 1
    assert current.readiness_issues == ()
    assert current.persistence_version == RECOMMENDATION_PERSISTENCE_VERSION
    assert current.input_assembly_version == RECOMMENDATION_INPUT_ASSEMBLY_VERSION
    assert current.current_run.run_fingerprint == result.run_fingerprint
    assert current.current_run.batch_fingerprint == result.batch_fingerprint
    assert len(current.current_run.assessments) == result.assessment_count


def test_a_ready_sync_leaves_a_history_the_persistence_audit_accepts(corpus):
    connection, _, identity, _, _ = corpus
    sync_recommendations(connection, identity.profile_id)

    report = audit_recommendation_profile_history(connection, identity.profile_id)

    assert report.ok is True and report.issues == ()
    assert report.status == "READY"
    assert report.run_count == 1


def test_repeating_an_unchanged_ready_sync_reuses_the_very_same_run(corpus):
    """Idempotence is by operational identity, not by a second insert refused."""
    connection, _, identity, _, _ = corpus
    first = sync_recommendations(connection, identity.profile_id)

    second = sync_recommendations(connection, identity.profile_id)

    assert first.created is True and second.created is False
    assert (second.run_id, second.run_fingerprint, second.batch_fingerprint) == (
        first.run_id,
        first.run_fingerprint,
        first.batch_fingerprint,
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_runs"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_assessments"
    ).fetchone()[0] == first.assessment_count
    assert audit_recommendation_profile_history(connection, identity.profile_id).ok


def test_a_repeated_sync_moves_only_the_state_timestamp(corpus):
    """The state may be re-upserted; that is not a change of business identity.

    The audit fingerprint is taken over stable persisted identities and
    findings, so a `updated_at` that moved must not move it.
    """
    connection, _, identity, _, _ = corpus
    sync_recommendations(connection, identity.profile_id)
    before = audit_recommendation_profile_history(
        connection, identity.profile_id
    ).audit_fingerprint

    connection.execute(
        "UPDATE recommendation_profile_state SET updated_at='2020-01-01'"
        " WHERE profile_id=?",
        (identity.profile_id,),
    )
    connection.commit()
    sync_recommendations(connection, identity.profile_id)

    assert (
        audit_recommendation_profile_history(
            connection, identity.profile_id
        ).audit_fingerprint
        == before
    )


# --------------------------------------------------------------------------
# the INCOMPLETE publication
# --------------------------------------------------------------------------


def test_an_incomplete_sync_publishes_the_state_and_creates_no_run(corpus):
    connection, _, identity, ids, _ = corpus
    make_stale(connection, ids["strong"])

    result = sync_recommendations(connection, identity.profile_id, persist=True)

    assert result.state is RecommendationReadinessStatus.INCOMPLETE
    # `persisted` says a write happened, and one did: the state row is new.
    assert result.persisted is True
    assert (result.created, result.run_id, result.run_fingerprint) == (None, None, None)
    assert result.batch_fingerprint is None and result.assessment_count == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_runs"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_assessments"
    ).fetchone()[0] == 0
    state = state_row(connection, identity.profile_id)
    assert state[0] == "INCOMPLETE" and state[1] is None


def test_the_stored_readiness_issues_are_canonical_and_in_assembly_order(corpus):
    """Byte for byte what the assembly produced, in the order it produced it.

    The assembly owns the ordering — it sorts once, before anything stores — and
    both the read model and the audit verify that order rather than repairing
    it. Re-sorting here would invent a second authority over it.
    """
    connection, _, identity, ids, _ = corpus
    # Two stale postings, so the order is a fact about the array rather than a
    # property of a single entry.
    make_stale(connection, ids["strong"])
    make_stale(connection, ids["blocked"], "Lyon, France")
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    assert len(assembly.issues) == 2

    sync_recommendations(connection, identity.profile_id, persist=True)

    stored = state_row(connection, identity.profile_id)[4]
    expected = canonical_json(
        [
            {
                "code": issue.code.value,
                "message": issue.message,
                "opportunity_id": issue.opportunity_id,
            }
            for issue in assembly.issues
        ]
    )
    assert stored == expected
    # Canonical, and carrying the assembly's exact text — nothing trimmed,
    # nothing rewritten, nothing dropped.
    assert json.loads(stored) == json.loads(canonical_json(json.loads(stored)))
    assert [entry["message"] for entry in json.loads(stored)] == [
        issue.message for issue in assembly.issues
    ]


def test_an_incomplete_sync_is_what_the_read_model_then_reads(corpus):
    connection, _, identity, ids, _ = corpus
    make_stale(connection, ids["strong"])
    result = sync_recommendations(connection, identity.profile_id, persist=True)

    current = read_current_recommendation(connection, identity.profile_id)

    assert current.status == "INCOMPLETE"
    assert current.current_run_id is None and current.current_run is None
    assert current.history_count == 0
    assert current.readiness_issues == result.issues
    assert current.input_assembly_version == RECOMMENDATION_INPUT_ASSEMBLY_VERSION


def test_an_incomplete_sync_leaves_a_state_the_persistence_audit_accepts(corpus):
    connection, _, identity, ids, _ = corpus
    make_stale(connection, ids["strong"])
    sync_recommendations(connection, identity.profile_id, persist=True)

    report = audit_recommendation_profile_history(connection, identity.profile_id)

    assert report.ok is True and report.issues == ()
    assert report.status == "INCOMPLETE"
    assert report.current_run_id is None


def test_repeating_an_incomplete_sync_creates_nothing_and_says_the_same_thing(corpus):
    connection, _, identity, ids, _ = corpus
    make_stale(connection, ids["strong"])
    first = sync_recommendations(connection, identity.profile_id, persist=True)

    second = sync_recommendations(connection, identity.profile_id, persist=True)

    assert first == second
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_runs"
    ).fetchone()[0] == 0
    # Structurally the same state; only the timestamp may have moved.
    assert state_row(connection, identity.profile_id)[:5] == (
        "INCOMPLETE",
        None,
        RECOMMENDATION_PERSISTENCE_VERSION,
        RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        state_row(connection, identity.profile_id)[4],
    )


# --------------------------------------------------------------------------
# the transitions, which are the whole point of the state
# --------------------------------------------------------------------------


def test_going_stale_stops_naming_a_run_and_deletes_none(corpus):
    """READY(R1) -> upstream goes stale -> INCOMPLETE, with R1 still history.

    R1 *was* what this profile recommended, and a later staleness cannot make
    that untrue. What changes is only which run — if any — is current.
    """
    connection, _, identity, ids, _ = corpus
    ready = sync_recommendations(connection, identity.profile_id)
    assessments_before = connection.execute(
        "SELECT id,run_id,opportunity_id,rank_position,assessment_fingerprint"
        " FROM recommendation_assessments ORDER BY id"
    ).fetchall()
    runs_before = connection.execute(
        "SELECT * FROM recommendation_runs ORDER BY id"
    ).fetchall()

    make_stale(connection, ids["strong"])
    stale = sync_recommendations(connection, identity.profile_id)

    assert stale.state is RecommendationReadinessStatus.INCOMPLETE
    assert codes(stale.issues) == [
        RecommendationReadinessIssueCode.GEOGRAPHY_PROJECTION_STALE
    ]
    # The history is untouched, row for row.
    assert connection.execute(
        "SELECT * FROM recommendation_runs ORDER BY id"
    ).fetchall() == runs_before
    assert connection.execute(
        "SELECT id,run_id,opportunity_id,rank_position,assessment_fingerprint"
        " FROM recommendation_assessments ORDER BY id"
    ).fetchall() == assessments_before
    # And the pointer names nothing.
    state = state_row(connection, identity.profile_id)
    assert state[0] == "INCOMPLETE" and state[1] is None

    current = read_current_recommendation(connection, identity.profile_id)
    assert current.status == "INCOMPLETE"
    assert current.current_run_id is None
    # The run is still there to be read; it is simply no longer current.
    assert current.history_count == 1
    assert ready.run_id == runs_before[0][0]
    assert audit_recommendation_profile_history(connection, identity.profile_id).ok


def test_restoring_the_upstream_world_reuses_the_exact_same_run(corpus):
    """INCOMPLETE -> READY, and the recommendation is R1 again, not R1'.

    The upstream fact is put back exactly as it was, so the assembly produces
    the same cohort over the same Matching run, the engine produces the same
    ranking, and the operational identity is therefore the same one. A second
    history row here would mean the identity depended on something other than
    what it claims to be made of.
    """
    connection, _, identity, ids, _ = corpus
    first = sync_recommendations(connection, identity.profile_id)
    make_stale(connection, ids["strong"])
    assert (
        sync_recommendations(connection, identity.profile_id).state
        is RecommendationReadinessStatus.INCOMPLETE
    )

    restore(connection, ids["strong"])
    again = sync_recommendations(connection, identity.profile_id)

    assert again.state is RecommendationReadinessStatus.READY
    assert again.created is False
    assert again.run_id == first.run_id
    assert again.run_fingerprint == first.run_fingerprint
    assert again.batch_fingerprint == first.batch_fingerprint
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_runs"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_assessments"
    ).fetchone()[0] == first.assessment_count
    current = read_current_recommendation(connection, identity.profile_id)
    assert current.status == "READY" and current.current_run_id == first.run_id
    assert audit_recommendation_profile_history(connection, identity.profile_id).ok


# --------------------------------------------------------------------------
# what makes a recommendation run the run it is
# --------------------------------------------------------------------------


def test_the_source_matching_identity_is_part_of_the_recommendation_identity(corpus):
    """The same batch content over another Matching snapshot is another run.

    `batch_fingerprint` is a *content* digest and holds no profile, no posting
    and no source run by construction. If it decided persistence identity, a
    recommendation produced over a superseded Matching snapshot would collapse
    into the one produced over the current snapshot, and the history would claim
    they were the same act. The operational identity carries the source pair,
    which is what keeps them apart.

    The second Matching run here is real — a posting is edited and Matching is
    resynchronized by its own entry point — and the batch stored against it is
    the real engine's, deliberately the *same* batch, so the only thing that
    differs between the two stored runs is the source identity.
    """
    connection, _, identity, ids, matching = corpus
    first = sync_recommendations(connection, identity.profile_id)
    assembly = assemble_recommendation_inputs(connection, identity.profile_id)
    batch = build_recommendation_batch(
        identity.profile_id,
        [record.recommendation_input for record in assembly.records],
    )
    assert batch.batch_fingerprint == first.batch_fingerprint

    # A real second Matching run for the same profile.
    connection.execute(
        "UPDATE opportunities SET description = description || ? WHERE id = ?",
        (" TEST ONLY neutral sentence.", ids["unbridged_fine"]),
    )
    connection.commit()
    persist_qualifications(connection)
    second_matching = sync_matching(connection, identity.profile_id)
    assert second_matching.run_id != matching.run_id

    stored = store_recommendation_batch(
        connection,
        identity.profile_id,
        batch,
        source_matching_run_id=second_matching.run_id,
        source_matching_run_fingerprint=second_matching.run_fingerprint,
    )

    # Same content digest, different operational identity, two history rows.
    assert stored.created is True
    assert stored.run_id != first.run_id
    assert stored.run_fingerprint != first.run_fingerprint
    both = connection.execute(
        "SELECT batch_fingerprint FROM recommendation_runs ORDER BY id"
    ).fetchall()
    assert [row[0] for row in both] == [batch.batch_fingerprint] * 2
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_runs"
    ).fetchone()[0] == 2


def test_a_sync_over_a_new_matching_run_appends_rather_than_reusing(corpus):
    """The same property, reached through the synchronization itself."""
    connection, _, identity, ids, matching = corpus
    first = sync_recommendations(connection, identity.profile_id)

    connection.execute(
        "UPDATE opportunities SET description = description || ? WHERE id = ?",
        (" TEST ONLY neutral sentence.", ids["unbridged_fine"]),
    )
    connection.commit()
    persist_qualifications(connection)
    second_matching = sync_matching(connection, identity.profile_id)

    second = sync_recommendations(connection, identity.profile_id)

    assert second_matching.run_id != matching.run_id
    assert second.state is RecommendationReadinessStatus.READY
    assert second.source_matching_run_id == second_matching.run_id
    assert second.created is True
    assert second.run_id != first.run_id
    assert second.run_fingerprint != first.run_fingerprint
    # The superseded run is still history, and still exactly what it was.
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_runs"
    ).fetchone()[0] == 2
    assert audit_recommendation_profile_history(connection, identity.profile_id).ok


# --------------------------------------------------------------------------
# rollback: a failed publication leaves no trace of itself
# --------------------------------------------------------------------------


class Interrupted(RuntimeError):
    """A test-only failure, deliberately not a `sqlite3.Error`."""


def interrupt_store(monkeypatch, at):
    """Wrap the real store primitive so it fails at one insert position.

    The primitive is the real one and it really runs; only the persistence
    layer's own `after_assessment_insert` seam is used to stop it partway. What
    is being proved is the transaction, so nothing about the write is faked.
    """
    real = sync_module._store_prepared_recommendation_batch_in_transaction

    def interrupted(*args, **kwargs):
        def fail(position):
            if position == at:
                raise Interrupted(f"interrupted at insert {position}")

        kwargs["after_assessment_insert"] = fail
        return real(*args, **kwargs)

    monkeypatch.setattr(
        sync_module,
        "_store_prepared_recommendation_batch_in_transaction",
        interrupted,
    )


def test_an_interruption_mid_assessment_insert_leaves_nothing_behind(
    corpus, monkeypatch
):
    connection, _, identity, _, _ = corpus
    before = rows_of(connection)
    interrupt_store(monkeypatch, at=1)

    with pytest.raises(Interrupted):
        sync_recommendations(connection, identity.profile_id)

    assert rows_of(connection) == before
    assert connection.in_transaction is False
    assert read_current_recommendation(connection, identity.profile_id).status == (
        "NOT_SYNCED"
    )


def test_an_interruption_before_the_ready_state_update_rolls_the_run_back(
    corpus, monkeypatch
):
    """Every assessment is in, the pointer is not yet moved, and then it fails.

    This is the case that would otherwise leave a run nobody points at, or —
    worse, in the other order — a pointer to a run that is not fully there.
    """
    connection, _, identity, _, _ = corpus
    expected = sync_recommendations(connection, identity.profile_id, persist=False)
    before = rows_of(connection)
    interrupt_store(monkeypatch, at=expected.assessment_count - 1)

    with pytest.raises(Interrupted):
        sync_recommendations(connection, identity.profile_id)

    assert rows_of(connection) == before
    assert state_row(connection, identity.profile_id) is None
    assert connection.in_transaction is False


def test_a_failure_after_a_complete_publication_still_rolls_it_back(
    corpus, monkeypatch
):
    """Written in full, not yet committed, then interrupted: nothing survives.

    The run, its assessments and the READY pointer were all written by the real
    primitive before this fails, so what is proved is the `COMMIT` boundary
    itself rather than any single statement.
    """
    connection, _, identity, _, _ = corpus
    before = rows_of(connection)
    real = sync_module._store_prepared_recommendation_batch_in_transaction

    def store_then_fail(*args, **kwargs):
        result = real(*args, **kwargs)
        # Proof the work really happened inside the transaction.
        connection = args[0]
        assert connection.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT current_run_id FROM recommendation_profile_state"
            " WHERE profile_id=?",
            (args[1],),
        ).fetchone()[0] == result.run_id
        raise Interrupted("after a complete publication")

    monkeypatch.setattr(
        sync_module,
        "_store_prepared_recommendation_batch_in_transaction",
        store_then_fail,
    )

    with pytest.raises(Interrupted):
        sync_recommendations(connection, identity.profile_id)

    assert rows_of(connection) == before
    assert connection.in_transaction is False


def test_a_failure_writing_the_incomplete_state_rolls_it_back(corpus, monkeypatch):
    """The previous READY pointer must not be left half-switched."""
    connection, _, identity, ids, _ = corpus
    ready = sync_recommendations(connection, identity.profile_id)
    before = rows_of(connection)
    make_stale(connection, ids["strong"])
    real = sync_module._set_recommendation_state_incomplete_in_transaction

    def write_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise Interrupted("after the state write")

    monkeypatch.setattr(
        sync_module,
        "_set_recommendation_state_incomplete_in_transaction",
        write_then_fail,
    )

    with pytest.raises(Interrupted):
        sync_recommendations(connection, identity.profile_id)

    assert rows_of(connection) == before
    assert state_row(connection, identity.profile_id)[:2] == ("READY", ready.run_id)
    assert connection.in_transaction is False


# --------------------------------------------------------------------------
# concurrency: the reason the transaction is IMMEDIATE
# --------------------------------------------------------------------------


def test_a_second_writer_cannot_land_between_readiness_and_publication(tmp_path):
    """Under WAL, with a real second connection actually trying to write.

    The sync holds `BEGIN IMMEDIATE` from before its first business read until
    its commit, so a writer that arrives after readiness has been decided cannot
    commit into the middle of it: under a deliberately short busy timeout it is
    refused, and it succeeds only once the synchronization has released the
    database. The recommendation that becomes current therefore describes the
    world before that write — wholly, never blended with it.

    The seam is a timing-only wrapper around the **real**
    `assemble_recommendation_inputs`: it is called, its real result is returned,
    and the probe runs in between. Nothing about assembly, the engine or the
    store is replaced.
    """
    connection, path, identity, ids, _ = build_corpus(tmp_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.commit()
    writer = connect_database(path)
    writer.execute("PRAGMA busy_timeout=200")
    observed = {}
    real_assemble = sync_module.assemble_recommendation_inputs

    def assemble_then_probe(*args, **kwargs):
        assembly = real_assemble(*args, **kwargs)
        # Readiness has now been decided inside the sync's transaction.
        try:
            writer.execute(
                "UPDATE opportunity_constraint_locations SET location_text=?"
                " WHERE opportunity_id=?",
                ("Paris, France", ids["strong"]),
            )
            writer.commit()
            observed["blocked"] = False
        except sqlite3.OperationalError as error:
            writer.rollback()
            observed["blocked"] = True
            observed["error"] = str(error)
        return assembly

    sync_module.assemble_recommendation_inputs = assemble_then_probe
    try:
        result = sync_recommendations(connection, identity.profile_id, persist=True)
    finally:
        sync_module.assemble_recommendation_inputs = real_assemble

    try:
        # The competing write really was kept out of the synchronization.
        assert observed["blocked"] is True
        assert "locked" in observed["error"] or "busy" in observed["error"]
        assert result.state is RecommendationReadinessStatus.READY

        # The published recommendation is the pre-write world, wholly.
        assert (
            writer.execute(
                "SELECT location_text FROM opportunity_constraint_locations"
                " WHERE opportunity_id=?",
                (ids["strong"],),
            ).fetchone()[0]
            == "Casablanca"
        )
        assert audit_recommendation_profile_history(connection, identity.profile_id).ok

        # And once the sync has released the database, the writer proceeds.
        writer.execute(
            "UPDATE opportunity_constraint_locations SET location_text=?"
            " WHERE opportunity_id=?",
            ("Paris, France", ids["strong"]),
        )
        writer.commit()
        # Which the *next* synchronization sees, as staleness — never the one
        # that was already deciding while the write was waiting.
        assert (
            sync_recommendations(connection, identity.profile_id).state
            is RecommendationReadinessStatus.INCOMPLETE
        )
    finally:
        writer.close()
        connection.close()


# --------------------------------------------------------------------------
# structural guarantees
#
# Read out of the module's own AST, so the orchestration *cannot* drift into
# owning business rules rather than merely happening not to today.
# --------------------------------------------------------------------------


def sync_tree():
    with open(sync_module.__file__, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def imported_names():
    names = set()
    for node in ast.walk(sync_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def called_names():
    names = set()
    for node in ast.walk(sync_tree()):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_the_sync_never_repairs_or_reruns_an_upstream_phase():
    """It reports that an upstream is stale; it never brings one up to date.

    Synchronizing Matching, reprojecting qualifications, resolving geography or
    re-evaluating eligibility from here would make this module a second writer
    of truth that other phases own — and would turn a readiness report into a
    silent repair, which is precisely what the INCOMPLETE state exists to avoid.
    """
    names = imported_names() | called_names()
    for forbidden in (
        "sync_matching",
        "persist_qualifications",
        "synchronize_location_resolutions",
        "synchronize_eligibility",
        "synchronize_profile_preferences",
        "synchronize_profile_skills",
        "synchronize_opportunity_constraints",
        "synchronize_opportunity_requirements",
        "store_matching_batch",
        "set_matching_state_empty",
    ):
        assert forbidden not in names, forbidden
    for fragment in ("priority", "notification", "digest", "apply"):
        assert not any(fragment in name.lower() for name in imported_names()), fragment


def test_the_sync_scores_nothing_and_ranks_nothing_of_its_own():
    """The engine owns the product; this module hands it inputs and stores it."""
    names = called_names()
    assert "build_recommendation_batch" in names
    for forbidden in (
        "build_recommendation_assessment",
        "rank_recommendation_assessments",
        "recommendation_disposition",
        "build_domain_component",
        "build_skill_evidence",
        "build_geography_signal",
        "build_eligibility_evidence",
        "sorted",
        "sort",
    ):
        assert forbidden not in names, forbidden
    # And no weight, threshold or constant of Phase 9A is restated here.
    imported = imported_names()
    for forbidden in (
        "REQUIRED_SKILL_WEIGHT",
        "SEMANTIC_WEIGHT",
        "DOMAIN_WEIGHT",
        "DISPOSITION_RANK",
        "DISPOSITION_PRECEDENCE",
    ):
        assert forbidden not in imported, forbidden


def test_the_sync_uses_the_real_assembly_and_the_persistence_layers_own_sql():
    """Orchestration, not a second persistence layer.

    Every statement that writes these three tables stays in `persistence.py`;
    what this module holds is the transaction around them.
    """
    assert "assemble_recommendation_inputs" in called_names()
    statements = []
    for node in ast.walk(sync_tree()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            statements.append(" ".join(node.args[0].value.split()).upper())
    for statement in statements:
        assert statement.startswith(("SELECT ", "BEGIN", "COMMIT", "ROLLBACK")), (
            statement
        )
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ", "PRAGMA "):
            assert forbidden not in statement, (forbidden, statement)
    # The write transaction is immediate, and there is no exclusive one.
    assert "BEGIN IMMEDIATE" in statements
    assert not any(item.startswith("BEGIN EXCLUSIVE") for item in statements)


def executable_strings():
    """Every string literal the module can actually use, docstrings excluded.

    Prose is not what runs: the module explains *why* there is no EMPTY state,
    and a plain text search would read that explanation as the thing it warns
    against. Docstrings are therefore skipped and every other literal is kept.
    """
    tree = sync_tree()
    documented = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            documented.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documented
    ]


def test_recommendation_has_no_empty_state():
    """Matching's EMPTY is a selection outcome; recommendation has no selection.

    A cohort that cannot be recommended over is INCOMPLETE, carrying the
    assembly issue that says why — never a fourth state that would read as a
    healthy "nothing to recommend".
    """
    assert not [item for item in executable_strings() if "EMPTY" in item]
    assert {item.value for item in RecommendationReadinessStatus} == {
        "READY",
        "INCOMPLETE",
    }
