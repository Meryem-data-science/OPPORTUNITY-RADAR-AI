from dataclasses import replace

import pytest

from services.portfolio import (
    PortfolioPersistenceError,
    assemble_portfolio_inputs,
    build_portfolio_assessments,
    store_portfolio_batch,
)
from tests.integration.test_priority_persistence_sqlite import priority_fixture, store


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


def test_all_assessments_are_persisted_and_identical_store_is_noop(tmp_path):
    connection, _, ids, arguments = portfolio_fixture(tmp_path, 3)
    first = persist(connection, arguments)
    dump = tuple(connection.iterdump())
    second = persist(connection, arguments)
    assert first.created is True and first.state_changed is True
    assert second.created is False and second.state_changed is False
    assert (second.run_id, second.run_fingerprint) == (
        first.run_id,
        first.run_fingerprint,
    )
    assert connection.execute("SELECT COUNT(*) FROM portfolio_assessments").fetchone()[
        0
    ] == len(ids)
    assert tuple(connection.iterdump()) == dump


def test_validation_occurs_before_writes(tmp_path):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    bad = replace(arguments["assessments"][0], assessment_fingerprint="0" * 64)
    connection.execute("BEGIN")
    with pytest.raises(PortfolioPersistenceError, match="fingerprint"):
        store_portfolio_batch(connection, **(arguments | {"assessments": (bad,)}))
    assert connection.execute("SELECT COUNT(*) FROM portfolio_runs").fetchone()[0] == 0
    connection.rollback()


def test_store_requires_transaction_and_rejects_corrupt_reuse(tmp_path):
    connection, _, _, arguments = portfolio_fixture(tmp_path)
    with pytest.raises(PortfolioPersistenceError, match="active transaction"):
        store_portfolio_batch(connection, **arguments)
    result = persist(connection, arguments)
    connection.execute(
        "UPDATE portfolio_assessments SET assessment_payload_json='{}' WHERE run_id=?",
        (result.run_id,),
    )
    connection.commit()
    with pytest.raises(PortfolioPersistenceError, match="corrupt"):
        persist(connection, arguments)
