"""Read-only database healthcheck shared by supported backends."""

from services.collector.database.connection import DatabaseConnection


def check_database_health(connection: DatabaseConnection) -> bool:
    """Return whether the connection produces the expected result for SELECT 1."""
    row = connection.execute("SELECT 1").fetchone()
    return row is not None and row[0] == 1
