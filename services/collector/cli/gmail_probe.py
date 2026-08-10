"""Safely summarize messages returned by the read-only Gmail intake."""

import argparse
import json
import os
import sys
from typing import Sequence

from services.collector.gmail import (
    GmailApiError,
    GmailAuthenticationError,
    GmailClient,
    GmailConfiguration,
    GmailConfigurationError,
    GmailPayloadError,
)
from services.collector.gmail.client import MAX_MESSAGE_LIMIT


def bounded_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if not 1 <= limit <= MAX_MESSAGE_LIMIT:
        raise argparse.ArgumentTypeError(
            f"limit must be between 1 and {MAX_MESSAGE_LIMIT}"
        )
    return limit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe Gmail without modifying messages")
    parser.add_argument("--query", default=os.environ.get("GMAIL_ALERT_QUERY"))
    parser.add_argument("--limit", type=bounded_limit, default=5)
    return parser


def safe_summary(message: object) -> dict[str, str | int | None]:
    """Return only bounded metadata and body lengths."""
    snippet = getattr(message, "snippet")
    body_text = getattr(message, "body_text")
    body_html = getattr(message, "body_html")
    return {
        "message_id": getattr(message, "message_id"),
        "sender": getattr(message, "sender"),
        "subject": getattr(message, "subject"),
        "received_at": getattr(message, "received_at"),
        "snippet": snippet[:200] if snippet else None,
        "body_text_length": len(body_text) if body_text is not None else 0,
        "body_html_length": len(body_html) if body_html is not None else 0,
    }


def run(query: str | None, limit: int) -> list[dict[str, str | int | None]]:
    if not query or not query.strip():
        raise GmailConfigurationError(
            "provide --query or set GMAIL_ALERT_QUERY to a non-empty Gmail query"
        )
    configuration = GmailConfiguration.from_environment()
    client = GmailClient.from_configuration(configuration)
    summaries = [safe_summary(message) for message in client.search(query, limit)]
    for summary in summaries:
        print(json.dumps(summary, ensure_ascii=False))
    return summaries


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        run(args.query, args.limit)
    except (
        GmailConfigurationError,
        GmailAuthenticationError,
        GmailApiError,
        GmailPayloadError,
    ) as error:
        print(f"gmail probe error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
