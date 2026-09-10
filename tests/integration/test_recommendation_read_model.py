"""Phase 9B.2a read model, on disposable SQLite databases these tests build.

Every database here is created under `tmp_path` and thrown away, the profile is
a `.invalid` address, every posting is invented, and **the operational database
is never opened**: nothing below reads or writes anything a person uses.

Nothing is faked out either. The schema is the project's own migrations up to
`0026`, the matching snapshot comes from Phase 4's `store_matching_batch`, the
batches come from Phase 9A's own engine, and the runs come from Phase 9B.1's
`store_recommendation_batch`. A hand-rolled mini-schema would let the read model
agree with a table this file invented rather than with the one it will actually
be pointed at, so there is none.

Corrupt states are produced the only honest way: a **valid** history is built by
the real components first, and then one specific value is broken in the
temporary database. Two of the checks — the disposition vocabulary and the
"no evidence means no score" contract — are also enforced by `0026`'s own CHECK
constraints, so exercising them needs `PRAGMA ignore_check_constraints`. They
are not redundant: the read model is the layer a consumer holds, and it must
refuse a row rather than pass on whatever a future migration, a restore or a
hand-edit happened to leave behind.

What is deliberately **not** tested here, because it is deliberately not
implemented here: the recomputation of `recommendation_assessment_fingerprint`,
`recommendation_batch_fingerprint` and `recommendation_run_fingerprint` over a
stored history. That is the Phase 9B.2b persistence audit. This module checks
that every exposed digest has the *form* of a SHA-256 digest, and stops there.
"""

import ast
from contextlib import contextmanager
from dataclasses import fields
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
    RECOMMENDATION_ENGINE_VERSION,
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RECOMMENDATION_PERSISTENCE_VERSION,
    RECOMMENDATION_RULES_VERSION,
    RecommendationDisposition,
    RecommendationReadError,
    RecommendationReadinessIssueCode,
    list_recommendation_runs,
    read_current_recommendation,
    read_recommendation_run,
)
from tests.integration.test_matching_persistence import (
    add_opportunity,
    make_batch as make_matching_batch,
)
from tests.integration.test_recommendation_persistence import (
    SELECTION_VERSION,
    Fixture,
)

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "recommendation.read.model@example.invalid"
TEST_ONLY_OTHER_EMAIL = "recommendation.read.model.other@example.invalid"

#: Every shape a public identifier must be refused for. `True` and `False` are
#: first on purpose: both are `int` in Python and `True == 1`, so a profile that
#: happens to be id 1 must not be reachable by asking for `True`.
INVALID_IDENTIFIERS = (True, False, 0, -1, 1.0, "1", None)

READ_TABLES = (
    "recommendation_runs",
    "recommendation_assessments",
    "recommendation_profile_state",
)


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "recommendation-read-model.db"


@pytest.fixture
def database(database_path):
    connection = connect_database(database_path)
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def synced(database):
    """One profile, three postings, and the matching run they were matched by."""
    profile_id = ensure_user_profile(database, TEST_ONLY_EMAIL).profile_id
    ids = tuple(add_opportunity(database, f"read-{suffix}") for suffix in "abc")
    database.commit()
    matching = store_matching_batch(
        database,
        profile_id,
        make_matching_batch(profile_id, ids),
        selection_version=SELECTION_VERSION,
    )
    return Fixture(database, profile_id, ids, matching)


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


def force_state(connection, profile_id, **columns):
    """Write one state row directly, bypassing 9B.1 — which never writes these.

    Phase 9B.1 only ever writes READY, and 9B.3 has not been built yet, so an
    INCOMPLETE row cannot be produced through a supported entry point today.
    This is a fixture, not a shortcut past a boundary: it writes exactly the row
    9B.3's contract describes so the reader can be held to that contract now.
    """
    assignments = ",".join(f"{name}=?" for name in columns)
    connection.execute(
        f"UPDATE recommendation_profile_state SET {assignments} WHERE profile_id=?",
        (*columns.values(), profile_id),
    )
    connection.commit()


def make_incomplete(connection, profile_id, issues=ONE_ISSUE):
    force_state(
        connection,
        profile_id,
        state="INCOMPLETE",
        current_run_id=None,
        readiness_issues_json=issues,
    )


def snapshot(connection):
    return {
        table: connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in READ_TABLES
    }


# --------------------------------------------------------------------------
# arguments, existence, and the states
# --------------------------------------------------------------------------


def test_public_identifiers_must_be_positive_integers_and_never_booleans(synced):
    stored = synced.store()
    # Both really exist, so nothing below can pass merely by being absent.
    assert synced.profile_id == 1 and stored.run_id == 1

    for invalid in INVALID_IDENTIFIERS:
        with pytest.raises(RecommendationReadError, match="positive integer"):
            read_recommendation_run(synced.connection, invalid)
        with pytest.raises(RecommendationReadError, match="positive integer"):
            list_recommendation_runs(synced.connection, invalid)
        with pytest.raises(RecommendationReadError, match="positive integer"):
            read_current_recommendation(synced.connection, invalid)

    # And the reachable ids are genuinely reachable: the refusal above is about
    # the type, not about the read being broken.
    assert read_recommendation_run(synced.connection, 1).run_id == 1
    assert read_current_recommendation(synced.connection, 1).status == "READY"


def test_a_profile_that_does_not_exist_is_refused_by_both_profile_reads(synced):
    for read in (list_recommendation_runs, read_current_recommendation):
        with pytest.raises(RecommendationReadError, match="profile 99999 does not exist"):
            read(synced.connection, 99999)


def test_a_run_that_does_not_exist_is_refused(synced):
    synced.store()
    with pytest.raises(RecommendationReadError, match="run 99999 does not exist"):
        read_recommendation_run(synced.connection, 99999)


def test_a_profile_with_no_state_and_no_history_reads_as_not_synced(synced):
    model = read_current_recommendation(synced.connection, synced.profile_id)

    assert model.profile_id == synced.profile_id
    assert model.status == "NOT_SYNCED"
    assert model.current_run_id is None
    assert model.current_run is None
    assert model.readiness_issues == ()
    assert model.persistence_version is None
    assert model.input_assembly_version is None
    assert model.history_count == 0
    assert list_recommendation_runs(synced.connection, synced.profile_id) == ()


