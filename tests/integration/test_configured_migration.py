"""Integration test for the configured SQLite migration flow."""

from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_configured_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.schema import check_foundation_schema


def test_configured_sqlite_migration_is_ready_empty_and_idempotent(tmp_path) -> None:
    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.SQLITE,
        sqlite_database_path=tmp_path / "configured.db",
    )
    connection = connect_configured_database(settings)
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
        assert check_foundation_schema(connection) is True
        assert apply_migrations(connection) == []
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "opportunities", "opportunity_sources")
        }
    finally:
        connection.close()

    assert counts == {"sources": 0, "opportunities": 0, "opportunity_sources": 0}
