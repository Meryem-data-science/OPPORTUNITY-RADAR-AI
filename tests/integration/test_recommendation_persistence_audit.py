"""Phase 9B.2b persistence audit, on disposable SQLite databases these tests build.

Every database here is created under `tmp_path` and thrown away, the profiles are
`.invalid` addresses, every posting is invented, and **the operational database is
never opened**: nothing below reads or writes anything a person uses.

Nothing is faked out either. The schema is the project's own migrations up to
`0026`, the Matching snapshots come from Phase 4's `store_matching_batch`, the
batches come from Phase 9A's own engine, and the runs come from Phase 9B.1's
`store_recommendation_batch`. A hand-rolled mini-schema would let the audit agree
with a table this file invented rather than with the one it will actually be
pointed at, so there is none.

Corruption is produced the only honest way: a **valid** history is built by the
real components first, and then one specific value is broken in the temporary
database. Where `0026`'s own CHECK constraints or its foreign keys would refuse
the broken row, `PRAGMA ignore_check_constraints` / `PRAGMA foreign_keys=OFF` is
switched on for that one edit and switched back in a `finally`. Production audit
code issues no such statement — that is asserted structurally, from its AST.

The two properties this module exists to hold:

    the ranking is the product — the audit reads `ORDER BY rank_position ASC` and
    never re-sorts. A reversal that leaves every row individually valid is caught
    by the batch content statement and by the operational run fingerprint.

    integrity is not freshness — a historical run whose source Matching run has
    been superseded is valid history. `M1 -> R1`, then `M2` becoming the current
    Matching run, must leave `R1` clean, and corruption in the unrelated `M2`
    must not touch it either. Only corrupting `M1` itself may.
"""

import ast
from contextlib import contextmanager
import json
import sqlite3

import pytest

from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.database.migrations import apply_migrations
from services.collector.matching import store_matching_batch
from services.collector.matching.fingerprint import canonical_json
from services.digital_twin.repository import ensure_user_profile
from services.recommendation import (
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RECOMMENDATION_PERSISTENCE_AUDIT_VERSION,
    RECOMMENDATION_PERSISTENCE_VERSION,
    RecommendationPersistenceAuditError,
    RecommendationReadinessIssueCode,
    audit_recommendation_profile_history,
)
from tests.integration.test_matching_read_audit import (
    add_opportunity,
    make_batch as make_matching_batch,
)
from tests.integration.test_recommendation_persistence import (
    SELECTION_VERSION,
    Fixture,
    make_batch as make_recommendation_batch,
)

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "recommendation.persistence.audit@example.invalid"
TEST_ONLY_OTHER_EMAIL = "recommendation.persistence.audit.other@example.invalid"

#: Every shape the public identifier must be refused for. `True` and `False` come
#: first on purpose: both are `int` in Python and `True == 1`, so a profile that
#: happens to be id 1 must not be reachable by asking for `True`.
INVALID_IDENTIFIERS = (True, False, 0, -1, 1.0, "1", None)

AUDITED_TABLES = (
    "recommendation_runs",
    "recommendation_assessments",
    "recommendation_profile_state",
    "matching_runs",
    "matching_assessments",
    "matching_profile_state",
)

#: A different, well-formed SHA-256 digest. Valid in shape, wrong in content.
OTHER_DIGEST = "0" * 64


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "recommendation-persistence-audit.db"


@pytest.fixture
def database(database_path):
    connection = connect_database(database_path)
    apply_migrations(connection)
    yield connection
    connection.close()


def matching_fixture(connection, email, suffixes="abc"):
    """One profile, its postings, and the Matching run they were matched by.

    The Matching batch comes from Phase 4's *audit* fixture rather than from its
    persistence fixture, because only that one carries the per-assessment
    semantic corpus and model fingerprints Phase 4's own audit checks. A source
    Matching run that failed its own audit would make every provenance assertion
    below ambiguous.
    """
    profile_id = ensure_user_profile(connection, email).profile_id
    ids = tuple(add_opportunity(connection, f"{email}-{item}") for item in suffixes)
    connection.commit()
    matching = store_matching_batch(
        connection,
        profile_id,
        make_matching_batch(profile_id, ids),
        selection_version=SELECTION_VERSION,
    )
    connection.commit()
    return Fixture(connection, profile_id, ids, matching)


@pytest.fixture
def synced(database):
    return matching_fixture(database, TEST_ONLY_EMAIL)


#: Scores that make the persisted ranking differ from the opportunity-id order,
#: and give the three assessments three *different* content digests. Without
#: them every assessment over identical inputs digests identically — Phase 9A's
#: content payload holds no opportunity id — and a ranking test could pass on a
#: coincidence rather than on the order.
RANKING_SCORES = {0: 0.1, 1: 0.9}


def ranked_batch(fixture, opportunity_ids=None):
    ids = opportunity_ids or fixture.opportunity_ids
    scores = {
        ids[index]: value
        for index, value in RANKING_SCORES.items()
        if index < len(ids)
    }
    return make_recommendation_batch(fixture.profile_id, ids, scores)


# --------------------------------------------------------------------------
# corruption helpers
#
# Each writes exactly one broken value into a temporary database that a real
# component built first, so what a test proves is the audit's reading of that
# value rather than an accident of how the row was created.
# --------------------------------------------------------------------------


#: Each test-only relaxation, as (value while relaxed, value to restore).
RELAXATIONS = {
    "ignore_check_constraints": ("ON", "OFF"),
    "foreign_keys": ("OFF", "ON"),
}


@contextmanager
def relaxed(connection, *pragmas):
    """Switch test-only PRAGMAs for one edit, and always switch them back.

    `0026`'s CHECK constraints and its foreign keys protect the writes this
    build makes. They do not protect an audit from a restore, a hand-edit or a
    future migration, so producing such a row at all needs the guard off for
    exactly one statement — here, and never in production, which issues no
    PRAGMA at all and is held to that structurally further down.
    """
    for pragma in pragmas:
        connection.execute(f"PRAGMA {pragma}={RELAXATIONS[pragma][0]}")
    try:
        yield
    finally:
        for pragma in pragmas:
            connection.execute(f"PRAGMA {pragma}={RELAXATIONS[pragma][1]}")


def corrupt_run(connection, run_id, **columns):
    assignments = ",".join(f"{name}=?" for name in columns)
    connection.execute(
        f"UPDATE recommendation_runs SET {assignments} WHERE id=?",
        (*columns.values(), run_id),
    )
    connection.commit()


def corrupt_assessment(connection, run_id, at_rank, **columns):
    """Break one value on the assessment currently stored at `at_rank`."""
    assignments = ",".join(f"{name}=?" for name in columns)
    connection.execute(
        f"UPDATE recommendation_assessments SET {assignments}"
        " WHERE run_id=? AND rank_position=?",
        (*columns.values(), run_id, at_rank),
    )
    connection.commit()


def corrupt_matching_run(connection, run_id, **columns):
    assignments = ",".join(f"{name}=?" for name in columns)
    connection.execute(
        f"UPDATE matching_runs SET {assignments} WHERE id=?",
        (*columns.values(), run_id),
    )
    connection.commit()


def force_state(connection, profile_id, **columns):
    """Write one state row directly, bypassing 9B.1 — which only ever writes READY.

    Phase 9B.3 has not been built yet, so an INCOMPLETE row cannot be produced
    through a supported entry point today. This is a fixture, not a shortcut past
    a boundary: it writes exactly the row 9B.3's contract describes so the audit
    can be held to that contract now.
    """
    assignments = ",".join(f"{name}=?" for name in columns)
    connection.execute(
        f"UPDATE recommendation_profile_state SET {assignments} WHERE profile_id=?",
        (*columns.values(), profile_id),
    )
    connection.commit()


def insert_state(connection, profile_id, state, readiness_issues_json):
    connection.execute(
        """INSERT INTO recommendation_profile_state
             (profile_id,state,current_run_id,persistence_version,
              input_assembly_version,readiness_issues_json)
           VALUES (?,?,NULL,?,?,?)""",
        (
            profile_id,
            state,
            RECOMMENDATION_PERSISTENCE_VERSION,
            RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
            readiness_issues_json,
        ),
    )
    connection.commit()


def assessment_payload(connection, run_id, rank_position):
    return json.loads(
        connection.execute(
            "SELECT assessment_payload_json FROM recommendation_assessments"
            " WHERE run_id=? AND rank_position=?",
            (run_id, rank_position),
        ).fetchone()[0]
    )


def batch_payload(connection, run_id):
    return json.loads(
        connection.execute(
            "SELECT batch_payload_json FROM recommendation_runs WHERE id=?",
            (run_id,),
        ).fetchone()[0]
    )


def stored_readiness(connection, profile_id):
    """The readiness text exactly as SQLite holds it, not as a test built it."""
    return connection.execute(
        "SELECT readiness_issues_json FROM recommendation_profile_state"
        " WHERE profile_id=?",
        (profile_id,),
    ).fetchone()[0]


def readiness_json(*issues):
    """The canonical array 9B.3 will write, built from `(code, message, id)`."""
    return canonical_json(
        [
            {"code": code, "message": message, "opportunity_id": opportunity_id}
            for code, message, opportunity_id in issues
        ]
    )


ONE_ISSUE = readiness_json(
    (RecommendationReadinessIssueCode.MATCHING_NOT_READY.value, "no matching run", None)
)


def codes(report):
    return [issue.code for issue in report.issues]


def rows_of(connection):
    return {
        table: connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in AUDITED_TABLES
    }


# --------------------------------------------------------------------------
# arguments, existence, and the healthy states
# --------------------------------------------------------------------------


def test_profile_id_must_be_a_positive_integer_and_never_a_boolean(synced):
    stored = synced.store(ranked_batch(synced))
    # Both really exist, so nothing below can pass merely by being absent.
    assert synced.profile_id == 1 and stored.run_id == 1

    for invalid in INVALID_IDENTIFIERS:
        with pytest.raises(
            RecommendationPersistenceAuditError, match="positive integer"
        ):
            audit_recommendation_profile_history(synced.connection, invalid)

    # The reachable id really is reachable: the refusals above are about the
    # argument's type, not about a broken audit.
    assert audit_recommendation_profile_history(synced.connection, 1).ok is True


def test_a_profile_that_does_not_exist_cannot_be_audited(synced):
    with pytest.raises(
        RecommendationPersistenceAuditError, match="profile 99999 does not exist"
    ):
        audit_recommendation_profile_history(synced.connection, 99999)


