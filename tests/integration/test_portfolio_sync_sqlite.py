import pytest

from services.portfolio import (
    PortfolioAssemblyStatus,
    PortfolioSyncError,
    sync_portfolio,
)
from tests.integration.test_portfolio_persistence_sqlite import portfolio_fixture
from tests.integration.test_priority_persistence_sqlite import priority_fixture, store


def test_sync_is_atomic_and_idempotent(tmp_path):
    connection, identity, _, arguments = priority_fixture(tmp_path, opportunities=3)
    store(connection, arguments)
    first = sync_portfolio(connection, identity.profile_id)
    state = connection.execute("SELECT * FROM portfolio_profile_state").fetchone()
    second = sync_portfolio(connection, identity.profile_id)
    assert first.status is PortfolioAssemblyStatus.READY and first.persisted
    assert (first.created, first.state_changed) == (True, True)
    assert (second.created, second.state_changed) == (False, False)
    assert (
        second.run_id == first.run_id
        and connection.execute("SELECT * FROM portfolio_profile_state").fetchone()
        == state
    )
    assert first.assessment_count == first.included_count + first.excluded_count == 3


def test_incomplete_preserves_existing_state(tmp_path):
    connection, identity, opportunity_ids, _ = portfolio_fixture(tmp_path)
    ready = sync_portfolio(connection, identity.profile_id)
    connection.execute(
        "DELETE FROM priority_profile_state WHERE profile_id=?", (identity.profile_id,)
    )
    connection.commit()
    changed = tuple(connection.iterdump())
    result = sync_portfolio(connection, identity.profile_id)
    assert result.status is PortfolioAssemblyStatus.INCOMPLETE and not result.persisted
    assert result.run_id is None and result.created is None
    assert tuple(connection.iterdump()) == changed
    assert ready.run_id is not None and opportunity_ids


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