def test_a_stored_run_reads_back_as_ready_with_the_versions_it_was_written_under(
    synced,
):
    batch = synced.batch()
    stored = synced.store(batch)

    model = read_current_recommendation(synced.connection, synced.profile_id)

    assert model.status == "READY"
    assert model.current_run_id == stored.run_id
    assert model.readiness_issues == ()
    assert model.history_count == 1
    assert model.persistence_version == RECOMMENDATION_PERSISTENCE_VERSION
    assert model.input_assembly_version == RECOMMENDATION_INPUT_ASSEMBLY_VERSION

    run = model.current_run
    assert run is not None
    assert run == read_recommendation_run(synced.connection, stored.run_id)
    assert run.run_id == stored.run_id
    assert run.profile_id == synced.profile_id
    assert run.source_matching_run_id == synced.matching_run_id
    assert run.source_matching_run_fingerprint == synced.matching_run_fingerprint
    assert run.run_fingerprint == stored.run_fingerprint
    assert run.batch_fingerprint == batch.batch_fingerprint
    assert run.persistence_version == RECOMMENDATION_PERSISTENCE_VERSION
    assert run.input_assembly_version == RECOMMENDATION_INPUT_ASSEMBLY_VERSION
    assert run.recommendation_engine_version == RECOMMENDATION_ENGINE_VERSION
    assert run.recommendation_rules_version == RECOMMENDATION_RULES_VERSION
    assert run.assessment_count == len(batch.assessments)
    assert isinstance(run.created_at, str) and run.created_at
    # The payload is the stored envelope, decoded — not a rebuilt one.
    assert run.batch_payload["assessment_count"] == len(batch.assessments)
    assert run.batch_payload["ranked_assessment_fingerprints"] == tuple(
        item.assessment_fingerprint for item in batch.assessments
    )


def test_the_ranking_is_returned_in_the_persisted_order_positions_one_to_n(synced):
    # A deliberately weak first posting, so the engine's order is not the order
    # the ids happen to be in and a re-sort by id would be visible.
    batch = synced.batch(scores={synced.opportunity_ids[0]: 0.1})
    stored = synced.store(batch)

    run = read_recommendation_run(synced.connection, stored.run_id)

    assert [item.rank_position for item in run.assessments] == [1, 2, 3]
    assert [item.opportunity_id for item in run.assessments] == [
        item.opportunity_id for item in batch.assessments
    ]
    for read, source in zip(run.assessments, batch.assessments):
        assert read.disposition == source.disposition
        assert isinstance(read.disposition, RecommendationDisposition)
        assert read.recommendation_score == source.recommendation_score
        assert read.evidence_coverage == source.recommendation_evidence_coverage
        assert read.assessment_fingerprint == source.assessment_fingerprint
        assert isinstance(read.created_at, str) and read.created_at
        assert read.assessment_payload["versions"]["recommendation_engine"] == (
            RECOMMENDATION_ENGINE_VERSION
        )
    # The order really is the stored one, not the id order.
    assert [item.opportunity_id for item in run.assessments] != sorted(
        synced.opportunity_ids
    )


def test_history_is_listed_newest_first_with_exactly_one_current_run(synced):
    first = synced.store(synced.batch(synced.opportunity_ids[:2]))
    second = synced.store(synced.batch())

    history = list_recommendation_runs(synced.connection, synced.profile_id)

    assert [item.run_id for item in history] == [second.run_id, first.run_id]
    assert [item.is_current for item in history] == [True, False]
    assert sum(item.is_current for item in history) == 1
    assert history[0].assessment_count == 3
    assert history[1].assessment_count == 2
    assert history[0].run_fingerprint == second.run_fingerprint
    assert all(
        item.persistence_version == RECOMMENDATION_PERSISTENCE_VERSION
        for item in history
    )
    # `is_current` follows the state pointer, never "the newest row".
    force_state(synced.connection, synced.profile_id, current_run_id=first.run_id)
    moved = list_recommendation_runs(synced.connection, synced.profile_id)
    assert [item.is_current for item in moved] == [False, True]
    assert read_current_recommendation(
        synced.connection, synced.profile_id
    ).current_run_id == first.run_id


def test_listing_history_does_not_load_assessment_payloads(synced):
    """History is an index, not a bulk read of every payload ever persisted."""
    synced.store()
    summary = list_recommendation_runs(synced.connection, synced.profile_id)[0]

    names = {item.name for item in fields(summary)}
    assert not any("payload" in name or "assessment_payload" == name for name in names)
    assert "assessments" not in names
    assert names == {
        "run_id",
        "created_at",
        "assessment_count",
        "source_matching_run_id",
        "persistence_version",
        "input_assembly_version",
        "recommendation_engine_version",
        "recommendation_rules_version",
        "source_matching_run_fingerprint",
        "batch_fingerprint",
        "run_fingerprint",
        "is_current",
    }


def test_an_incomplete_profile_names_no_run_and_carries_its_readiness_issues(synced):
    # A state row only exists once something has been persisted, so one run is
    # stored and then removed: what is left is a profile that has been through a
    # synchronization, has no history, and is INCOMPLETE.
    synced.store()
    make_incomplete(synced.connection, synced.profile_id)
    synced.connection.execute("DELETE FROM recommendation_runs")
    synced.connection.commit()

    model = read_current_recommendation(synced.connection, synced.profile_id)

    assert model.status == "INCOMPLETE"
    assert model.current_run_id is None
    assert model.current_run is None
    assert model.history_count == 0
    assert len(model.readiness_issues) == 1
    issue = model.readiness_issues[0]
    assert issue.code is RecommendationReadinessIssueCode.MATCHING_NOT_READY
    assert issue.message == "no matching run"
    assert issue.opportunity_id is None


def test_an_incomplete_profile_may_still_hold_a_valid_earlier_history(synced):
    first = synced.store(synced.batch(synced.opportunity_ids[:2]))
    second = synced.store(synced.batch())
    make_incomplete(synced.connection, synced.profile_id)

    model = read_current_recommendation(synced.connection, synced.profile_id)

    assert model.status == "INCOMPLETE"
    assert model.current_run_id is None and model.current_run is None
    assert model.history_count == 2
    assert model.readiness_issues[0].code is (
        RecommendationReadinessIssueCode.MATCHING_NOT_READY
    )
    # The history stays readable and stays honest: no run claims to be current.
    history = list_recommendation_runs(synced.connection, synced.profile_id)
    assert [item.run_id for item in history] == [second.run_id, first.run_id]
    assert not any(item.is_current for item in history)
    assert read_recommendation_run(synced.connection, first.run_id).run_id == first.run_id


