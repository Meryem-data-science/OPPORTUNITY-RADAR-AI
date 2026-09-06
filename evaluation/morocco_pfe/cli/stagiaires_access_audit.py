"""Phase 7C.4A — bounded, GET-only audit of Stagiaires.ma's public structure.

This is the network edge, and it is the *only* file in this slice that opens a
socket. It answers the question a collector would have to answer first: does
Stagiaires.ma publish its offers in a form an honest, browser-free client can
read, and does an offer page carry the fields a PFE opportunity needs?

    python -m evaluation.morocco_pfe.cli.stagiaires_access_audit
    python -m evaluation.morocco_pfe.cli.stagiaires_access_audit --limit 1
    python -m evaluation.morocco_pfe.cli.stagiaires_access_audit \
        --terms-url https://www.stagiaires.ma/<a real public terms path>

There is deliberately no flag that names a sitemap, a listing or an offer URL.
The chain below is the only way this command chooses what to fetch, so no
invocation of it can make it GET an arbitrary address.

It follows the site's **own published discovery chain**, and nothing else:

    robots.txt -> the Sitemap: it declares -> offre-sitemap*.xml
               -> /stage-emploi-maroc/<numeric-id>-<slug>

The public PFE listing is a Next.js application whose initial HTML does not
carry the offer list as ordinary links. This audit therefore **does not** parse
an offer list out of it — it fetches it exactly once to record that it is
publicly reachable and what it structurally contains. Reading the rendered
listing would require driving a browser, which this phase forbids and which the
sitemap chain makes unnecessary.

What it does, and refuses to do:

* **GET only**, sequential, one page at a time, with a delay between requests
  and an explicit timeout. It is an audit, not a crawler. Detail sampling is
  hard bounded at three pages by `select_detail_sample`, and the number of
  sitemap files it will read is bounded too;
* it fetches `robots.txt` first and **obeys it**, under an explicit status
  policy. HTTP 200 is parsed and obeyed, and a path robots disallows is not
  fetched. 404/410 means the file is definitively absent, so no explicit rule
  applies and the audit may continue — a statement about robots.txt alone and
  **not** permission of any kind. 401/403/407/429 means we were *refused* the
  file, so we do not know what it says, and an unknown rule is never assumed
  permissive: the audit stops before requesting anything else;
* it discovers the sitemap from robots' `Sitemap:` declaration rather than from
  a URL written here or passed to it. If robots declares none, the audit stops
  and says so instead of guessing one;
* it **never follows a redirect off the Stagiaires.ma hosts.** Redirects are
  resolved by the audit itself, bounded, and only while they stay on the same
  host; an off-domain `Location` is reported as a finding and its target is
  never requested;
* it **writes nothing**: no SQLite, no benchmark row, no raw HTML on disk, no
  `config/sources.yaml` change. It prints one JSON object to stdout;
* it prints **structure, not content**: statuses, counts, JSON-LD type and key
  *names*, per-signal presence with a short excerpt for verification. Job
  descriptions are reported by length only. No page body is emitted or stored;
* `<lastmod>` is carried as `sitemap_lastmod` and is **never** presented as a
  publication date. A page's modification time is not an offer's posting time;
* it stops at any wall it meets — 403, 429, CAPTCHA/bot challenge, a login
  redirect — and reports it. Nothing here bypasses a CAPTCHA, rotates a proxy,
  impersonates a browser, drives a browser engine, calls a private API found in
  a JS bundle, authenticates, or sends a cookie. The user agent names this
  audit truthfully.

Exit codes: `0` audit completed, `1` transport/usage failure, `2` an access
barrier was encountered, `3` robots.txt disallows a required path.

Reading this report does not establish that automated collection of this site
is permitted, and it activates nothing. Stagiaires.ma has no collector, no
`config/sources.yaml` row and no `SourceConfig` type, and this command creates
none of them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

from evaluation.morocco_pfe.stagiaires_access import (
    AUDIT_USER_AGENT,
    DEFAULT_DETAIL_LIMIT,
    DETAIL_SAMPLE_STRATEGY,
    MAX_DETAIL_LIMIT,
    PFE_TARGET_URL,
    ROBOTS_ABSENT,
    ROBOTS_BARRIER,
    ROBOTS_OBEY,
    ROBOTS_URL,
    TERMS_NOT_ATTEMPTED,
    TERMS_REVIEW_NOTE,
    DetailObservation,
    StagiairesAccessError,
    classify_robots_response,
    combine_offer_sitemaps,
    describe_detail_page,
    detect_access_barrier,
    is_stagiaires_host,
    parse_offer_sitemap,
    parse_robots_txt,
    parse_sitemap_index,
    robots_verdict,
    select_detail_sample,
    signal_support,
    sitemap_declarations,
    summarize_jsonld,
    validate_detail_limit,
)

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_DELAY_SECONDS = 1.0

#: A bound on the *file* count, not just the page count. The index is expected
#: to declare two offer sitemaps today and may declare a third; it is not
#: expected to declare fifty, and an audit that would fetch fifty because a
#: document told it to is a crawler wearing an audit's name.
MAX_OFFER_SITEMAPS = 20

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_BARRIER = 2
EXIT_ROBOTS_DISALLOWED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Stagiaires.ma public access and sitemap/detail structure, "
            "GET-only. Obeys robots.txt, never authenticates, writes nothing."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_DETAIL_LIMIT,
        help=f"offer detail pages to sample (1-{MAX_DETAIL_LIMIT})",
    )
    parser.add_argument(
        "--terms-url",
        default=None,
        help=(
            "a real public terms/legal URL to check for reachability only. No "
            "URL is guessed: omit this and the check is reported as not "
            "attempted."
        ),
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    return parser


#: Redirect statuses this audit will resolve itself. It resolves them itself
#: precisely because httpx's own `follow_redirects=True` would follow a
#: same-host URL off to another host without ever asking us.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: A redirect chain longer than this is a loop or a misconfiguration, not a
#: page move. Bounded so the audit cannot be walked around the web by a chain
#: of Location headers.
MAX_REDIRECTS = 3

#: The barrier reported when a public Stagiaires.ma URL tries to hand the audit
#: to another host. It is reported, never followed.
OFF_DOMAIN_REDIRECT = "OFF_DOMAIN_REDIRECT_NOT_FOLLOWED"


def _get(
    client: httpx.Client, url: str, timeout: float
) -> tuple[httpx.Response | None, str | None]:
    """One polite GET that does **not** follow redirects on its own.

    `follow_redirects=False` is the network boundary, not a detail. With it
    left on, a same-host URL answering `302 Location: https://elsewhere/...`
    would be followed automatically and the audit would issue a GET to a host
    it never decided to talk to — the same-host rule enforced everywhere else
    in this slice would be true only until a site chose otherwise. Redirects
    are resolved by `_follow_same_host` instead, which can refuse.
    """
    try:
        response = client.get(url, timeout=timeout, follow_redirects=False)
    except httpx.TimeoutException:
        return None, "TIMEOUT"
    except httpx.TooManyRedirects:
        return None, "TOO_MANY_REDIRECTS"
    except httpx.RequestError as error:
        return None, f"NETWORK_ERROR:{type(error).__name__}"
    return response, None


def _redirect_target(response: httpx.Response, current_url: str) -> str | None:
    """The absolute URL a redirect response points at, or None."""
    if response.status_code not in _REDIRECT_STATUSES:
        return None
    location = (response.headers.get("location") or "").strip()
    if not location:
        return None
    return urljoin(current_url, location)


def _follow_same_host(
    client: httpx.Client, url: str, timeout: float
) -> tuple[httpx.Response | None, str | None, list[str], str | None]:
    """GET ``url``, resolving only redirects that stay on a Stagiaires.ma host.

    Returns the final response, a transport error, the redirect chain, and a
    barrier. The one rule that matters: an off-domain `Location` is **recorded
    and refused**. The audit never issues a GET to a host outside
    `STAGIAIRES_HOSTS`, and a site cannot obtain one by redirecting — the
    off-domain URL is reported as a finding and left unfetched.

    Bounded at `MAX_REDIRECTS` hops so this stays a redirect resolver and not a
    crawler that follows wherever it is sent.
    """
    chain: list[str] = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        response, error = _get(client, current, timeout)
        if response is None:
            return None, error, chain, None
        target = _redirect_target(response, current)
        if target is None:
            return response, None, chain, None
        chain.append(target)
        if not is_stagiaires_host(target):
            # Recorded, not followed. Reporting a URL is not fetching it.
            return response, None, chain, OFF_DOMAIN_REDIRECT
        current = target
    return None, None, chain, "TOO_MANY_REDIRECTS"


def _fetch_document(
    client: httpx.Client, url: str, timeout: float
) -> tuple[dict[str, Any], str | None]:
    """GET one URL once and describe the response without keeping its body.

    Returns the report row and the body. The body is handed back to the caller
    to be *analysed*, never stored: no code path writes it to disk, puts it in
    the report, or logs it. A body is returned only for a response the audit
    actually resolved — a refused off-domain redirect yields none.
    """
    row: dict[str, Any] = {"url": url}
    response, error, chain, barrier = _follow_same_host(client, url, timeout)
    if chain:
        row["redirect_chain"] = list(chain)
    if response is None:
        row["error"] = error
        if barrier:
            row["barrier"] = barrier
        return row, None
    body = response.text
    row["status_code"] = response.status_code
    row["final_url"] = str(response.url)
    row["content_type"] = response.headers.get("content-type")
    row["body_bytes"] = len(response.content)
    if barrier == OFF_DOMAIN_REDIRECT:
        # The response in hand is the redirect itself, not a page. Its body is
        # not analysed and the destination is not requested.
        row["barrier"] = barrier
        row["redirect_target"] = chain[-1]
        row["redirect_target_host"] = urlsplit(chain[-1]).hostname
        return row, None
    row["barrier"] = barrier or detect_access_barrier(
        response.status_code, row["final_url"], body
    )
    return row, body


def _robots_allows(
    groups: tuple[Any, ...], enforced: bool, url: str
) -> bool:
    if not enforced:
        return True
    return robots_verdict(groups, urlsplit(url).path or "/").allowed


def _audit_terms(
    client: httpx.Client,
    terms_url: str | None,
    timeout: float,
    *,
    groups: tuple[Any, ...],
    enforced: bool,
) -> dict[str, Any]:
    """Check one operator-supplied public terms URL for reachability only.

    Never decides anything and never reads the text. `manual_review_required`
    is unconditionally true: this audit does not and cannot determine whether
    automated collection is permitted, and neither a 200 nor a 404 changes that.
    """
    report: dict[str, Any] = {
        "url": terms_url,
        "status_code": None,
        "final_url": None,
        "available": False,
        "barrier": None,
        "manual_review_required": True,
        "note": TERMS_REVIEW_NOTE,
    }
    if not terms_url:
        report["barrier"] = TERMS_NOT_ATTEMPTED
        return report
    if not is_stagiaires_host(terms_url):
        report["barrier"] = "NOT_ATTEMPTED_OFF_DOMAIN"
        return report
    if not _robots_allows(groups, enforced, terms_url):
        report["barrier"] = "ROBOTS_DISALLOWED"
        return report
    row, _ = _fetch_document(client, terms_url, timeout)
    report["status_code"] = row.get("status_code")
    report["final_url"] = row.get("final_url")
    report["barrier"] = row.get("barrier") or row.get("error")
    report["available"] = row.get("status_code") == 200 and not report["barrier"]
    return report


def run(
    *,
    limit: int = DEFAULT_DETAIL_LIMIT,
    terms_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    delay: float = DEFAULT_DELAY_SECONDS,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Run the bounded audit and return its report. Writes nothing, anywhere."""
    detail_limit = validate_detail_limit(limit)
    owns_client = client is None
    active = client or httpx.Client(headers={"User-Agent": AUDIT_USER_AGENT})
    report: dict[str, Any] = {
        "audit_phase": "7C.4A",
        "user_agent": AUDIT_USER_AGENT,
        "detail_limit": detail_limit,
        "detail_sample_strategy": DETAIL_SAMPLE_STRATEGY,
        "authenticated": False,
        "browser_automation": False,
        "wrote_database": False,
        "wrote_raw_html": False,
        "activated_production_source": False,
        "lastmod_mapped_to_published_at": False,
    }
    try:
        # --- robots.txt, fetched once, and its status policy ---------------
        robots_row, robots_body = _fetch_document(active, ROBOTS_URL, timeout)
        if robots_body is None:
            report["robots"] = {"available": False, **robots_row}
            report["outcome"] = "ROBOTS_UNREACHABLE"
            report["exit_code"] = EXIT_BARRIER
            return report

        disposition = classify_robots_response(
            int(robots_row["status_code"]), str(robots_row["final_url"]), robots_body
        )
        enforced = disposition.disposition == ROBOTS_OBEY
        groups = parse_robots_txt(robots_body) if enforced else ()
        declarations = sitemap_declarations(robots_body) if enforced else ()
        robots_report: dict[str, Any] = {
            **robots_row,
            "available": enforced,
            "group_count": len(groups),
            "sitemap_declarations": list(declarations),
            **disposition.as_dict(),
        }
        if disposition.disposition == ROBOTS_ABSENT:
            robots_report["note"] = (
                "No robots.txt exists, so no explicit crawl rule applies. That "
                "is not permission of any kind; the site's terms remain a "
                "separate question for a human."
            )
        report["robots"] = robots_report

        if not disposition.may_proceed:
            report["terms"] = _audit_terms(
                active, None, timeout, groups=(), enforced=False
            )
            report["terms"]["barrier"] = "NOT_ATTEMPTED_ROBOTS_UNRESOLVED"
            report["outcome"] = (
                "ROBOTS_BARRIER"
                if disposition.disposition == ROBOTS_BARRIER
                else "ROBOTS_UNRESOLVED"
            )
            report["exit_code"] = EXIT_BARRIER
            return report

        # --- the public PFE listing: reachability evidence only -------------
        # Fetched once, never parsed for an offer list. Its structure is
        # recorded precisely so the report can state *why* the sitemap is the
        # discovery mechanism rather than this page.
        target_verdict = robots_verdict(groups, urlsplit(PFE_TARGET_URL).path)
        target_report: dict[str, Any] = {
            "url": PFE_TARGET_URL,
            "robots_verdict": target_verdict.as_dict(),
            "used_as_discovery_source": False,
            "note": (
                "Fetched once for reachability and structure only. The listing "
                "is a client-rendered application and is NOT parsed for offers; "
                "discovery uses the official sitemap chain instead."
            ),
        }
        if enforced and not target_verdict.allowed:
            target_report["barrier"] = "ROBOTS_DISALLOWED"
            report["pfe_listing"] = target_report
            report["outcome"] = "ROBOTS_DISALLOWED"
            report["exit_code"] = EXIT_ROBOTS_DISALLOWED
            return report
        listing_row, listing_body = _fetch_document(active, PFE_TARGET_URL, timeout)
        target_report.update(listing_row)
        if listing_body is not None and not listing_row.get("barrier"):
            summary = summarize_jsonld(listing_body)
            target_report["jsonld"] = summary.as_dict()
            target_report["exposes_job_posting_jsonld"] = (
                summary.job_posting_count > 0
            )
        report["pfe_listing"] = target_report

        # --- the official sitemap index, as robots declares it ---------------
        # The sitemap index is whatever robots.txt declares, and there is no
        # way to say otherwise. An override flag was removed deliberately: it
        # let an operator point the audit at an arbitrary URL, which turns "the
        # official discovery chain" into "whatever was typed" and makes the
        # audit a general-purpose fetcher. If robots declares no same-host
        # sitemap, the audit stops rather than guessing one.
        same_host = [item for item in declarations if is_stagiaires_host(item)]
        chosen = same_host[0] if same_host else None
        sitemap_report: dict[str, Any] = {
            "declared_by_robots": list(declarations),
            "same_host_declarations": same_host,
            "selected": chosen,
            "selected_from": "robots.txt",
        }
        report["sitemap_index"] = sitemap_report
        if chosen is None:
            # No declaration, and none is invented: a sitemap URL we guessed
            # would not be "the official sitemap" in any meaningful sense.
            sitemap_report["error"] = "NO_SAME_HOST_SITEMAP_DECLARED"
            report["outcome"] = "NO_SITEMAP_DECLARED"
            report["exit_code"] = EXIT_FAILURE
            return report
        if not _robots_allows(groups, enforced, chosen):
            sitemap_report["barrier"] = "ROBOTS_DISALLOWED"
            report["outcome"] = "ROBOTS_DISALLOWED"
            report["exit_code"] = EXIT_ROBOTS_DISALLOWED
            return report

        time.sleep(max(delay, 0.0))
        index_row, index_body = _fetch_document(active, chosen, timeout)
        sitemap_report.update(index_row)
        if index_body is None or index_row.get("barrier"):
            report["outcome"] = (
                "BARRIER" if index_row.get("barrier") else "TRANSPORT_FAILURE"
            )
            report["exit_code"] = (
                EXIT_BARRIER if index_row.get("barrier") else EXIT_FAILURE
            )
            return report
        try:
            index = parse_sitemap_index(index_body, chosen)
        except StagiairesAccessError as error:
            sitemap_report["error"] = f"SITEMAP_INDEX_UNPARSABLE: {error}"
            report["outcome"] = "SITEMAP_INDEX_UNPARSABLE"
            report["exit_code"] = EXIT_FAILURE
            return report
        sitemap_report.update(index.as_dict())

        # --- each declared offer sitemap, sequentially ----------------------
        offer_files: list[dict[str, Any]] = []
        parses = []
        for offer_url in index.offer_sitemaps[:MAX_OFFER_SITEMAPS]:
            file_report: dict[str, Any] = {"url": offer_url}
            if not _robots_allows(groups, enforced, offer_url):
                file_report["barrier"] = "ROBOTS_DISALLOWED"
                offer_files.append(file_report)
                continue
            time.sleep(max(delay, 0.0))
            row, body = _fetch_document(active, offer_url, timeout)
            file_report.update(row)
            if body is None or row.get("barrier"):
                offer_files.append(file_report)
                continue
            try:
                parse = parse_offer_sitemap(body, offer_url)
            except StagiairesAccessError as error:
                # A urlset we could not parse is a finding, not a zero: the
                # audit says so rather than reporting "0 offers found".
                file_report["error"] = f"URLSET_UNPARSABLE: {error}"
                offer_files.append(file_report)
                continue
            file_report.update(parse.as_dict())
            offer_files.append(file_report)
            parses.append(parse)
        sitemap_report["offer_sitemaps_read"] = offer_files
        sitemap_report["offer_sitemaps_skipped"] = max(
            len(index.offer_sitemaps) - MAX_OFFER_SITEMAPS, 0
        )

        audit = combine_offer_sitemaps(parses)
        report["offers"] = audit.as_dict()
        report["offers"]["source_external_id_note"] = (
            "The numeric ID in /stage-emploi-maroc/<id> is a CANDIDATE "
            "source_external_id observed in this audit. Stability over time is "
            "not established by a single audit and is not claimed here."
        )

        # --- at most three real offer pages ---------------------------------
        sample = select_detail_sample(audit.entries, detail_limit)
        observations: list[DetailObservation] = []
        for entry in sample:
            observation = DetailObservation(
                source_external_id=entry.source_external_id,
                canonical_url=entry.canonical_url,
                sitemap_lastmod=entry.sitemap_lastmod,
            )
            if not _robots_allows(groups, enforced, entry.canonical_url):
                observation.barrier = "ROBOTS_DISALLOWED"
                observations.append(observation)
                continue
            time.sleep(max(delay, 0.0))
            row, body = _fetch_document(active, entry.canonical_url, timeout)
            observation.status_code = row.get("status_code")
            observation.final_url = row.get("final_url")
            observation.content_type = row.get("content_type")
            observation.body_bytes = int(row.get("body_bytes", 0))
            observation.barrier = row.get("barrier")
            observation.error = row.get("error")
            if body is not None and not observation.barrier:
                described = describe_detail_page(
                    body, observation.final_url or entry.canonical_url
                )
                observation.has_next_data = bool(described["has_next_data_script"])
                observation.jsonld = dict(described["jsonld"])
                observation.signals = dict(described["signals"])
            observations.append(observation)

        report["detail_audit"] = {
            "requested": detail_limit,
            "sampled": len(observations),
            "strategy": DETAIL_SAMPLE_STRATEGY,
            "pages": [item.as_dict() for item in observations],
            "signal_support": signal_support(observations),
            "note": (
                "Structural feasibility only. A signal reported as not found is "
                "not published through any standard mechanism on the sampled "
                "pages; no site-specific selector was invented to fill it, and "
                "sitemap <lastmod> was never used as a publication date."
            ),
        }

        # --- optional, operator-supplied terms check -------------------------
        report["terms"] = _audit_terms(
            active, terms_url, timeout, groups=groups, enforced=enforced
        )

        barriers = sorted(
            {
                str(item)
                for item in [
                    target_report.get("barrier"),
                    sitemap_report.get("barrier"),
                    *[file.get("barrier") for file in offer_files],
                    *[item.barrier for item in observations],
                ]
                if item
            }
        )
        report["barriers"] = barriers
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
            limit=args.limit,
            terms_url=args.terms_url,
            timeout=args.timeout,
            delay=args.delay,
        )
    except StagiairesAccessError as error:
        print(f"stagiaires access audit error: {error}", file=sys.stderr)
        raise SystemExit(EXIT_FAILURE) from error
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(int(report.get("exit_code", EXIT_FAILURE)))


if __name__ == "__main__":
    main()
