from services.collector.agent import RadarAgent
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig
from services.collector.qualification.classifier import CLASSIFIER_VERSION


def test_agent_automatically_reconciles_qualifications_idempotently_and_on_input_change(tmp_path):
    path = tmp_path / "radar.db"
    connection = connect_database(path)
    try:
        assert apply_migrations(connection) == [
            "0001",
            "0002",
            "0003",
            "0004",
            "0005",
            "0006",
            "0007",
            "0008",
            "0009",
            "0010",
            "0011",
            "0012",
            "0013",
            "0014",
            "0015",
            "0016",
            "0017",
            "0018",
            "0019",
            "0020",
            "0021",
            "0022",
            "0023",
            "0024",
            "0025",
            "0026",
            "0027",
            "0028",
        ]
    finally:
        connection.close()
    source = SourceConfig("fake_greenhouse", "greenhouse", True, "Org", "fake", status="active")
    items = [OpportunityCandidate(
        source.id, "1", "Role", "Org", "Casablanca, Morocco", "description", None,
        "https://example.com/jobs/1", "https://example.com/jobs/1", "https://example.com/jobs/1",
    )]
    class FakeCollector:
        def collect(self):
            return items
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
        qualification = connection.execute(
            "SELECT classifier_version, input_fingerprint, classified_at, updated_at "
            "FROM opportunity_qualifications"
        ).fetchone()
    finally:
        connection.close()
    assert (first.items_created, first.items_updated) == (1, 0)
    assert (second.items_created, second.items_updated) == (0, 1)
    assert counts == [1, 1, 1]
    assert location == "Casablanca, Morocco"
    assert first.qualification.created == 1
    assert second.qualification.unchanged == 1
    assert qualification[0] == CLASSIFIER_VERSION
    assert len(qualification[1]) == 64

    items[0] = OpportunityCandidate(
        source.id, "1", "Role", "Different Org", "Rabat, Morocco",
        "Changed description with machine learning and artificial intelligence", None,
        "https://example.com/jobs/1", "https://example.com/jobs/1", "https://example.com/jobs/1",
    )
    changed = agent.run_once()
    connection = connect_database(path)
    try:
        changed_qualification = connection.execute(
            "SELECT input_fingerprint, classified_at, updated_at FROM opportunity_qualifications"
        ).fetchone()
    finally:
        connection.close()
    assert changed.qualification.updated == 1
    assert changed_qualification[0] != qualification[1]
    assert changed_qualification[1:] != qualification[2:]

    items[0] = OpportunityCandidate(
        source.id, "1", "Role", "Another Org", "Tangier, Morocco",
        items[0].description, None, "https://example.com/jobs/1",
        "https://example.com/jobs/1", "https://example.com/jobs/1",
    )
    metadata_only = agent.run_once()
    connection = connect_database(path)
    try:
        metadata_fingerprint = connection.execute(
            "SELECT input_fingerprint FROM opportunity_qualifications"
        ).fetchone()[0]
    finally:
        connection.close()
    assert metadata_only.qualification.unchanged == 1
    assert metadata_fingerprint == changed_qualification[0]


def test_qualification_failure_does_not_rollback_persisted_opportunity(tmp_path):
    path = tmp_path / "missing-qualification-schema.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        connection.execute("DROP TABLE opportunity_qualifications")
    finally:
        connection.close()
    source = SourceConfig("fake", "greenhouse", True, "Org", "fake", status="active")
    item = OpportunityCandidate(
        source.id, "1", "Role", "Org", None, "description", None,
        "https://example.com/1", None, None,
    )
    collector = type("FakeCollector", (), {"collect": lambda self: [item]})()
    settings = Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE, path)

    summary = RadarAgent(
        source_loader=lambda: [source], collector_factory=lambda unused: collector,
        settings_loader=lambda: settings,
    ).run_once()

    connection = connect_database(path)
    try:
        opportunity_count = connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        qualification_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_qualifications'"
        ).fetchone()
    finally:
        connection.close()
    assert summary.sources_succeeded == 1
    assert summary.sources_failed == 0
    assert not summary.qualification.success
    assert summary.qualification.error_type == "QualificationPersistenceError"
    assert opportunity_count == 1
    assert qualification_table is None