def test_history_without_a_state_row_is_refused_rather_than_read_as_not_synced(synced):
    synced.store()
    synced.connection.execute("DELETE FROM recommendation_profile_state")
    synced.connection.commit()

    for read in (read_current_recommendation, list_recommendation_runs):
        with pytest.raises(RecommendationReadError, match="without a state row"):
            read(synced.connection, synced.profile_id)


# --------------------------------------------------------------------------
# corrupt runs
# --------------------------------------------------------------------------


def corrupt_run(connection, run_id, **columns):
    assignments = ",".join(f"{name}=?" for name in columns)
    connection.execute(
        f"UPDATE recommendation_runs SET {assignments} WHERE id=?",
        (*columns.values(), run_id),
    )
    connection.commit()


@pytest.mark.parametrize(
    "payload,expected",
    [("{", "invalid batch payload JSON"), ("[]", "batch payload JSON.*is not an object")],
)
def test_a_batch_payload_that_is_not_a_json_object_is_refused(synced, payload, expected):
    stored = synced.store()
    corrupt_run(synced.connection, stored.run_id, batch_payload_json=payload)

    with pytest.raises(RecommendationReadError, match=expected):
        read_recommendation_run(synced.connection, stored.run_id)
    with pytest.raises(RecommendationReadError, match=expected):
        read_current_recommendation(synced.connection, synced.profile_id)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ("{", "invalid assessment payload JSON"),
        ("[]", "assessment payload JSON.*is not an object"),
    ],
)
def test_an_assessment_payload_that_is_not_a_json_object_is_refused(
    synced, payload, expected
):
    stored = synced.store()
    synced.connection.execute(
        "UPDATE recommendation_assessments SET assessment_payload_json=?"
        " WHERE run_id=? AND rank_position=2",
        (payload, stored.run_id),
    )
    synced.connection.commit()

    with pytest.raises(RecommendationReadError, match=expected):
        read_recommendation_run(synced.connection, stored.run_id)


def test_a_declared_assessment_count_that_no_longer_matches_the_rows_is_refused(synced):
    stored = synced.store()
    corrupt_run(synced.connection, stored.run_id, assessment_count=99)

    with pytest.raises(RecommendationReadError, match="assessment count mismatch"):
        read_recommendation_run(synced.connection, stored.run_id)


def test_a_ranking_with_a_hole_in_it_is_refused_rather_than_renumbered(synced):
    stored = synced.store()
    # 1, 3 — with the count corrected, so the only defect is the hole itself and
    # the refusal is provably about contiguity rather than about the count.
    synced.connection.execute(
        "DELETE FROM recommendation_assessments WHERE run_id=? AND rank_position=2",
        (stored.run_id,),
    )
    corrupt_run(synced.connection, stored.run_id, assessment_count=2)
    assert [
        row[0]
        for row in synced.connection.execute(
            "SELECT rank_position FROM recommendation_assessments"
            " WHERE run_id=? ORDER BY rank_position",
            (stored.run_id,),
        )
    ] == [1, 3]

    with pytest.raises(RecommendationReadError, match="non-contiguous ranking"):
        read_recommendation_run(synced.connection, stored.run_id)


def test_a_ranking_that_does_not_start_at_one_is_refused(synced):
    stored = synced.store()
    synced.connection.execute(
        "UPDATE recommendation_assessments SET rank_position=rank_position+10"
        " WHERE run_id=?",
        (stored.run_id,),
    )
    synced.connection.commit()

    with pytest.raises(RecommendationReadError, match="non-contiguous ranking"):
        read_recommendation_run(synced.connection, stored.run_id)


def test_an_exposed_digest_without_the_form_of_a_sha256_is_refused(synced):
    """Uppercase hex is not the stored form, and is refused rather than folded.

    `0026` checks the same shape on write, so producing the row needs the CHECK
    constraints off. The read model states it again because it is what a
    consumer holds, and because a digest it hands on in another case than the
    one it was written in would not compare equal to the stored one.
    """
    stored = synced.store()
    synced.connection.execute("PRAGMA ignore_check_constraints=ON")
    try:
        corrupt_run(synced.connection, stored.run_id, batch_fingerprint="A" * 64)
        with pytest.raises(RecommendationReadError, match="is not a SHA-256 digest"):
            read_recommendation_run(synced.connection, stored.run_id)

        synced.connection.execute(
            "UPDATE recommendation_assessments SET assessment_fingerprint='abc'"
            " WHERE run_id=? AND rank_position=1",
            (stored.run_id,),
        )
        synced.connection.commit()
        with pytest.raises(RecommendationReadError, match="is not a SHA-256 digest"):
            read_recommendation_run(synced.connection, stored.run_id)
    finally:
        synced.connection.execute("PRAGMA ignore_check_constraints=OFF")


def test_the_column_level_business_contract_is_enforced_on_the_way_out(synced):
    """Rows `0026` itself would refuse still have to be refused when read.

    A CHECK constraint protects the writes this build makes. It does not protect
    a reader from a restore, a hand-edit or a future migration that left a row
    behind, and the read model is the layer a consumer actually holds — so the
    disposition vocabulary and the "no evidence means no score" contract are
    re-stated here. `ignore_check_constraints` is how such a row is produced at
    all; it is switched off again immediately.
    """
    stored = synced.store()
    synced.connection.execute("PRAGMA ignore_check_constraints=ON")
    try:
        synced.connection.execute(
            "UPDATE recommendation_assessments SET disposition='NOT_A_DISPOSITION'"
            " WHERE run_id=? AND rank_position=1",
            (stored.run_id,),
        )
        synced.connection.commit()
        with pytest.raises(RecommendationReadError, match="unknown disposition"):
            read_recommendation_run(synced.connection, stored.run_id)

        synced.connection.execute(
            "UPDATE recommendation_assessments"
            " SET disposition='RECOMMENDED',evidence_coverage=0.0,"
            "recommendation_score=0.0 WHERE run_id=? AND rank_position=1",
            (stored.run_id,),
        )
        synced.connection.commit()
        with pytest.raises(
            RecommendationReadError, match="score availability mismatch"
        ):
            read_recommendation_run(synced.connection, stored.run_id)

        synced.connection.execute(
            "UPDATE recommendation_assessments SET evidence_coverage=1.5,"
            "recommendation_score=0.5 WHERE run_id=? AND rank_position=1",
            (stored.run_id,),
        )
        synced.connection.commit()
        with pytest.raises(RecommendationReadError, match=r"not a ratio in \[0, 1\]"):
            read_recommendation_run(synced.connection, stored.run_id)
    finally:
        synced.connection.execute("PRAGMA ignore_check_constraints=OFF")


