"""Read LinkedIn job alerts from Gmail without accessing LinkedIn pages."""

from collections.abc import Callable
import logging

from services.collector.gmail.client import GmailClient, GmailConfiguration
from services.collector.logging_config import get_logger
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.linkedin_job_alert import (
    LINKEDIN_JOB_ALERT_SOURCE_ID,
    parse_linkedin_job_alert,
)
from services.collector.sources import SourceConfig


GmailClientFactory = Callable[[GmailConfiguration], GmailClient]


class LinkedInJobAlertCollector:
    """Collect and batch-deduplicate jobs from read-only Gmail messages."""

    def __init__(
        self,
        source: SourceConfig,
        *,
        gmail_client_factory: GmailClientFactory = GmailClient.from_configuration,
        configuration_loader: Callable[[], GmailConfiguration] = GmailConfiguration.from_environment,
        logger: logging.Logger | None = None,
    ) -> None:
        if source.id != LINKEDIN_JOB_ALERT_SOURCE_ID:
            raise ValueError(
                f"LinkedIn parser source id requires source {LINKEDIN_JOB_ALERT_SOURCE_ID!r}"
            )
        if source.type != "gmail_linkedin_alert":
            raise ValueError("LinkedIn alert collector requires gmail_linkedin_alert")
        if source.gmail_query is None or source.gmail_message_limit is None:
            raise ValueError("LinkedIn Gmail source is missing query or message limit")
        self.source = source
        self._gmail_client_factory = gmail_client_factory
        self._configuration_loader = configuration_loader
        self._logger = logger or get_logger(__name__)

    def collect(self) -> list[OpportunityCandidate]:
        client = self._gmail_client_factory(self._configuration_loader())
        messages = client.search(self.source.gmail_query, self.source.gmail_message_limit)
        parsed: list[OpportunityCandidate] = []
        for message in messages:
            parsed.extend(parse_linkedin_job_alert(message))

        unique: list[OpportunityCandidate] = []
        seen: set[str] = set()
        for candidate in parsed:
            if candidate.source_id != self.source.id:
                raise ValueError("parser returned a candidate for an inconsistent source")
            key = candidate.source_external_id
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        self._logger.info(
            "LinkedIn Gmail alert collection completed.",
            extra={
                "event": "linkedin_alert_collection_completed",
                "source_id": self.source.id,
                "messages_found": len(messages),
                "candidates_parsed": len(parsed),
                "candidates_unique": len(unique),
            },
        )
        return unique
