"""Phase 7C.5A — bounded, GET-only audit of Stage.ma's public discovery.

Stage.ma supplied both seed rows of the gold benchmark, which makes it a source
we already have evidence *about* — and evidence about two postings in the past
says nothing about whether the site can be collected today. This command asks
the question a collector would have to answer first, and is allowed to answer it
with "no".

    python -m evaluation.morocco_pfe.cli.stage_ma_access_audit
    python -m evaluation.morocco_pfe.cli.stage_ma_access_audit --limit 3
    python -m evaluation.morocco_pfe.cli.stage_ma_access_audit \
        --terms-url https://www.stage.ma/<a real public terms path>

It is the only file in this slice that opens a socket. There is deliberately no
flag that names a discovery URL: the surfaces it inspects are a fixed, bounded
set, so no invocation can turn it into a general-purpose fetcher. `--terms-url`
is the single operator-supplied URL, is same-host only, is fetched at most once
for reachability metadata, and never becomes a discovery source.

What it does, and refuses to do:

* **GET only**, sequential, one page at a time, with a delay and an explicit
  timeout. Detail sampling is hard bounded at five pages and defaults to three;
* it fetches `robots.txt` first and **obeys it**, matching on path *and* query.
  200 is parsed and obeyed; 404/410 means the file is absent so no explicit rule
  applies — a statement about robots.txt alone and **not** permission of any
  kind; 401/403/407/429 means we were refused it, and an unknown rule is never
  read as permissive, so the audit stops; 5xx or an unreadable response leaves
  the audit incomplete rather than permitted;
* it **never follows a redirect off the Stage.ma hosts, and never into a path
  robots disallows.** Redirects are resolved here rather than by the HTTP
  client, bounded, and every hop is re-checked against both rules before the
  next GET;
* it **writes nothing**: no SQLite, no benchmark row, no raw HTML on disk. One
  JSON document goes to stdout;
* it prints **structure, not content**: statuses, counts, link shapes, JSON-LD
  type and key *names*, per-field presence with a short bounded excerpt.
  Descriptions are reported by length only, and no page body is emitted;
* it separates **audit execution** from **source feasibility**. An audit that
  ran perfectly and found no usable discovery path is `COMPLETED` with a
  feasibility of `INSUFFICIENT_DISCOVERY` — a real answer, not a failure;
* a date the site displays is a **candidate**, never a publication timestamp,
  and `01/01/1970` and its relatives are flagged as sentinels rather than
  proposed as real. Nothing is fabricated to replace one;
* the two committed benchmark URLs may be fetched as **structure canaries** when
  nothing was discovered live. They are labelled `BENCHMARK_CANARY` and can
  never count as evidence that discovery works.

Nothing here drives a browser, impersonates one, executes JavaScript,
authenticates, sends a cookie, rotates a proxy, handles a CAPTCHA, POSTs
anything, submits an application, or calls a private API. An off-domain
application URL may be recorded as a string; it is never fetched.

Exit codes: `0` COMPLETED, `1` usage failure or INCOMPLETE, `2` an access
barrier, `3` robots.txt disallows a required target.

Reading this report does not establish that automated collection of this site is
permitted, and it activates nothing. Stage.ma has no collector, no
`config/sources.yaml` row and no `SourceConfig` type, and this command creates
none of them.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import sys
import time
from typing import Any, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

from evaluation.morocco_pfe.stage_ma_access import (
    AUDIT_USER_AGENT,
    AUDIT_VERSION,
    BENCHMARK_CANARY_URLS,
    DEFAULT_DETAIL_LIMIT,
    DETAIL_SELECTION_STRATEGY,
    DISCOVERY_CANARY,
    DISCOVERY_LIVE,
    MAX_DETAIL_LIMIT,
    ROBOTS_ABSENT,
    ROBOTS_BARRIER,
    ROBOTS_OBEY,
    ROBOTS_URL,
    SURFACE_URLS,
    TARGET_HOST,
    TERMS_NOT_ATTEMPTED,
    TERMS_REVIEW_NOTE,
    StageMaAccessError,
    application_evidence,
    canonical_url,
    ISSUE_BARRIER,
    ISSUE_UNAVAILABLE,
    ISSUE_UNREADABLE,
    classify_response,
    classify_robots_response,
    collect_surface_links,
    derive_feasibility,
    detail_field_evidence,
    detect_publication_state,
    is_stage_ma_host,
    looks_browser_rendered,
    parse_robots_txt,
    parse_sitemap_document,
    publication_date_candidate,
    robots_verdict,
    select_detail_targets,
    sitemap_declarations,
    summarize_structured_data,
    surface_identity,
    validate_detail_limit,
)

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_DELAY_SECONDS = 1.0

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3

#: A bound on declared sitemap documents. The audit reads what robots declares,
#: not a sitemap tree it walks until it runs out.
MAX_SITEMAPS = 3

OFF_DOMAIN_REDIRECT = "OFF_DOMAIN_REDIRECT_NOT_FOLLOWED"
ROBOTS_DISALLOWED_REDIRECT = "ROBOTS_DISALLOWED_REDIRECT"
_REFUSED_REDIRECTS = frozenset({OFF_DOMAIN_REDIRECT, ROBOTS_DISALLOWED_REDIRECT})

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_BARRIER = 2
EXIT_ROBOTS_DISALLOWED = 3

OUTCOME_COMPLETED = "COMPLETED"
OUTCOME_INCOMPLETE = "INCOMPLETE"
OUTCOME_BARRIER = "BARRIER"
OUTCOME_ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"


def _finalize(
    report: dict[str, Any],
    *,
    barriers: list[str],
    incomplete_reasons: list[str],
    robots_disallowed: list[str] | None = None,
) -> dict[str, Any]:
    """Set the one execution outcome/exit pair, by a single fixed precedence.

    `BARRIER > ROBOTS_DISALLOWED > INCOMPLETE > COMPLETED`. Being refused
    outranks being forbidden, because a wall tells us the site is turning us
    away; being forbidden outranks not knowing, because robots is a rule we
    chose to obey and the run genuinely did not audit what it set out to.

    Execution status is **not** the feasibility verdict, and this is where the
    two are kept apart: an audit that ran cleanly and found nothing usable ends
    `COMPLETED` / exit 0 with a feasibility of `INSUFFICIENT_DISCOVERY`. What
    fails a run is being refused, being forbidden, or being unable to read
    something required — never the answer being disappointing.
    """
    report["barriers"] = sorted(set(barriers))
    report["robots_disallowed_targets"] = sorted(set(robots_disallowed or []))
    report["incomplete_reasons"] = sorted(set(incomplete_reasons))
    if report["barriers"]:
        report["outcome"] = OUTCOME_BARRIER
        report["exit_code"] = EXIT_BARRIER
    elif report["robots_disallowed_targets"]:
        report["outcome"] = OUTCOME_ROBOTS_DISALLOWED
        report["exit_code"] = EXIT_ROBOTS_DISALLOWED
    elif report["incomplete_reasons"]:
        report["outcome"] = OUTCOME_INCOMPLETE
        report["exit_code"] = EXIT_FAILURE
    else:
        report["outcome"] = OUTCOME_COMPLETED
        report["exit_code"] = EXIT_OK
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Stage.ma public access and discovery structure, GET-only. "
            "Obeys robots.txt, never authenticates, writes nothing."
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
            "a real public Stage.ma terms/legal URL to check for reachability "
            "only. No URL is guessed: omit this and the check is reported as "
            "not attempted."
        ),
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    return parser


def _get(
    client: httpx.Client, url: str, timeout: float
) -> tuple[httpx.Response | None, str | None]:
    """One polite GET that does **not** follow redirects on its own.

    `follow_redirects=False` is the network boundary, not a detail: a client
    told to follow them would take a same-host URL's `302 Location:
    https://elsewhere/…` and issue a GET to a host we never chose.
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


