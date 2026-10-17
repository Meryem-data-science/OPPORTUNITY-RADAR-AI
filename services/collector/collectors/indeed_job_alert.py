"""Collect official Indeed job-alert emails through the read-only Gmail client."""

from collections.abc import Callable
import logging

from services.collector.gmail.client import GmailClient, GmailConfiguration
from services.collector.logging_config import get_logger
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.indeed_job_alert import (
    INDEED_JOB_ALERT_SOURCE_ID,
    PARSER_VERSION,
    IndeedJobAlertParseError,
    parse_indeed_job_alert,
)
from services.collector.sources import SourceConfig


GmailClientFactory = Callable[[GmailConfiguration], GmailClient]


class IndeedJobAlertPartialError(ValueError):
    """Some emails were unreadable; usable candidates can still be persisted."""


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
        self.collection_warning: Exception | None = None

    def run_metrics(self) -> dict[str, str]:
        """Expose the parser version without labelling Gmail messages as pages."""
        return {"parser_version": PARSER_VERSION}

    def collect(self) -> list[OpportunityCandidate]:
        self.collection_warning = None
        client = self._gmail_client_factory(self._configuration_loader())
        messages = client.search(self.source.gmail_query, self.source.gmail_message_limit)
        parsed: list[OpportunityCandidate] = []
        unreadable_messages = 0
        for message in messages:
            try:
                parsed.extend(parse_indeed_job_alert(message))
            except IndeedJobAlertParseError:
                unreadable_messages += 1

        unique: list[OpportunityCandidate] = []
        seen: set[str] = set()
        for candidate in parsed:
            if candidate.source_id != self.source.id:
                raise ValueError("parser returned a candidate for an inconsistent source")
            if candidate.source_external_id in seen:
                continue
            seen.add(candidate.source_external_id)
            unique.append(candidate)
        if unreadable_messages:
            warning = IndeedJobAlertPartialError(
                f"{unreadable_messages} email(s) Indeed sur {len(messages)} non exploitables "
                f"(format ou lien de suivi non pris en charge). "
                f"{len(unique)} annonce(s) lisible(s). Les liens opaques ne sont pas ouverts."
            )
            if not unique:
                raise warning
            self.collection_warning = warning
        self._logger.info(
            "Indeed Gmail alert collection completed.",
            extra={
                "event": "indeed_alert_collection_completed",
                "source_id": self.source.id,
                "messages_found": len(messages),
                "candidates_parsed": len(parsed),
                "candidates_unique": len(unique),
                "unreadable_messages": unreadable_messages,
            },
        )
        return unique
