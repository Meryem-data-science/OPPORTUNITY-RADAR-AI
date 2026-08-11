from services.collector.agent import RadarAgent
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig


def test_two_agent_runs_insert_then_update_real_sqlite(tmp_path):
    path = tmp_path / "radar.db"
    connection = connect_database(path)
    try:
        assert apply_migrations(connection) == ["0001", "0002", "0003"]
    finally:
        connection.close()
    source = SourceConfig("fake_greenhouse", "greenhouse", True, "Org", "fake", status="active")
    item = OpportunityCandidate(
        source.id, "1", "Role", "Org", "Casablanca, Morocco", "description", None,
        "https://example.com/jobs/1", "https://example.com/jobs/1", "https://example.com/jobs/1",
    )
    class FakeCollector:
        def collect(self):
            return [item]
    settings = Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE, path)
    agent = RadarAgent(
        source_loader=lambda: [source], collector_factory=lambda unused: FakeCollector(),
        settings_loader=lambda: settings,
    )
    first = agent.run_once()
    second = agent.run_once()
    connection = connect_database(path)
    try:
        counts = [connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("sources", "opportunities", "opportunity_sources")]
        location = connection.execute("SELECT location FROM opportunities").fetchone()[0]
    finally:
        connection.close()
    assert (first.items_created, first.items_updated) == (1, 0)
    assert (second.items_created, second.items_updated) == (0, 1)
    assert counts == [1, 1, 1]
    assert location == "Casablanca, Morocco"
