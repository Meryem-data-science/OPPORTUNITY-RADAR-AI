from datetime import date
import json

import pytest

from services.priority import (
    PriorityAuditIssueCode,
    PriorityAuditStatus,
    PriorityProfileAuditStatus,
    audit_current_priority,
    audit_priority_run,
    sync_priority,
)
from tests.integration.test_priority_persistence_sqlite import priority_fixture


DAY = date(2026, 9, 2)


def _ready(tmp_path):
    connection, identity, opportunity_ids, arguments = priority_fixture(tmp_path)
    run = sync_priority(connection, identity.profile_id, DAY)
    return connection, identity, opportunity_ids, arguments, run


def _codes(result):
    return {issue.code for issue in result.issues}


def _corrupt_with_foreign_keys_disabled(connection, statement, parameters):
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    try:
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_audit_issue_codes_cover_the_complete_phase_5_1d_contract():
    assert {code.value for code in PriorityAuditIssueCode} == {
        "INVALID_RUN_JSON",
        "NON_CANONICAL_RUN_JSON",
        "RUN_FINGERPRINT_MISMATCH",
        "RUN_METADATA_MISMATCH",
        "ASSESSMENT_COUNT_MISMATCH",
        "INVALID_ASSESSMENT_JSON",
        "NON_CANONICAL_ASSESSMENT_JSON",
        "ASSESSMENT_FINGERPRINT_MISMATCH",
        "ASSESSMENT_COLUMNS_MISMATCH",
        "RUN_COHORT_MISMATCH",
        "PROFILE_MISSING",
        "PROFILE_USER_PROVENANCE_MISMATCH",
        "MATCHING_RUN_MISSING",
        "MATCHING_PROFILE_MISMATCH",
        "MATCHING_FINGERPRINT_MISMATCH",
        "STATE_MISSING_WITH_HISTORY",
        "STATE_RUN_MISSING",
        "STATE_RUN_PROFILE_MISMATCH",
        "STATE_VERSION_MISMATCH",
    }


def test_clean_run_and_current_profile_are_ready_and_deterministic(tmp_path):
    connection, identity, _, _, run = _ready(tmp_path)
    first = audit_priority_run(connection, run.run_id)
    assert first == audit_priority_run(connection, run.run_id)
    assert first.status is PriorityAuditStatus.OK and first.issues == ()
    current = audit_current_priority(connection, identity.profile_id)
    assert current.status is PriorityProfileAuditStatus.READY and current.issues == ()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("not-json", PriorityAuditIssueCode.INVALID_RUN_JSON),
        ('{ "assessment_count": 2 }', PriorityAuditIssueCode.NON_CANONICAL_RUN_JSON),
    ],
)
def test_run_json_corruption_is_structured(tmp_path, raw, expected):
    connection, _, _, _, run = _ready(tmp_path)
    connection.execute(
        "UPDATE priority_runs SET run_payload_json=? WHERE id=?", (raw, run.run_id)
    )
    connection.commit()
    assert expected in _codes(audit_priority_run(connection, run.run_id))


def test_semantically_equal_noncanonical_run_json_keeps_fingerprint_valid(tmp_path):
    connection, _, _, _, run = _ready(tmp_path)
    raw = connection.execute(
        "SELECT run_payload_json FROM priority_runs WHERE id=?", (run.run_id,)
    ).fetchone()[0]
    connection.execute(
        "UPDATE priority_runs SET run_payload_json=? WHERE id=?",
        (json.dumps(json.loads(raw), indent=2), run.run_id),
    )
    connection.commit()
    codes = _codes(audit_priority_run(connection, run.run_id))
    assert PriorityAuditIssueCode.NON_CANONICAL_RUN_JSON in codes
    assert PriorityAuditIssueCode.RUN_FINGERPRINT_MISMATCH not in codes


@pytest.mark.parametrize(
    "statement,expected",
    [
        (
            "UPDATE priority_runs SET run_fingerprint=lower(hex(randomblob(32))) WHERE id=?",
            PriorityAuditIssueCode.RUN_FINGERPRINT_MISMATCH,
        ),
        (
            "UPDATE priority_runs SET evaluation_date='2026-09-03' WHERE id=?",
            PriorityAuditIssueCode.RUN_METADATA_MISMATCH,
        ),
        (
            "UPDATE priority_assessments SET assessment_fingerprint=lower(hex(randomblob(32))) WHERE run_id=?",
            PriorityAuditIssueCode.ASSESSMENT_FINGERPRINT_MISMATCH,
        ),
        (
            "UPDATE priority_assessments SET priority_category='LOW' WHERE run_id=?",
            PriorityAuditIssueCode.ASSESSMENT_COLUMNS_MISMATCH,
        ),
    ],
)
def test_fingerprint_metadata_and_column_corruptions(tmp_path, statement, expected):
    connection, _, _, _, run = _ready(tmp_path)
    connection.execute(statement, (run.run_id,))
    connection.commit()
    assert expected in _codes(audit_priority_run(connection, run.run_id))


