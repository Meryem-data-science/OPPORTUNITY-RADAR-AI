import json

import pytest

from services.collector.matching import sync_cli
from services.collector.matching.sync import MatchingSyncResult


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _result(persisted: bool):
    return MatchingSyncResult(
        profile_id=1,
        selection_version="matching-selection-v1",
        selected_count=0,
        state="EMPTY",
        persisted=persisted,
        created=None,
        run_id=None,
        run_fingerprint=None,
        batch_fingerprint=None,
        corpus_fingerprint=None,
        tfidf_model_fingerprint=None,
        lane_counts={"PRIMARY": 0, "UNCERTAIN": 0, "OUTSIDE_PREFERENCES": 0},
    )


@pytest.mark.parametrize("arguments", [[], ["--database", "x"], ["--profile-id", "1"]])
def test_required_arguments(arguments):
    with pytest.raises(SystemExit) as raised:
        sync_cli.parse_args(arguments)
    assert raised.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_invalid_profile_id_is_rejected(value):
    with pytest.raises(SystemExit) as raised:
        sync_cli.parse_args(["--database", "x", "--profile-id", value])
    assert raised.value.code == 2


def test_dry_run_uses_readonly_connection_and_stable_json(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(
        sync_cli,
        "connect_readonly_database",
        lambda path: opened.append(path) or _Connection(),
    )
    monkeypatch.setattr(
        sync_cli,
        "sync_matching",
        lambda connection, profile_id, *, persist: _result(persist),
    )
    assert (
        sync_cli.main(["--database", "target.db", "--profile-id", "1", "--dry-run"])
        == 0
    )
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert opened == ["target.db"]
    assert payload["persisted"] is False
    assert stdout == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def test_persisted_mode_uses_writable_connection(monkeypatch, capsys):
    monkeypatch.setattr(sync_cli, "connect_database", lambda path: _Connection())
    monkeypatch.setattr(
        sync_cli,
        "sync_matching",
        lambda connection, profile_id, *, persist: _result(persist),
    )
    assert sync_cli.main(["--database", "target.db", "--profile-id", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["persisted"] is True
