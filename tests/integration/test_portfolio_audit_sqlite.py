import ast
import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from services.digital_twin.repository import ensure_user_profile
from services.portfolio import (
    PortfolioAuditIssueCode,
    PortfolioAuditStatus,
    PortfolioProfileAuditStatus,
    audit_current_portfolio,
    audit_portfolio_run,
    list_portfolio_runs,
    read_current_portfolio,
    read_portfolio_run,
    sync_portfolio,
)
from tests.integration.test_portfolio_persistence_sqlite import (
    create_matching_run,
    portfolio_fixture,
)
from services.priority import sync_priority
from tests.integration.test_priority_persistence_sqlite import (
    DAY,
    priority_fixture,
    store,
)


EXPECTED_CODES = {
    "INVALID_RUN_JSON",
    "NON_CANONICAL_RUN_JSON",
    "RUN_FINGERPRINT_MISMATCH",
    "RUN_METADATA_MISMATCH",
    "ASSESSMENT_COUNT_MISMATCH",
    "RUN_COUNTS_MISMATCH",
    "RUN_COHORT_MISMATCH",
    "INVALID_ASSESSMENT_JSON",
    "NON_CANONICAL_ASSESSMENT_JSON",
    "ASSESSMENT_FINGERPRINT_MISMATCH",
    "ASSESSMENT_COLUMNS_MISMATCH",
    "PROFILE_MISSING",
    "PRIORITY_RUN_MISSING",
    "PRIORITY_PROFILE_MISMATCH",
    "PRIORITY_FINGERPRINT_MISMATCH",
    "MATCHING_RUN_MISSING",
    "MATCHING_PROFILE_MISMATCH",
    "MATCHING_FINGERPRINT_MISMATCH",
    "PRIORITY_MATCHING_PROVENANCE_MISMATCH",
    "PRIORITY_ASSESSMENT_PROVENANCE_MISMATCH",
    "MATCHING_ASSESSMENT_PROVENANCE_MISMATCH",
    "STATE_MISSING_WITH_HISTORY",
    "STATE_RUN_MISSING",
    "STATE_RUN_PROFILE_MISMATCH",
    "STATE_VERSION_MISMATCH",
}


def _ready(tmp_path):
    connection, identity, opportunity_ids, _ = portfolio_fixture(tmp_path)
    stored = sync_portfolio(connection, identity.profile_id)
    return connection, identity, opportunity_ids, stored


def _codes(result):
    return {issue.code for issue in result.issues}


def _corrupt(connection, statement, parameters=()):
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("PRAGMA ignore_check_constraints=ON")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    connection.execute(statement, parameters)
    connection.commit()


