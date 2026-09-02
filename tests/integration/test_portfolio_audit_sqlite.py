import ast
import hashlib
from datetime import timedelta
from pathlib import Path

from services.portfolio import (
    PortfolioAuditIssueCode,
    PortfolioAuditStatus,
    PortfolioProfileAuditStatus,
    audit_current_portfolio,
    audit_portfolio_run,
    sync_portfolio,
)
from tests.integration.test_portfolio_persistence_sqlite import portfolio_fixture
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


def test_exact_taxonomy_and_clean_audits(tmp_path):
    assert {item.value for item in PortfolioAuditIssueCode} == EXPECTED_CODES
    connection, identity, _, stored = _ready(tmp_path)
    run = audit_portfolio_run(connection, stored.run_id)
    assert run.status is PortfolioAuditStatus.OK and run.issues == ()
    profile = audit_current_portfolio(connection, identity.profile_id)
    assert profile.status is PortfolioProfileAuditStatus.READY
    assert profile.run_audit == run and profile.issues == ()


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
    for name in ("read_model.py", "audit.py"):
        source = (
            Path(__file__).parents[2] / "services" / "portfolio" / name
        ).read_text()
        tree = ast.parse(source)
        sql = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "SELECT" in node.value.upper()
        ]
        assert sql
        assert all(
            not any(
                word in value.upper().split()
                for word in (
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "REPLACE",
                    "CREATE",
                    "ALTER",
                    "DROP",
                )
            )
            for value in sql
        )


def test_read_and_audit_are_byte_noop(tmp_path):
    connection, identity, _, stored = _ready(tmp_path)
    path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    connection.close()
    before = hashlib.sha256(path.read_bytes()).digest()
    import sqlite3

    connection = sqlite3.connect(path)
    audit_portfolio_run(connection, stored.run_id)
    audit_current_portfolio(connection, identity.profile_id)
    connection.close()
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_historical_run_stays_valid_after_upstream_current_moves(tmp_path):
    connection, identity, _, arguments = priority_fixture(tmp_path)
    store(connection, arguments)
    portfolio = sync_portfolio(connection, identity.profile_id)
    sync_priority(connection, identity.profile_id, DAY + timedelta(days=1))

    result = audit_portfolio_run(connection, portfolio.run_id)
    assert result.status is PortfolioAuditStatus.OK
    assert result.issues == ()
