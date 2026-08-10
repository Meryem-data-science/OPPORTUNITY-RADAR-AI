"""Read-only probe for LinkedIn job alerts obtained through Gmail."""

import argparse
import json
import os
import sys
from typing import Sequence

from services.collector.cli.gmail_probe import bounded_limit
from services.collector.gmail import (
    GmailApiError, GmailAuthenticationError, GmailClient, GmailConfiguration,
    GmailConfigurationError, GmailPayloadError,
)
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers import parse_linkedin_job_alert


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse LinkedIn job-alert emails read-only")
    parser.add_argument("--query", default=os.environ.get("GMAIL_LINKEDIN_ALERT_QUERY"))
    parser.add_argument("--limit", type=bounded_limit, default=5)
    return parser


def safe_summary(candidate: OpportunityCandidate) -> dict[str, str | int | None]:
    return {
        "source_id": candidate.source_id,
        "source_external_id": candidate.source_external_id,
        "canonical_title": candidate.canonical_title,
        "organization": candidate.organization,
        "location": candidate.location,
        "published_at": candidate.published_at,
        "source_url": candidate.source_url,
        "application_url": candidate.application_url,
        "canonical_url": candidate.canonical_url,
        "description_length": len(candidate.description or ""),
    }


def run(query: str | None, limit: int) -> list[dict[str, str | int | None]]:
    if not query or not query.strip():
        raise GmailConfigurationError(
            "provide --query or set GMAIL_LINKEDIN_ALERT_QUERY to a non-empty Gmail query"
        )
    client = GmailClient.from_configuration(GmailConfiguration.from_environment())
    summaries = [safe_summary(candidate) for message in client.search(query, limit)
                 for candidate in parse_linkedin_job_alert(message)]
    for summary in summaries:
        print(json.dumps(summary, ensure_ascii=False))
    return summaries


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        run(args.query, args.limit)
    except (GmailConfigurationError, GmailAuthenticationError, GmailApiError, GmailPayloadError) as error:
        print(f"linkedin alert probe error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