def test_exact_taxonomy_and_clean_audits(tmp_path):
    assert {item.value for item in PortfolioAuditIssueCode} == EXPECTED_CODES
    connection, identity, _, stored = _ready(tmp_path)
    run = audit_portfolio_run(connection, stored.run_id)
    assert run.status is PortfolioAuditStatus.OK and run.issues == ()
    profile = audit_current_portfolio(connection, identity.profile_id)
    assert profile.status is PortfolioProfileAuditStatus.READY
    assert profile.run_audit == run and profile.issues == ()


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (
            "UPDATE portfolio_runs SET run_payload_json='not-json' WHERE id=?",
            PortfolioAuditIssueCode.INVALID_RUN_JSON,
        ),
        (
            "UPDATE portfolio_runs SET run_fingerprint=lower(hex(randomblob(32))) WHERE id=?",
            PortfolioAuditIssueCode.RUN_FINGERPRINT_MISMATCH,
        ),
        (
            "UPDATE portfolio_runs SET portfolio_engine_version='corrupt' WHERE id=?",
            PortfolioAuditIssueCode.RUN_METADATA_MISMATCH,
        ),
        (
            "UPDATE portfolio_runs SET assessment_count=assessment_count+1 WHERE id=?",
            PortfolioAuditIssueCode.ASSESSMENT_COUNT_MISMATCH,
        ),
        (
            "UPDATE portfolio_runs SET included_count=included_count+1 WHERE id=?",
            PortfolioAuditIssueCode.RUN_COUNTS_MISMATCH,
        ),
        (
            "UPDATE portfolio_assessments SET assessment_fingerprint=lower(hex(randomblob(32))) WHERE run_id=? LIMIT 1",
            PortfolioAuditIssueCode.RUN_COHORT_MISMATCH,
        ),
        (
            "UPDATE portfolio_assessments SET assessment_payload_json='not-json' WHERE run_id=? LIMIT 1",
            PortfolioAuditIssueCode.INVALID_ASSESSMENT_JSON,
        ),
        (
            "UPDATE portfolio_assessments SET assessment_fingerprint=lower(hex(randomblob(32))) WHERE run_id=? LIMIT 1",
            PortfolioAuditIssueCode.ASSESSMENT_FINGERPRINT_MISMATCH,
        ),
        (
            "UPDATE portfolio_assessments SET eligibility_status='ELIGIBLE' WHERE run_id=? LIMIT 1",
            PortfolioAuditIssueCode.ASSESSMENT_COLUMNS_MISMATCH,
        ),
        (
            "UPDATE priority_runs SET run_fingerprint=lower(hex(randomblob(32))) WHERE id=(SELECT priority_run_id FROM portfolio_runs WHERE id=?)",
            PortfolioAuditIssueCode.PRIORITY_FINGERPRINT_MISMATCH,
        ),
        (
            "UPDATE matching_runs SET run_fingerprint=lower(hex(randomblob(32))) WHERE id=(SELECT matching_run_id FROM portfolio_runs WHERE id=?)",
            PortfolioAuditIssueCode.MATCHING_FINGERPRINT_MISMATCH,
        ),
        (
            "UPDATE priority_runs SET matching_run_fingerprint=lower(hex(randomblob(32))) WHERE id=(SELECT priority_run_id FROM portfolio_runs WHERE id=?)",
            PortfolioAuditIssueCode.PRIORITY_MATCHING_PROVENANCE_MISMATCH,
        ),
        (
            "UPDATE priority_assessments SET assessment_fingerprint=lower(hex(randomblob(32))) WHERE run_id=(SELECT priority_run_id FROM portfolio_runs WHERE id=?) LIMIT 1",
            PortfolioAuditIssueCode.PRIORITY_ASSESSMENT_PROVENANCE_MISMATCH,
        ),
        (
            "UPDATE matching_assessments SET assessment_fingerprint=lower(hex(randomblob(32))) WHERE run_id=(SELECT matching_run_id FROM portfolio_runs WHERE id=?) LIMIT 1",
            PortfolioAuditIssueCode.MATCHING_ASSESSMENT_PROVENANCE_MISMATCH,
        ),
    ],
)
def test_run_corruption_emits_expected_issue(tmp_path, statement, expected):
    connection, _, _, stored = _ready(tmp_path)
    _corrupt(connection, statement, (stored.run_id,))
    assert expected in _codes(audit_portfolio_run(connection, stored.run_id))


def test_noncanonical_run_and_assessment_json_are_reported(tmp_path):
    connection, _, _, stored = _ready(tmp_path)
    run_raw = connection.execute(
        "SELECT run_payload_json FROM portfolio_runs WHERE id=?", (stored.run_id,)
    ).fetchone()[0]
    assessment_raw = connection.execute(
        "SELECT assessment_payload_json FROM portfolio_assessments WHERE run_id=? LIMIT 1",
        (stored.run_id,),
    ).fetchone()[0]
    connection.execute(
        "UPDATE portfolio_runs SET run_payload_json=? WHERE id=?",
        (json.dumps(json.loads(run_raw), indent=2), stored.run_id),
    )
    connection.execute(
        "UPDATE portfolio_assessments SET assessment_payload_json=? WHERE run_id=? LIMIT 1",
        (json.dumps(json.loads(assessment_raw), indent=2), stored.run_id),
    )
    connection.commit()
    codes = _codes(audit_portfolio_run(connection, stored.run_id))
    assert PortfolioAuditIssueCode.NON_CANONICAL_RUN_JSON in codes
    assert PortfolioAuditIssueCode.NON_CANONICAL_ASSESSMENT_JSON in codes


