import hashlib
from datetime import timedelta
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest

from services.portfolio import (
    PortfolioProfileReadStatus,
    PortfolioReadError,
    list_portfolio_runs,
    read_current_portfolio,
    read_portfolio_run,
    sync_portfolio,
)
from tests.integration.test_portfolio_persistence_sqlite import portfolio_fixture
from services.priority import sync_priority
from tests.integration.test_priority_persistence_sqlite import (
    DAY,
    priority_fixture,
    store,
)


def _ready(tmp_path):
    connection, identity, opportunity_ids, _ = portfolio_fixture(tmp_path)
    stored = sync_portfolio(connection, identity.profile_id)
    return connection, identity, opportunity_ids, stored


def test_not_synced_and_missing_entities(tmp_path):
    connection, identity, _, _ = portfolio_fixture(tmp_path)
    current = read_current_portfolio(connection, identity.profile_id)
    assert current.status is PortfolioProfileReadStatus.NOT_SYNCED
    assert current.current_run is None and current.history_count == 0
    assert list_portfolio_runs(connection, identity.profile_id) == ()
    with pytest.raises(PortfolioReadError):
        read_portfolio_run(connection, 99999)
    with pytest.raises(PortfolioReadError):
        read_current_portfolio(connection, 99999)


def test_ready_is_typed_sorted_and_recursively_immutable(tmp_path):
    connection, identity, _, stored = _ready(tmp_path)
    current = read_current_portfolio(connection, identity.profile_id)
    run = read_portfolio_run(connection, stored.run_id)
    assert current.status is PortfolioProfileReadStatus.READY
    assert current.current_run == run
    assert tuple(item.opportunity_id for item in run.assessments) == tuple(
        sorted(item.opportunity_id for item in run.assessments)
    )
    assert isinstance(run.run_payload, MappingProxyType)
    assert isinstance(run.run_payload["assessments"], tuple)
    assert isinstance(run.assessments[0].assessment_payload, MappingProxyType)
    with pytest.raises(TypeError):
        run.run_payload["profile_id"] = 2
    with pytest.raises(FrozenInstanceError):
        run.profile_id = 2


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE portfolio_runs SET run_payload_json='bad' WHERE id=?",
        "UPDATE portfolio_assessments SET assessment_payload_json='bad' WHERE run_id=?",
        "UPDATE portfolio_assessments SET disposition='BROKEN' WHERE run_id=?",
        "UPDATE portfolio_assessments SET bucket='BROKEN' WHERE run_id=?",
        "UPDATE portfolio_assessments SET priority_category='BROKEN' WHERE run_id=?",
        "UPDATE portfolio_assessments SET eligibility_status='BROKEN' WHERE run_id=?",
        "UPDATE portfolio_assessments SET matching_lane='BROKEN' WHERE run_id=?",
        "UPDATE portfolio_assessments SET safe_cap_applied=2 WHERE run_id=?",
        "UPDATE portfolio_assessments SET required_skill_matched_count=-1 WHERE run_id=?",
        "UPDATE portfolio_runs SET assessment_count=999 WHERE id=?",
        "UPDATE portfolio_runs SET included_count=999 WHERE id=?",
    ],
)
def test_strict_corruption_rejection(tmp_path, statement):
    connection, _, _, stored = _ready(tmp_path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(statement, (stored.run_id,))
    connection.commit()
    with pytest.raises(PortfolioReadError):
        read_portfolio_run(connection, stored.run_id)


def test_state_missing_and_version_mismatch_are_rejected(tmp_path):
    connection, identity, _, stored = _ready(tmp_path)
    connection.execute(
        "UPDATE portfolio_profile_state SET persistence_version='bad' WHERE profile_id=?",
        (identity.profile_id,),
    )
    connection.commit()
    with pytest.raises(PortfolioReadError):
        read_current_portfolio(connection, identity.profile_id)
    connection.execute(
        "DELETE FROM portfolio_profile_state WHERE profile_id=?", (identity.profile_id,)
    )
    connection.commit()
    with pytest.raises(PortfolioReadError):
        read_current_portfolio(connection, identity.profile_id)
    assert read_portfolio_run(connection, stored.run_id)


def test_all_read_surfaces_are_byte_noop(tmp_path):
    connection, identity, _, stored = _ready(tmp_path)
    path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    connection.close()
    before = hashlib.sha256(path.read_bytes()).digest()
    import sqlite3

    connection = sqlite3.connect(path)
    read_portfolio_run(connection, stored.run_id)
    list_portfolio_runs(connection, identity.profile_id)
    read_current_portfolio(connection, identity.profile_id)
    connection.close()
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_current_pointer_not_maximum_run_controls_history(tmp_path):
    connection, identity, _, arguments = priority_fixture(tmp_path)
    priority_a = store(connection, arguments)
    portfolio_a = sync_portfolio(connection, identity.profile_id)
    sync_priority(connection, identity.profile_id, DAY + timedelta(days=1))
    portfolio_b = sync_portfolio(connection, identity.profile_id)
    connection.execute(
        "UPDATE portfolio_profile_state SET current_run_id=? WHERE profile_id=?",
        (portfolio_a.run_id, identity.profile_id),
    )
    connection.commit()

    current = read_current_portfolio(connection, identity.profile_id)
    history = list_portfolio_runs(connection, identity.profile_id)
    assert portfolio_a.run_id < portfolio_b.run_id
    assert current.current_run_id == portfolio_a.run_id
    assert [(item.run_id, item.is_current) for item in history] == [
        (portfolio_b.run_id, False),
        (portfolio_a.run_id, True),
    ]
    assert (
        read_portfolio_run(connection, portfolio_a.run_id).priority_run_id
        == priority_a.run_id
    )
