import json

import pytest

from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.database.migrations import apply_migrations
from services.collector.matching import set_matching_state_empty, store_matching_batch
from services.collector.matching import read_cli
from services.collector.matching.read_cli import main, parse_args
from services.digital_twin.repository import ensure_user_profile
from tests.integration.test_matching_read_audit import add_opportunity, make_batch


@pytest.fixture
def cli_database(tmp_path):
    path = tmp_path / "matching-read-cli.db"
    with connect_database(path) as connection:
        apply_migrations(connection)
        profile_id = ensure_user_profile(
            connection, "read-cli@example.invalid"
        ).profile_id
        connection.commit()
    return path, profile_id


def execute(capsys, *arguments):
    result = main(list(arguments))
    captured = capsys.readouterr()
    return result, captured, json.loads(captured.out) if captured.out else None


def test_subcommand_and_explicit_arguments_are_required():
    for arguments in ([], ["current"], ["run", "--database", "x"]):
        with pytest.raises(SystemExit):
            parse_args(arguments)


def test_positive_identifier_validation():
    for value in ("0", "-1", "not-an-integer"):
        with pytest.raises(SystemExit):
            parse_args(["current", "--database", "x", "--profile-id", value])


def test_all_commands_parse_explicit_database():
    for command in ("current", "history", "audit"):
        parsed = parse_args([command, "--database", "x", "--profile-id", "1"])
        assert parsed.command == command and parsed.profile_id == 1
    parsed = parse_args(["run", "--database", "x", "--run-id", "2"])
    assert parsed.command == "run" and parsed.run_id == 2


def test_current_not_synced_and_empty_execute_successfully(cli_database, capsys):
    path, profile_id = cli_database
    arguments = ("current", "--database", str(path), "--profile-id", str(profile_id))
    result, captured, payload = execute(capsys, *arguments)
    assert result == 0 and captured.err == "" and payload["status"] == "NOT_SYNCED"
    with connect_database(path) as connection:
        set_matching_state_empty(
            connection, profile_id, selection_version="selection-test-v1"
        )
    result, captured, payload = execute(capsys, *arguments)
    assert result == 0 and captured.err == "" and payload["status"] == "EMPTY"


def test_ready_history_run_audit_and_stable_json(cli_database, capsys):
    path, profile_id = cli_database
    with connect_database(path) as connection:
        opportunity_ids = [
            add_opportunity(connection, f"cli-{value}") for value in (2, 1)
        ]
        connection.commit()
        stored = store_matching_batch(
            connection,
            profile_id,
            make_batch(profile_id, opportunity_ids),
            selection_version="selection-test-v1",
        )
    current_args = ("current", "--database", str(path), "--profile-id", str(profile_id))
    result, captured, current = execute(capsys, *current_args)
    assert result == 0 and captured.err == ""
    assert (
        current["status"] == "READY"
        and current["current_run"]["run_id"] == stored.run_id
    )
    assert current["current_run"]["lane_counts"] == {
        "OUTSIDE_PREFERENCES": 0,
        "PRIMARY": 2,
        "UNCERTAIN": 0,
    }
    first_stdout = captured.out
    assert execute(capsys, *current_args)[1].out == first_stdout

    result, _, history = execute(
        capsys, "history", "--database", str(path), "--profile-id", str(profile_id)
    )
    assert result == 0 and history["runs"][0]["is_current"] is True
    result, _, run = execute(
        capsys, "run", "--database", str(path), "--run-id", str(stored.run_id)
    )
    assert result == 0 and isinstance(run["batch_payload"], dict)
    assert [item["opportunity_id"] for item in run["assessments"]] == sorted(
        opportunity_ids
    )
    result, _, audit = execute(
        capsys, "audit", "--database", str(path), "--profile-id", str(profile_id)
    )
    assert result == 0 and audit["ok"] is True


def test_audit_corruption_is_reportable_but_technical_error_is_nonzero(
    cli_database, capsys
):
    path, profile_id = cli_database
    with connect_database(path) as connection:
        opportunity_id = add_opportunity(connection, "cli-corrupt")
        connection.commit()
        stored = store_matching_batch(
            connection,
            profile_id,
            make_batch(profile_id, [opportunity_id]),
            selection_version="selection-test-v1",
        )
        connection.execute(
            "UPDATE matching_runs SET run_fingerprint=? WHERE id=?",
            ("c" * 64, stored.run_id),
        )
        connection.commit()
    result, captured, audit = execute(
        capsys, "audit", "--database", str(path), "--profile-id", str(profile_id)
    )
    assert result == 0 and captured.err == "" and audit["ok"] is False
    assert "RUN_FINGERPRINT_MISMATCH" in {issue["code"] for issue in audit["issues"]}
    result = main(["current", "--database", str(path), "--profile-id", "99999"])
    captured = capsys.readouterr()
    assert result != 0 and captured.out == "" and "does not exist" in captured.err


def test_cli_uses_readonly_connector_and_enables_query_only(
    cli_database, capsys, monkeypatch
):
    path, profile_id = cli_database
    statements = []

    def traced_connector(database):
        connection = connect_readonly_database(database)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(read_cli, "connect_readonly_database", traced_connector)
    result, _, payload = execute(
        capsys, "current", "--database", str(path), "--profile-id", str(profile_id)
    )
    assert result == 0 and payload["status"] == "NOT_SYNCED"
    assert "PRAGMA query_only = ON" in statements
    assert "PRAGMA query_only" in statements
