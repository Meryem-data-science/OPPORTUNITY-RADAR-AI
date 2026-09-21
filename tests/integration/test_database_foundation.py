"""Integration test for the local database foundation."""

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations


def test_file_database_is_ready_after_migrations(tmp_path) -> None:
    database = tmp_path / "integration.db"

    with connect_database(database) as connection:
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
        ]

    assert database.is_file()

    with connect_database(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        applied = connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()

    assert {"schema_migrations", "sources", "opportunities", "opportunity_sources"} <= tables
    assert applied == [
        ("0001",),
        ("0002",),
        ("0003",),
        ("0004",),
        ("0005",),
        ("0006",),
        ("0007",),
        ("0008",),
        ("0009",),
        ("0010",),
        ("0011",),
        ("0012",),
        ("0013",),
        ("0014",),
        ("0015",),
        ("0016",),
        ("0017",),
        ("0018",),
        ("0019",),
        ("0020",),
        ("0021",),
        ("0022",),
        ("0023",),
        ("0024",),
        ("0025",),
        ("0026",),
        ("0027",),
    ]