def test_missing_profile_and_upstream_runs_are_reported(tmp_path):
    connection, identity, _, stored = _ready(tmp_path / "missing-profile")
    _corrupt(connection, "DELETE FROM profiles WHERE id=?", (identity.profile_id,))
    assert PortfolioAuditIssueCode.PROFILE_MISSING in _codes(
        audit_portfolio_run(connection, stored.run_id)
    )

    connection, _, _, stored = _ready(tmp_path / "missing-priority")
    _corrupt(
        connection,
        "DELETE FROM priority_runs WHERE id=(SELECT priority_run_id FROM portfolio_runs WHERE id=?)",
        (stored.run_id,),
    )
    assert PortfolioAuditIssueCode.PRIORITY_RUN_MISSING in _codes(
        audit_portfolio_run(connection, stored.run_id)
    )

    connection, _, _, stored = _ready(tmp_path / "missing-matching")
    _corrupt(
        connection,
        "DELETE FROM matching_runs WHERE id=(SELECT matching_run_id FROM portfolio_runs WHERE id=?)",
        (stored.run_id,),
    )
    assert PortfolioAuditIssueCode.MATCHING_RUN_MISSING in _codes(
        audit_portfolio_run(connection, stored.run_id)
    )


@pytest.mark.parametrize(
    ("table", "foreign_key", "expected"),
    [
        (
            "priority_runs",
            "priority_run_id",
            PortfolioAuditIssueCode.PRIORITY_PROFILE_MISMATCH,
        ),
        (
            "matching_runs",
            "matching_run_id",
            PortfolioAuditIssueCode.MATCHING_PROFILE_MISMATCH,
        ),
    ],
)
def test_upstream_profile_mismatch_is_reported(tmp_path, table, foreign_key, expected):
    connection, _, _, stored = _ready(tmp_path)
    other = ensure_user_profile(connection, f"foreign-{table}@example.invalid")
    _corrupt(
        connection,
        f"UPDATE {table} SET profile_id=? WHERE id=(SELECT {foreign_key} FROM portfolio_runs WHERE id=?)",
        (other.profile_id, stored.run_id),
    )
    assert expected in _codes(audit_portfolio_run(connection, stored.run_id))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", PortfolioAuditIssueCode.STATE_RUN_MISSING),
        ("foreign", PortfolioAuditIssueCode.STATE_RUN_PROFILE_MISMATCH),
        ("persistence", PortfolioAuditIssueCode.STATE_VERSION_MISMATCH),
        ("assembly", PortfolioAuditIssueCode.STATE_VERSION_MISMATCH),
    ],
)
def test_state_corruption_emits_expected_issue(tmp_path, mutation, expected):
    connection, identity, _, stored = _ready(tmp_path)
    if mutation == "missing":
        _corrupt(
            connection,
            "UPDATE portfolio_profile_state SET current_run_id=999999 WHERE profile_id=?",
            (identity.profile_id,),
        )
    elif mutation == "foreign":
        other = ensure_user_profile(connection, "foreign-state@example.invalid")
        _corrupt(
            connection,
            "UPDATE portfolio_runs SET profile_id=? WHERE id=?",
            (other.profile_id, stored.run_id),
        )
    else:
        column = (
            "persistence_version"
            if mutation == "persistence"
            else "input_assembly_version"
        )
        _corrupt(
            connection,
            f"UPDATE portfolio_profile_state SET {column}='corrupt' WHERE profile_id=?",
            (identity.profile_id,),
        )
    assert expected in _codes(audit_current_portfolio(connection, identity.profile_id))