def test_noncanonical_assessment_json_does_not_imply_fingerprint_mismatch(tmp_path):
    connection, _, opportunity_ids, _, run = _ready(tmp_path)
    raw = connection.execute(
        "SELECT assessment_payload_json FROM priority_assessments WHERE run_id=? AND opportunity_id=?",
        (run.run_id, opportunity_ids[0]),
    ).fetchone()[0]
    connection.execute(
        "UPDATE priority_assessments SET assessment_payload_json=? WHERE run_id=? AND opportunity_id=?",
        (json.dumps(json.loads(raw), indent=2), run.run_id, opportunity_ids[0]),
    )
    connection.commit()
    codes = _codes(audit_priority_run(connection, run.run_id))
    assert PriorityAuditIssueCode.NON_CANONICAL_ASSESSMENT_JSON in codes
    assert PriorityAuditIssueCode.ASSESSMENT_FINGERPRINT_MISMATCH not in codes


def test_invalid_assessment_json_is_structured_with_opportunity_context(tmp_path):
    connection, _, opportunity_ids, _, run = _ready(tmp_path)
    opportunity_id = opportunity_ids[0]
    connection.execute(
        """UPDATE priority_assessments SET assessment_payload_json='not-json'
        WHERE run_id=? AND opportunity_id=?""",
        (run.run_id, opportunity_id),
    )
    connection.commit()

    result = audit_priority_run(connection, run.run_id)

    assert result.status is PriorityAuditStatus.CORRUPT
    issue = next(
        issue
        for issue in result.issues
        if issue.code is PriorityAuditIssueCode.INVALID_ASSESSMENT_JSON
    )
    assert (issue.run_id, issue.opportunity_id) == (run.run_id, opportunity_id)


def test_missing_assessment_breaks_count_and_cohort(tmp_path):
    connection, _, opportunity_ids, _, run = _ready(tmp_path)
    connection.execute(
        "DELETE FROM priority_assessments WHERE run_id=? AND opportunity_id=?",
        (run.run_id, opportunity_ids[0]),
    )
    connection.commit()
    codes = _codes(audit_priority_run(connection, run.run_id))
    assert {
        PriorityAuditIssueCode.ASSESSMENT_COUNT_MISMATCH,
        PriorityAuditIssueCode.RUN_COHORT_MISMATCH,
    } <= codes


def test_profile_owner_provenance_uses_user_id_not_profile_id(tmp_path):
    connection, identity, _, _, run = _ready(tmp_path)
    assert identity.profile_id != identity.user_id
    assert audit_priority_run(connection, run.run_id).status is PriorityAuditStatus.OK
    connection.execute("INSERT INTO users (email) VALUES ('new-owner@example.test')")
    new_owner = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    connection.execute(
        "UPDATE profiles SET user_id=? WHERE id=?", (new_owner, identity.profile_id)
    )
    connection.commit()
    assert PriorityAuditIssueCode.PROFILE_USER_PROVENANCE_MISMATCH in _codes(
        audit_priority_run(connection, run.run_id)
    )


def test_matching_fingerprint_provenance(tmp_path):
    connection, _, _, arguments, run = _ready(tmp_path)
    connection.execute(
        "UPDATE matching_runs SET run_fingerprint=? WHERE id=?",
        ("f" * 64, arguments["matching_run_id"]),
    )
    connection.commit()
    assert PriorityAuditIssueCode.MATCHING_FINGERPRINT_MISMATCH in _codes(
        audit_priority_run(connection, run.run_id)
    )


def test_missing_matching_run_is_structured_corruption(tmp_path):
    connection, _, _, _, run = _ready(tmp_path)
    _corrupt_with_foreign_keys_disabled(
        connection,
        "UPDATE priority_runs SET matching_run_id=? WHERE id=?",
        (999_999, run.run_id),
    )

    result = audit_priority_run(connection, run.run_id)

    assert result.status is PriorityAuditStatus.CORRUPT
    assert PriorityAuditIssueCode.MATCHING_RUN_MISSING in _codes(result)