# --------------------------------------------------------------------------
# corrupt states
# --------------------------------------------------------------------------


def test_malformed_readiness_issue_json_is_refused(synced):
    synced.store()
    force_state(synced.connection, synced.profile_id, readiness_issues_json="{")

    with pytest.raises(RecommendationReadError, match="invalid readiness issues JSON"):
        read_current_recommendation(synced.connection, synced.profile_id)


def test_readiness_issues_that_are_not_a_canonical_array_are_refused(synced):
    synced.store()
    make_incomplete(synced.connection, synced.profile_id, issues='{"code":"x"}')
    with pytest.raises(RecommendationReadError, match="are not an array"):
        read_current_recommendation(synced.connection, synced.profile_id)

    make_incomplete(
        synced.connection,
        synced.profile_id,
        issues='[{"code": "MATCHING_NOT_READY", "message": "m", "opportunity_id": null}]',
    )
    with pytest.raises(RecommendationReadError, match="not canonical JSON"):
        read_current_recommendation(synced.connection, synced.profile_id)


def test_a_ready_state_carrying_readiness_issues_is_refused(synced):
    synced.store()
    force_state(synced.connection, synced.profile_id, readiness_issues_json=ONE_ISSUE)

    with pytest.raises(
        RecommendationReadError, match="READY state .* carries readiness issues"
    ):
        read_current_recommendation(synced.connection, synced.profile_id)


def test_an_incomplete_state_carrying_no_readiness_issue_is_refused(synced):
    synced.store()
    make_incomplete(synced.connection, synced.profile_id, issues=canonical_json([]))

    with pytest.raises(
        RecommendationReadError, match="INCOMPLETE state .* carries no readiness issue"
    ):
        read_current_recommendation(synced.connection, synced.profile_id)


def test_an_unknown_readiness_issue_code_is_refused_rather_than_passed_through(synced):
    synced.store()
    make_incomplete(
        synced.connection,
        synced.profile_id,
        issues=readiness_json(("NOT_A_REAL_CODE", "invented", None)),
    )

    with pytest.raises(RecommendationReadError, match="unknown readiness issue code"):
        read_current_recommendation(synced.connection, synced.profile_id)


@pytest.mark.parametrize("opportunity_id", [True, False, 0, -1, "7", 1.0])
def test_a_readiness_issue_with_an_invalid_opportunity_id_is_refused(
    synced, opportunity_id
):
    synced.store()
    make_incomplete(
        synced.connection,
        synced.profile_id,
        issues=readiness_json(
            (
                RecommendationReadinessIssueCode.OPPORTUNITY_MISSING.value,
                "gone",
                opportunity_id,
            )
        ),
    )

    with pytest.raises(
        RecommendationReadError, match="readiness issue opportunity_id .* is invalid"
    ):
        read_current_recommendation(synced.connection, synced.profile_id)


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"code": "MATCHING_NOT_READY", "message": "m"}, "malformed readiness issue"),
        (
            {"code": "MATCHING_NOT_READY", "message": "m", "opportunity_id": None,
             "extra": 1},
            "malformed readiness issue",
        ),
        (
            {"code": 7, "message": "m", "opportunity_id": None},
            "readiness issue code .* is not text",
        ),
        (
            {"code": "MATCHING_NOT_READY", "message": "  ", "opportunity_id": None},
            "readiness issue message .* is not text",
        ),
        ("MATCHING_NOT_READY", "malformed readiness issue"),
    ],
)
def test_a_readiness_issue_of_the_wrong_shape_is_refused(synced, entry, expected):
    synced.store()
    make_incomplete(synced.connection, synced.profile_id, issues=canonical_json([entry]))

    with pytest.raises(RecommendationReadError, match=expected):
        read_current_recommendation(synced.connection, synced.profile_id)


def test_readiness_issues_out_of_assembly_order_are_refused_not_re_sorted(synced):
    """The stored array is the assembly's order, and it is verified, not fixed.

    `input_assembly` sorts its issues profile-wide first, then per posting, then
    by code. Re-sorting what was read would make a corrupted array look correct
    and would hide exactly the defect this refuses.
    """
    synced.store()
    out_of_order = canonical_json(
        [
            {
                "code": RecommendationReadinessIssueCode.OPPORTUNITY_MISSING.value,
                "message": "posting first",
                "opportunity_id": 4,
            },
            {
                "code": RecommendationReadinessIssueCode.MATCHING_NOT_READY.value,
                "message": "profile-wide second",
                "opportunity_id": None,
            },
        ]
    )
    make_incomplete(synced.connection, synced.profile_id, issues=out_of_order)

    with pytest.raises(RecommendationReadError, match="not in assembly order"):
        read_current_recommendation(synced.connection, synced.profile_id)


def test_a_state_whose_versions_disagree_with_the_run_it_names_is_refused(synced):
    synced.store()
    force_state(
        synced.connection, synced.profile_id, input_assembly_version="other-assembly-v9"
    )

    with pytest.raises(
        RecommendationReadError, match="state versions .* disagree with current run"
    ):
        read_current_recommendation(synced.connection, synced.profile_id)


def test_a_current_run_belonging_to_another_profile_is_refused(database):
    """The composite foreign key already forbids this; the reader still checks.

    `recommendation_profile_state` references `(current_run_id, profile_id)`, so
    a cross-profile pointer cannot be written while foreign keys are on. It can
    exist in a database restored or edited with them off, and a read model that
    followed such a pointer would hand one person another person's ranking.
    """
    profiles = []
    for email, suffix in ((TEST_ONLY_EMAIL, "x"), (TEST_ONLY_OTHER_EMAIL, "y")):
        profile_id = ensure_user_profile(database, email).profile_id
        ids = tuple(add_opportunity(database, f"{suffix}{n}") for n in range(2))
        database.commit()
        matching = store_matching_batch(
            database,
            profile_id,
            make_matching_batch(profile_id, ids),
            selection_version=SELECTION_VERSION,
        )
        fixture = Fixture(database, profile_id, ids, matching)
        profiles.append((fixture, fixture.store()))

    (first, _), (second, other_run) = profiles
    database.execute("PRAGMA foreign_keys=OFF")
    force_state(database, first.profile_id, current_run_id=other_run.run_id)
    database.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(RecommendationReadError, match="belongs to profile"):
        read_current_recommendation(database, first.profile_id)
    with pytest.raises(RecommendationReadError, match="is not in profile .* history"):
        list_recommendation_runs(database, first.profile_id)
    assert read_current_recommendation(database, second.profile_id).status == "READY"