def test_multi_issue_audit_is_deterministic(tmp_path):
    connection, _, _, stored = _ready(tmp_path)
    connection.execute(
        "UPDATE portfolio_runs SET run_payload_json='bad', run_fingerprint=? WHERE id=?",
        ("0" * 64, stored.run_id),
    )
    connection.execute(
        "UPDATE portfolio_assessments SET assessment_payload_json='bad' WHERE run_id=?",
        (stored.run_id,),
    )
    connection.commit()
    first = audit_portfolio_run(connection, stored.run_id)
    assert first == audit_portfolio_run(connection, stored.run_id)
    assert first.status is PortfolioAuditStatus.CORRUPT
    assert len({issue.code for issue in first.issues}) >= 2
    assert first.issues == tuple(
        sorted(
            first.issues,
            key=lambda item: (
                item.opportunity_id is not None,
                item.opportunity_id or 0,
                item.code.value,
            ),
        )
    )


def test_not_synced_and_state_missing(tmp_path):
    connection, identity, _, _ = portfolio_fixture(tmp_path)
    result = audit_current_portfolio(connection, identity.profile_id)
    assert result.status is PortfolioProfileAuditStatus.NOT_SYNCED
    stored = sync_portfolio(connection, identity.profile_id)
    connection.execute(
        "DELETE FROM portfolio_profile_state WHERE profile_id=?", (identity.profile_id,)
    )
    connection.commit()
    result = audit_current_portfolio(connection, identity.profile_id)
    assert result.status is PortfolioProfileAuditStatus.CORRUPT
    assert result.issues[0].code is PortfolioAuditIssueCode.STATE_MISSING_WITH_HISTORY
    assert stored.run_id


def test_production_modules_contain_only_select_sql():
    mutation = re.compile(
        r"\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b", re.IGNORECASE
    )
    for name in ("read_model.py", "audit.py"):
        source = (
            Path(__file__).parents[2] / "services" / "portfolio" / name
        ).read_text()
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany", "executescript"}
        ]
        assert calls
        for call in calls:
            assert call.args, "SQL call must have an explicit statement"
            statement = call.args[0]
            assert isinstance(statement, ast.Constant) and isinstance(
                statement.value, str
            ), "SQL must be a statically inspectable string literal"
            assert mutation.search(statement.value) is None


def test_read_and_audit_are_byte_noop(tmp_path):
    connection, identity, _, stored = _ready(tmp_path)
    path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    connection.close()
    before = hashlib.sha256(path.read_bytes()).digest()
    import sqlite3

    connection = sqlite3.connect(path)
    read_portfolio_run(connection, stored.run_id)
    list_portfolio_runs(connection, identity.profile_id)
    read_current_portfolio(connection, identity.profile_id)
    audit_portfolio_run(connection, stored.run_id)
    audit_current_portfolio(connection, identity.profile_id)
    connection.close()
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_historical_run_stays_valid_after_upstream_current_moves(tmp_path):
    connection, identity, opportunity_ids, arguments = priority_fixture(tmp_path)
    priority_a = store(connection, arguments)
    matching_a_id = arguments["matching_run_id"]
    portfolio = sync_portfolio(connection, identity.profile_id)
    priority_b = sync_priority(connection, identity.profile_id, DAY + timedelta(days=1))
    matching_b = create_matching_run(
        connection, identity.profile_id, opportunity_ids[0]
    )

    assert priority_b.run_id != priority_a.run_id
    assert matching_b.run_id != matching_a_id
    assert (
        connection.execute(
            "SELECT current_run_id FROM priority_profile_state WHERE profile_id=?",
            (identity.profile_id,),
        ).fetchone()[0]
        == priority_b.run_id
    )
    assert (
        connection.execute(
            "SELECT current_run_id FROM matching_profile_state WHERE profile_id=?",
            (identity.profile_id,),
        ).fetchone()[0]
        == matching_b.run_id
    )
    assert connection.execute(
        "SELECT 1 FROM priority_runs WHERE id=?", (priority_a.run_id,)
    ).fetchone()
    assert connection.execute(
        "SELECT 1 FROM matching_runs WHERE id=?", (matching_a_id,)
    ).fetchone()

    result = audit_portfolio_run(connection, portfolio.run_id)
    assert result.status is PortfolioAuditStatus.OK
    assert result.issues == ()
