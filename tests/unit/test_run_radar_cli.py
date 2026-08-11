import logging
from unittest.mock import Mock

import pytest

from services.collector.agent import QualificationRunSummary, RadarRunSummary
from services.collector.cli import run_radar


@pytest.fixture(autouse=True)
def isolated_logger(monkeypatch):
    monkeypatch.setattr(run_radar, "get_logger", lambda unused: logging.getLogger("test.run_radar"))


def summary(failed=0, *, qualification_success=True):
    qualification = QualificationRunSummary(
        qualification_success, 1 if qualification_success else 0, 0, 1, 2,
        None if qualification_success else "SensitiveQualificationError",
    )
    return RadarRunSummary(1, 1 - failed, failed, 2, 1, 1, (), qualification)


def test_cli_refuses_without_apply(monkeypatch):
    run_once = Mock()
    monkeypatch.setattr(run_radar.RadarAgent, "run_once", run_once)
    assert run_radar.main(["--once"]) == 1
    run_once.assert_not_called()


def test_cli_requires_once(monkeypatch):
    run_once = Mock()
    monkeypatch.setattr(run_radar.RadarAgent, "run_once", run_once)
    assert run_radar.main(["--apply"]) == 1
    run_once.assert_not_called()


def test_cli_returns_zero_and_prints_summary(monkeypatch, capsys):
    monkeypatch.setattr(run_radar.RadarAgent, "run_once", lambda self: summary())
    assert run_radar.main(["--once", "--apply"]) == 0
    output = capsys.readouterr().out
    assert "Radar run completed" in output
    assert "items_created=1" in output
    assert "qualification_success=True" in output
    assert "qualifications_created=1" in output
    assert "qualifications_unchanged=1" in output


def test_cli_returns_one_when_a_source_fails(monkeypatch):
    monkeypatch.setattr(run_radar.RadarAgent, "run_once", lambda self: summary(1))
    assert run_radar.main(["--once", "--apply"]) == 1


def test_cli_returns_one_and_prints_only_qualification_error_type(monkeypatch, capsys):
    monkeypatch.setattr(
        run_radar.RadarAgent, "run_once",
        lambda self: summary(qualification_success=False),
    )
    assert run_radar.main(["--once", "--apply"]) == 1
    output = capsys.readouterr().out
    assert "qualification_success=False" in output
    assert "qualification_error_type=SensitiveQualificationError" in output
    assert "private exception message" not in output


def test_cli_handles_expected_agent_error(monkeypatch):
    def fail(self):
        raise ValueError("invalid configuration")
    monkeypatch.setattr(run_radar.RadarAgent, "run_once", fail)
    assert run_radar.main(["--once", "--apply"]) == 1


def test_cli_passes_repeatable_source_filter(monkeypatch):
    built = []
    class Agent:
        def __init__(self, **kwargs):
            built.append(kwargs)
        def run_once(self):
            return summary()
    monkeypatch.setattr(run_radar, "RadarAgent", Agent)
    assert run_radar.main(["--once", "--apply", "--source", "one", "--source", "two"]) == 0
    assert built == [{"source_ids": ["one", "two"]}]
