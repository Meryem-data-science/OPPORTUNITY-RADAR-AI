"""Safe, non-persistent CLI for the bounded public ReKrute collector."""

import argparse
import json
from typing import Sequence

from services.collector.collectors.rekrute import HARD_LIMIT, ReKruteCollector, ReKruteError, ReKruteHttpClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only ReKrute public probe")
    parser.add_argument("--query", default="data-engineer")
    parser.add_argument("--limit", type=int, default=3, choices=range(1, HARD_LIMIT + 1))
    parser.add_argument("--pause", type=float, default=0.5)
    return parser


def candidate_summary(candidate):
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


def main(argv: Sequence[str] | None = None, *, client=None) -> int:
    args = build_parser().parse_args(argv)
    owned_client = client is None
    http_client = client or ReKruteHttpClient()
    try:
        result = ReKruteCollector(http_client, pause_seconds=args.pause).collect(args.query, args.limit)
        print(json.dumps({"status": result.status, "candidate_count": len(result.candidates), "detail_fetch_failed": result.detail_fetch_failed, "parse_failed": result.parse_failed}))
        for candidate in result.candidates:
            print(json.dumps(candidate_summary(candidate), ensure_ascii=False))
        return 0 if result.candidates else 1
    except ReKruteError as error:
        status = {
            "RobotsDeniedError": "robots_denied",
            "SearchFetchError": "search_fetch_failed",
            "NoOfferLinksError": "no_offer_links",
        }.get(type(error).__name__, "parse_failed")
        print(json.dumps({"status": status, "error": type(error).__name__}))
        return 1
    finally:
        if owned_client:
            http_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
