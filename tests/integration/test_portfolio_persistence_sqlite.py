from dataclasses import replace

import pytest

from services.digital_twin.repository import ensure_user_profile
from services.collector.matching import (
    MATCHING_SELECTION_VERSION,
    MatchingBatchResult,
    matching_batch_fingerprint,
    store_matching_batch,
)
from services.portfolio import (
    PortfolioDisposition,
    PortfolioPersistenceError,
    PortfolioReasonCode,
    assemble_portfolio_inputs,
    build_portfolio_assessments,
    portfolio_assessment_fingerprint,
    store_portfolio_batch,
)
from tests.integration.test_priority_persistence_sqlite import priority_fixture, store
from tests.integration.test_priority_dry_run_sqlite import _batch


def portfolio_fixture(tmp_path, opportunities=2):
    connection, identity, opportunity_ids, priority_arguments = priority_fixture(
        tmp_path, opportunities=opportunities
    )
    priority = store(connection, priority_arguments)
    assembly = assemble_portfolio_inputs(connection, identity.profile_id)
    arguments = dict(
        profile_id=identity.profile_id,
        priority_run_id=priority.run_id,
        priority_run_fingerprint=priority.run_fingerprint,
        matching_run_id=assembly.matching_run_id,
        matching_run_fingerprint=assembly.matching_run_fingerprint,
        assessments=build_portfolio_assessments(assembly.inputs),
    )
    return connection, identity, opportunity_ids, arguments


def persist(connection, arguments):
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = store_portfolio_batch(connection, **arguments)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


def snapshot(connection):
    return (
        connection.execute("SELECT COUNT(*) FROM portfolio_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM portfolio_assessments").fetchone()[0],
        connection.execute(
            "SELECT * FROM portfolio_profile_state ORDER BY profile_id"
        ).fetchall(),
    )


def refingerprint(assessment, **changes):
    changed = replace(assessment, **changes, assessment_fingerprint="")
    return replace(
        changed,
        assessment_fingerprint=portfolio_assessment_fingerprint(changed),
    )


def create_matching_run(connection, profile_id, opportunity_id):
    item = _batch(profile_id, opportunity_id).assessments[0]
    batch = MatchingBatchResult((item,), "c" * 64, "d" * 64, 1)
    batch = replace(batch, batch_fingerprint=matching_batch_fingerprint(batch))
    return store_matching_batch(
        connection,
        profile_id,
        batch,
        selection_version=MATCHING_SELECTION_VERSION,
    )


def assert_rejected_without_writes(connection, arguments, match):
    before = snapshot(connection)
    with pytest.raises(PortfolioPersistenceError, match=match):
        persist(connection, arguments)
    assert snapshot(connection) == before
    assert connection.in_transaction is False


def test_all_assessments_are_persisted_and_identical_store_is_byte_noop(tmp_path):
    connection, _, ids, arguments = portfolio_fixture(tmp_path, 3)
    first = persist(connection, arguments)
    state = connection.execute("SELECT * FROM portfolio_profile_state").fetchone()
    dump = tuple(connection.iterdump())
    second = persist(connection, arguments)

    assert (first.created, first.state_changed) == (True, True)
    assert (second.created, second.state_changed) == (False, False)
    assert (second.run_id, second.run_fingerprint) == (
        first.run_id,
        first.run_fingerprint,
    )
    rows = connection.execute(
        "SELECT opportunity_id,disposition,bucket FROM portfolio_assessments ORDER BY opportunity_id"
    ).fetchall()
    assert len(rows) == len(ids)
    assert {row[1] for row in rows} <= {"INCLUDED", "EXCLUDED"}
    assert all((row[1] == "EXCLUDED") == (row[2] is None) for row in rows)
    assert (
        connection.execute("SELECT * FROM portfolio_profile_state").fetchone() == state
    )
    assert tuple(connection.iterdump()) == dump


def test_store_requires_transaction_and_non_empty_tuple(tmp_path):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    with pytest.raises(PortfolioPersistenceError, match="active transaction"):
        store_portfolio_batch(connection, **arguments)
    assert_rejected_without_writes(
        connection, arguments | {"assessments": ()}, "non-empty tuple"
    )


def test_mixed_profile_and_duplicate_opportunities_are_rejected(tmp_path):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    first, second = arguments["assessments"]
    assert_rejected_without_writes(
        connection,
        arguments | {"assessments": (first, replace(second, profile_id=999))},
        "profile mismatch",
    )
    assert_rejected_without_writes(
        connection,
        arguments | {"assessments": (first, first)},
        "duplicate opportunity",
    )


