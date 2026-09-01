import pytest

from services.collector.matching.read_cli import parse_args


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