def _follow_same_host(
    client: httpx.Client,
    url: str,
    timeout: float,
    *,
    allows: Callable[[str], bool] | None = None,
) -> tuple[httpx.Response | None, str | None, list[str], str | None]:
    """GET ``url``, resolving only redirects the audit is permitted to follow.

    Two rules gate every hop, checked on the URL the site hands us rather than
    only on the one we asked for: an off-domain `Location` is refused, and so is
    a same-host one that ``allows`` rejects. Obeying robots on the requested URL
    alone is not obeying robots. ``allows`` is `None` only for robots.txt itself,
    where there are no rules to consult yet.
    """
    chain: list[str] = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        response, error = _get(client, current, timeout)
        if response is None:
            return None, error, chain, None
        if response.status_code not in _REDIRECT_STATUSES:
            return response, None, chain, None
        location = (response.headers.get("location") or "").strip()
        if not location:
            return response, None, chain, None
        target = urljoin(current, location)
        chain.append(target)
        if not is_stage_ma_host(target):
            return response, None, chain, OFF_DOMAIN_REDIRECT
        if allows is not None and not allows(target):
            return response, None, chain, ROBOTS_DISALLOWED_REDIRECT
        current = target
    return None, None, chain, "TOO_MANY_REDIRECTS"


def _fetch(
    client: httpx.Client,
    url: str,
    timeout: float,
    *,
    allows: Callable[[str], bool] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """GET one URL once and describe the response without keeping its body.

    The body is handed back to be *analysed*, never stored: no path writes it to
    disk, puts it in the report, or logs it. A refused redirect yields no body.
    """
    row: dict[str, Any] = {"requested_url": url}
    response, error, chain, barrier = _follow_same_host(
        client, url, timeout, allows=allows
    )
    if chain:
        row["redirect_chain"] = list(chain)
    if response is None:
        row["error"] = error
        row["issue"] = {"kind": ISSUE_UNREADABLE, "code": error or "UNREADABLE"}
        if barrier:
            row["barrier"] = barrier
            row["issue"] = {"kind": ISSUE_BARRIER, "code": barrier}
        return row, None
    body = response.text
    row["status_code"] = response.status_code
    row["final_url"] = str(response.url)
    row["content_type"] = response.headers.get("content-type")
    row["body_bytes"] = len(response.content)
    if barrier in _REFUSED_REDIRECTS:
        row["barrier"] = barrier
        row["redirect_target"] = chain[-1]
        row["redirect_target_host"] = urlsplit(chain[-1]).hostname
        return row, None
    if barrier:
        row["barrier"] = barrier
        row["issue"] = {"kind": ISSUE_BARRIER, "code": barrier}
        return row, body
    issue = classify_response(response.status_code, row["final_url"], body)
    if issue is not None:
        # The *kind* travels with the row so the caller can judge by context: a
        # 404 means something different on a discovery surface, on a declared
        # sitemap, and on an offer page, and only the caller knows which it has.
        row["issue"] = issue.as_dict()
        if issue.kind == ISSUE_BARRIER:
            row["barrier"] = issue.code
        else:
            row["unavailable" if issue.kind == ISSUE_UNAVAILABLE else "error"] = (
                issue.code
            )
        if issue.kind != ISSUE_UNAVAILABLE:
            return row, None
    return row, body


def _audit_terms(
    client: httpx.Client,
    terms_url: str | None,
    timeout: float,
    *,
    allows: Callable[[str], bool],
) -> dict[str, Any]:
    """Check one operator-supplied terms URL for reachability only.

    Never decides anything and never reads the text. `manual_review_required` is
    unconditionally true: this audit does not and cannot determine whether
    automated collection is permitted, and neither a 200 nor a 404 changes that.
    """
    report: dict[str, Any] = {
        "url": terms_url,
        "status_code": None,
        "final_url": None,
        "available": False,
        "attempted": False,
        "barrier": None,
        "error": None,
        "manual_review_required": True,
        "note": TERMS_REVIEW_NOTE,
    }
    if not terms_url:
        report["barrier"] = TERMS_NOT_ATTEMPTED
        return report
    if not is_stage_ma_host(terms_url):
        report["barrier"] = "NOT_ATTEMPTED_OFF_DOMAIN"
        return report
    if not allows(terms_url):
        report["barrier"] = "ROBOTS_DISALLOWED"
        return report
    report["attempted"] = True
    row, _ = _fetch(client, terms_url, timeout, allows=allows)
    report["status_code"] = row.get("status_code")
    report["final_url"] = row.get("final_url")
    report["barrier"] = row.get("barrier")
    report["error"] = row.get("error")
    report["available"] = (
        row.get("status_code") == 200 and not report["barrier"] and not report["error"]
    )
    return report


def _describe_detail(html: str, url: str, discovery: str) -> dict[str, Any]:
    """Summarize one offer page structurally, quoting nothing of substance."""
    state = detect_publication_state(html)
    date_candidate = publication_date_candidate(html)
    fields = detail_field_evidence(html)
    return {
        "url": url,
        # LIVE_DISCOVERED or BENCHMARK_CANARY. A canary proves a page parses; it
        # never proves that current discovery found anything.
        "discovery": discovery,
        "publication_state": state.as_dict(),
        "publication_date_candidate": date_candidate.as_dict(),
        "fields": {name: item.as_dict() for name, item in fields.items()},
        "application": application_evidence(html, url).as_dict(),
        "structured_data": summarize_structured_data(html),
        "has_required_title_and_organization": bool(
            fields["title"].found and fields["organization"].found
        ),
    }


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
        "audit": "7C.5A",
        "audit_version": AUDIT_VERSION,
        "target_host": TARGET_HOST,
        "user_agent": AUDIT_USER_AGENT,
        "detail_limit": detail_limit,
        "detail_selection_strategy": DETAIL_SELECTION_STRATEGY,
        "manual_review_required": True,
        "authenticated": False,
        "browser_automation": False,
        "javascript_executed": False,
        "wrote_database": False,
        "wrote_raw_html": False,
        "activated_production_source": False,
        "displayed_date_mapped_to_published_at": False,
    }
    barriers: list[str] = []
    incomplete: list[str] = []
    robots_disallowed: list[str] = []
    try:
        # --- robots.txt, fetched once, and its status policy ---------------
        robots_row, robots_body = _fetch(active, ROBOTS_URL, timeout)
        if robots_row.get("status_code") is None:
            # We never reached the file at all — a timeout or a transport
            # failure. That is not a refusal and not permission: we simply do
            # not know what robots says, so the audit is incomplete.
            report["robots"] = {"available": False, **robots_row}
            report["feasibility"] = "UNKNOWN"
            report["terms_review"] = _audit_terms(
                active, None, timeout, allows=lambda _: True
            )
            return _finalize(
                report,
                barriers=[],
                incomplete_reasons=[f"ROBOTS_UNREACHABLE:{robots_row.get('error')}"],
            )

        # Classified from the status even when the body was withheld: a response
        # that refused us has a wall page for a body, and there is nothing in it
        # worth reading.
        disposition = classify_robots_response(
            int(robots_row["status_code"]),
            str(robots_row.get("final_url") or ROBOTS_URL),
            robots_body or "",
        )
        enforced = disposition.disposition == ROBOTS_OBEY
        groups = parse_robots_txt(robots_body or "") if enforced else ()
        declarations = sitemap_declarations(robots_body or "") if enforced else ()
        report["robots"] = {
            **robots_row,
            "available": enforced,
            "group_count": len(groups),
            "sitemap_declarations": list(declarations),
            **disposition.as_dict(),
            "note": (
                "An absent robots.txt means no explicit crawl rule applies. That "
                "is not permission of any kind; the site's terms remain a "
                "separate question for a human."
            ),
        }
        if not disposition.may_proceed:
            report["feasibility"] = "ACCESS_BLOCKED" if (
                disposition.disposition == ROBOTS_BARRIER
            ) else "UNKNOWN"
            report["terms_review"] = _audit_terms(
                active, None, timeout, allows=lambda _: True
            )
            report["terms_review"]["barrier"] = "NOT_ATTEMPTED_ROBOTS_UNRESOLVED"
            return _finalize(
                report,
                barriers=[str(disposition.barrier)]
                if disposition.disposition == ROBOTS_BARRIER
                else [],
                incomplete_reasons=[f"ROBOTS_{disposition.disposition}"]
                if disposition.disposition != ROBOTS_BARRIER
                else [],
            )

        def allows(candidate: str) -> bool:
            return (not enforced) or robots_verdict(groups, candidate).allowed

        # --- the bounded set of public discovery surfaces -------------------
        surfaces: list[dict[str, Any]] = []
        discovered: list[str] = []
        surfaces_with_offers = 0
        for surface_url in SURFACE_URLS:
            entry: dict[str, Any] = {"url": surface_url}
            verdict = robots_verdict(groups, surface_url)
            entry["robots_verdict"] = verdict.as_dict()
            if enforced and not verdict.allowed:
                # A required audit target the site forbids: recorded, never
                # fetched, and surfaced at the top level rather than skipped.
                entry["barrier"] = "ROBOTS_DISALLOWED"
                robots_disallowed.append(f"surface:{surface_url}")
                surfaces.append(entry)
                continue
            time.sleep(max(delay, 0.0))
            row, body = _fetch(active, surface_url, timeout, allows=allows)
            entry.update(row)
            kind = (row.get("issue") or {}).get("kind")
            if kind == ISSUE_BARRIER:
                barriers.append(str(row.get("barrier")))
                surfaces.append(entry)
                continue
            if kind == ISSUE_UNAVAILABLE:
                # A discovery surface that is gone is a fact about the site's
                # structure, not about our access, and the audit can still
                # complete on the surfaces that remain.
                entry["page_state"] = "PAGE_UNAVAILABLE"
                surfaces.append(entry)
                continue
            if body is None or kind == ISSUE_UNREADABLE:
                incomplete.append(f"SURFACE_UNREADABLE:{surface_url}")
                surfaces.append(entry)
                continue
            links = collect_surface_links(body, row.get("final_url") or surface_url)
            collector_meta = looks_browser_rendered(body, links)
            entry.update(links.as_dict())
            entry["rendering"] = collector_meta
            entry.update(surface_identity(body))
            entry["structured_data"] = summarize_structured_data(body)
            surfaces.append(entry)
            if links.offer_urls:
                surfaces_with_offers += 1
                discovered.extend(links.offer_urls)
        report["surfaces"] = surfaces

        # --- sitemap, only where robots declares one ------------------------
        report["sitemap"] = _audit_sitemaps(
            active,
            declarations,
            timeout,
            delay,
            allows=allows,
            enforced=enforced,
            barriers=barriers,
            incomplete=incomplete,
            robots_disallowed=robots_disallowed,
        )
        sitemap_offers = report["sitemap"]["offer_detail_urls_total"]
        discovered.extend(report["sitemap"]["offer_url_pool"])

        # --- the bounded detail sample --------------------------------------
        live_targets = select_detail_targets(tuple(discovered), detail_limit)
        sampled: list[dict[str, Any]] = []
        used_canaries = False
        targets: list[tuple[str, str]] = [
            (url, DISCOVERY_LIVE) for url in live_targets
        ]
        if not targets:
            # Nothing was discovered. The committed benchmark URLs can still say
            # whether a detail page parses, but they are canaries and are
            # labelled as such: they discovered nothing and prove nothing about
            # discovery.
            used_canaries = True
            targets = [
                (url, DISCOVERY_CANARY) for url in BENCHMARK_CANARY_URLS[:detail_limit]
            ]
        for url, discovery in targets:
            entry: dict[str, Any] = {"url": url, "discovery": discovery}
            if enforced and not robots_verdict(groups, url).allowed:
                entry["barrier"] = "ROBOTS_DISALLOWED"
                if discovery == DISCOVERY_LIVE:
                    # A live-discovered page is part of the bounded detail audit
                    # this run set out to do; a canary is optional fallback
                    # evidence and never decides the run's outcome on its own.
                    robots_disallowed.append(f"detail:{url}")
                sampled.append(entry)
                continue
            time.sleep(max(delay, 0.0))
            row, body = _fetch(active, url, timeout, allows=allows)
            entry.update(row)
            kind = (row.get("issue") or {}).get("kind")
            if kind == ISSUE_BARRIER:
                barriers.append(str(row.get("barrier")))
                sampled.append(entry)
                continue
            if kind == ISSUE_UNAVAILABLE:
                # An offer that has been taken down is a lifecycle observation
                # about the source, not a refusal, and never fails the audit.
                entry["page_state"] = "PAGE_UNAVAILABLE"
                sampled.append(entry)
                continue
            if body is None or kind == ISSUE_UNREADABLE:
                if discovery == DISCOVERY_LIVE:
                    incomplete.append(f"DETAIL_UNREADABLE:{url}")
                sampled.append(entry)
                continue
            entry.update(_describe_detail(body, url, discovery))
            sampled.append(entry)

        live_pages = [item for item in sampled if item.get("discovery") == DISCOVERY_LIVE]
        report["discovery"] = {
            "live_offer_urls_found": len(set(discovered)),
            "surfaces_exposing_offer_urls": surfaces_with_offers,
            "sitemap_offer_urls": sitemap_offers,
            "used_benchmark_canaries": used_canaries,
            "canary_note": (
                "BENCHMARK_CANARY pages are the two committed gold-benchmark "
                "URLs. They are fetched only when nothing was discovered live, "
                "and never count as evidence that discovery works."
            ),
        }
        report["detail_sample"] = {
            "requested": detail_limit,
            "sampled": len(sampled),
            "live_discovered": len(live_pages),
            "benchmark_canaries": len(sampled) - len(live_pages),
            "strategy": DETAIL_SELECTION_STRATEGY,
            "pages": sampled,
        }
        report["date_anomalies"] = _date_anomalies(sampled)
        report["terms_review"] = _audit_terms(
            active, terms_url, timeout, allows=allows
        )
        if report["terms_review"].get("attempted") and report["terms_review"].get(
            "barrier"
        ):
            barriers.append(str(report["terms_review"]["barrier"]))

        report["feasibility"] = derive_feasibility(
            access_blocked=bool(barriers),
            surfaces_with_offers=surfaces_with_offers,
            sitemap_offer_urls=sitemap_offers,
            live_detail_urls=len(set(discovered)),
            sampled_live_pages=len(live_pages),
            pages_with_title_and_organization=sum(
                1 for item in live_pages if item.get("has_required_title_and_organization")
            ),
        )
        report["feasibility_note"] = (
            "Evidence for the Architect, not a Phase 7C.5B decision. A completed "
            "audit may legitimately report that this source is not usable."
        )
        return _finalize(
            report,
            barriers=barriers,
            incomplete_reasons=incomplete,
            robots_disallowed=robots_disallowed,
        )
    finally:
        if owns_client:
            active.close()