def test_an_unknown_stored_state_value_is_refused(synced):
    synced.store()
    synced.connection.execute("PRAGMA ignore_check_constraints=ON")
    try:
        force_state(synced.connection, synced.profile_id, state="ALMOST_READY")
        for read in (read_current_recommendation, list_recommendation_runs):
            with pytest.raises(
                RecommendationReadError, match="unknown recommendation state"
            ):
                read(synced.connection, synced.profile_id)
    finally:
        synced.connection.execute("PRAGMA ignore_check_constraints=OFF")


def test_an_incomplete_state_that_still_names_a_run_is_refused(synced):
    stored = synced.store()
    synced.connection.execute("PRAGMA ignore_check_constraints=ON")
    try:
        force_state(
            synced.connection,
            synced.profile_id,
            state="INCOMPLETE",
            current_run_id=stored.run_id,
            readiness_issues_json=ONE_ISSUE,
        )
        for read in (read_current_recommendation, list_recommendation_runs):
            with pytest.raises(
                RecommendationReadError, match="INCOMPLETE state .* names a current run"
            ):
                read(synced.connection, synced.profile_id)
    finally:
        synced.connection.execute("PRAGMA ignore_check_constraints=OFF")


# --------------------------------------------------------------------------
# immutability, freshness, and read-only behaviour
# --------------------------------------------------------------------------


def test_returned_payloads_cannot_be_mutated_at_any_depth(synced):
    stored = synced.store()
    run = read_recommendation_run(synced.connection, stored.run_id)

    with pytest.raises(TypeError):
        run.batch_payload["assessment_count"] = 0
    assert isinstance(run.batch_payload["ranked_assessment_fingerprints"], tuple)
    with pytest.raises(AttributeError):
        run.batch_payload["ranked_assessment_fingerprints"].append("x")

    payload = run.assessments[0].assessment_payload
    with pytest.raises(TypeError):
        payload["versions"] = {}
    with pytest.raises(TypeError):
        # A nested object is frozen too, not merely the outermost one.
        payload["versions"]["recommendation_engine"] = "tampered"
    evidence = payload["required_skill"]["evidence"]
    assert isinstance(evidence, tuple)
    if evidence:
        with pytest.raises(TypeError):
            evidence[0]["canonical_key"] = "tampered"
        assert isinstance(evidence[0]["sources"], tuple)

    # The read models themselves are frozen values.
    for frozen in (run, run.assessments[0]):
        with pytest.raises(Exception):
            frozen.run_id = 999
    # And a second read is unaffected by any of the attempts above.
    assert read_recommendation_run(synced.connection, stored.run_id) == run


def test_an_older_run_over_a_superseded_matching_snapshot_stays_valid_history(synced):
    """Freshness is Phase 9B.3's question, and this module never asks it."""
    first = synced.store(synced.batch(synced.opportunity_ids[:2]))
    # A different cohort, so this is genuinely a second matching run rather than
    # the idempotent reuse of the fixture's own.
    newer_matching = store_matching_batch(
        synced.connection,
        synced.profile_id,
        make_matching_batch(synced.profile_id, synced.opportunity_ids[:2]),
        selection_version=SELECTION_VERSION,
    )
    assert newer_matching.run_id != synced.matching_run_id
    # The same cohort, ranked identically, over the *newer* snapshot: same batch
    # content digest, different operational identity, so it is a second run.
    second = synced.store(
        synced.batch(synced.opportunity_ids[:2]),
        source_matching_run_id=newer_matching.run_id,
        source_matching_run_fingerprint=newer_matching.run_fingerprint,
    )
    assert second.created is True and second.run_id != first.run_id
    assert (
        synced.connection.execute(
            "SELECT current_run_id FROM matching_profile_state WHERE profile_id=?",
            (synced.profile_id,),
        ).fetchone()[0]
        == newer_matching.run_id
    )

    history = list_recommendation_runs(synced.connection, synced.profile_id)

    assert [item.run_id for item in history] == [second.run_id, first.run_id]
    assert [item.source_matching_run_id for item in history] == [
        newer_matching.run_id,
        synced.matching_run_id,
    ]
    # The superseded one is still fully readable, and is not flagged in any way.
    stale = read_recommendation_run(synced.connection, first.run_id)
    assert stale.source_matching_run_id == synced.matching_run_id
    assert [item.rank_position for item in stale.assessments] == [1, 2]


def test_every_read_leaves_the_database_byte_for_byte_unchanged(synced):
    first = synced.store(synced.batch(synced.opportunity_ids[:2]))
    stored = synced.store(synced.batch())
    synced.connection.commit()
    before_changes = synced.connection.total_changes
    before_rows = snapshot(synced.connection)

    read_current_recommendation(synced.connection, synced.profile_id)
    read_recommendation_run(synced.connection, stored.run_id)
    read_recommendation_run(synced.connection, first.run_id)
    list_recommendation_runs(synced.connection, synced.profile_id)
    with pytest.raises(RecommendationReadError):
        read_recommendation_run(synced.connection, 99999)

    assert synced.connection.total_changes == before_changes
    assert snapshot(synced.connection) == before_rows
    assert synced.connection.in_transaction is False


def test_the_module_never_writes_and_the_reads_work_on_a_read_only_connection(
    synced, database_path
):
    stored = synced.store()
    synced.connection.commit()
    synced.connection.close()

    read_only = connect_readonly_database(database_path)
    try:
        # The connection really is read-only: a write on it fails.
        with pytest.raises(sqlite3.OperationalError):
            read_only.execute(
                "UPDATE recommendation_profile_state SET state='INCOMPLETE'"
            )

        model = read_current_recommendation(read_only, synced.profile_id)
        assert model.status == "READY"
        assert model.current_run_id == stored.run_id
        assert model.current_run is not None
        assert [item.rank_position for item in model.current_run.assessments] == [
            1,
            2,
            3,
        ]
        assert read_recommendation_run(read_only, stored.run_id) == model.current_run
        history = list_recommendation_runs(read_only, synced.profile_id)
        assert [item.run_id for item in history] == [stored.run_id]
        assert history[0].is_current is True
        assert read_only.total_changes == 0
    finally:
        read_only.close()


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


