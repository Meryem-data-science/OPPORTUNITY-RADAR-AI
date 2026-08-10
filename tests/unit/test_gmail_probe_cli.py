import json
from unittest.mock import patch

import pytest

from services.collector.cli.gmail_probe import build_parser, run
from services.collector.models.gmail_message import GmailMessageCandidate


def candidate() -> GmailMessageCandidate:
    return GmailMessageCandidate(
        message_id="fictitious-message",
        thread_id="fictitious-thread",
        sender="jobs@example.test",
        subject="Example role",
        received_at="2026-01-01T00:00:00+00:00",
        snippet="x" * 250,
        body_text="PRIVATE FULL TEXT",
        body_html="<p>PRIVATE FULL HTML</p>",
    )


def test_probe_prints_only_safe_summary(capsys: pytest.CaptureFixture[str]) -> None:
    fake_client = patch("services.collector.cli.gmail_probe.GmailClient.from_configuration")
    with patch("services.collector.cli.gmail_probe.GmailConfiguration.from_environment"), fake_client as factory:
        factory.return_value.search.return_value = [candidate()]
        summaries = run("from:jobs@example.test", 1)
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed == summaries[0]
    assert parsed["body_text_length"] == len("PRIVATE FULL TEXT")
    assert parsed["body_html_length"] == len("<p>PRIVATE FULL HTML</p>")
    assert len(parsed["snippet"]) == 200
    assert "PRIVATE FULL" not in output
    assert "access_token" not in output and "client_secret" not in output


@pytest.mark.parametrize("value", ["0", "-1", "101", "not-an-int"])
def test_probe_rejects_invalid_limits(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--query", "query", "--limit", value])
