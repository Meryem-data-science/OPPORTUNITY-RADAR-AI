import hashlib
import pathlib

from services.collector.database.connection import connect_readonly_database
from services.portfolio import (
    PortfolioAssemblyStatus,
    PortfolioDisposition,
    assemble_portfolio_inputs,
    dry_run_portfolio,
)
from tests.integration.test_priority_persistence_sqlite import priority_fixture, store


def test_portfolio_dry_run_reads_complete_persisted_cohort_without_writes(tmp_path):
    connection, identity, opportunity_ids, arguments = priority_fixture(
        tmp_path, opportunities=3
    )
    priority_store = store(connection, arguments)
    path = pathlib.Path(connection.execute("PRAGMA database_list").fetchone()[2])
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with connect_readonly_database(path) as readonly:
        assembly = assemble_portfolio_inputs(readonly, identity.profile_id)
        result = dry_run_portfolio(readonly, identity.profile_id)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert assembly.status is PortfolioAssemblyStatus.READY
    assert assembly.priority_run_id == priority_store.run_id
    assert assembly.matching_run_id == arguments["matching_run_id"]
    assert [item.opportunity_id for item in assembly.inputs] == sorted(opportunity_ids)
    assert result.status is PortfolioAssemblyStatus.READY
    assert result.assessment_count == len(opportunity_ids)
    assert len(result.assessments) == len(opportunity_ids)
    assert result.included_count + result.excluded_count == result.assessment_count
    assert sum(dict(result.bucket_counts).values()) == result.included_count
    assert set(dict(result.bucket_counts)) == {"SAFE", "TARGET", "AMBITIOUS"}
    assert result.excluded_count == sum(
        item.disposition is PortfolioDisposition.EXCLUDED for item in result.assessments
    )


def test_not_synced_priority_fails_closed(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path, opportunities=1)
    result = dry_run_portfolio(connection, identity.profile_id)
    assert result.status is PortfolioAssemblyStatus.INCOMPLETE
    assert result.assessment_count == 0
    assert result.assessments == ()
    assert [issue.code.value for issue in result.issues] == ["PRIORITY_NOT_SYNCED"]


def test_phase_5_2b_modules_contain_no_sql_write_statements():
    for name in ("input_assembly.py", "dry_run.py"):
        source = (pathlib.Path("services/portfolio") / name).read_text().upper()
        assert not any(
            token in source
            for token in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ")
        )