def test_a_sqlite_failure_is_reported_as_an_audit_error_with_its_cause(tmp_path):
    """`sqlite3.Error` is not this module's public contract; the cause survives."""
    empty = connect_database(tmp_path / "no-migrations.db")
    try:
        with pytest.raises(RecommendationPersistenceAuditError) as raised:
            audit_recommendation_profile_history(empty, 1)
        assert isinstance(raised.value.__cause__, sqlite3.Error)
        assert "cannot read" in str(raised.value)
    finally:
        empty.close()


def test_a_profile_with_no_state_and_no_history_audits_as_a_clean_not_synced(synced):
    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.audit_version == RECOMMENDATION_PERSISTENCE_AUDIT_VERSION
    assert report.profile_id == synced.profile_id
    assert report.status == "NOT_SYNCED"
    assert report.current_run_id is None
    assert (report.run_count, report.audited_run_count) == (0, 0)
    assert report.assessment_count == 0
    assert report.runs == ()
    assert report.issues == ()
    assert report.ok is True
    assert len(report.audit_fingerprint) == 64


def test_a_healthy_single_run_history_audits_clean_and_describes_itself(synced):
    batch = ranked_batch(synced)
    stored = synced.store(batch)
    synced.connection.commit()

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is True and report.issues == ()
    assert report.status == "READY"
    assert report.current_run_id == stored.run_id
    assert (report.run_count, report.audited_run_count) == (1, 1)
    assert report.assessment_count == 3

    run = report.runs[0]
    assert run.run_id == stored.run_id
    assert run.ok is True and run.issues == ()
    assert run.run_fingerprint == stored.run_fingerprint
    assert run.batch_fingerprint == batch.batch_fingerprint
    assert run.source_matching_run_id == synced.matching_run_id
    assert run.source_matching_run_fingerprint == synced.matching_run_fingerprint
    assert run.assessment_count == 3
    assert [item.rank_position for item in run.ranked_assessments] == [1, 2, 3]
    assert [item.opportunity_id for item in run.ranked_assessments] == [
        item.opportunity_id for item in batch.assessments
    ]
    assert [item.assessment_fingerprint for item in run.ranked_assessments] == [
        item.assessment_fingerprint for item in batch.assessments
    ]


def test_repeating_the_audit_over_an_unchanged_database_is_identical(synced):
    """Determinism is the whole contract: same state, same report, same digest."""
    synced.store(ranked_batch(synced))
    synced.store(ranked_batch(synced, synced.opportunity_ids[:2]))
    synced.connection.commit()

    first = audit_recommendation_profile_history(synced.connection, synced.profile_id)
    second = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert first == second
    assert first.audit_fingerprint == second.audit_fingerprint


def test_a_multi_run_history_is_audited_whole_newest_first(synced):
    older = synced.store(ranked_batch(synced, synced.opportunity_ids[:2]))
    newer = synced.store(ranked_batch(synced))
    synced.connection.commit()

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is True and report.issues == ()
    # Every persisted run is audited, not only the current one, and history is
    # ordered newest first — the same deterministic order 9B.2a lists it in.
    assert [item.run_id for item in report.runs] == [newer.run_id, older.run_id]
    assert (report.run_count, report.audited_run_count) == (2, 2)
    assert report.assessment_count == 5
    assert report.current_run_id == newer.run_id


def test_the_persisted_rank_order_is_the_order_audited_and_is_never_re_sorted(synced):
    """The audit reads the ranking; it does not reconstruct one."""
    batch = ranked_batch(synced)
    stored = synced.store(batch)
    synced.connection.commit()
    persisted = synced.connection.execute(
        "SELECT opportunity_id FROM recommendation_assessments WHERE run_id=?"
        " ORDER BY rank_position ASC",
        (stored.run_id,),
    ).fetchall()
    order = [row[0] for row in persisted]
    # The fixture exists to make this true; without it the assertion below would
    # hold for a re-sorting audit too.
    assert order != sorted(order)

    run = audit_recommendation_profile_history(
        synced.connection, synced.profile_id
    ).runs[0]

    assert [item.opportunity_id for item in run.ranked_assessments] == order


# --------------------------------------------------------------------------
# assessment payloads and the columns duplicated out of them
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("{", "ASSESSMENT_PAYLOAD_INVALID_JSON"),
        ("[]", "ASSESSMENT_PAYLOAD_NOT_OBJECT"),
    ],
)
def test_an_assessment_payload_that_is_not_a_json_object_is_reported(
    synced, payload, expected
):
    """And is reported, not raised: the rest of the history is still audited."""
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_assessment(
        synced.connection, stored.run_id, 2, assessment_payload_json=payload
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert expected in codes(report)
    found = [item for item in report.issues if item.code == expected]
    assert len(found) == 1
    assert found[0].run_id == stored.run_id
    assert found[0].scope == "ASSESSMENT"
    # The two intact assessments were still audited, and the run still reports
    # its own identity, so one broken envelope did not end the audit.
    assert len(report.runs[0].ranked_assessments) == 3
    assert report.runs[0].run_fingerprint == stored.run_fingerprint


def test_a_non_canonical_assessment_payload_is_reported_without_a_false_digest(synced):
    """Re-spacing the same object changes the bytes, not the canonical digest."""
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    payload = assessment_payload(synced.connection, stored.run_id, 1)
    corrupt_assessment(
        synced.connection,
        stored.run_id,
        1,
        assessment_payload_json=json.dumps(payload, indent=None, sort_keys=False),
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "ASSESSMENT_PAYLOAD_NOT_CANONICAL" in codes(report)
    # The digest is over the canonical form of what was stored, so re-spacing
    # alone must not be reported as a fingerprint mismatch too.
    assert "ASSESSMENT_FINGERPRINT_MISMATCH" not in codes(report)


def test_an_assessment_fingerprint_that_does_not_hash_its_payload_is_reported(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_assessment(
        synced.connection, stored.run_id, 1, assessment_fingerprint=OTHER_DIGEST
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "ASSESSMENT_FINGERPRINT_MISMATCH" in codes(report)
    # The digest is an input to both derived statements, so both move with it.
    assert "BATCH_CONTENT_MISMATCH" in codes(report)
    assert "RUN_FINGERPRINT_MISMATCH" in codes(report)


def test_a_malformed_assessment_fingerprint_stops_the_derived_statements(synced):
    """An unusable prerequisite is reported once, not echoed as three failures."""
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    with relaxed(synced.connection, "ignore_check_constraints"):
        corrupt_assessment(
            synced.connection, stored.run_id, 1, assessment_fingerprint="not-a-digest"
        )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "ASSESSMENT_FINGERPRINT_INVALID" in codes(report)
    assert "BATCH_CONTENT_MISMATCH" not in codes(report)
    assert "RUN_FINGERPRINT_MISMATCH" not in codes(report)
    assert report.runs[0].ranked_assessments[0].assessment_fingerprint is None


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        ({"disposition": "UNCERTAIN"}, "ASSESSMENT_DISPOSITION_MISMATCH"),
        ({"recommendation_score": 0.123}, "ASSESSMENT_SCORE_MISMATCH"),
        ({"evidence_coverage": 0.5}, "ASSESSMENT_EVIDENCE_COVERAGE_MISMATCH"),
    ],
)
def test_a_duplicated_column_that_no_longer_agrees_with_the_payload(
    synced, columns, expected
):
    """The columns exist so a reader can filter without decoding every payload.

    They are only useful while they still say what the payload says, and each of
    these edits is one `0026` would accept — which is exactly why the audit has
    to be the layer that refuses it.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_assessment(synced.connection, stored.run_id, 1, **columns)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert expected in codes(report)
    found = [item for item in report.issues if item.code == expected]
    assert len(found) == 1 and found[0].opportunity_id is not None
    # The payload itself was untouched, so its own digest still holds.
    assert "ASSESSMENT_FINGERPRINT_MISMATCH" not in codes(report)


def test_an_assessment_payload_without_a_result_object_is_reported_not_crashed(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    payload = assessment_payload(synced.connection, stored.run_id, 1)
    payload.pop("result")
    corrupt_assessment(
        synced.connection,
        stored.run_id,
        1,
        assessment_payload_json=canonical_json(payload),
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "ASSESSMENT_RESULT_SECTION_INVALID" in codes(report)
    # No `KeyError` leaked out as the three column comparisons were skipped.
    assert "ASSESSMENT_DISPOSITION_MISMATCH" not in codes(report)


def test_an_assessment_payload_without_a_versions_object_is_reported_not_crashed(
    synced,
):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    payload = assessment_payload(synced.connection, stored.run_id, 1)
    payload["versions"] = "not an object"
    corrupt_assessment(
        synced.connection,
        stored.run_id,
        1,
        assessment_payload_json=canonical_json(payload),
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "ASSESSMENT_VERSIONS_SECTION_INVALID" in codes(report)
    assert "ASSESSMENT_ENGINE_VERSION_MISMATCH" not in codes(report)


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("recommendation_engine_version", "ASSESSMENT_ENGINE_VERSION_MISMATCH"),
        ("recommendation_rules_version", "ASSESSMENT_RULES_VERSION_MISMATCH"),
    ],
)
def test_an_assessment_whose_provenance_disagrees_with_its_run(synced, column, expected):
    """Each assessment carries the engine and rules it was produced under.

    Moving the run's own version is what makes them disagree, and it moves the
    two derived statements with it — both are asserted rather than left implied,
    because a test that named only the version mismatch would not notice if the
    audit had stopped checking the rest.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_run(synced.connection, stored.run_id, **{column: "moved-v9"})

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert [item.code for item in report.issues].count(expected) == 3
    assert "BATCH_CONTENT_MISMATCH" in codes(report)
    assert "RUN_FINGERPRINT_MISMATCH" in codes(report)


def test_a_stored_assessment_count_that_disagrees_with_the_rows_is_reported(synced):
    """The run's declared size against the rows it actually owns.

    The operational run fingerprint is derived from the ranked rows rather than
    from this column, so it is untouched here — which is what makes the finding
    specific instead of one symptom among many.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_run(synced.connection, stored.run_id, assessment_count=2)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "ASSESSMENT_COUNT_MISMATCH" in codes(report)
    assert "BATCH_CONTENT_MISMATCH" in codes(report)
    assert "RUN_FINGERPRINT_MISMATCH" not in codes(report)
    assert report.runs[0].assessment_count == 3


def test_a_deleted_assessment_row_is_reported_rather_than_silently_shortening(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    synced.connection.execute(
        "DELETE FROM recommendation_assessments WHERE run_id=? AND rank_position=3",
        (stored.run_id,),
    )
    synced.connection.commit()

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "ASSESSMENT_COUNT_MISMATCH" in codes(report)
    assert report.assessment_count == 2


@pytest.mark.parametrize("shift", [1, 3])
def test_a_ranking_that_is_not_exactly_one_to_n_is_reported(synced, shift):
    """A gap still reads as an ordered list, and shifts every later position."""
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    # Rank 3 becomes 4 (a gap) or the whole ranking starts at 2 (a wrong start).
    if shift == 1:
        corrupt_assessment(synced.connection, stored.run_id, 3, rank_position=4)
    else:
        # Highest first: `UNIQUE (run_id, rank_position)` is checked row by
        # row, so shifting the whole ranking upward has to leave no collision
        # behind at any intermediate step.
        for position in (3, 2, 1):
            corrupt_assessment(
                synced.connection, stored.run_id, position, rank_position=position + 1
            )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "ASSESSMENT_RANK_NOT_CONTIGUOUS" in codes(report)
    # An unreproducible ranking is reported once; the two statements derived
    # from it are skipped rather than echoed as further failures.
    assert "BATCH_CONTENT_MISMATCH" not in codes(report)
    assert "RUN_FINGERPRINT_MISMATCH" not in codes(report)


def test_a_reversed_ranking_is_caught_by_the_batch_and_run_statements(synced):
    """Every row stays individually valid; only the *order* moved.

    This is the corruption a content-only audit cannot see and the one the
    ranking-aware statements exist for: no row is malformed, the count is right,
    the positions are still exactly 1..N, and each payload still hashes to its
    own digest — yet the run no longer recommends what it recommended.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    before = audit_recommendation_profile_history(synced.connection, synced.profile_id)
    assert before.ok is True

    # A three-step swap through a free position, because
    # `UNIQUE (run_id, rank_position)` refuses the intermediate state of a
    # direct exchange and `0026` refuses position 0 outright.
    for source, target in ((1, 99), (3, 1), (99, 3)):
        corrupt_assessment(
            synced.connection, stored.run_id, source, rank_position=target
        )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "ASSESSMENT_RANK_NOT_CONTIGUOUS" not in codes(report)
    assert "ASSESSMENT_FINGERPRINT_MISMATCH" not in codes(report)
    assert "BATCH_CONTENT_MISMATCH" in codes(report)
    assert "RUN_FINGERPRINT_MISMATCH" in codes(report)
    assert [item.opportunity_id for item in report.runs[0].ranked_assessments] != [
        item.opportunity_id for item in before.runs[0].ranked_assessments
    ]


# --------------------------------------------------------------------------
# the batch content statement and the operational run identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("{", "BATCH_PAYLOAD_INVALID_JSON"),
        ("[]", "BATCH_PAYLOAD_NOT_OBJECT"),
    ],
)
def test_a_batch_payload_that_is_not_a_json_object_is_reported(
    synced, payload, expected
):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_run(synced.connection, stored.run_id, batch_payload_json=payload)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert expected in codes(report)
    # An undecodable envelope says nothing about the digest it should hash to,
    # so no second, derived complaint is invented about it.
    assert "BATCH_FINGERPRINT_MISMATCH" not in codes(report)
    assert "BATCH_CONTENT_MISMATCH" not in codes(report)
    # And the run's own identity was still recomputed and still holds.
    assert "RUN_FINGERPRINT_MISMATCH" not in codes(report)


