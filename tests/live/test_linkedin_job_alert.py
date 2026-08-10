"""Opt-in validation against a real Gmail LinkedIn job alert (never synthetic)."""

import os

import pytest

from services.collector.gmail import GmailClient, GmailConfiguration
from services.collector.parsers.linkedin_job_alert import (
    LINKEDIN_JOB_ALERT_SOURCE_ID,
    parse_linkedin_job_alert,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LINKEDIN_ALERT_TEST") != "1"
    or not os.environ.get("GMAIL_LINKEDIN_ALERT_QUERY", "").strip(),
    reason="requires explicit live LinkedIn-alert opt-in and Gmail query",
)


def test_real_linkedin_job_alert(capsys):
    query = os.environ["GMAIL_LINKEDIN_ALERT_QUERY"]
    messages = GmailClient.from_configuration(GmailConfiguration.from_environment()).search(query, 5)
    assert messages, "no real Gmail message matched GMAIL_LINKEDIN_ALERT_QUERY"
    candidates = [candidate for message in messages for candidate in parse_linkedin_job_alert(message)]
    assert candidates, "matched Gmail messages contained no confidently parseable LinkedIn jobs"
    for candidate in candidates:
        assert candidate.source_id == LINKEDIN_JOB_ALERT_SOURCE_ID
        assert candidate.source_external_id
        assert candidate.canonical_title
        assert candidate.organization
        assert "/jobs/view/" in candidate.source_url
        assert "?" not in candidate.source_url
        if candidate.location is not None:
            assert candidate.location.casefold() not in {
                "recrutement actif", "actively recruiting"
            }
            assert " relation" not in candidate.location.casefold()
            assert " connection" not in candidate.location.casefold()
        assert candidate.canonical_url == f"https://www.linkedin.com/jobs/view/{candidate.source_external_id}"
    assert capsys.readouterr().out == ""
