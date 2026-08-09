import logging

import pytest

from services.collector.agent import RadarRunSummary
from services.collector.cli import run_radar


@pytest.fixture(autouse=True)
def isolated_logger(monkeypatch):
    monkeypatch.setattr(run_radar, "get_logger", lambda unused: logging.getLogger("test.run_radar"))


def summary(failed=0):
    return RadarRunSummary(1, 1 - failed, failed, 2, 1, 1, ())


def test_cli_refuses_without_apply(monkeypatch):
    agent = monkeypatch.setattr(run_radar.RadarAgent, "run_once", lambda self: summary())
    assert run_radar.main(["--once"]) == 1


def test_cli_requires_once():
    assert run_radar.main(["--apply"]) == 1


def test_cli_returns_zero_and_prints_summary(monkeypatch, capsys):
    monkeypatch.setattr(run_radar.RadarAgent, "run_once", lambda self: summary())
    assert run_radar.main(["--once", "--apply"]) == 0
    output = capsys.readouterr().out
    assert "Radar run completed" in output
    assert "items_created=1" in output


def test_cli_returns_one_when_a_source_fails(monkeypatch):
    monkeypatch.setattr(run_radar.RadarAgent, "run_once", lambda self: summary(1))
    assert run_radar.main(["--once", "--apply"]) == 1


def test_cli_handles_expected_agent_error(monkeypatch):
    def fail(self):
        raise ValueError("invalid configuration")
    monkeypatch.setattr(run_radar.RadarAgent, "run_once", fail)
    assert run_radar.main(["--once", "--apply"]) == 1