def test_a_non_canonical_batch_payload_is_reported_without_a_false_digest(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    payload = batch_payload(synced.connection, stored.run_id)
    corrupt_run(
        synced.connection,
        stored.run_id,
        batch_payload_json=json.dumps(payload, sort_keys=False),
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["BATCH_PAYLOAD_NOT_CANONICAL"]


def test_a_batch_payload_that_no_longer_states_the_stored_rows_is_reported(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    payload = batch_payload(synced.connection, stored.run_id)
    payload["assessment_count"] = 99
    corrupt_run(
        synced.connection, stored.run_id, batch_payload_json=canonical_json(payload)
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "BATCH_CONTENT_MISMATCH" in codes(report)
    # The stored digest is over the stored bytes, and the bytes moved.
    assert "BATCH_FINGERPRINT_MISMATCH" in codes(report)


def test_a_batch_payload_whose_ranked_digests_were_re_sorted_is_reported(synced):
    """Sorting the ranked digests is a re-ranking, and must not audit clean.

    The three digests are all present and all correct; only their order differs.
    An audit that sorted them before comparing — alphabetically, or by
    opportunity id — would call this history intact.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    payload = batch_payload(synced.connection, stored.run_id)
    ranked = payload["ranked_assessment_fingerprints"]
    assert ranked != sorted(ranked)
    payload["ranked_assessment_fingerprints"] = sorted(ranked)
    corrupt_run(
        synced.connection, stored.run_id, batch_payload_json=canonical_json(payload)
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "BATCH_CONTENT_MISMATCH" in codes(report)


def test_a_batch_fingerprint_that_does_not_hash_its_payload_is_reported(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_run(synced.connection, stored.run_id, batch_fingerprint=OTHER_DIGEST)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "BATCH_FINGERPRINT_MISMATCH" in codes(report)
    # The content digest is an input to the operational identity, never a
    # substitute for it — so moving it moves the run fingerprint too.
    assert "RUN_FINGERPRINT_MISMATCH" in codes(report)
    assert report.runs[0].batch_fingerprint == OTHER_DIGEST


def test_a_malformed_batch_fingerprint_stops_the_statements_derived_from_it(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    with relaxed(synced.connection, "ignore_check_constraints"):
        corrupt_run(synced.connection, stored.run_id, batch_fingerprint="nope")

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "RUN_BATCH_FINGERPRINT_INVALID" in codes(report)
    assert "BATCH_FINGERPRINT_MISMATCH" not in codes(report)
    assert "RUN_FINGERPRINT_MISMATCH" not in codes(report)
    assert report.runs[0].batch_fingerprint is None


def test_a_run_fingerprint_that_is_not_the_runs_operational_identity_is_reported(
    synced,
):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_run(synced.connection, stored.run_id, run_fingerprint=OTHER_DIGEST)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["RUN_FINGERPRINT_MISMATCH"]
    assert report.runs[0].run_fingerprint == OTHER_DIGEST


def test_a_malformed_run_fingerprint_is_reported_as_a_shape_not_a_mismatch(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    with relaxed(synced.connection, "ignore_check_constraints"):
        corrupt_run(synced.connection, stored.run_id, run_fingerprint="A" * 64)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["RUN_FINGERPRINT_INVALID"]
    assert report.runs[0].run_fingerprint is None


# --------------------------------------------------------------------------
# source Matching provenance
# --------------------------------------------------------------------------


def test_a_source_matching_run_that_does_not_exist_is_reported(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    with relaxed(synced.connection, "foreign_keys"):
        corrupt_run(synced.connection, stored.run_id, source_matching_run_id=99999)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "SOURCE_MATCHING_RUN_MISSING" in codes(report)
    # The source run id is part of the operational identity, so it moved too.
    assert "RUN_FINGERPRINT_MISMATCH" in codes(report)


def test_a_source_matching_run_owned_by_another_profile_is_reported(database, synced):
    """The composite foreign key forbids this; a restore or hand-edit does not."""
    other = matching_fixture(database, TEST_ONLY_OTHER_EMAIL, suffixes="de")
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    with relaxed(database, "foreign_keys"):
        corrupt_run(
            database, stored.run_id, source_matching_run_id=other.matching_run_id
        )

    report = audit_recommendation_profile_history(database, synced.profile_id)

    assert report.ok is False
    assert "SOURCE_MATCHING_RUN_PROFILE_MISMATCH" in codes(report)
    # Nothing is said about the other profile's run beyond the reference itself:
    # auditing another profile's history from here is not this audit's business.
    assert "SOURCE_MATCHING_RUN_INVALID" not in codes(report)


def test_a_copied_source_matching_fingerprint_that_no_longer_matches_is_reported(
    synced,
):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_run(
        synced.connection, stored.run_id, source_matching_run_fingerprint=OTHER_DIGEST
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "SOURCE_MATCHING_RUN_FINGERPRINT_MISMATCH" in codes(report)
    assert "RUN_FINGERPRINT_MISMATCH" in codes(report)


def test_a_referenced_matching_run_that_fails_its_own_audit_is_reported(synced):
    """Phase 4's audit is the authority on a Matching run's internal validity.

    The Matching run's *own* fingerprint is left alone, so the copied provenance
    still matches: what is broken is the run's internal content, and only Phase
    4's audit can say so.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_matching_run(
        synced.connection, synced.matching_run_id, batch_payload_json="{}"
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "SOURCE_MATCHING_RUN_INVALID" in codes(report)
    assert "SOURCE_MATCHING_RUN_FINGERPRINT_MISMATCH" not in codes(report)
    # And the recommendation run's own content is untouched: this is a
    # provenance finding, not a recommendation-integrity one.
    assert "RUN_FINGERPRINT_MISMATCH" not in codes(report)
    assert "BATCH_CONTENT_MISMATCH" not in codes(report)


def test_corruption_in_an_unrelated_matching_run_does_not_poison_a_healthy_source(
    synced,
):
    """Per-run, never per-profile: Matching's global `ok` is not the question.

    `R1` cites `M1`. `M2` exists in the same profile's Matching history and is
    corrupt, so Phase 4's report for this profile is `ok = False` — and using
    that report-level verdict as the provenance test would declare `R1` corrupt
    on the strength of a run it never touched.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    second_matching = store_matching_batch(
        synced.connection,
        synced.profile_id,
        make_matching_batch(synced.profile_id, synced.opportunity_ids[:2]),
        selection_version=SELECTION_VERSION,
    )
    synced.connection.commit()
    assert second_matching.run_id != synced.matching_run_id
    corrupt_matching_run(
        synced.connection, second_matching.run_id, batch_payload_json="{}"
    )

    from services.collector.matching import audit_matching_profile_history

    matching_report = audit_matching_profile_history(
        synced.connection, synced.profile_id
    )
    assert matching_report.ok is False
    per_run = {item.run_id: item.ok for item in matching_report.runs}
    assert per_run == {synced.matching_run_id: True, second_matching.run_id: False}

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is True and report.issues == ()
    assert report.runs[0].source_matching_run_id == synced.matching_run_id
    assert stored.run_id == report.runs[0].run_id


# --------------------------------------------------------------------------
# the historical provenance rule
#
# The one place where an integrity audit could quietly become a freshness check,
# and the reason this test is explicit rather than implied by the ones above.
# --------------------------------------------------------------------------


def current_matching_run_id(connection, profile_id):
    return connection.execute(
        "SELECT current_run_id FROM matching_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()[0]


def test_a_superseded_source_matching_run_leaves_its_recommendation_valid(synced):
    """M1 -> R1, then M2 becomes current: R1 is still exactly what it was.

    `R1` *was* produced over `M1`. A later Matching run cannot falsify a fact
    about the past, and requiring
    `recommendation_runs.source_matching_run_id == matching_profile_state.current_run_id`
    would turn every superseded run in every history into corruption. Whether a
    *new* recommendation is due is Phase 9B.3's question and is not asked here.

    The three steps are one test on purpose: the same `R1`, audited across a
    superseding M2, across a corrupt unrelated M2, and finally across a corrupt
    M1 — so the boundary between "not fresh" and "not intact" is drawn by the
    audit's own answers rather than by three separate fixtures.
    """
    first = synced.store(ranked_batch(synced))
    synced.connection.commit()
    before = audit_recommendation_profile_history(synced.connection, synced.profile_id)
    assert before.ok is True and before.issues == ()

    # -- M2 is persisted and becomes the current Matching run ---------------
    # No new recommendation run is produced: this is precisely the window in
    # which a freshness check masquerading as an integrity check would fire.
    second_matching = store_matching_batch(
        synced.connection,
        synced.profile_id,
        make_matching_batch(synced.profile_id, synced.opportunity_ids[:2]),
        selection_version=SELECTION_VERSION,
    )
    synced.connection.commit()
    assert current_matching_run_id(
        synced.connection, synced.profile_id
    ) == second_matching.run_id != synced.matching_run_id

    superseded = audit_recommendation_profile_history(
        synced.connection, synced.profile_id
    )
    assert superseded.ok is True and superseded.issues == ()
    assert superseded.runs[0].source_matching_run_id == synced.matching_run_id
    # Nothing about the audit changed either: the report is the one it was.
    assert superseded == before

    # -- M2, the unrelated current run, is corrupted ------------------------
    corrupt_matching_run(
        synced.connection, second_matching.run_id, batch_payload_json="{}"
    )
    isolated = audit_recommendation_profile_history(
        synced.connection, synced.profile_id
    )
    assert isolated.ok is True and isolated.issues == ()
    assert isolated == before

    # -- M1, the run R1 actually cites, is corrupted ------------------------
    corrupt_matching_run(
        synced.connection, synced.matching_run_id, batch_payload_json="{}"
    )
    broken = audit_recommendation_profile_history(synced.connection, synced.profile_id)
    assert broken.ok is False
    assert "SOURCE_MATCHING_RUN_INVALID" in codes(broken)
    assert [item.run_id for item in broken.issues] == [first.run_id]


# --------------------------------------------------------------------------
# profile state
# --------------------------------------------------------------------------


def test_recommendation_history_without_a_state_row_is_reported(synced):
    """NOT_SYNCED is only honest when nothing has ever run."""
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    synced.connection.execute(
        "DELETE FROM recommendation_profile_state WHERE profile_id=?",
        (synced.profile_id,),
    )
    synced.connection.commit()

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["STATE_ROW_MISSING"]
    assert report.status == "NOT_SYNCED"
    assert report.current_run_id is None
    # The history itself is intact and was still audited in full.
    assert report.run_count == 1 and report.runs[0].run_id == stored.run_id
    assert report.runs[0].ok is True


def test_a_ready_state_naming_a_run_outside_the_profiles_history_is_reported(
    database, synced
):
    other = matching_fixture(database, TEST_ONLY_OTHER_EMAIL, suffixes="de")
    foreign = other.store(ranked_batch(other, other.opportunity_ids))
    synced.store(ranked_batch(synced))
    database.commit()
    with relaxed(database, "foreign_keys"):
        force_state(database, synced.profile_id, current_run_id=foreign.run_id)

    report = audit_recommendation_profile_history(database, synced.profile_id)

    assert report.ok is False
    assert "STATE_CURRENT_RUN_UNKNOWN" in codes(report)
    assert report.status == "READY"
    assert report.current_run_id == foreign.run_id


def test_a_ready_state_carrying_readiness_issues_is_reported(synced):
    """READY means nothing stopped the synchronization, and says so exactly."""
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    force_state(synced.connection, synced.profile_id, readiness_issues_json=ONE_ISSUE)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["STATE_READY_HAS_READINESS_ISSUES"]


def test_a_ready_state_whose_versions_disagree_with_its_current_run_is_reported(synced):
    """State and run must agree with *each other*, never with today's constants."""
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    force_state(
        synced.connection, synced.profile_id, input_assembly_version="assembly-v9"
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["STATE_VERSION_MISMATCH"]


def test_stored_versions_are_never_compared_to_the_constants_this_build_ships(synced):
    """An old history read under a newer build is history, not corruption.

    Both the state row and the run it names are moved to the *same* older
    version. Nothing internal disagrees, so nothing is reported — an audit that
    compared either against `RECOMMENDATION_INPUT_ASSEMBLY_VERSION` would call
    this whole profile corrupt, which is version obsolescence and belongs to
    Phase 9B.3.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    corrupt_run(
        synced.connection, stored.run_id, input_assembly_version="assembly-v0"
    )
    force_state(
        synced.connection, synced.profile_id, input_assembly_version="assembly-v0"
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    # The run fingerprint does move, because the assembly version is part of the
    # operational identity — that is a *content* statement, and it is the only
    # thing reported. No version-obsolescence finding appears beside it.
    assert codes(report) == ["RUN_FINGERPRINT_MISMATCH"]
    assert "STATE_VERSION_MISMATCH" not in codes(report)


def test_an_incomplete_state_with_canonical_issues_and_no_history_is_valid(database):
    """INCOMPLETE names no run and says what stopped the synchronization."""
    profile_id = ensure_user_profile(database, TEST_ONLY_EMAIL).profile_id
    database.commit()
    insert_state(database, profile_id, "INCOMPLETE", ONE_ISSUE)

    report = audit_recommendation_profile_history(database, profile_id)

    assert report.ok is True and report.issues == ()
    assert report.status == "INCOMPLETE"
    assert report.current_run_id is None
    assert report.run_count == 0


def test_an_incomplete_state_beside_an_older_recommendation_history_is_valid(synced):
    """The last synchronization failed; the ones before it did not.

    Those runs are still exactly what was recommended then, and an audit that
    called a whole history corrupt because the newest attempt failed would be
    destroying evidence rather than reporting it.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    force_state(
        synced.connection,
        synced.profile_id,
        state="INCOMPLETE",
        current_run_id=None,
        readiness_issues_json=ONE_ISSUE,
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is True and report.issues == ()
    assert report.status == "INCOMPLETE"
    assert report.current_run_id is None
    assert report.run_count == 1
    assert report.runs[0].run_id == stored.run_id and report.runs[0].ok is True


def incomplete_with(connection, profile_id, readiness_issues_json):
    force_state(
        connection,
        profile_id,
        state="INCOMPLETE",
        current_run_id=None,
        readiness_issues_json=readiness_issues_json,
    )


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("{", "READINESS_ISSUES_INVALID_JSON"),
        ('{"code":"MATCHING_NOT_READY"}', "READINESS_ISSUES_NOT_ARRAY"),
        ("[]", "READINESS_ISSUES_EMPTY"),
    ],
)
def test_an_incomplete_state_whose_readiness_array_is_unusable(synced, stored, expected):
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    incomplete_with(synced.connection, synced.profile_id, stored)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert expected in codes(report)
    assert report.status == "INCOMPLETE"


def test_a_non_canonical_readiness_array_is_reported(synced):
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    incomplete_with(
        synced.connection,
        synced.profile_id,
        json.dumps(json.loads(ONE_ISSUE), sort_keys=False, indent=None),
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["READINESS_ISSUES_NOT_CANONICAL"]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {"code": "NOT_A_REAL_CODE", "message": "m", "opportunity_id": None},
            "READINESS_ISSUE_CODE_UNKNOWN",
        ),
        (
            {"code": "MATCHING_NOT_READY", "message": "  ", "opportunity_id": None},
            "READINESS_ISSUE_MESSAGE_INVALID",
        ),
        (
            {"code": "MATCHING_NOT_READY", "message": "m", "opportunity_id": True},
            "READINESS_ISSUE_OPPORTUNITY_ID_INVALID",
        ),
        (
            {"code": "MATCHING_NOT_READY", "message": "m", "opportunity_id": 0},
            "READINESS_ISSUE_OPPORTUNITY_ID_INVALID",
        ),
        (
            {"code": "MATCHING_NOT_READY", "message": "m"},
            "READINESS_ISSUE_MALFORMED",
        ),
        ("MATCHING_NOT_READY", "READINESS_ISSUE_MALFORMED"),
    ],
)
def test_a_readiness_issue_outside_the_stored_contract_is_reported(
    synced, entry, expected
):
    """`True` is an `int` in Python, so a boolean posting id must be refused.

    The vocabulary is `input_assembly.RecommendationReadinessIssueCode` and no
    second one is invented here: a code this build cannot name is a code the
    audit cannot honestly claim to be reporting.
    """
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    incomplete_with(synced.connection, synced.profile_id, canonical_json([entry]))

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert expected in codes(report)


def test_readiness_issues_stored_out_of_assembly_order_are_reported(synced):
    """The order is a fact about the stored array, and is verified, never repaired.

    `input_assembly._incomplete` sorts profile-wide issues before per-opportunity
    ones, then by posting, then by code. Re-sorting what was read would hide
    exactly the corruption this detects.
    """
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    ordered = (
        (RecommendationReadinessIssueCode.MATCHING_NOT_READY.value, "profile", None),
        (
            RecommendationReadinessIssueCode.OPPORTUNITY_MISSING.value,
            "posting",
            synced.opportunity_ids[0],
        ),
    )
    incomplete_with(synced.connection, synced.profile_id, readiness_json(*ordered))
    assert (
        audit_recommendation_profile_history(
            synced.connection, synced.profile_id
        ).ok
        is True
    )

    incomplete_with(
        synced.connection, synced.profile_id, readiness_json(*reversed(ordered))
    )
    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["READINESS_ISSUES_NOT_ORDERED"]


def test_an_unknown_state_value_is_reported_rather_than_interpreted(synced):
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    with relaxed(synced.connection, "ignore_check_constraints"):
        force_state(synced.connection, synced.profile_id, state="ALMOST_READY")

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["STATE_UNKNOWN"]
    assert report.status == "UNKNOWN"


def test_an_incomplete_state_that_names_a_current_run_is_reported(synced):
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    with relaxed(synced.connection, "ignore_check_constraints"):
        force_state(synced.connection, synced.profile_id, state="INCOMPLETE")

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "STATE_INCOMPLETE_NAMES_RUN" in codes(report)
    assert report.current_run_id == stored.run_id


def test_blank_state_version_text_is_reported(synced):
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    with relaxed(synced.connection, "ignore_check_constraints"):
        force_state(synced.connection, synced.profile_id, persistence_version="  ")

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "STATE_PERSISTENCE_VERSION_INVALID" in codes(report)


# --------------------------------------------------------------------------
# deterministic ordering and the audit fingerprint
# --------------------------------------------------------------------------

#: The report's ordering contract, restated here so a change to it has to be a
#: deliberate change in two places. Profile findings first, then run-scoped ones,
#: then assessment-scoped ones; inside a scope, by run, then by posting, then by
#: code and detail. Each nullable identity contributes *two* components on
#: purpose: ordering `(run_id, ...)` tuples directly would compare `None` with
#: `int` and raise as soon as two findings of one code differed in whether they
#: name a run — which is exactly what an audit produces.
EXPECTED_SCOPE_ORDER = {"PROFILE": 0, "RUN": 1, "ASSESSMENT": 2}


def expected_sort_key(issue):
    return (
        EXPECTED_SCOPE_ORDER[issue.scope],
        issue.run_id is not None,
        issue.run_id if issue.run_id is not None else 0,
        issue.opportunity_id is not None,
        issue.opportunity_id if issue.opportunity_id is not None else 0,
        issue.code,
        issue.detail,
    )


def broadly_corrupted(fixture):
    """Two runs and a state row, each broken differently, all at once."""
    older = fixture.store(ranked_batch(fixture, fixture.opportunity_ids[:2]))
    newer = fixture.store(ranked_batch(fixture))
    fixture.connection.commit()
    corrupt_assessment(
        fixture.connection, newer.run_id, 1, assessment_payload_json="{"
    )
    corrupt_assessment(fixture.connection, newer.run_id, 2, disposition="UNCERTAIN")
    corrupt_run(fixture.connection, older.run_id, run_fingerprint=OTHER_DIGEST)
    corrupt_run(
        fixture.connection, older.run_id, source_matching_run_fingerprint=OTHER_DIGEST
    )
    force_state(
        fixture.connection, fixture.profile_id, input_assembly_version="assembly-v9"
    )
    return older, newer


def test_findings_from_many_simultaneous_corruptions_come_back_in_one_order(synced):
    """Several defects across two runs and the state row, ordered once and stably."""
    older, newer = broadly_corrupted(synced)

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)
    repeated = audit_recommendation_profile_history(
        synced.connection, synced.profile_id
    )

    assert report.ok is False
    assert len(report.issues) >= 5
    assert list(report.issues) == sorted(report.issues, key=expected_sort_key)
    assert report.issues == repeated.issues
    assert report.audit_fingerprint == repeated.audit_fingerprint
    # Profile findings really do lead, and both runs really were audited.
    assert report.issues[0].scope == "PROFILE"
    assert {item.run_id for item in report.issues if item.run_id is not None} == {
        older.run_id,
        newer.run_id,
    }
    # Each run's own findings are the subset of the report that names it, and
    # every run result carries its own verdict.
    assert all(item.ok is False for item in report.runs)
    for run in report.runs:
        assert list(run.issues) == sorted(run.issues, key=expected_sort_key)
        assert set(run.issues) <= set(report.issues)


# --------------------------------------------------------------------------
# the persisted state's own identity
#
# A finding is not the only thing that distinguishes two states. Two rows can
# both be entirely valid — no issue, `ok = True` — and still be different states,
# and a digest that could not tell them apart would be asserting an equivalence
# that does not hold. These are the cases where the audit has nothing to
# complain about and everything to distinguish.
# --------------------------------------------------------------------------


def incomplete_profile(connection, email=TEST_ONLY_EMAIL, issues=ONE_ISSUE):
    """One profile whose state is a valid INCOMPLETE, with no history at all."""
    profile_id = ensure_user_profile(connection, email).profile_id
    connection.commit()
    insert_state(connection, profile_id, "INCOMPLETE", issues)
    return profile_id


def fingerprint_of(connection, profile_id):
    """Audit twice, require the digest to be stable, and return it.

    Every call below goes through this, so repeatability is asserted at each
    step rather than once at the end: a digest that moved between two audits of
    one unchanged state would fail here before any comparison is made.
    """
    first = audit_recommendation_profile_history(connection, profile_id)
    second = audit_recommendation_profile_history(connection, profile_id)
    assert first == second
    assert first.audit_fingerprint == second.audit_fingerprint
    return first


def test_two_valid_incomplete_states_with_different_readiness_issues_differ(database):
    """The blocker case: both valid, both silent, and genuinely not the same state.

    `MATCHING_NOT_READY` and `STALE_MATCHING_COHORT` are both codes this build
    knows, so neither array produces a finding. If the digest were computed from
    `status`, `current_run_id` and the findings alone — as it was — these two
    would collide, and an operator comparing two audit reports would be told a
    state had not changed when it had.
    """
    profile_id = incomplete_profile(database)
    first = fingerprint_of(database, profile_id)
    assert first.ok is True and first.issues == ()
    assert first.status == "INCOMPLETE" and first.current_run_id is None

    # The row's clocks are not part of its identity, and reading the state row
    # more closely must not have quietly let them in.
    database.execute(
        "UPDATE recommendation_profile_state SET created_at='2020-01-01',"
        " updated_at='2020-01-01' WHERE profile_id=?",
        (profile_id,),
    )
    database.commit()
    assert (
        fingerprint_of(database, profile_id).audit_fingerprint
        == first.audit_fingerprint
    )

    other = readiness_json(
        (
            RecommendationReadinessIssueCode.STALE_MATCHING_COHORT.value,
            "cohort moved",
            None,
        )
    )
    assert other != ONE_ISSUE
    force_state(database, profile_id, readiness_issues_json=other)
    second = fingerprint_of(database, profile_id)

    # Still nothing to report: this is a difference between two *valid* states.
    assert second.ok is True and second.issues == ()
    assert second.status == "INCOMPLETE" and second.current_run_id is None
    assert second.run_count == first.run_count == 0
    assert second.audit_fingerprint != first.audit_fingerprint


def test_a_readiness_message_or_posting_alone_moves_the_state_digest(database):
    """The whole stored entry is the identity, not just its code.

    Two issues under one code can say different things about different postings,
    and both spellings are valid. The digest has to follow each of them.
    """
    code = RecommendationReadinessIssueCode.OPPORTUNITY_MISSING.value
    profile_id = incomplete_profile(
        database, issues=readiness_json((code, "posting is gone", 7))
    )
    baseline = fingerprint_of(database, profile_id).audit_fingerprint

    seen = {baseline}
    for entry in (
        (code, "posting was withdrawn", 7),
        (code, "posting is gone", 9),
    ):
        force_state(database, profile_id, readiness_issues_json=readiness_json(entry))
        report = fingerprint_of(database, profile_id)
        assert report.ok is True and report.issues == ()
        assert report.audit_fingerprint not in seen
        seen.add(report.audit_fingerprint)


def test_the_stored_order_of_valid_readiness_issues_is_part_of_the_state_digest(
    database,
):
    """Order is a fact about the array, so the digest may not sort it away.

    Only the *valid* ordering is used here — its reverse is reported as
    `READINESS_ISSUES_NOT_ORDERED` and would move the digest through the finding
    instead, which would prove nothing about the identity.
    """
    first = (RecommendationReadinessIssueCode.MATCHING_NOT_READY.value, "a", None)
    second = (
        RecommendationReadinessIssueCode.OPPORTUNITY_MISSING.value,
        "b",
        11,
    )
    profile_id = incomplete_profile(database, issues=readiness_json(first, second))
    both = fingerprint_of(database, profile_id)
    assert both.ok is True and both.issues == ()

    # The same two codes, one of them dropped: a different valid state, and the
    # remaining entry is byte-identical to the one it kept.
    force_state(database, profile_id, readiness_issues_json=readiness_json(first))
    one = fingerprint_of(database, profile_id)
    assert one.ok is True and one.issues == ()
    assert one.audit_fingerprint != both.audit_fingerprint


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("input_assembly_version", "recommendation-input-assembly-v0"),
        ("persistence_version", "recommendation-persistence-v0"),
    ],
)
def test_a_valid_stored_state_version_moves_the_digest_with_no_run_to_disagree_with(
    database, column, replacement
):
    """An INCOMPLETE profile names no run, so no version comparison can fire.

    `STATE_VERSION_MISMATCH` needs a current run to disagree with and there is
    none here, and the stored version is deliberately never compared against the
    constant this build ships — that would be version obsolescence, which is
    9B.3's question. So neither state produces a finding, and the digest is the
    only thing that can distinguish them.
    """
    profile_id = incomplete_profile(database)
    before = fingerprint_of(database, profile_id)
    assert before.ok is True and before.issues == ()

    stored = database.execute(
        f"SELECT {column} FROM recommendation_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()[0]
    assert replacement != stored and replacement == replacement.strip()
    force_state(database, profile_id, **{column: replacement})

    after = fingerprint_of(database, profile_id)

    assert after.ok is True and after.issues == ()
    assert after.status == "INCOMPLETE"
    assert after.audit_fingerprint != before.audit_fingerprint


def test_the_absence_of_a_state_row_is_a_state_the_digest_can_name(database):
    """NOT_SYNCED is stated, not left as a gap.

    A profile that has never synchronized and one carrying a valid INCOMPLETE
    row are different states with no finding between them, so the digest has to
    separate them — and it must come back to exactly its old value when the row
    is removed again.
    """
    profile_id = ensure_user_profile(database, TEST_ONLY_EMAIL).profile_id
    database.commit()
    absent = fingerprint_of(database, profile_id)
    assert absent.ok is True and absent.status == "NOT_SYNCED"

    insert_state(database, profile_id, "INCOMPLETE", ONE_ISSUE)
    present = fingerprint_of(database, profile_id)
    assert present.ok is True and present.issues == ()
    assert present.audit_fingerprint != absent.audit_fingerprint

    database.execute(
        "DELETE FROM recommendation_profile_state WHERE profile_id=?", (profile_id,)
    )
    database.commit()
    assert (
        fingerprint_of(database, profile_id).audit_fingerprint
        == absent.audit_fingerprint
    )


def test_a_ready_state_digest_follows_its_stored_versions_too(synced):
    """The same property on the state 9B.1 actually writes.

    Both the state row and the run it names are moved to the same older version,
    so they still agree with each other and `STATE_VERSION_MISMATCH` stays
    silent. The run fingerprint does move — the assembly version is part of the
    operational identity — so the run identity is asserted to be the *only*
    finding, and the state's own contribution is what the changed digest then
    proves.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    before = fingerprint_of(synced.connection, synced.profile_id)
    assert before.ok is True and before.issues == ()

    corrupt_run(synced.connection, stored.run_id, input_assembly_version="assembly-v0")
    force_state(
        synced.connection, synced.profile_id, input_assembly_version="assembly-v0"
    )
    after = fingerprint_of(synced.connection, synced.profile_id)

    assert codes(after) == ["RUN_FINGERPRINT_MISMATCH"]
    assert after.audit_fingerprint != before.audit_fingerprint


def test_a_malformed_state_value_is_carried_into_the_digest_without_crashing(synced):
    """Corruption must still be distinguishable, and must not normalize away.

    Two different unknown state values are both reported as `STATE_UNKNOWN`, and
    the detail names each — so the digest would separate them through the finding
    alone. What is asserted here is the weaker but necessary property: an
    unreadable state row does not crash the digest, and repeats stably.
    """
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    seen = set()
    for value in ("ALMOST_READY", "NEARLY_READY"):
        with relaxed(synced.connection, "ignore_check_constraints"):
            force_state(synced.connection, synced.profile_id, state=value)
        report = fingerprint_of(synced.connection, synced.profile_id)
        assert report.ok is False and codes(report) == ["STATE_UNKNOWN"]
        assert report.status == "UNKNOWN"
        seen.add(report.audit_fingerprint)
    assert len(seen) == 2


def test_the_audit_fingerprint_moves_with_what_was_audited_and_not_with_anything_else(
    synced,
):
    """Stable identities and findings decide the digest; nothing else may.

    A timestamp is the value most likely to creep in, so both of the audited
    tables' clocks are moved and the digest is required not to notice.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    baseline = audit_recommendation_profile_history(
        synced.connection, synced.profile_id
    ).audit_fingerprint
    assert (
        audit_recommendation_profile_history(
            synced.connection, synced.profile_id
        ).audit_fingerprint
        == baseline
    )

    # -- clocks and unaudited columns move; the digest does not --------------
    synced.connection.execute(
        "UPDATE recommendation_runs SET created_at='2020-01-01' WHERE id=?",
        (stored.run_id,),
    )
    synced.connection.execute(
        "UPDATE recommendation_profile_state SET updated_at='2020-01-01',"
        " created_at='2020-01-01' WHERE profile_id=?",
        (synced.profile_id,),
    )
    synced.connection.commit()
    assert (
        audit_recommendation_profile_history(
            synced.connection, synced.profile_id
        ).audit_fingerprint
        == baseline
    )

    # -- a stable audited identity moves; the digest follows -----------------
    second = synced.store(ranked_batch(synced, synced.opportunity_ids[:2]))
    synced.connection.commit()
    with_history = audit_recommendation_profile_history(
        synced.connection, synced.profile_id
    )
    assert with_history.ok is True
    assert with_history.audit_fingerprint != baseline

    # -- a finding appears; the digest follows again -------------------------
    corrupt_run(synced.connection, second.run_id, run_fingerprint=OTHER_DIGEST)
    broken = audit_recommendation_profile_history(synced.connection, synced.profile_id)
    assert broken.ok is False
    assert broken.audit_fingerprint not in (baseline, with_history.audit_fingerprint)
    assert (
        audit_recommendation_profile_history(
            synced.connection, synced.profile_id
        ).audit_fingerprint
        == broken.audit_fingerprint
    )


