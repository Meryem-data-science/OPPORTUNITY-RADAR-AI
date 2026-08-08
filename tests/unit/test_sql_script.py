"""Tests for backend-neutral SQL script splitting."""

import pytest

from services.collector.database.migrations import MigrationError, split_sql_statements


def test_splitter_handles_multiple_and_multiline_statements() -> None:
    script = """
    CREATE TABLE first_table (
        id INTEGER PRIMARY KEY
    );
    CREATE INDEX first_table_id ON first_table(id);
    """

    statements = split_sql_statements(script)

    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE")
    assert statements[1].startswith("CREATE INDEX")


def test_splitter_handles_comments_and_semicolon_inside_string() -> None:
    script = """
    -- A comment with a semicolon;
    INSERT INTO audit_log(message) VALUES ('keeps; semicolon');
    /* A block comment; */
    UPDATE audit_log SET message = 'updated';
    """

    statements = split_sql_statements(script)

    assert len(statements) == 2
    assert "'keeps; semicolon'" in statements[0]
    assert statements[1].endswith("'updated';")


def test_splitter_ignores_whitespace_and_comments_only() -> None:
    assert split_sql_statements("  \n-- comment only\n/* block comment */\n") == []


def test_splitter_rejects_incomplete_statement() -> None:
    with pytest.raises(MigrationError, match="incomplete"):
        split_sql_statements("CREATE TABLE incomplete (id INTEGER")