def _audit_sitemaps(
    client: httpx.Client,
    declarations: tuple[str, ...],
    timeout: float,
    delay: float,
    *,
    allows: Callable[[str], bool],
    enforced: bool,
    barriers: list[str],
    incomplete: list[str],
    robots_disallowed: list[str],
) -> dict[str, Any]:
    """Walk the official sitemap chain within one global document budget.

    Two rules make this honest rather than merely bounded.

    **No filename is guessed.** Traversal starts only at `Sitemap:` URLs robots
    itself declares, and continues only into children an official index we
    already read told us about. Treating a lucky 200 on a name we invented as a
    discovery contract is the fabrication this layer exists to prevent, so
    "robots declared no sitemap" is recorded as the finding it is.

    **The budget is a refusal, not a truncation.** `MAX_SITEMAPS` is a global
    budget across roots *and* nested documents. When robots declares more roots
    than that, or when an index reveals more children than the remaining budget
    can honestly inspect, the audit stops and reports itself incomplete rather
    than reading the first few and presenting their offer URLs as the site's.
    Reading three of nine sitemaps and calling the result a sitemap audit is how
    a partial read comes to look complete.
    """
    same_host = [item for item in declarations if is_stage_ma_host(item)]
    seen: set[str] = set()
    roots: list[str] = []
    for item in same_host:
        try:
            resolved = canonical_url(item)
        except StageMaAccessError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)

    report: dict[str, Any] = {
        "declared_by_robots": list(declarations),
        "same_host_declarations": same_host,
        "same_host_root_documents": len(roots),
        "off_domain_declarations_ignored": len(declarations) - len(same_host),
        "guessed_any_url": False,
        "document_budget": MAX_SITEMAPS,
        "documents_read": 0,
        "documents": [],
        "complete": True,
        "offer_detail_urls_total": 0,
        "offer_url_pool": [],
    }
    if not roots:
        report["note"] = (
            "robots.txt declares no same-host sitemap. No filename was guessed, "
            "so this audit has no official sitemap to read for this source."
        )
        return report

    if len(roots) > MAX_SITEMAPS:
        # Refused before a single document is fetched: any subset we read would
        # be a partial view of the site's sitemap coverage, and no
        # sitemap-based feasibility claim may rest on it.
        report["complete"] = False
        report["note"] = (
            f"robots.txt declares {len(roots)} same-host sitemap documents, above "
            f"the audit's global budget of {MAX_SITEMAPS}. No sitemap was fetched: "
            "a truncated subset would misrepresent the site's sitemap coverage."
        )
        incomplete.append("SITEMAP_DECLARATION_COUNT_EXCEEDS_BOUND")
        return report

    pool: list[str] = []
    queue: list[str] = list(roots)
    budget = MAX_SITEMAPS
    while queue and budget > 0:
        sitemap_url = queue.pop(0)
        entry: dict[str, Any] = {"url": sitemap_url}
        if enforced and not allows(sitemap_url):
            # A document the official chain requires and robots forbids: recorded
            # and never fetched, and the run says so at the top level.
            entry["barrier"] = "ROBOTS_DISALLOWED"
            robots_disallowed.append(f"sitemap:{sitemap_url}")
            report["complete"] = False
            report["documents"].append(entry)
            continue
        time.sleep(max(delay, 0.0))
        row, body = _fetch(client, sitemap_url, timeout, allows=allows)
        entry.update(row)
        budget -= 1
        report["documents_read"] += 1
        kind = (row.get("issue") or {}).get("kind")
        if body is None or kind is not None:
            report["complete"] = False
            if kind == ISSUE_BARRIER:
                barriers.append(str(row.get("barrier")))
            else:
                # Includes 404/410: a declared sitemap that is gone is still a
                # required document we could not read, which is different from
                # an offer page that has simply expired.
                incomplete.append(f"SITEMAP_UNREADABLE:{sitemap_url}")
            report["documents"].append(entry)
            continue
        try:
            parsed = parse_sitemap_document(body, sitemap_url)
        except StageMaAccessError as error:
            entry["error"] = f"SITEMAP_UNPARSABLE: {error}"
            incomplete.append(f"SITEMAP_UNPARSABLE:{sitemap_url}")
            report["complete"] = False
            report["documents"].append(entry)
            continue
        entry.update(parsed.as_dict())
        report["documents"].append(entry)
        pool.extend(parsed.offer_urls)

        children = [url for url in parsed.nested_sitemaps if url not in seen]
        if children:
            entry["nested_children_declared"] = len(children)
            if len(children) > budget:
                # The index points at more of the site than we may read. Stop
                # here: inspecting the first few and reporting their URLs would
                # present a fraction of the site's sitemap coverage as all of it.
                report["complete"] = False
                report["note"] = (
                    f"a sitemap index at {sitemap_url} declares {len(children)} "
                    f"same-host child documents, more than the {budget} remaining "
                    "in the audit's global budget; traversal stopped rather than "
                    "inspecting a subset."
                )
                incomplete.append(
                    f"SITEMAP_INDEX_CHILDREN_EXCEED_BOUND:{sitemap_url}"
                )
                break
            seen.update(children)
            queue.extend(children)

    report["unread_queued_documents"] = len(queue)
    if queue:
        report["complete"] = False
    report["offer_detail_urls_total"] = len(set(pool))
    report["offer_url_pool"] = sorted(set(pool))
    if not report["complete"]:
        report["coverage_note"] = (
            "PARTIAL: this audit did not read every sitemap document the "
            "official chain declares, so these URLs are a lower bound and no "
            "sitemap-based feasibility claim rests on them."
        )
    return report