# --------------------------------------------------------------------------
# corruption strict UTF-8 cannot carry
#
# `json.loads` is more permissive than the encoding this project fingerprints
# under. `"\ud800"` is syntactically valid JSON and decodes to the high half of
# a surrogate pair with no low half after it — a Python `str` that strict UTF-8
# refuses to encode. So a stored payload can parse and still have no canonical
# bytes, and the audit has to report that as the corruption it is instead of
# aborting over it.
#
# The escape is the only way such a value reaches storage at all: SQLite is
# handed UTF-8, so binding the decoded string is refused outright. That is
# asserted below rather than assumed, because it is what makes these fixtures
# honest — the corruption they write is corruption a database can really hold.
# --------------------------------------------------------------------------

#: The escape as stored, and the character it decodes to.
LONE_SURROGATE = "\ud800"

#: Text that is legitimately non-ASCII and legitimately UTF-8. It must keep
#: behaving exactly as it always did: the fix separates "not encodable" from
#: "not ASCII", and conflating the two would break every accented message.
ACCENTED_MESSAGE = "aucune exécution de matching disponible"


def storable_json(payload):
    """`payload` as JSON text SQLite can hold, escaping what UTF-8 cannot carry.

    `ensure_ascii=True` writes the unpaired surrogate as the six characters
    `\ud800`, which is what a restore, a hand-edit or an older writer would
    leave behind. `json.loads` decodes those six characters back into the one
    character that has no strict UTF-8 encoding.
    """
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def readiness_json_with(message):
    """One readiness issue whose message is `message`, as stored text."""
    return storable_json(
        [
            {
                "code": RecommendationReadinessIssueCode.MATCHING_NOT_READY.value,
                "message": message,
                "opportunity_id": None,
            }
        ]
    )


