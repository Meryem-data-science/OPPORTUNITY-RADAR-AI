import logging
from unittest.mock import Mock

import pytest

from services.collector.collectors.linkedin_job_alert import LinkedInJobAlertCollector
from services.collector.gmail.client import GmailConfiguration
from services.collector.models.gmail_message import GmailMessageCandidate
from services.collector.sources import SourceConfig


def source(source_id="linkedin_job_alert_email"):
    return SourceConfig(
        source_id, "gmail_linkedin_alert", True,
        gmail_query="newer_than:7d from:linkedin.com", gmail_message_limit=50,
    )


def message(job_id="123456", title="Data Engineer", company="Fiction Labs", location="Rabat, Morocco"):
    html = f'''<table><tr><td><a href="https://www.linkedin.com/jobs/view/{job_id}?trk=secret">{title}</a><div>{company}</div><div>{location}</div></td></tr></table>'''
    return GmailMessageCandidate("private-message-id", "private-thread-id", "Jobs <alerts@linkedin.com>", "Jobs", None, "private snippet", None, html)


def build(messages, *, logger=None):
    client = Mock()
    client.search.return_value = messages
    configuration = GmailConfiguration.__new__(GmailConfiguration)
    collector = LinkedInJobAlertCollector(
        source(), gmail_client_factory=lambda received: client,
        configuration_loader=lambda: configuration, logger=logger,
    )
    return collector, client


def test_zero_messages_and_exact_search_configuration():
    collector, client = build([])
    assert collector.collect() == []
    client.search.assert_called_once_with("newer_than:7d from:linkedin.com", 50)


def test_parses_aggregates_and_deduplicates_canonical_jobs():
    collector, unused = build([message(), message(), message("789", "ML Engineer", "Example Corp", "Paris, France")])
    candidates = collector.collect()
    assert [item.source_external_id for item in candidates] == ["123456", "789"]
    assert candidates[0].organization == "Fiction Labs"
    assert candidates[0].location == "Rabat, Morocco"
    assert candidates[0].description is None and candidates[0].published_at is None
    assert candidates[0].source_url == "https://www.linkedin.com/jobs/view/123456"
    assert candidates[0].source_url == candidates[0].application_url == candidates[0].canonical_url
    assert "?" not in candidates[0].source_url


def test_non_linkedin_email_is_ignored_and_client_errors_propagate():
    other = message()
    other = GmailMessageCandidate(other.message_id, other.thread_id, "sender@example.com", other.subject, other.received_at, other.snippet, other.body_text, other.body_html)
    collector, client = build([other])
    assert collector.collect() == []
    client.search.side_effect = RuntimeError("gmail unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        collector.collect()


def test_logs_counts_but_never_message_content_or_ids(caplog):
    logger = logging.getLogger("test.linkedin.collector")
    collector, unused = build([message()], logger=logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        collector.collect()
    assert "private-message-id" not in caplog.text
    assert "private-thread-id" not in caplog.text
    assert "private snippet" not in caplog.text
    assert "Data Engineer" not in caplog.text
    record = caplog.records[-1]
    assert (record.messages_found, record.candidates_parsed, record.candidates_unique) == (1, 1, 1)


def test_inconsistent_source_id_fails_fast():
    with pytest.raises(ValueError, match="parser source id"):
        LinkedInJobAlertCollector(source("wrong"))