def test_matching_run_from_another_profile_is_structured_corruption(tmp_path):
    connection, _, _, arguments, run = _ready(tmp_path)
    connection.execute("INSERT INTO users(email) VALUES ('matching-b@example.invalid')")
    user_b = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    connection.execute("INSERT INTO profiles(user_id) VALUES (?)", (user_b,))
    profile_b = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    connection.commit()
    _corrupt_with_foreign_keys_disabled(
        connection,
        "UPDATE matching_runs SET profile_id=? WHERE id=?",
        (profile_b, arguments["matching_run_id"]),
    )

    result = audit_priority_run(connection, run.run_id)

    assert result.status is PriorityAuditStatus.CORRUPT
    assert PriorityAuditIssueCode.MATCHING_PROFILE_MISMATCH in _codes(result)
    assert PriorityAuditIssueCode.MATCHING_RUN_MISSING not in _codes(result)


def test_history_without_state_is_profile_corruption(tmp_path):
    connection, identity, _, _, _ = _ready(tmp_path)
    connection.execute(
        "DELETE FROM priority_profile_state WHERE profile_id=?", (identity.profile_id,)
    )
    connection.commit()
    result = audit_current_priority(connection, identity.profile_id)
    assert result.status is PriorityProfileAuditStatus.CORRUPT
    assert _codes(result) == {PriorityAuditIssueCode.STATE_MISSING_WITH_HISTORY}


def test_missing_current_state_run_is_structured_corruption(tmp_path):
    connection, identity, _, _, _ = _ready(tmp_path)
    _corrupt_with_foreign_keys_disabled(
        connection,
        "UPDATE priority_profile_state SET current_run_id=? WHERE profile_id=?",
        (999_999, identity.profile_id),
    )

    result = audit_current_priority(connection, identity.profile_id)

    assert result.status is PriorityProfileAuditStatus.CORRUPT
    assert _codes(result) == {PriorityAuditIssueCode.STATE_RUN_MISSING}
    assert result.run_audit is None


def test_current_state_run_from_another_profile_is_structured_corruption(tmp_path):
    connection, identity, _, _, first = _ready(tmp_path)
    second = sync_priority(connection, identity.profile_id, date(2026, 9, 3))
    connection.execute("INSERT INTO users(email) VALUES ('priority-b@example.invalid')")
    user_b = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    connection.execute("INSERT INTO profiles(user_id) VALUES (?)", (user_b,))
    profile_b = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    connection.commit()
    _corrupt_with_foreign_keys_disabled(
        connection,
        "UPDATE priority_runs SET profile_id=? WHERE id=?",
        (profile_b, second.run_id),
    )
    assert first.run_id != second.run_id

    result = audit_current_priority(connection, identity.profile_id)

    assert result.status is PriorityProfileAuditStatus.CORRUPT
    assert PriorityAuditIssueCode.STATE_RUN_PROFILE_MISMATCH in _codes(result)
    assert result.run_audit is None


def test_current_state_version_mismatch_is_structured_corruption(tmp_path):
    connection, identity, _, _, _ = _ready(tmp_path)
    connection.execute(
        """UPDATE priority_profile_state SET persistence_version=?
        WHERE profile_id=?""",
        ("different-persistence-version", identity.profile_id),
    )
    connection.commit()

    result = audit_current_priority(connection, identity.profile_id)

    assert result.status is PriorityProfileAuditStatus.CORRUPT
    assert _codes(result) == {PriorityAuditIssueCode.STATE_VERSION_MISMATCH}
    assert result.run_audit is None


def test_current_old_historical_run_is_valid(tmp_path):
    connection, identity, _, _, first = _ready(tmp_path)
    sync_priority(connection, identity.profile_id, date(2026, 9, 3))
    connection.execute(
        "UPDATE priority_profile_state SET current_run_id=? WHERE profile_id=?",
        (first.run_id, identity.profile_id),
    )
    connection.commit()
    assert (
        audit_current_priority(connection, identity.profile_id).status
        is PriorityProfileAuditStatus.READY
    )


def test_production_read_paths_contain_no_sql_mutation_statements():
    from services.priority import audit, read_model

    for module in (audit, read_model):
        source = open(module.__file__, encoding="utf-8").read().upper()
        assert not any(
            f'"{verb} ' in source
            for verb in (
                "INSERT",
                "UPDATE",
                "DELETE",
                "REPLACE",
                "CREATE",
                "ALTER",
                "DROP",
            )
        )