def test_an_unpaired_surrogate_survives_storage_only_as_an_escape(synced):
    """The premise every fixture below rests on, asserted rather than assumed."""
    with pytest.raises(UnicodeEncodeError):
        LONE_SURROGATE.encode("utf-8")
    # SQLite is handed UTF-8, so the decoded character cannot even be bound.
    with pytest.raises(UnicodeEncodeError):
        synced.connection.execute("SELECT ?", (LONE_SURROGATE,))
    # Escaped, it stores as plain ASCII and decodes back to the same character.
    stored = storable_json({"value": LONE_SURROGATE})
    assert stored.isascii() and stored == '{"value":"\\ud800"}'
    assert json.loads(stored)["value"] == LONE_SURROGATE
    # And the project's canonical form of it has no bytes at all.
    with pytest.raises(UnicodeEncodeError):
        canonical_json(json.loads(stored)).encode("utf-8")


def test_a_matching_schema_failure_is_translated_into_this_modules_error(synced):
    """A `sqlite3.Error` from the delegated Matching audit is not this API's.

    Matching reads its per-run assessments outside its own `sqlite3.Error`
    boundary, so schema damage there — a `matching_assessments` table a restore
    never recreated — reaches this module raw. Callers are promised
    `RecommendationPersistenceAuditError` for a failed audit, and that promise is
    kept at the delegation boundary, with the original error chained.
    """
    from services.collector.matching import audit_matching_profile_history
    from services.collector.matching.persistence_audit import (
        MatchingPersistenceAuditError,
    )

    synced.store(ranked_batch(synced))
    synced.connection.commit()
    assert (
        audit_recommendation_profile_history(synced.connection, synced.profile_id).ok
        is True
    )
    # Only the temporary database is damaged, and only this one table.
    synced.connection.execute("DROP TABLE matching_assessments")
    synced.connection.commit()

    # The delegated audit is what fails, and it does not fail as this module's
    # error: whatever it raises, the translation below is what holds the
    # contract. Matching itself is not changed by this slice.
    with pytest.raises((MatchingPersistenceAuditError, sqlite3.Error)):
        audit_matching_profile_history(synced.connection, synced.profile_id)

    with pytest.raises(RecommendationPersistenceAuditError) as raised:
        audit_recommendation_profile_history(synced.connection, synced.profile_id)

    # The type callers are given, and never a raw SQLite error.
    assert not isinstance(raised.value, sqlite3.Error)
    assert "matching history" in str(raised.value)
    # The cause is not swallowed: it is whichever error the delegated audit
    # actually raised.
    assert isinstance(
        raised.value.__cause__, (MatchingPersistenceAuditError, sqlite3.Error)
    )


