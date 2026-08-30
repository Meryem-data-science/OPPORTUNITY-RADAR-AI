from services.collector.agent import RadarAgent
from services.collector.collectors.linkedin_job_alert import LinkedInJobAlertCollector
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.models.gmail_message import GmailMessageCandidate
from services.collector.sources import SourceConfig


def test_linkedin_email_to_radar_to_sqlite_is_idempotent(tmp_path):
    database = tmp_path / "linkedin-radar.db"
    connection = connect_database(database)
    try:
        assert apply_migrations(connection) == [
            "0001",
            "0002",
            "0003",
            "0004",
            "0005",
            "0006",
            "0007",
            "0008",
            "0009",
            "0010",
            "0011",
        ]
    finally:
        connection.close()

    source = SourceConfig(
        "linkedin_job_alert_email", "gmail_linkedin_alert", True,
        category="jobs", frequency_minutes=60, status="active",
        gmail_query="newer_than:7d from:linkedin.com", gmail_message_limit=50,
    )
    first_email = GmailMessageCandidate(
        "fictional-1", None, "alerts@linkedin.com", "Alert", None, None, None,
        '<div><a href="https://www.linkedin.com/jobs/view/111?trk=fake">Data Engineer</a><div>Fiction Labs</div><div>Rabat, Morocco</div></div>',
    )
    second_email = GmailMessageCandidate(
        "fictional-2", None, "alerts@linkedin.com", "Alert", None, None, None,
        '<div><a href="https://www.linkedin.com/jobs/view/111?tracking=fake">Data Engineer</a><div>Fiction Labs</div><div>Rabat, Morocco</div></div><div><a href="https://www.linkedin.com/jobs/view/222?trk=fake">ML Engineer</a><div>Example Corp</div><div>Paris, France</div></div>',
    )
    class FakeGmailClient:
        def search(self, query, limit):
            assert (query, limit) == (source.gmail_query, source.gmail_message_limit)
            return [first_email, second_email]

    collector = LinkedInJobAlertCollector(
        source,
        gmail_client_factory=lambda unused: FakeGmailClient(),
        configuration_loader=lambda: object(),
    )
    agent = RadarAgent(
        source_loader=lambda: [source],
        collector_factory=lambda received: collector,
        settings_loader=lambda: Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE, database),
    )
    first = agent.run_once()
    second = agent.run_once()
    assert (first.items_created, first.items_updated) == (2, 0)
    assert (second.items_created, second.items_updated) == (0, 2)

    connection = connect_database(database)
    try:
        rows = connection.execute(
            "SELECT source_url, application_url, canonical_url, description, published_at "
            "FROM opportunities ORDER BY source_url"
        ).fetchall()
        occurrences = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT source_url) FROM opportunity_sources "
            "WHERE source_id = ?", (source.id,),
        ).fetchone()
    finally:
        connection.close()
    assert occurrences == (2, 2)
    assert len(rows) == 2
    for source_url, application_url, canonical_url, description, published_at in rows:
        assert source_url == application_url == canonical_url
        assert source_url.startswith("https://www.linkedin.com/jobs/view/")
        assert "?" not in source_url
        assert description is None and published_at is None