def executed_sql():
    """Every SQL string the read model can hand to SQLite, from its own AST.

    Narrower than scanning the file, and stronger: prose in a docstring is not
    what runs, and a keyword smuggled through an f-string interpolation would
    slip past a plain grep. Two shapes reach SQLite — a literal handed straight
    to `connection.execute(...)`, and a literal handed to the `_query` helper,
    which is the module's single indirection — and both are collected. Any
    *other* computed statement fails the walk rather than being skipped.
    """
    from services.recommendation import read_model

    with open(read_model.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    statements = []
    for node in ast.walk(tree):
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


def test_the_read_model_can_only_execute_reads_and_its_own_transaction_control():
    """A structural guarantee, not a stylistic one: this module cannot write.

    The invariant moved when the read-snapshot boundary landed: a read
    transaction legitimately needs `BEGIN` and a way to release it, so those are
    allowed — and *only* those. Everything that could change a row, and
    `BEGIN IMMEDIATE` in particular, stays prohibited: reserving the write lock
    is exactly what a reader must not do, and it would fail on a `mode=ro`
    connection.
    """
    statements = executed_sql()
    assert statements, "no SQL found; the AST walk is not seeing the module"

    allowed_openings = ("SELECT ", "BEGIN", "COMMIT", "ROLLBACK")
    for statement in statements:
        normalized = " ".join(statement.split()).upper()
        assert normalized.startswith(allowed_openings), statement
        if normalized.startswith("BEGIN"):
            # BEGIN or BEGIN DEFERRED, never IMMEDIATE and never EXCLUSIVE.
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


def test_the_read_model_never_calls_a_connection_transaction_method():
    """`connection.commit()` would end a transaction this module may not own.

    Ownership is decided by `in_transaction` and released with an explicit
    `ROLLBACK` on the transaction this module opened itself. A bare
    `.commit()` / `.rollback()` — or `executescript`, which commits implicitly —
    would bypass that decision and could finish a caller's transaction.
    """
    from services.recommendation import read_model

    with open(read_model.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("commit", "rollback", "executemany", "executescript", "cursor"):
        assert forbidden not in called, forbidden


def test_a_sqlite_failure_is_reported_as_a_read_error_with_its_cause(synced, tmp_path):
    """`sqlite3.Error` is not this module's public contract; the cause survives."""
    empty = connect_database(tmp_path / "no-migrations.db")
    try:
        with pytest.raises(RecommendationReadError) as raised:
            read_current_recommendation(empty, 1)
        assert isinstance(raised.value.__cause__, sqlite3.Error)
        assert "cannot read" in str(raised.value)
    finally:
        empty.close()


def test_the_stored_envelope_the_reader_returns_is_the_one_on_disk(synced):
    """No re-encoding, no reordering: what is decoded is what was written."""
    stored = synced.store()
    raw = synced.connection.execute(
        "SELECT batch_payload_json FROM recommendation_runs WHERE id=?",
        (stored.run_id,),
    ).fetchone()[0]
    run = read_recommendation_run(synced.connection, stored.run_id)

    assert json.loads(raw) == json.loads(canonical_json(json.loads(raw)))
    decoded = json.loads(raw)
    assert set(run.batch_payload) == set(decoded)
    assert run.batch_payload["ranked_assessment_fingerprints"] == tuple(
        decoded["ranked_assessment_fingerprints"]
    )


# --------------------------------------------------------------------------
# snapshot consistency
#
# Every public read is several SELECTs, and without a boundary each is its own
# implicit read transaction. The tests below interleave a *real* commit from a
# second connection between two of the read model's own statements — no sleep,
# no timing, the hook fires on a named statement — and require the projection
# handed back to be one database state rather than a blend of two.
# --------------------------------------------------------------------------


class InterleavingConnection(sqlite3.Connection):
    """Runs a callback once, immediately before a chosen statement.

    This is how a concurrent commit is placed at an exact point inside a public
    read without a sleep: the hook fires on the first statement whose SQL
    contains `marker`, which is a position in the read model's own sequence,
    not a moment in wall-clock time.
    """

    marker = None
    action = None
    fired = False

    def execute(self, sql, parameters=()):
        if self.action is not None and not self.fired and self.marker in sql:
            self.fired = True
            self.action()
        return super().execute(sql, parameters)


def wal_fixture(path):
    """A migrated database in WAL mode, plus the profile and matching run.

    Production runs SQLite's default rollback journal, where this module's read
    transaction locks a concurrent writer out completely — the guarantee holds
    there too, and is asserted separately below. WAL is used here because it is
    the mode in which the writer genuinely *succeeds* while a reader holds a
    snapshot, which is what makes "one stable snapshot" observable rather than
    merely unfalsifiable.
    """
    connection = connect_database(path)
    connection.execute("PRAGMA journal_mode=WAL")
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
    ids = tuple(add_opportunity(connection, f"wal-{suffix}") for suffix in "abc")
    connection.commit()
    matching = store_matching_batch(
        connection,
        profile_id,
        make_matching_batch(profile_id, ids),
        selection_version=SELECTION_VERSION,
    )
    connection.commit()
    return Fixture(connection, profile_id, ids, matching)


def reader_for(path, marker, action):
    reader = sqlite3.connect(path, factory=InterleavingConnection)
    reader.marker = marker
    reader.action = action
    return reader


def test_read_current_recommendation_observes_one_snapshot(tmp_path):
    """A newer run committed mid-read must not blend into the older picture."""
    path = tmp_path / "snapshot-current.db"
    writer = wal_fixture(path)
    first = writer.store(writer.batch(writer.opportunity_ids[:2]))
    writer.connection.commit()

    appended = []

    def append_a_newer_run():
        # Lands between the history count and the state row: without one
        # snapshot the result would carry a count of 1 beside a pointer at
        # run 2 — a database state that never existed.
        second = writer.store(writer.batch())
        writer.connection.commit()
        appended.append(second.run_id)

    reader = reader_for(path, "recommendation_profile_state", append_a_newer_run)
    try:
        model = read_current_recommendation(reader, writer.profile_id)
        assert reader.fired, "the interleaved commit never ran"
        assert reader.in_transaction is False
    finally:
        reader.close()

    assert appended and appended[0] != first.run_id
    assert model.status == "READY"
    assert model.history_count == 1
    assert model.current_run_id == first.run_id
    assert model.current_run.run_id == first.run_id
    assert len(model.current_run.assessments) == 2

    # The commit was real: once the snapshot is over, the newer state is there.
    fresh = read_current_recommendation(writer.connection, writer.profile_id)
    assert fresh.history_count == 2
    assert fresh.current_run_id == appended[0]
    writer.connection.close()


def test_list_recommendation_runs_observes_one_snapshot(tmp_path):
    """The listing and the pointer it marks `is_current` with are one picture."""
    path = tmp_path / "snapshot-history.db"
    writer = wal_fixture(path)
    first = writer.store(writer.batch(writer.opportunity_ids[:2]))
    writer.connection.commit()

    appended = []

    def append_a_newer_run():
        # Lands between the state row and the run listing: without one snapshot
        # the listing would show run 2 while `is_current` still marked run 1.
        second = writer.store(writer.batch())
        writer.connection.commit()
        appended.append(second.run_id)

    reader = reader_for(path, "ORDER BY id DESC", append_a_newer_run)
    try:
        history = list_recommendation_runs(reader, writer.profile_id)
        assert reader.fired, "the interleaved commit never ran"
        assert reader.in_transaction is False
    finally:
        reader.close()

    assert appended
    assert [item.run_id for item in history] == [first.run_id]
    assert [item.is_current for item in history] == [True]

    after = list_recommendation_runs(writer.connection, writer.profile_id)
    assert [item.run_id for item in after] == [appended[0], first.run_id]
    assert [item.is_current for item in after] == [True, False]
    writer.connection.close()


def test_read_recommendation_run_observes_one_snapshot(tmp_path):
    """A run and its assessments are read from one state, or not at all."""
    path = tmp_path / "snapshot-run.db"
    writer = wal_fixture(path)
    stored = writer.store(writer.batch())
    writer.connection.commit()

    def remove_an_assessment():
        # Lands between the run row and its assessments. Without one snapshot
        # the reader would hold `assessment_count == 3` from before and find two
        # rows after, and report a corruption that is really a race.
        writer.connection.execute(
            "DELETE FROM recommendation_assessments"
            " WHERE run_id=? AND rank_position=3",
            (stored.run_id,),
        )
        writer.connection.commit()

    reader = reader_for(path, "FROM recommendation_assessments", remove_an_assessment)
    try:
        run = read_recommendation_run(reader, stored.run_id)
        assert reader.fired, "the interleaved delete never ran"
        assert reader.in_transaction is False
    finally:
        reader.close()

    assert run.assessment_count == 3
    assert [item.rank_position for item in run.assessments] == [1, 2, 3]

    # The delete was real, and is visible — as a genuine corruption — after it.
    with pytest.raises(RecommendationReadError, match="assessment count mismatch"):
        read_recommendation_run(writer.connection, stored.run_id)
    writer.connection.close()


def test_the_default_rollback_journal_locks_a_writer_out_of_the_snapshot(tmp_path):
    """The same guarantee in the mode production actually runs.

    Under SQLite's default rollback journal there is no second snapshot to give
    a writer, so a commit attempted inside the read model's read transaction is
    refused outright. Both modes give the reader one consistent state; only the
    writer's experience differs.
    """
    path = tmp_path / "snapshot-delete-journal.db"
    connection = connect_database(path)
    apply_migrations(connection)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
    ids = tuple(add_opportunity(connection, f"dj-{suffix}") for suffix in "ab")
    connection.commit()
    matching = store_matching_batch(
        connection,
        profile_id,
        make_matching_batch(profile_id, ids),
        selection_version=SELECTION_VERSION,
    )
    writer = Fixture(connection, profile_id, ids, matching)
    writer.store()
    connection.commit()

    refused = []

    def try_to_commit_mid_read():
        # timeout=0 so the refusal is immediate: this asserts a lock, not a wait.
        other = sqlite3.connect(path, timeout=0)
        try:
            # A perfectly legal write — only the lock may stop it.
            other.execute(
                "UPDATE recommendation_profile_state SET updated_at='2020-01-01'"
                " WHERE profile_id=?",
                (profile_id,),
            )
            other.commit()
        except sqlite3.OperationalError as error:
            refused.append(str(error))
            other.rollback()
        finally:
            other.close()

    reader = reader_for(path, "recommendation_profile_state", try_to_commit_mid_read)
    try:
        model = read_current_recommendation(reader, profile_id)
    finally:
        reader.close()

    assert reader.fired
    assert refused and "locked" in refused[0]
    assert model.status == "READY"
    connection.close()


def test_a_caller_owned_transaction_is_borrowed_and_never_finished(synced):
    """The read model may join a caller's transaction; it may never end one.

    The caller's own uncommitted change is the probe. If the read model had
    committed, the rollback afterwards could not undo it; if it had rolled back,
    the change would already be gone before the rollback.
    """
    synced.store()
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

    assert read_current_recommendation(synced.connection, synced.profile_id).status == (
        "READY"
    )
    assert synced.connection.in_transaction is True
    assert organization() == "CALLER-PENDING"

    assert list_recommendation_runs(synced.connection, synced.profile_id)
    assert synced.connection.in_transaction is True
    assert organization() == "CALLER-PENDING"

    assert read_recommendation_run(synced.connection, 1).run_id == 1
    assert synced.connection.in_transaction is True
    assert organization() == "CALLER-PENDING"

    # Still the caller's to end, and still undoable — so nothing committed it.
    synced.connection.execute("ROLLBACK")
    assert synced.connection.in_transaction is False
    assert organization() == original


def test_a_failing_read_releases_the_snapshot_it_opened(synced):
    """An exception must not leave the connection holding a read transaction."""
    stored = synced.store()
    synced.connection.commit()
    assert synced.connection.in_transaction is False

    # A run that does not exist: the failure happens inside the snapshot.
    with pytest.raises(RecommendationReadError, match="does not exist"):
        read_recommendation_run(synced.connection, 99999)
    assert synced.connection.in_transaction is False

    corrupt_run(synced.connection, stored.run_id, batch_payload_json="{")
    assert synced.connection.in_transaction is False
    for read, argument in (
        (read_recommendation_run, stored.run_id),
        (read_current_recommendation, synced.profile_id),
    ):
        with pytest.raises(RecommendationReadError):
            read(synced.connection, argument)
        assert synced.connection.in_transaction is False

    # And the connection is still perfectly usable afterwards.
    assert synced.connection.execute("SELECT 1").fetchone() == (1,)
    assert list_recommendation_runs(synced.connection, synced.profile_id)
    assert synced.connection.in_transaction is False


def test_the_snapshot_boundary_works_on_a_read_only_connection(
    synced, database_path
):
    """A deferred read transaction takes no write lock, so `mode=ro` is fine."""
    stored = synced.store()
    synced.connection.commit()
    synced.connection.close()

    read_only = connect_readonly_database(database_path)
    try:
        assert read_only.in_transaction is False
        assert read_current_recommendation(read_only, synced.profile_id).status == (
            "READY"
        )
        assert read_only.in_transaction is False
        assert read_recommendation_run(read_only, stored.run_id).run_id == stored.run_id
        assert read_only.in_transaction is False
        assert len(list_recommendation_runs(read_only, synced.profile_id)) == 1
        assert read_only.in_transaction is False
        with pytest.raises(RecommendationReadError):
            read_recommendation_run(read_only, 99999)
        assert read_only.in_transaction is False
        assert read_only.total_changes == 0
    finally:
        read_only.close()


# --------------------------------------------------------------------------
# strict validation of history summaries
#
# The listing used to build a summary straight out of the cursor, which made it
# the one public read with a weaker contract than the rest of the module: a row
# `read_recommendation_run` refuses would still be handed out as history. Each
# field it exposes is corrupted below and the listing must refuse it.
# --------------------------------------------------------------------------


@contextmanager
def constraints_off(connection):
    """Write a row `0026` itself would refuse, then put the guards back.

    The schema already rejects most of these values on write. That protects the
    writes *this build* makes; it does not protect a reader from a restore, a
    hand-edit or a future migration, and the read model is the layer a consumer
    holds. Foreign keys go off alongside the CHECKs because some of the corrupt
    values below are also dangling references.
    """
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        yield
    finally:
        connection.execute("PRAGMA ignore_check_constraints=OFF")
        connection.execute("PRAGMA foreign_keys=ON")


@pytest.mark.parametrize(
    "column,value,expected",
    [
        ("id", 0, "run_id in recommendation history must be a positive integer"),
        ("id", -5, "run_id in recommendation history must be a positive integer"),
        ("assessment_count", 0, "assessment_count of .* is not a positive integer"),
        ("assessment_count", -3, "assessment_count of .* is not a positive integer"),
        (
            "source_matching_run_id",
            0,
            "source_matching_run_id of .* must be a positive integer",
        ),
        ("created_at", "", "created_at of .* is not stored text"),
        ("created_at", "   ", "created_at of .* is not stored text"),
        ("created_at", " 2026-01-01 ", "created_at of .* is not stored text"),
        ("persistence_version", "  ", "persistence_version of .* is not stored text"),
        (
            "input_assembly_version",
            "",
            "input_assembly_version of .* is not stored text",
        ),
        (
            "recommendation_engine_version",
            " ",
            "recommendation_engine_version of .* is not stored text",
        ),
        (
            "recommendation_rules_version",
            "",
            "recommendation_rules_version of .* is not stored text",
        ),
        (
            "source_matching_run_fingerprint",
            "A" * 64,
            "source_matching_run_fingerprint of .* is not a SHA-256 digest",
        ),
        (
            "source_matching_run_fingerprint",
            "abc",
            "source_matching_run_fingerprint of .* is not a SHA-256 digest",
        ),
        (
            "batch_fingerprint",
            "z" * 64,
            "batch_fingerprint of .* is not a SHA-256 digest",
        ),
        (
            "batch_fingerprint",
            "",
            "batch_fingerprint of .* is not a SHA-256 digest",
        ),
        (
            "run_fingerprint",
            "0" * 63,
            "run_fingerprint of .* is not a SHA-256 digest",
        ),
        (
            "run_fingerprint",
            "F" * 64,
            "run_fingerprint of .* is not a SHA-256 digest",
        ),
    ],
)
def test_a_corrupt_history_row_is_refused_by_the_listing(synced, column, value, expected):
    synced.store()
    # The listing works before the corruption, so the refusal is provably the
    # corrupted field and not some other defect in the fixture.
    assert len(list_recommendation_runs(synced.connection, synced.profile_id)) == 1

    with constraints_off(synced.connection):
        corrupt_run(synced.connection, 1, **{column: value})

    with pytest.raises(RecommendationReadError, match=expected):
        list_recommendation_runs(synced.connection, synced.profile_id)


def test_the_listing_refuses_exactly_what_the_full_run_reader_refuses(synced):
    """The two public readers must not disagree about what is readable.

    A field exposed by both is validated by both, under the same policy — the
    history listing is not a back door around `read_recommendation_run`.
    """
    stored = synced.store()
    with constraints_off(synced.connection):
        corrupt_run(synced.connection, stored.run_id, batch_fingerprint="A" * 64)

    with pytest.raises(RecommendationReadError, match="is not a SHA-256 digest"):
        read_recommendation_run(synced.connection, stored.run_id)
    with pytest.raises(RecommendationReadError, match="is not a SHA-256 digest"):
        list_recommendation_runs(synced.connection, synced.profile_id)
    with pytest.raises(RecommendationReadError, match="is not a SHA-256 digest"):
        read_current_recommendation(synced.connection, synced.profile_id)


def test_validating_a_summary_still_loads_no_assessment_row(synced):
    """Strictness must not turn the index into a bulk read.

    The listing is proven to touch no assessment: every statement it issues is
    counted, and none of them names `recommendation_assessments`.
    """
    synced.store(synced.batch(synced.opportunity_ids[:2]))
    synced.store(synced.batch())
    synced.connection.commit()

    seen = []
    synced.connection.set_trace_callback(seen.append)
    try:
        history = list_recommendation_runs(synced.connection, synced.profile_id)
    finally:
        synced.connection.set_trace_callback(None)

    assert len(history) == 2
    assert seen, "the trace callback saw nothing"
    assert not any("recommendation_assessments" in statement for statement in seen), seen
    assert not any("payload" in statement for statement in seen), seen