def test_an_assessment_payload_with_an_unpaired_surrogate_is_reported(synced):
    """It parses, it has no canonical bytes, and the audit says so and goes on.

    The envelope is otherwise untouched, so the only thing wrong with it is the
    one thing under test: the digest and the canonical-form comparison are both
    skipped — neither can be made honestly over a value that cannot be encoded —
    and no second, invented mismatch is reported in their place.
    """
    older = synced.store(ranked_batch(synced, synced.opportunity_ids[:2]))
    newer = synced.store(ranked_batch(synced))
    synced.connection.commit()
    payload = assessment_payload(synced.connection, newer.run_id, 2)
    payload["eligibility"]["evidence"][0]["explanation"] = LONE_SURROGATE
    stored_text = storable_json(payload)
    assert stored_text.isascii()
    corrupt_assessment(
        synced.connection, newer.run_id, 2, assessment_payload_json=stored_text
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["ASSESSMENT_PAYLOAD_INVALID_JSON"]
    found = report.issues[0]
    assert found.scope == "ASSESSMENT" and found.run_id == newer.run_id
    assert "canonical UTF-8" in found.detail
    # No digest was possible, so none is claimed to have failed.
    assert "ASSESSMENT_FINGERPRINT_MISMATCH" not in codes(report)
    assert "ASSESSMENT_PAYLOAD_NOT_CANONICAL" not in codes(report)
    # The rest of the history was audited: both runs, every assessment, and the
    # two intact runs still report their own identities.
    assert [item.run_id for item in report.runs] == [newer.run_id, older.run_id]
    assert [len(item.ranked_assessments) for item in report.runs] == [3, 2]
    assert report.runs[1].ok is True
    assert report.assessment_count == 5

    repeated = audit_recommendation_profile_history(
        synced.connection, synced.profile_id
    )
    assert repeated == report
    assert repeated.audit_fingerprint == report.audit_fingerprint


def test_a_batch_payload_with_an_unpaired_surrogate_is_reported(synced):
    """The same protection on the run's own batch statement."""
    older = synced.store(ranked_batch(synced, synced.opportunity_ids[:2]))
    newer = synced.store(ranked_batch(synced))
    synced.connection.commit()
    payload = batch_payload(synced.connection, newer.run_id)
    payload["recommendation_engine_version"] = LONE_SURROGATE
    corrupt_run(
        synced.connection, newer.run_id, batch_payload_json=storable_json(payload)
    )

    report = audit_recommendation_profile_history(synced.connection, synced.profile_id)

    assert report.ok is False
    assert "BATCH_PAYLOAD_INVALID_JSON" in codes(report)
    found = [
        item for item in report.issues if item.code == "BATCH_PAYLOAD_INVALID_JSON"
    ]
    assert len(found) == 1
    assert found[0].scope == "RUN" and found[0].run_id == newer.run_id
    assert "canonical UTF-8" in found[0].detail
    # The digest step was impossible and is skipped rather than guessed at, and
    # so is the canonical-form comparison.
    assert "BATCH_FINGERPRINT_MISMATCH" not in codes(report)
    assert "BATCH_PAYLOAD_NOT_CANONICAL" not in codes(report)
    # What *can* still be compared honestly still is: this payload genuinely no
    # longer states the versions the run stores.
    assert "BATCH_CONTENT_MISMATCH" in codes(report)
    # And the other run is untouched and still audited.
    assert [item.run_id for item in report.runs] == [newer.run_id, older.run_id]
    assert report.runs[1].ok is True and report.runs[1].run_id == older.run_id

    repeated = audit_recommendation_profile_history(
        synced.connection, synced.profile_id
    )
    assert repeated == report
    assert repeated.audit_fingerprint == report.audit_fingerprint


def test_a_readiness_message_with_an_unpaired_surrogate_is_reported(synced):
    """A profile-scoped finding, a stable digest, and no repaired message."""
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    incomplete_with(
        synced.connection, synced.profile_id, readiness_json_with(LONE_SURROGATE)
    )

    report = fingerprint_of(synced.connection, synced.profile_id)

    assert report.ok is False
    assert codes(report) == ["READINESS_ISSUES_INVALID_JSON"]
    assert report.issues[0].scope == "PROFILE"
    assert "canonical UTF-8" in report.issues[0].detail
    assert report.status == "INCOMPLETE"
    assert len(report.audit_fingerprint) == 64
    # The run beside the broken state row was still audited.
    assert report.runs[0].ok is True


def test_a_readiness_message_that_cannot_be_encoded_is_never_repaired_into_a_valid_one(
    synced,
):
    """The escape stays evidence: it is tagged, not turned into a real message.

    The comparison state stores the *literal* six characters `\ud800`, which is
    an ordinary, perfectly valid message. If the unencodable one were normalized
    — the character dropped, replaced, or written back out as its escape — the
    two states would become indistinguishable, and the corrupt one would read as
    clean.
    """
    synced.store(ranked_batch(synced))
    synced.connection.commit()

    incomplete_with(
        synced.connection, synced.profile_id, readiness_json_with("\\ud800")
    )
    literal = fingerprint_of(synced.connection, synced.profile_id)
    assert literal.ok is True and literal.issues == ()

    incomplete_with(
        synced.connection, synced.profile_id, readiness_json_with(LONE_SURROGATE)
    )
    corrupt = fingerprint_of(synced.connection, synced.profile_id)

    assert corrupt.ok is False
    assert corrupt.audit_fingerprint != literal.audit_fingerprint


#: An ordinary JSON object whose stored content is exactly what a value-by-value
#: rendering of an unpaired surrogate produces. Nothing about it is corrupt: it
#: is six perfectly ordinary characters under a key a persisted array is free to
#: carry, and a state storing it is a different state from one storing the
#: character it resembles.
SENTINEL_SHAPED_ENTRY = {"__not_utf8__": "\\ud800"}


def test_two_corrupt_readiness_states_that_render_alike_stay_distinct(synced):
    """The review case: a real surrogate, and stored content shaped like its tag.

    State A stores two actual unpaired surrogates. State B stores the object a
    rendered surrogate looks like, beside one real surrogate — so a stable
    identity built by rewriting the decoded tree value by value collapses both
    onto the same representation. Their findings are identical by construction,
    which is what made that collision total: the digest had nothing else left to
    tell two genuinely different persisted states apart.

    Carrying the stored text for an array that has no canonical UTF-8 form is
    what fixes it, and the fix is only real if the findings stay as they were —
    weakening them would separate the digests by hiding corruption instead.
    """
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    state_a = storable_json([LONE_SURROGATE, LONE_SURROGATE])
    state_b = storable_json([SENTINEL_SHAPED_ENTRY, LONE_SURROGATE])
    # Two different persisted states, and SQLite really is holding both texts.
    assert state_a != state_b

    incomplete_with(synced.connection, synced.profile_id, state_a)
    assert stored_readiness(synced.connection, synced.profile_id) == state_a
    audit_a = fingerprint_of(synced.connection, synced.profile_id)

    incomplete_with(synced.connection, synced.profile_id, state_b)
    assert stored_readiness(synced.connection, synced.profile_id) == state_b
    audit_b = fingerprint_of(synced.connection, synced.profile_id)

    assert audit_a.ok is False and audit_b.ok is False
    # Neither state is reported any less than it was: same codes, same details,
    # in the same order — so the findings genuinely cannot separate them.
    assert audit_a.issues == audit_b.issues
    assert codes(audit_a) == [
        "READINESS_ISSUES_INVALID_JSON",
        "READINESS_ISSUE_MALFORMED",
        "READINESS_ISSUE_MALFORMED",
    ]
    # The identity does.
    assert audit_a.audit_fingerprint != audit_b.audit_fingerprint


def test_no_stored_column_value_can_render_as_the_unencodable_tag(synced):
    """Why the tag cannot collide: nothing SQLite stores digests as an object.

    The tag is only safe while `_stable_value` is a function of one *stored
    column value* — null, an integer, a real, text or a blob. A decoded JSON
    tree is the one kind of input that can be an object itself, and it is never
    rendered here, so no ordinary content can reach the tagged shape.

    The tagged branch is unreachable in this build for a second, independent
    reason, asserted below: a string strict UTF-8 refuses cannot be written to a
    column in the first place. It is kept as a guard, not as a rendering of
    anything a database can hold.
    """
    from services.recommendation.persistence_audit import _stable_value

    for value in (
        None,
        0,
        1,
        -1,
        1.5,
        True,
        False,
        "",
        "text",
        "café",
        b"\x00\xff",
        # Including the stored text that resembles the tag, and the tag's own key.
        "\\ud800",
        "__not_utf8__",
        '{"__not_utf8__":"\\ud800"}',
    ):
        rendered = _stable_value(value)
        assert not isinstance(rendered, dict), value
        assert canonical_json(rendered).encode("utf-8")

    # Only a string strict UTF-8 refuses is tagged, and SQLite cannot hand one
    # back: the write is refused before any column could hold it.
    assert isinstance(_stable_value(LONE_SURROGATE), dict)
    with pytest.raises(UnicodeEncodeError):
        synced.connection.execute("SELECT ?", (LONE_SURROGATE,))


def test_legitimate_non_ascii_text_is_not_treated_as_corruption(synced):
    """Non-ASCII is not the defect; unencodable is. Accented text stays valid."""
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()

    ready = fingerprint_of(synced.connection, synced.profile_id)
    assert ready.ok is True and ready.issues == ()
    assert ready.status == "READY" and ready.current_run_id == stored.run_id

    accented = readiness_json_with(ACCENTED_MESSAGE)
    # The stored text really does carry the accented characters, canonically:
    # `ensure_ascii=False` is this project's canonical form, and escaping them
    # would be a different stored value.
    assert accented != canonical_json(json.loads(accented))
    incomplete_with(
        synced.connection, synced.profile_id, canonical_json(json.loads(accented))
    )

    report = fingerprint_of(synced.connection, synced.profile_id)

    assert report.ok is True and report.issues == ()
    assert report.status == "INCOMPLETE"
    # And it is its own state, distinct from the same issue written in ASCII.
    incomplete_with(synced.connection, synced.profile_id, readiness_json_with("plain"))
    assert (
        fingerprint_of(synced.connection, synced.profile_id).audit_fingerprint
        != report.audit_fingerprint
    )


def test_a_surrogate_corrupted_history_is_audited_read_only(synced, database_path):
    """The new path writes nothing either, over a `mode=ro` connection."""
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    payload = assessment_payload(synced.connection, stored.run_id, 1)
    payload["eligibility"]["evidence"][0]["explanation"] = LONE_SURROGATE
    corrupt_assessment(
        synced.connection,
        stored.run_id,
        1,
        assessment_payload_json=storable_json(payload),
    )
    incomplete_with(
        synced.connection, synced.profile_id, readiness_json_with(LONE_SURROGATE)
    )
    synced.connection.close()

    read_only = connect_readonly_database(database_path)
    try:
        report = audit_recommendation_profile_history(read_only, synced.profile_id)

        assert report.ok is False
        assert codes(report) == [
            "READINESS_ISSUES_INVALID_JSON",
            "ASSESSMENT_PAYLOAD_INVALID_JSON",
        ]
        assert len(report.audit_fingerprint) == 64
        assert read_only.total_changes == 0
        assert read_only.in_transaction is False
    finally:
        read_only.close()


def test_the_canonical_utf8_primitive_separates_valid_text_from_unencodable(synced):
    """The private primitive, directly: `é` encodes, an unpaired surrogate does not."""
    from services.recommendation.persistence_audit import (
        _canonical_utf8,
        _stable_value,
    )

    text, data = _canonical_utf8({"value": "café"})
    assert text == '{"value":"café"}'
    assert data == text.encode("utf-8")
    assert _canonical_utf8({"value": LONE_SURROGATE}) is None

    # A valid string keeps exactly its old stable rendering, and an unencodable
    # one is tagged rather than carried.
    assert _stable_value("café") == "café"
    assert canonical_json(_stable_value(LONE_SURROGATE)).encode("utf-8")
    # Decoded trees are not rendered here at all — `_readiness_identity` carries
    # the stored text instead — but one reaching this far still has to encode.
    nested = _stable_value([{"message": LONE_SURROGATE, "opportunity_id": None}])
    assert canonical_json(nested).encode("utf-8")
    assert LONE_SURROGATE not in canonical_json(nested)


# --------------------------------------------------------------------------
# the snapshot, transaction ownership, and the read-only guarantee
# --------------------------------------------------------------------------


def test_a_caller_owned_transaction_is_borrowed_and_never_finished(synced):
    """The audit may join a caller's transaction; it may never end one.

    The caller's own uncommitted change is the probe. If the audit had
    committed, the rollback afterwards could not undo it; if it had rolled back,
    the change would already be gone before the rollback.
    """
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    target = synced.opportunity_ids[0]

    def organization():
        return synced.connection.execute(
            "SELECT organization FROM opportunities WHERE id=?", (target,)
        ).fetchone()[0]

    original = organization()
    synced.connection.execute("BEGIN")
    synced.connection.execute(
        "UPDATE opportunities SET organization='CALLER-PENDING' WHERE id=?", (target,)
    )
    assert synced.connection.in_transaction is True

    report = audit_recommendation_profile_history(
        synced.connection, synced.profile_id
    )

    assert report.ok is True
    assert synced.connection.in_transaction is True
    assert organization() == "CALLER-PENDING"

    # Still the caller's to end, and still undoable — so nothing committed it.
    synced.connection.execute("ROLLBACK")
    assert synced.connection.in_transaction is False
    assert organization() == original


def test_a_failing_audit_releases_the_snapshot_it_opened(synced):
    """An exception must not leave the connection holding a read transaction."""
    synced.store(ranked_batch(synced))
    synced.connection.commit()
    assert synced.connection.in_transaction is False

    with pytest.raises(RecommendationPersistenceAuditError, match="does not exist"):
        audit_recommendation_profile_history(synced.connection, 99999)

    assert synced.connection.in_transaction is False
    assert (
        audit_recommendation_profile_history(
            synced.connection, synced.profile_id
        ).ok
        is True
    )


class InterleavingConnection(sqlite3.Connection):
    """Runs a callback once, immediately before a chosen statement.

    This is how a concurrent commit is placed at an exact point inside the audit
    without a sleep: the hook fires on the first statement whose SQL contains
    `marker`, which is a position in the audit's own sequence rather than a
    moment in wall-clock time.
    """

    marker = None
    action = None
    fired = False

    def execute(self, sql, parameters=()):
        if self.action is not None and not self.fired and self.marker in sql:
            self.fired = True
            self.action()
        return super().execute(sql, parameters)


def test_the_whole_profile_audit_observes_one_sqlite_snapshot(tmp_path):
    """A newer run committed mid-audit must not blend into the older picture.

    WAL is used because it is the mode in which a writer genuinely *succeeds*
    while a reader holds a snapshot, which is what makes "one snapshot"
    observable rather than merely unfalsifiable. The commit lands after the run
    listing and before the state row: without one snapshot the audit would hold
    a one-run history beside a pointer at run 2 and would report
    `STATE_CURRENT_RUN_UNKNOWN` — a corruption that is really a race.
    """
    path = tmp_path / "snapshot-audit.db"
    writer = connect_database(path)
    writer.execute("PRAGMA journal_mode=WAL")
    apply_migrations(writer)
    fixture = matching_fixture(writer, TEST_ONLY_EMAIL)
    first = fixture.store(ranked_batch(fixture, fixture.opportunity_ids[:2]))
    writer.commit()

    appended = []

    def append_a_newer_run():
        second = fixture.store(ranked_batch(fixture))
        writer.commit()
        appended.append(second.run_id)

    reader = sqlite3.connect(path, factory=InterleavingConnection)
    reader.marker = "FROM recommendation_assessments"
    reader.action = append_a_newer_run
    try:
        report = audit_recommendation_profile_history(reader, fixture.profile_id)
        assert reader.fired, "the interleaved commit never ran"
        assert reader.in_transaction is False
    finally:
        reader.close()

    assert appended and appended[0] != first.run_id
    assert report.ok is True and report.issues == ()
    assert report.run_count == 1
    assert [item.run_id for item in report.runs] == [first.run_id]
    assert report.current_run_id == first.run_id

    # The commit was real: once the snapshot is over, the newer state is there.
    after = audit_recommendation_profile_history(writer, fixture.profile_id)
    assert after.ok is True
    assert [item.run_id for item in after.runs] == [appended[0], first.run_id]
    writer.close()


def test_the_audit_runs_over_a_read_only_connection_and_writes_nothing(
    synced, database_path
):
    """`mode=ro` is the strongest available proof that no write is attempted.

    It also proves the snapshot is a *deferred* transaction: `BEGIN IMMEDIATE`
    reserves the write lock and would fail outright on this connection.
    """
    stored = synced.store(ranked_batch(synced))
    synced.connection.commit()
    synced.connection.close()

    read_only = connect_readonly_database(database_path)
    try:
        # The connection really is read-only: a write on it fails.
        with pytest.raises(sqlite3.OperationalError):
            read_only.execute(
                "UPDATE recommendation_profile_state SET state='INCOMPLETE'"
            )
        # Python's `sqlite3` opened an implicit transaction for that DML before
        # SQLite refused it, and the audit would have *borrowed* that one. It is
        # cleared here so what the audit opens below is genuinely its own.
        read_only.rollback()
        assert read_only.in_transaction is False

        report = audit_recommendation_profile_history(read_only, synced.profile_id)

        assert report.ok is True and report.issues == ()
        assert report.current_run_id == stored.run_id
        assert [item.rank_position for item in report.runs[0].ranked_assessments] == [
            1,
            2,
            3,
        ]
        assert read_only.total_changes == 0
        assert read_only.in_transaction is False
    finally:
        read_only.close()


def test_every_audit_leaves_the_database_byte_for_byte_unchanged(synced):
    """Healthy or corrupt, an audit is an observation and never a repair."""
    broadly_corrupted(synced)
    before_changes = synced.connection.total_changes
    before_rows = rows_of(synced.connection)

    assert (
        audit_recommendation_profile_history(synced.connection, synced.profile_id).ok
        is False
    )
    with pytest.raises(RecommendationPersistenceAuditError):
        audit_recommendation_profile_history(synced.connection, 99999)

    assert synced.connection.total_changes == before_changes
    assert rows_of(synced.connection) == before_rows
    assert synced.connection.in_transaction is False


def test_a_freshly_migrated_database_is_internally_consistent(tmp_path):
    """The migrations the audit is pointed at are themselves sound."""
    connection = connect_database(tmp_path / "integrity.db")
    try:
        apply_migrations(connection)
        fixture = matching_fixture(connection, TEST_ONLY_EMAIL)
        fixture.store(ranked_batch(fixture))
        connection.commit()

        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            audit_recommendation_profile_history(connection, fixture.profile_id).ok
            is True
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# structural guarantees
#
# Not stylistic ones: what follows is read out of the production module's AST,
# so the audit *cannot* write rather than merely happening not to.
# --------------------------------------------------------------------------


def literal_sql(node):
    """The literal text of one SQL argument, joining an f-string's fixed parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def audit_tree():
    from services.recommendation import persistence_audit

    with open(persistence_audit.__file__, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def executed_sql():
    """Every SQL string the audit can hand to SQLite, from its own AST.

    Narrower than scanning the file, and stronger: prose in a docstring is not
    what runs, and a keyword smuggled through an f-string interpolation would
    slip past a plain grep. Two shapes reach SQLite — a literal handed straight
    to `connection.execute(...)`, and a literal handed to the `_query` helper,
    which is the module's single indirection — and both are collected. Any
    *other* computed statement fails the walk rather than being skipped.
    """
    statements = []
    for node in ast.walk(audit_tree()):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "_query":
            # _query(connection, sql, parameters, description)
            assert len(node.args) >= 2, ast.dump(node)
            text = literal_sql(node.args[1])
            assert text is not None, f"non-literal _query SQL at line {node.lineno}"
            statements.append(text)
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "execute"):
            continue
        assert node.args, f"execute() with no SQL at line {node.lineno}"
        text = literal_sql(node.args[0])
        if text is None:
            # The single permitted indirection: `_query` forwarding its own
            # `sql` parameter, whose literals were collected at each call site.
            assert isinstance(node.args[0], ast.Name) and node.args[0].id == "sql", (
                f"non-literal SQL at line {node.lineno}"
            )
            continue
        statements.append(text)
    return statements


def test_the_audit_can_only_execute_reads_and_its_own_transaction_control():
    """A read audit legitimately needs `BEGIN` and a way to release it.

    Those, and only those. Everything that could change a row stays prohibited,
    and `BEGIN IMMEDIATE` in particular: reserving the write lock is exactly what
    a reader must not do, and it would fail on a `mode=ro` connection. No
    `PRAGMA` either — the test-only ones this file uses are the tests', never
    production's.
    """
    statements = executed_sql()
    assert statements, "no SQL found; the AST walk is not seeing the module"

    for statement in statements:
        normalized = " ".join(statement.split()).upper()
        assert normalized.startswith(("SELECT ", "BEGIN", "COMMIT", "ROLLBACK")), (
            statement
        )
        if normalized.startswith("BEGIN"):
            assert normalized in ("BEGIN", "BEGIN DEFERRED"), statement
        for forbidden in (
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "REPLACE ",
            "BEGIN IMMEDIATE",
            "BEGIN EXCLUSIVE",
            "CREATE ",
            "DROP ",
            "ALTER ",
            "PRAGMA ",
            "VACUUM",
            "ATTACH ",
        ):
            assert forbidden not in normalized, (forbidden, statement)

    # Exactly one transaction may be opened, and it must be the deferred one.
    assert [item for item in statements if item.upper().startswith("BEGIN")] == ["BEGIN"]


def test_the_audit_never_calls_a_connection_transaction_method():
    """`connection.commit()` would end a transaction this module may not own.

    Ownership is decided by `in_transaction` and released with an explicit
    `ROLLBACK` on the transaction the audit opened itself. A bare `.commit()` /
    `.rollback()` — or `executescript`, which commits implicitly — would bypass
    that decision and could finish a caller's transaction.
    """
    called = {
        node.func.attr
        for node in ast.walk(audit_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("commit", "rollback", "executemany", "executescript", "cursor"):
        assert forbidden not in called, forbidden


def test_the_audit_does_not_lean_on_the_strict_read_model():
    """Two different responsibilities, and one must not be built out of the other.

    The read model answers "can I safely read this?" and refuses at the first
    value it cannot hand back. The audit answers "what exactly is corrupt across
    this history?" and has to keep going. Implementing the second on top of the
    first would silently reduce it to the first.
    """
    imported = set()
    for node in ast.walk(audit_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("read_model" in name for name in imported), imported


def test_the_audit_never_reads_the_current_matching_run_pointer():
    """The one comparison that would turn this audit into a freshness check.

    `matching_profile_state` names the profile's *current* Matching run.
    Requiring a historical recommendation run's source to equal it is explicitly
    forbidden, and the cheapest way to keep that true is for the table never to
    be named in the audit's SQL at all.
    """
    for statement in executed_sql():
        assert "matching_profile_state" not in statement.lower(), statement
