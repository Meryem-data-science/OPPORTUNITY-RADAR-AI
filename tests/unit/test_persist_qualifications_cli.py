"""Safety tests for the explicit qualification persistence CLI."""

import sqlite3

from services.collector.cli.persist_qualifications import main
from services.collector.database.migrations import apply_migrations


def test_without_apply_refuses_and_performs_no_writes(tmp_path, capsys) -> None:
    path = tmp_path / "ready.db"
    with sqlite3.connect(path) as connection:
        apply_migrations(connection)
    assert main(["--database", str(path)]) == 2
    assert "--apply is required" in capsys.readouterr().out
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM opportunity_qualifications").fetchone() == (0,)


def test_missing_database_is_not_created(tmp_path, capsys) -> None:
    path = tmp_path / "missing.db"
    assert main(["--database", str(path), "--apply"]) == 1
    assert "does not exist" in capsys.readouterr().out
    assert not path.exists()


def test_unmigrated_database_fails_without_creating_table(tmp_path, capsys) -> None:
    path = tmp_path / "unmigrated.db"
    sqlite3.connect(path).close()
    assert main(["--database", str(path), "--apply"]) == 1
    assert "migration 0004 is required" in capsys.readouterr().out
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='opportunity_qualifications'"
        ).fetchone() is None