def _date_anomalies(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Count how the sampled pages represented their dates.

    The headline number is `sentinel_or_invalid`: a site that renders unset date
    columns as `01/01/1970` will hand a careless collector a 1970 timestamp for
    a 2026 internship, and this is where that shows up before anybody writes one.
    """
    counts: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    for page in pages:
        candidate = page.get("publication_date_candidate")
        if not candidate:
            continue
        status = str(candidate.get("status"))
        counts[status] = counts.get(status, 0) + 1
        if status == "SENTINEL_OR_INVALID_DATE":
            examples.append({"url": page.get("url"), "raw": candidate.get("raw")})
    return {
        "by_status": dict(sorted(counts.items())),
        "sentinel_or_invalid": counts.get("SENTINEL_OR_INVALID_DATE", 0),
        "sentinel_examples": examples[:5],
        "note": (
            "A displayed date is a CANDIDATE for a later phase to consider. This "
            "audit maps nothing to published_at, and never substitutes a crawl "
            "time, a sitemap timestamp or anything derived from the numeric URL "
            "id for a missing one."
        ),
    }


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        report = run(
            limit=args.limit,
            terms_url=args.terms_url,
            timeout=args.timeout,
            delay=args.delay,
        )
    except StageMaAccessError as error:
        print(f"stage.ma access audit error: {error}", file=sys.stderr)
        raise SystemExit(EXIT_FAILURE) from error
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(int(report.get("exit_code", EXIT_FAILURE)))


if __name__ == "__main__":
    main()