def test_invalid_disposition_bucket_and_required_skill_ratio_are_rejected(tmp_path):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    item = arguments["assessments"][0]
    invalid_result = refingerprint(
        item,
        disposition=PortfolioDisposition.INCLUDED,
        bucket=None,
    )
    assert_rejected_without_writes(
        connection,
        arguments | {"assessments": (invalid_result,)},
        "disposition/bucket",
    )
    invalid_ratio = refingerprint(
        item,
        required_skill_score=0.5,
        required_skill_matched_count=0,
        required_skill_total_count=1,
    )
    assert_rejected_without_writes(
        connection,
        arguments | {"assessments": (invalid_ratio,)},
        "required-skill score",
    )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"priority_run_id": 999_999}, "Priority run"),
        ({"priority_run_fingerprint": "f" * 64}, "Priority run"),
        ({"matching_run_id": 999_999}, "Matching run"),
        ({"matching_run_fingerprint": "f" * 64}, "Matching run"),
    ],
)
def test_missing_or_wrong_upstream_run_provenance_is_rejected(
    tmp_path, override, message
):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    assert_rejected_without_writes(connection, arguments | override, message)


def test_priority_profile_mismatch_is_rejected(tmp_path):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    other = ensure_user_profile(connection, "other-portfolio@example.invalid")
    assessments = tuple(
        replace(item, profile_id=other.profile_id) for item in arguments["assessments"]
    )
    assert_rejected_without_writes(
        connection,
        arguments | {"profile_id": other.profile_id, "assessments": assessments},
        "Priority run",
    )


def test_matching_profile_and_priority_to_matching_mismatch_are_rejected(tmp_path):
    connection, _, opportunity_ids, arguments = portfolio_fixture(tmp_path)
    other = ensure_user_profile(connection, "foreign-matching@example.invalid")
    foreign = create_matching_run(connection, other.profile_id, opportunity_ids[0])
    assert_rejected_without_writes(
        connection,
        arguments
        | {
            "matching_run_id": foreign.run_id,
            "matching_run_fingerprint": foreign.run_fingerprint,
        },
        "Matching run",
    )
    same_profile = create_matching_run(
        connection, arguments["profile_id"], opportunity_ids[0]
    )
    assert_rejected_without_writes(
        connection,
        arguments
        | {
            "matching_run_id": same_profile.run_id,
            "matching_run_fingerprint": same_profile.run_fingerprint,
        },
        "Matching run",
    )


@pytest.mark.parametrize(
    "field",
    ["priority_assessment_fingerprint", "matching_assessment_fingerprint"],
)
def test_per_assessment_fingerprint_and_exact_cohort_are_enforced(tmp_path, field):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    item = refingerprint(arguments["assessments"][0], **{field: "f" * 64})
    assert_rejected_without_writes(
        connection,
        arguments | {"assessments": (item, *arguments["assessments"][1:])},
        "cohort provenance",
    )
    assert_rejected_without_writes(
        connection,
        arguments | {"assessments": arguments["assessments"][:-1]},
        "cohort provenance",
    )
    supplemental = refingerprint(arguments["assessments"][0], opportunity_id=999_999)
    assert_rejected_without_writes(
        connection,
        arguments | {"assessments": (*arguments["assessments"], supplemental)},
        "cohort provenance",
    )


def test_invalid_assessment_fingerprint_is_rejected_before_writes(tmp_path):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    bad = replace(arguments["assessments"][0], assessment_fingerprint="0" * 64)
    assert_rejected_without_writes(
        connection, arguments | {"assessments": (bad,)}, "fingerprint"
    )


@pytest.mark.parametrize(
    ("table", "assignment"),
    [
        ("portfolio_runs", "run_payload_json='{}'"),
        ("portfolio_assessments", "assessment_payload_json='{}'"),
        ("portfolio_assessments", "eligibility_status='ELIGIBLE'"),
    ],
)
def test_existing_run_or_assessment_corruption_is_rejected(tmp_path, table, assignment):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    result = persist(connection, arguments)
    connection.execute(
        f"UPDATE {table} SET {assignment} WHERE "
        + ("id=?" if table == "portfolio_runs" else "run_id=? LIMIT 1"),
        (result.run_id,),
    )
    connection.commit()
    state_before = connection.execute(
        "SELECT * FROM portfolio_profile_state"
    ).fetchone()
    with pytest.raises(PortfolioPersistenceError, match="corrupt"):
        persist(connection, arguments)
    assert (
        connection.execute("SELECT * FROM portfolio_profile_state").fetchone()
        == state_before
    )


def test_a_to_b_to_a_reuses_older_append_only_run(tmp_path):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    first = persist(connection, arguments)
    item = arguments["assessments"][0]
    changed = refingerprint(
        item,
        reason_codes=item.reason_codes
        + (PortfolioReasonCode.REQUIRED_SKILL_EVIDENCE_MISSING,),
    )
    second_args = arguments | {"assessments": (changed, *arguments["assessments"][1:])}
    second = persist(connection, second_args)
    reused = persist(connection, arguments)

    assert (first.created, second.created) == (True, True)
    assert (reused.created, reused.state_changed) == (False, True)
    assert reused.run_id == first.run_id < second.run_id
    assert (
        connection.execute(
            "SELECT current_run_id FROM portfolio_profile_state"
        ).fetchone()[0]
        == first.run_id
    )
    assert (
        connection.execute("SELECT MAX(id) FROM portfolio_runs").fetchone()[0]
        == second.run_id
    )
