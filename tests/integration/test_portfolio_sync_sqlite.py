from datetime import timedelta
import importlib

import pytest

from services.portfolio import (
    PortfolioAssemblyStatus,
    PortfolioPersistenceError,
    PortfolioSyncError,
    sync_portfolio,
)
from services.priority import sync_priority
from tests.integration.test_portfolio_persistence_sqlite import (
    create_matching_run,
    portfolio_fixture,
)
from tests.integration.test_priority_persistence_sqlite import (
    DAY,
    priority_fixture,
    store,
)


def portfolio_counts(connection):
    return (
        connection.execute("SELECT COUNT(*) FROM portfolio_runs").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM portfolio_assessments").fetchone()[0],
        connection.execute(
            "SELECT * FROM portfolio_profile_state ORDER BY profile_id"
        ).fetchall(),
    )


def test_sync_is_atomic_idempotent_and_second_call_is_noop(tmp_path):
    connection, identity, _, arguments = priority_fixture(tmp_path, opportunities=3)
    store(connection, arguments)
    first = sync_portfolio(connection, identity.profile_id)
    before = portfolio_counts(connection)
    dump = tuple(connection.iterdump())
    second = sync_portfolio(connection, identity.profile_id)

    assert first.status is PortfolioAssemblyStatus.READY and first.persisted
    assert (first.created, first.state_changed) == (True, True)
    assert (second.created, second.state_changed) == (False, False)
    assert (second.run_id, second.run_fingerprint) == (
        first.run_id,
        first.run_fingerprint,
    )
    assert portfolio_counts(connection) == before
    assert tuple(connection.iterdump()) == dump
    assert first.assessment_count == first.included_count + first.excluded_count == 3


def test_incomplete_strictly_preserves_existing_portfolio_state(tmp_path):
    connection, identity, opportunity_ids, _ = portfolio_fixture(tmp_path)
    ready = sync_portfolio(connection, identity.profile_id)
    before = portfolio_counts(connection)
    connection.execute(
        "DELETE FROM priority_profile_state WHERE profile_id=?", (identity.profile_id,)
    )
    connection.commit()
    upstream_changed = portfolio_counts(connection)
    result = sync_portfolio(connection, identity.profile_id)

    assert result.status is PortfolioAssemblyStatus.INCOMPLETE and not result.persisted
    assert result.run_id is None and result.created is None
    assert portfolio_counts(connection) == upstream_changed == before
    assert upstream_changed[2][0][1] == ready.run_id
    assert opportunity_ids


def test_priority_current_change_after_assembly_rolls_back_everything(
    tmp_path, monkeypatch
):
    connection, identity, _, arguments = priority_fixture(tmp_path)
    old_priority = store(connection, arguments)
    sync_portfolio(connection, identity.profile_id)
    new_priority = sync_priority(
        connection, identity.profile_id, DAY + timedelta(days=1)
    )
    connection.execute(
        "UPDATE priority_profile_state SET current_run_id=? WHERE profile_id=?",
        (old_priority.run_id, identity.profile_id),
    )
    connection.commit()
    before = portfolio_counts(connection)

    module = importlib.import_module("services.portfolio.sync")
    real_assembly = module.assemble_portfolio_inputs

    def assemble_then_change_current(handle, profile_id):
        result = real_assembly(handle, profile_id)
        handle.execute(
            "UPDATE priority_profile_state SET current_run_id=? WHERE profile_id=?",
            (new_priority.run_id, profile_id),
        )
        return result

    monkeypatch.setattr(
        module, "assemble_portfolio_inputs", assemble_then_change_current
    )
    with pytest.raises(PortfolioPersistenceError, match="current snapshot"):
        sync_portfolio(connection, identity.profile_id)

    assert portfolio_counts(connection) == before
    assert (
        connection.execute(
            "SELECT current_run_id FROM priority_profile_state"
        ).fetchone()[0]
        == old_priority.run_id
    )
    assert connection.in_transaction is False


def test_historical_matching_referenced_by_current_priority_remains_valid(tmp_path):
    connection, identity, opportunity_ids, arguments = priority_fixture(tmp_path)
    priority = store(connection, arguments)
    historical_matching_id = arguments["matching_run_id"]
    current_matching = create_matching_run(
        connection, identity.profile_id, opportunity_ids[0]
    )

    result = sync_portfolio(connection, identity.profile_id)

    assert current_matching.run_id != historical_matching_id
    assert (
        connection.execute(
            "SELECT current_run_id FROM matching_profile_state WHERE profile_id=?",
            (identity.profile_id,),
        ).fetchone()[0]
        == current_matching.run_id
    )
    assert result.status is PortfolioAssemblyStatus.READY
    assert result.priority_run_id == priority.run_id
    assert result.matching_run_id == historical_matching_id


def test_exception_after_persistence_rolls_back_run_assessments_and_state(
    tmp_path, monkeypatch
):
    connection, identity, _, arguments = priority_fixture(tmp_path)
    store(connection, arguments)
    before = portfolio_counts(connection)
    module = importlib.import_module("services.portfolio.sync")
    real_store = module.store_portfolio_batch

    def store_then_fail(*args, **kwargs):
        real_store(*args, **kwargs)
        raise RuntimeError("forced persistence failure")

    monkeypatch.setattr(module, "store_portfolio_batch", store_then_fail)
    with pytest.raises(RuntimeError, match="forced persistence failure"):
        sync_portfolio(connection, identity.profile_id)

    assert portfolio_counts(connection) == before
    assert connection.in_transaction is False


def test_schema_marker_without_all_portfolio_tables_is_rejected(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path)
    connection.execute("DROP TABLE portfolio_assessments")
    connection.commit()
    with pytest.raises(PortfolioSyncError, match="0017"):
        sync_portfolio(connection, identity.profile_id)
    assert connection.in_transaction is False


def test_sync_refuses_active_transaction_and_missing_migration_marker(tmp_path):
    connection, identity, _, _ = portfolio_fixture(tmp_path)
    connection.execute("BEGIN")
    with pytest.raises(PortfolioSyncError, match="without an active"):
        sync_portfolio(connection, identity.profile_id)
    connection.rollback()
    connection.execute("DELETE FROM schema_migrations WHERE version='0017'")
    connection.commit()
    with pytest.raises(PortfolioSyncError, match="0017"):
        sync_portfolio(connection, identity.profile_id)


def test_a_to_b_to_a_via_sync_reuses_the_older_run(tmp_path):
    connection, identity, _, arguments = priority_fixture(tmp_path)
    priority_a = store(connection, arguments)
    portfolio_a = sync_portfolio(connection, identity.profile_id)
    priority_b = sync_priority(connection, identity.profile_id, DAY + timedelta(days=1))
    portfolio_b = sync_portfolio(connection, identity.profile_id)
    connection.execute(
        "UPDATE priority_profile_state SET current_run_id=? WHERE profile_id=?",
        (priority_a.run_id, identity.profile_id),
    )
    connection.commit()
    reused_a = sync_portfolio(connection, identity.profile_id)

    assert priority_b.run_id != priority_a.run_id
    assert portfolio_b.run_id > portfolio_a.run_id
    assert (reused_a.created, reused_a.state_changed) == (False, True)
    assert reused_a.run_id == portfolio_a.run_id
    assert (
        connection.execute(
            "SELECT current_run_id FROM portfolio_profile_state"
        ).fetchone()[0]
        == portfolio_a.run_id
    )
    assert (
        connection.execute("SELECT MAX(id) FROM portfolio_runs").fetchone()[0]
        == portfolio_b.run_id
    )
