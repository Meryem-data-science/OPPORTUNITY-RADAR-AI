"""Phase 7C.3A — bounded, GET-only audit of ReKrute's public access.

This is the network edge, and it is the *only* file in this slice that opens a
socket. It exists because 7C.3A could not verify ReKrute from the Claude Code
Cloud session: that sandbox's egress policy answers 403 to
`CONNECT www.rekrute.com:443`, so no ReKrute byte was ever received and no
parser could honestly be written against the site's markup. Run from a network
that may reach the host, this command produces exactly the evidence that was
missing.

    python -m evaluation.morocco_pfe.cli.rekrute_access_audit
    python -m evaluation.morocco_pfe.cli.rekrute_access_audit \
        --url https://www.rekrute.com/<a public listing path> --follow-links --limit 5

What it does, and refuses to do:

* **GET only**, sequential, one page at a time, with a delay between requests
  and an explicit timeout. It is an audit, not a crawler, and `--limit` is hard
  bounded at 10 detail pages;
* it fetches `robots.txt` first and **obeys it**. A path robots disallows is
  not fetched, and the run exits `3` saying so;
* it stops at any wall it meets — 403, 429, CAPTCHA/bot challenge, a login
  redirect — and reports it. Nothing here bypasses a CAPTCHA, rotates a proxy,
  impersonates a browser, drives a browser engine, authenticates, or sends a
  cookie. The user agent names this audit truthfully;
* it writes **nothing**: no SQLite, no benchmark row, no raw HTML on disk. It
  prints one JSON object of structural findings to stdout;
* it prints **structure, not content**: JSON-LD types and key *names*, link
  counts and path shapes, response codes. No job description, no page body and
  no personal data is emitted.

Exit codes: `0` audit completed, `1` transport/usage failure, `2` an access
barrier was encountered, `3` robots.txt disallows the requested path.

Reading the report does not establish that automated collection of this site is
permitted. It records what a public GET returned; ReKrute's terms remain a
separate question for a human, and this file makes no legal claim.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx

from evaluation.morocco_pfe.rekrute_access import (
    AUDIT_USER_AGENT,
    DEFAULT_DETAIL_LIMIT,
    MAX_DETAIL_LIMIT,
    RekruteAccessError,
    detect_access_barrier,
    extract_rekrute_links,
    parse_robots_txt,
    path_shapes,
    robots_verdict,
    summarize_jsonld,
    validate_detail_limit,
)
from services.collector.parsers.rekrute import canonical_offer_url

#: The homepage recorded in the source map, whose status there is
#: WELL_KNOWN_UNVERIFIED. No listing path is hardcoded: 7C.3A never verified
#: one, and inventing a plausible-looking search URL is fabrication. An
#: operator passes the listing page they can see with `--url`.
DEFAULT_AUDIT_URL = "https://www.rekrute.com/"
ROBOTS_URL = "https://www.rekrute.com/robots.txt"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_DELAY_SECONDS = 1.0

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_BARRIER = 2
EXIT_ROBOTS_DISALLOWED = 3


@dataclass
class PageObservation:
    """What one public GET actually returned, in structural terms only."""

    url: str
    status_code: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    body_bytes: int = 0
    barrier: str | None = None
    error: str | None = None
    jsonld: dict[str, Any] = field(default_factory=dict)
    rekrute_links: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status_code": self.status_code,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "body_bytes": self.body_bytes,
            "barrier": self.barrier,
            "error": self.error,
            "jsonld": self.jsonld,
            "rekrute_links": self.rekrute_links,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit ReKrute public access GET-only and report page structure. "
            "Obeys robots.txt, never authenticates, writes nothing."
        )
    )
    parser.add_argument("--url", default=DEFAULT_AUDIT_URL, help="public page to audit")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_DETAIL_LIMIT,
        help=f"detail pages to sample (1-{MAX_DETAIL_LIMIT})",
    )
    parser.add_argument(
        "--follow-links",
        action="store_true",
        help="also sample discovered same-host pages, up to --limit",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    return parser


def _get(client: httpx.Client, url: str, timeout: float) -> tuple[httpx.Response | None, str | None]:
    """One polite GET. Transport failures are named, never retried in a loop."""
    try:
        response = client.get(url, timeout=timeout, follow_redirects=True)
    except httpx.TimeoutException:
        return None, "TIMEOUT"
    except httpx.TooManyRedirects:
        return None, "TOO_MANY_REDIRECTS"
    except httpx.RequestError as error:
        return None, f"NETWORK_ERROR:{type(error).__name__}"
    return response, None


def _observe(client: httpx.Client, url: str, timeout: float) -> PageObservation:
    observation = PageObservation(url=url)
    response, error = _get(client, url, timeout)
    if response is None:
        observation.error = error
        return observation
    body = response.text
    observation.status_code = response.status_code
    observation.final_url = str(response.url)
    observation.content_type = response.headers.get("content-type")
    observation.body_bytes = len(response.content)
    observation.barrier = detect_access_barrier(
        response.status_code, observation.final_url, body
    )
    if observation.barrier is None:
        observation.jsonld = summarize_jsonld(body).as_dict()
        observation.rekrute_links = len(extract_rekrute_links(body, observation.final_url))
    return observation


def run(
    url: str,
    limit: int,
    *,
    follow_links: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    delay: float = DEFAULT_DELAY_SECONDS,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Audit one public page, obeying robots.txt, and return the report."""
    detail_limit = validate_detail_limit(limit)
    try:
        target = canonical_offer_url(url)
    except Exception as error:
        raise RekruteAccessError(f"--url must be a public ReKrute http(s) URL: {error}")

    owns_client = client is None
    active = client or httpx.Client(headers={"User-Agent": AUDIT_USER_AGENT})
    report: dict[str, Any] = {
        "audit_phase": "7C.3A",
        "user_agent": AUDIT_USER_AGENT,
        "requested_url": target,
        "detail_limit": detail_limit,
        "authenticated": False,
        "browser_automation": False,
        "wrote_database": False,
        "wrote_raw_html": False,
    }
    try:
        robots_response, robots_error = _get(active, ROBOTS_URL, timeout)
        if robots_response is None:
            report["robots"] = {"available": False, "error": robots_error}
            report["outcome"] = "ROBOTS_UNREACHABLE"
            report["exit_code"] = EXIT_BARRIER
            return report
        robots_available = robots_response.status_code == 200
        groups = parse_robots_txt(robots_response.text) if robots_available else ()
        verdict = robots_verdict(groups, urlsplit(target).path or "/")
        report["robots"] = {
            "available": robots_available,
            "status_code": robots_response.status_code,
            "group_count": len(groups),
            "verdict": verdict.as_dict(),
        }
        if robots_available and not verdict.allowed:
            report["outcome"] = "ROBOTS_DISALLOWED"
            report["exit_code"] = EXIT_ROBOTS_DISALLOWED
            return report

        pages: list[PageObservation] = []
        first = _observe(active, target, timeout)
        pages.append(first)
        if first.error or first.barrier:
            report["pages"] = [page.as_dict() for page in pages]
            report["outcome"] = "BARRIER" if first.barrier else "TRANSPORT_FAILURE"
            report["exit_code"] = EXIT_BARRIER if first.barrier else EXIT_FAILURE
            return report

        listing_response, _ = _get(active, target, timeout)
        links = (
            extract_rekrute_links(listing_response.text, str(listing_response.url))
            if listing_response is not None
            else ()
        )
        report["discovered_links"] = len(links)
        report["path_shapes"] = [
            {"shape": shape, "count": count} for shape, count in path_shapes(links)
        ]
        if follow_links:
            for link in [item for item in links if item != target][:detail_limit]:
                if not robots_verdict(groups, urlsplit(link).path or "/").allowed:
                    continue
                time.sleep(max(delay, 0.0))
                pages.append(_observe(active, link, timeout))
        report["pages"] = [page.as_dict() for page in pages]
        report["pages_fetched"] = len(pages)
        report["pages_with_job_posting_jsonld"] = sum(
            1 for page in pages if page.jsonld.get("job_posting_count")
        )
        barriers = [page.barrier for page in pages if page.barrier]
        report["barriers"] = sorted(set(barriers))
        report["outcome"] = "BARRIER" if barriers else "COMPLETED"
        report["exit_code"] = EXIT_BARRIER if barriers else EXIT_OK
        return report
    finally:
        if owns_client:
            active.close()


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        report = run(
            args.url,
            args.limit,
            follow_links=args.follow_links,
            timeout=args.timeout,
            delay=args.delay,
        )
    except RekruteAccessError as error:
        print(f"rekrute access audit error: {error}", file=sys.stderr)
        raise SystemExit(EXIT_FAILURE) from error
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(int(report.get("exit_code", EXIT_FAILURE)))


if __name__ == "__main__":
    main()
