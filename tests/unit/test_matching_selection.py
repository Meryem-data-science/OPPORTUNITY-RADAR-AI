import sqlite3

from services.collector.matching.selection import (
    MATCHING_SELECTION_VERSION,
    select_matching_opportunity_ids,
)


def _database():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """CREATE TABLE opportunities (id INTEGER PRIMARY KEY, is_active INTEGER, status TEXT);
        CREATE TABLE opportunity_qualifications (opportunity_id INTEGER PRIMARY KEY, qualification TEXT);"""
    )
    return connection


def test_selection_contract_is_versioned_filtered_unique_and_ordered():
    connection = _database()
    rows = (
        (8, 1, "new", "ADJACENT_TARGET"),
        (2, 1, "new", "CORE_TARGET"),
        (3, 1, "new", "OUT_OF_SCOPE"),
        (4, 1, "new", "UNCERTAIN"),
        (5, 0, "new", "CORE_TARGET"),
        (6, 1, "merged_duplicate", "CORE_TARGET"),
    )
    for identifier, active, status, qualification in rows:
        connection.execute(
            "INSERT INTO opportunities VALUES (?, ?, ?)",
            (identifier, active, status),
        )
        connection.execute(
            "INSERT INTO opportunity_qualifications VALUES (?, ?)",
            (identifier, qualification),
        )

    assert MATCHING_SELECTION_VERSION == "matching-selection-v1"
    assert select_matching_opportunity_ids(connection) == (2, 8)


def test_selection_can_be_empty():
    connection = _database()
    assert select_matching_opportunity_ids(connection) == ()
