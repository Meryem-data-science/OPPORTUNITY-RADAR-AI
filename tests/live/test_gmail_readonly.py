"""Opt-in smoke test against a real Gmail account; never mutates messages."""

import os

import pytest

from services.collector.gmail import GmailClient, GmailConfiguration


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GMAIL_TEST") != "1",
    reason="set RUN_LIVE_GMAIL_TEST=1 for the local read-only Gmail smoke test",
)


def test_real_gmail_readonly_message_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    query = os.environ.get("GMAIL_ALERT_QUERY", "").strip()
    if not query:
        pytest.fail("GMAIL_ALERT_QUERY must identify real mail for the live smoke test")
    messages = GmailClient.from_configuration(GmailConfiguration.from_environment()).search(query, 3)
    assert capsys.readouterr().out == ""
    if not messages:
        pytest.fail(f"no real Gmail message currently matches the configured query {query!r}")
    assert all(message.message_id for message in messages)
