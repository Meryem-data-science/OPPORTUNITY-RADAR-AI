import json
from unittest.mock import Mock, patch

import pytest

from services.collector.cli.linkedin_alert_probe import build_parser, run
from services.collector.gmail import GmailConfigurationError
from services.collector.models.gmail_message import GmailMessageCandidate


def candidate():
    private_text = "PRIVATE BODY MUST NOT APPEAR"
    html = '<div><a href="https://www.linkedin.com/jobs/view/123?trk=x">Role</a><div>Company</div></div>'
    return GmailMessageCandidate("message-secret", None, "LinkedIn <jobs@linkedin.com>", "Subject", None, None, private_text, html)


def test_cli_reuses_gmail_and_prints_only_opportunity_summary(capsys):
    client = Mock()
    client.search.return_value = [candidate()]
    with patch("services.collector.cli.linkedin_alert_probe.GmailConfiguration.from_environment"), patch(
        "services.collector.cli.linkedin_alert_probe.GmailClient.from_configuration", return_value=client
    ):
        summaries = run("label:linkedin", 4)
    client.search.assert_called_once_with("label:linkedin", 4)
    output = capsys.readouterr().out
    assert "PRIVATE BODY MUST NOT APPEAR" not in output
    assert "message-secret" not in output
    assert "body_text" not in output and "body_html" not in output
    assert json.loads(output)["source_external_id"] == "123"
    assert summaries[0]["description_length"] == 0


def test_cli_requires_query_and_reads_dedicated_environment(monkeypatch):
    with pytest.raises(GmailConfigurationError, match="GMAIL_LINKEDIN_ALERT_QUERY"):
        run(" ", 1)
    monkeypatch.setenv("GMAIL_LINKEDIN_ALERT_QUERY", "category:updates")
    assert build_parser().parse_args([]).query == "category:updates"


def test_cli_limit_is_bounded_to_gmail_maximum():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--limit", "101"])
