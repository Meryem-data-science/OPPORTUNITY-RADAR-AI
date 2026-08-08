"""Read-only validation of the Opportunity Radar foundation schema."""

from services.collector.database.connection import DatabaseConnection


FOUNDATION_TABLES = frozenset(
    {"schema_migrations", "sources", "opportunities", "opportunity_sources"}
)


def check_foundation_schema(connection: DatabaseConnection) -> bool:
    """Return whether all foundation tables exist without changing the database."""
    placeholders = ", ".join("?" for _ in FOUNDATION_TABLES)
    query = (
        "SELECT name FROM sqlite_schema "
        f"WHERE type = 'table' AND name IN ({placeholders})"
    )
    rows = connection.execute(
        query,
        tuple(sorted(FOUNDATION_TABLES)),
    ).fetchall()
    return {row[0] for row in rows} == FOUNDATION_TABLES
