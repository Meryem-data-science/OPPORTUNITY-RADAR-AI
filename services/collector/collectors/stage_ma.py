"""Collector for public Stage.ma offers, from the audited specialty surface.

Phase 7C.5A audited this site and found exactly one browser-free discovery
surface. This collector reads that one surface and nothing else:

    robots.txt -> /specialites/computer-science
               -> /offres-stage/<numeric-id>-<slug> -> JobPosting JSON-LD

**Coverage is deliberately partial.** The audit reached the homepage and the
generic `/offres-stage` listing and found zero offer links in either's ordinary
server HTML; the Informatique specialty page carried ten. So this is the
Stage.ma *Informatique* specialty and not Stage.ma, and nothing here claims
otherwise. No sitemap is fetched — robots declares none, and guessing one is not
discovery. No other specialty is crawled, no "Afficher tout" is followed, and no
pagination is discovered: each of those would need its own audit.

Everything about how it behaves on the network is a constraint, not a default:
GET-only, sequential, one request at a time with a delay, an explicit timeout,
no retries, no concurrency; same-host throughout; redirects resolved here rather
than by the HTTP client, bounded, with every hop re-checked against robots on
path *and* query before the next request. No browser, no impersonation, no
JavaScript execution, no Selenium or Playwright, no authentication, no cookies,
no proxy, no CAPTCHA handling, no POST, no application submission, no private
API. An off-domain application URL may be *recorded*; it is never fetched.

The distinction this collector turns on is **skip versus fail**, because the
source publishes anonymous offers on purpose:

* an offer that expired, was unpublished, vanished between listing and detail,
  or whose employer is deliberately anonymous is **skipped, one at a time** —
  these are the source's ordinary content states, not faults;
* a robots refusal, an unreachable or ambiguous listing, a selected page with no
  `JobPosting`, or an offer with no usable title is a **structural failure** and
  raises, so the RadarAgent records the run as FAILED.

A failed read never becomes an empty batch — but an empty batch is not by itself
a failure. A successful run may legitimately return zero candidates two ways:

1. the listing itself declares an explicit empty state, or
2. the listing is structurally sound and exposes offers, and every selected one
   is individually rejected for an ordinary per-item reason — `EXPIRED`,
   `UNPUBLISHED`, `DETAIL_GONE_AFTER_DISCOVERY`, or an unavailable or
   deliberately anonymous organization.

The second is not hypothetical: the real Phase 7C.5B validation run read ten
live offers off the approved listing and every one of them was expired, so the
batch was empty and the run was a genuine success.

What stays a **structural failure** is a listing exposing zero offer links with
no explicit empty-state evidence — a template change is never "no opportunities
today" — and every robots refusal, network fault and parser structural failure,
none of which may ever be reported as an empty success.

It observes and normalizes; it does not judge. There is no Data & AI filter and
no PFE keyword filter here — the existing qualification layer classifies, and
later ranking prioritizes. Mixing those into collection would make the store a
record of what we were interested in on the day we collected, rather than of
what the source published.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from services.collector.collectors.base import BaseCollector
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.stage_ma import (
    COLLECTOR_USER_AGENT,
    LISTING_URL,
    PARSER_VERSION,
    ROBOTS_ABSENT,
    ROBOTS_OBEY,
    ROBOTS_URL,
    STATE_EXPIRED,
    STATE_UNPUBLISHED,
    RobotsGroup,
    StageMaCollectionError,
    StageMaListingEntry,
    StageMaPayloadError,
    access_barrier,
    application_url,
    classify_robots_response,
    has_explicit_empty_state,
    is_stage_ma_host,
    is_usable_organization,
    job_posting,
    normalize_text,
    parse_listing,
    parse_robots_txt,
    posting_description,
    posting_location,
    posting_organization,
    posting_published_at,
    posting_title,
    publication_state,
    robots_allows,
    select_listing_targets,
)
from services.collector.sources import SourceConfig

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Why one offer was left out. These are the source's ordinary content states,
#: recorded so a run can explain a small batch without anyone guessing.
SKIP_GONE = "DETAIL_GONE_AFTER_DISCOVERY"
SKIP_EXPIRED = "EXPIRED"
SKIP_UNPUBLISHED = "UNPUBLISHED"
SKIP_ANONYMOUS = "ORGANIZATION_ANONYMOUS_OR_UNAVAILABLE"


class StageMaCollector(BaseCollector):
    """Read public offers from the audited Stage.ma Informatique specialty page."""

    TIMEOUT_SECONDS = 20.0
    DELAY_SECONDS = 1.0
    MAX_REDIRECTS = 3

    def __init__(
        self, source: SourceConfig, client: httpx.Client | None = None
    ) -> None:
        if source.type != "stage_ma_html":
            raise ValueError("StageMaCollector requires a stage_ma_html source")
        if source.detail_page_limit is None:
            raise ValueError(f"source {source.id!r} must configure detail_page_limit")
        self.source = source
        self.source_id = source.id
        self.detail_page_limit = source.detail_page_limit
        self._client = client
        self._robots: tuple[RobotsGroup, ...] = ()
        self._robots_enforced = False
        self._pages_checked = 0
        #: Skip reasons for this run, keyed by canonical URL. Private state for
        #: logs and tests: `source_runs` has no column for it, and this slice
        #: does not change that schema.
        self.skipped: dict[str, str] = {}

    # -- metrics ---------------------------------------------------------
    def run_metrics(self) -> dict[str, Any]:
        """Report only what this run actually measured.

        `items_found` and `new_items` are the agent's to count, and
        `relevant_items` and `http_status` are omitted rather than guessed: a
        collector that fetches many pages has no single status, and relevance is
        the qualification layer's judgement, not collection's.
        """
        return {"pages_checked": self._pages_checked, "parser_version": PARSER_VERSION}

    # -- network ---------------------------------------------------------
    def _get_once(self, url: str) -> httpx.Response:
        """One polite GET that does not follow redirects on its own."""
        try:
            if self._client is not None:
                response = self._client.get(
                    url, timeout=self.TIMEOUT_SECONDS, follow_redirects=False
                )
            else:
                response = httpx.get(
                    url,
                    timeout=self.TIMEOUT_SECONDS,
                    follow_redirects=False,
                    headers={"User-Agent": COLLECTOR_USER_AGENT},
                )
        except httpx.TimeoutException as error:
            raise StageMaCollectionError(f"Stage.ma request timed out: {url}") from error
        except httpx.RequestError as error:
            raise StageMaCollectionError(
                f"Stage.ma network request failed ({type(error).__name__}): {url}"
            ) from error
        self._pages_checked += 1
        return response

    def _require_robots_allows(self, url: str) -> None:
        if self._robots_enforced and not robots_allows(self._robots, url):
            raise StageMaCollectionError(f"robots.txt disallows {url}")

    def _fetch(self, url: str) -> tuple[int, str]:
        """GET ``url``, resolving only redirects we are permitted to follow.

        Returns the final status and body. Both gates are checked on the URL the
        site hands us rather than only on the one we asked for: checking robots
        on the requested URL alone is not obeying robots, and a client told to
        follow redirects would take a same-host `302 Location:
        https://elsewhere/…` and GET a host we never chose.

        A 404/410 is returned rather than raised — whether a missing page is
        fatal depends on which page it was, and only the caller knows that.
        """
        if not is_stage_ma_host(url):
            raise StageMaCollectionError(f"refusing off-domain request: {url}")
        self._require_robots_allows(url)
        current = url
        for _ in range(self.MAX_REDIRECTS + 1):
            response = self._get_once(current)
            if response.status_code in _REDIRECT_STATUSES:
                location = (response.headers.get("location") or "").strip()
                if not location:
                    raise StageMaCollectionError(
                        f"Stage.ma redirect without a location: {current}"
                    )
                target = urljoin(current, location)
                if not is_stage_ma_host(target):
                    # Named in the error, never requested.
                    raise StageMaCollectionError(
                        f"refusing off-domain redirect from {current} to "
                        f"{urlsplit(target).hostname}"
                    )
                self._require_robots_allows(target)
                current = target
                continue
            body = response.text
            if response.status_code in {404, 410}:
                return response.status_code, body
            if (barrier := access_barrier(response.status_code, current, body)):
                raise StageMaCollectionError(f"Stage.ma refused {current}: {barrier}")
            if response.status_code != 200:
                raise StageMaCollectionError(
                    f"Stage.ma returned HTTP {response.status_code} for {current}"
                )
            return response.status_code, body
        raise StageMaCollectionError(f"too many redirects from {url}")

    def _resolve_robots(self) -> None:
        """Fetch robots.txt first, and fail closed when it cannot be resolved.

        A robots.txt we were *refused* tells us nothing about what is allowed,
        and an unknown rule is never read as a permissive one. A definitively
        absent file (404/410) means no explicit rule applies and collection may
        continue — a statement about robots.txt alone, and not permission of any
        kind. The 7C.5A audit observed HTTP 200 with a single applicable group.
        """
        response = self._get_once(ROBOTS_URL)
        disposition = classify_robots_response(
            response.status_code, ROBOTS_URL, response.text
        )
        if disposition == ROBOTS_OBEY:
            self._robots = parse_robots_txt(response.text)
            self._robots_enforced = True
            return
        if disposition == ROBOTS_ABSENT:
            self._robots_enforced = False
            return
        raise StageMaCollectionError(
            f"Stage.ma robots.txt could not be resolved safely: {disposition}"
        )

    # -- BaseCollector ---------------------------------------------------
    def fetch(self) -> Any:
        """Read robots, the one approved listing, and the bounded detail pages.

        The payload is a list of `(entry, status, html)` triples in the listing's
        own document order.
        """
        self._pages_checked = 0
        self.skipped = {}
        self._resolve_robots()

        status, listing_html = self._fetch(LISTING_URL)
        if status in {404, 410}:
            # The one surface this collector depends on. Its disappearance is a
            # structural change, never "no opportunities today".
            raise StageMaCollectionError(
                f"Stage.ma listing is unavailable (HTTP {status}): {LISTING_URL}"
            )

        entries = parse_listing(listing_html, LISTING_URL)
        if not entries:
            if has_explicit_empty_state(listing_html):
                # The site said, in its own words, that it has nothing. That is
                # an observation, and an empty batch is the honest result.
                return []
            raise StageMaPayloadError(
                "Stage.ma listing exposed no offer links and no explicit empty "
                "state; refusing to report a structural change as zero offers"
            )

        pages: list[tuple[StageMaListingEntry, int, str]] = []
        for entry in select_listing_targets(entries, self.detail_page_limit):
            time.sleep(self.DELAY_SECONDS)
            detail_status, detail_html = self._fetch(entry.canonical_url)
            pages.append((entry, detail_status, detail_html))
        return pages

    def parse(self, payload: Any) -> list[dict[str, Any]]:
        """Keep the offers this run may store, and say why the others went.

        Every rejection here is one of the source's own content states — gone,
        expired, unpublished, deliberately anonymous. Anything structural raises
        instead: a page with no `JobPosting`, or one with no usable title, means
        the contract this collector was built against no longer holds, and
        quietly returning fewer rows would hide that behind a small batch.
        """
        if not isinstance(payload, list):
            raise StageMaPayloadError("Stage.ma payload must be a list of pages")
        records: list[dict[str, Any]] = []
        for entry, status, html in payload:
            if status in {404, 410}:
                # Discovered from a valid listing and gone by the time we asked:
                # ordinary churn on a job board, and only this offer is lost.
                self.skipped[entry.canonical_url] = SKIP_GONE
                continue

            posting = job_posting(html)
            if posting is None:
                raise StageMaPayloadError(
                    f"Stage.ma offer {entry.canonical_url} publishes no JobPosting"
                )

            title = posting_title(posting) or entry.listing_title
            if not normalize_text(title):
                raise StageMaPayloadError(
                    f"Stage.ma offer {entry.canonical_url} has no usable title"
                )

            state = publication_state(html)
            if state == STATE_EXPIRED:
                self.skipped[entry.canonical_url] = SKIP_EXPIRED
                continue
            if state == STATE_UNPUBLISHED:
                self.skipped[entry.canonical_url] = SKIP_UNPUBLISHED
                continue

            organization = posting_organization(posting)
            if organization is None and is_usable_organization(
                entry.listing_organization
            ):
                organization = normalize_text(entry.listing_organization)
            if organization is None:
                # The source publishes anonymous offers on purpose. Storing one
                # would mean inventing an employer, so this offer is skipped and
                # the run continues.
                self.skipped[entry.canonical_url] = SKIP_ANONYMOUS
                continue

            records.append(
                {
                    "entry": entry,
                    "html": html,
                    "posting": posting,
                    "title": normalize_text(title),
                    "organization": organization,
                    "state": state,
                }
            )
        return records

    def normalize(self, item: dict[str, Any]) -> OpportunityCandidate:
        """Map explicit public signals into a candidate, inventing nothing.

        Title and organization were established in `parse`. Everything else is
        optional and stays `None` when the page does not publish it — no
        placeholder, no city inferred from the source's country, no date
        substituted for a missing one.

        `source_url` is the **canonical** listing-discovered URL rather than
        whatever a redirect ended on, because opportunities are keyed on
        `(source_id, source_url)`: an identity that drifted between runs would
        re-create every offer as new on the next one.
        """
        entry: StageMaListingEntry = item["entry"]
        posting: dict[str, Any] = item["posting"]
        detail_url = entry.canonical_url

        location = posting_location(posting)
        if location is None and entry.listing_location:
            location = normalize_text(entry.listing_location) or None

        return OpportunityCandidate(
            source_id=self.source.id,
            source_external_id=entry.source_external_id,
            canonical_title=item["title"],
            organization=item["organization"],
            location=location,
            description=posting_description(posting),
            # JobPosting.datePosted only, and None when it is missing, malformed
            # or an epoch sentinel.
            published_at=posting_published_at(posting),
            source_url=detail_url,
            application_url=application_url(item["html"], detail_url),
            canonical_url=detail_url,
        )


__all__ = [
    "SKIP_ANONYMOUS",
    "SKIP_EXPIRED",
    "SKIP_GONE",
    "SKIP_UNPUBLISHED",
    "StageMaCollectionError",
    "StageMaCollector",
    "StageMaPayloadError",
]
