"""Collect official Indeed job-alert emails through the read-only Gmail client."""

from collections.abc import Callable
import logging

from services.collector.gmail.client import GmailClient, GmailConfiguration
from services.collector.logging_config import get_logger
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.indeed_job_alert import (
    INDEED_JOB_ALERT_SOURCE_ID,
    PARSER_VERSION,
    parse_indeed_job_alert,
)
from services.collector.sources import SourceConfig


GmailClientFactory = Callable[[GmailConfiguration], GmailClient]


class IndeedJobAlertCollector:
    """Collect bounded Gmail results and deduplicate on Indeed's stable job key."""

    def __init__(
        self,
        source: SourceConfig,
        *,
        gmail_client_factory: GmailClientFactory = GmailClient.from_configuration,
        configuration_loader: Callable[[], GmailConfiguration] = GmailConfiguration.from_environment,
        logger: logging.Logger | None = None,
    ) -> None:
        if source.id != INDEED_JOB_ALERT_SOURCE_ID:
            raise ValueError(
                f"Indeed parser source id requires source {INDEED_JOB_ALERT_SOURCE_ID!r}"
            )
        if source.type != "gmail_indeed_alert":
            raise ValueError("Indeed alert collector requires gmail_indeed_alert")
        if source.gmail_query is None or source.gmail_message_limit is None:
            raise ValueError("Indeed Gmail source is missing query or message limit")
        self.source = source
        self._gmail_client_factory = gmail_client_factory
        self._configuration_loader = configuration_loader
        self._logger = logger or get_logger(__name__)

    def run_metrics(self) -> dict[str, str]:
        """Expose the parser version without labelling Gmail messages as pages."""
        return {"parser_version": PARSER_VERSION}

    def collect(self) -> list[OpportunityCandidate]:
        client = self._gmail_client_factory(self._configuration_loader())
        messages = client.search(self.source.gmail_query, self.source.gmail_message_limit)
        parsed: list[OpportunityCandidate] = []
        for message in messages:
            # Layout errors propagate so the source is visibly failed instead
            # of reporting a misleading successful collection of zero jobs.
            parsed.extend(parse_indeed_job_alert(message))

        unique: list[OpportunityCandidate] = []
        seen: set[str] = set()
        for candidate in parsed:
            if candidate.source_id != self.source.id:
                raise ValueError("parser returned a candidate for an inconsistent source")
            if candidate.source_external_id in seen:
                continue
            seen.add(candidate.source_external_id)
            unique.append(candidate)
        self._logger.info(
            "Indeed Gmail alert collection completed.",
            extra={
                "event": "indeed_alert_collection_completed",
                "source_id": self.source.id,
                "messages_found": len(messages),
                "candidates_parsed": len(parsed),
                "candidates_unique": len(unique),
            },
        )
        return unique
