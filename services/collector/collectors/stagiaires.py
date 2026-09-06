"""Collector for public Stagiaires.ma offers, via the site's own sitemap chain.

Phase 7C.4A audited what this site publicly exposes; this is the production
collector that finding made possible. It follows the chain the site publishes
for exactly this purpose, and nothing else:

    robots.txt -> the Sitemap: it declares -> offre-sitemap*.xml
               -> /stage-emploi-maroc/<numeric-id>-<slug> -> JobPosting JSON-LD

The public PFE listing page is **not** part of this. The audit fetched it once to
record that it is reachable, and found it to be a client-rendered application
whose initial HTML carries no offer list — reading it would mean driving a
browser. The sitemap makes that unnecessary, so nothing here depends on that page.

Everything about how this collector behaves on the network is a constraint, not
a default:

* **GET-only, sequential, bounded.** One request at a time, a delay between
  them, an explicit timeout, no retries and no concurrency. Detail fetching is
  capped by the source's `detail_page_limit`, so a run reads tens of pages, not
  the site's ~1700;
* **same-host, always.** Every URL is checked before it is requested, and
  redirects are resolved here rather than by the HTTP client, because a client
  told to follow them would take a same-host URL's `302 Location:
  https://elsewhere/…` and issue a GET to a host we never chose. An off-domain
  application URL may be *recorded* — recording is not requesting — but is never
  fetched;
* **robots-aware at every hop.** Rules are checked against path *and* query, and
  re-checked on the URL a redirect hands us: obeying robots only on the URL we
  asked for is not obeying robots;
* **fail closed.** A refused robots.txt, an unreadable required sitemap, a
  missing title — anything that would make the result partial or invented raises,
  and the RadarAgent records the source run as FAILED. A failed fetch never
  becomes "zero offers found", because zero is an observation and this is not one.

No browser, no browser impersonation, no Selenium or Playwright, no
authentication, no cookies, no proxy, no CAPTCHA handling, no private API taken
from a JS bundle. The user agent names this collector truthfully.

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
from services.collector.parsers.stagiaires import (
    COLLECTOR_USER_AGENT,
    PARSER_VERSION,
    ROBOTS_ABSENT,
    ROBOTS_OBEY,
    ROBOTS_URL,
    RobotsGroup,
    SitemapOfferEntry,
    StagiairesCollectionError,
    StagiairesPayloadError,
    access_barrier,
    application_action_url,
    canonical_url,
    classify_robots_response,
    deduplicate_entries,
    is_stagiaires_host,
    job_posting,
    parse_offer_sitemap,
    parse_robots_txt,
    parse_sitemap_index,
    posting_description,
    posting_location,
    posting_organization,
    posting_published_at,
    posting_title,
    robots_allows,
    select_detail_targets,
    sitemap_declarations,
)
from services.collector.sources import SourceConfig

#: Redirect statuses the collector resolves itself, so it can refuse one.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class StagiairesCollector(BaseCollector):
    """Read public offers from Stagiaires.ma through its official sitemaps."""

    TIMEOUT_SECONDS = 20.0
    DELAY_SECONDS = 1.0
    MAX_REDIRECTS = 3

    #: A bound on the number of sitemap *files*, not just pages. The index is
    #: expected to declare two or three; it is not expected to declare fifty,
    #: and a collector that would fetch fifty because a document said so is a
    #: crawler wearing a collector's name.
    #:
    #: Exceeding it is a **refusal, not a truncation**. Reading the first twenty
    #: and returning candidates anyway would turn a materially partial read into
    #: an apparently successful one, which is the failure this collector exists
    #: to make impossible.
    MAX_OFFER_SITEMAPS = 20

    def __init__(
        self, source: SourceConfig, client: httpx.Client | None = None
    ) -> None:
        if source.type != "stagiaires_sitemap":
            raise ValueError(
                "StagiairesCollector requires a stagiaires_sitemap source"
            )
        if source.detail_page_limit is None:
            raise ValueError(
                f"source {source.id!r} must configure detail_page_limit"
            )
        self.source = source
        self.source_id = source.id
        self.detail_page_limit = source.detail_page_limit
        self._client = client
        self._robots: tuple[RobotsGroup, ...] = ()
        self._robots_enforced = False
        self._pages_checked = 0

    # -- metrics ---------------------------------------------------------
    def run_metrics(self) -> dict[str, Any]:
        """Report only what this run actually measured.

        `items_found` is the agent's to count, and `new_items`,
        `relevant_items` and `http_status` are omitted rather than guessed: a
        collector that fetches many pages has no single HTTP status, and
        relevance is the qualification layer's judgement, not collection's.
        """
        return {
            "pages_checked": self._pages_checked,
            "parser_version": PARSER_VERSION,
        }

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
            raise StagiairesCollectionError(
                f"Stagiaires request timed out: {url}"
            ) from error
        except httpx.RequestError as error:
            raise StagiairesCollectionError(
                f"Stagiaires network request failed ({type(error).__name__}): {url}"
            ) from error
        self._pages_checked += 1
        return response

    def _fetch(self, url: str, *, robots_checked: bool = False) -> tuple[str, str]:
        """GET ``url``, resolving only redirects we are permitted to follow.

        Returns the final URL and its body. Every hop is checked against the
        same-host rule and against robots **before** the next request is issued,
        so neither can be escaped by redirecting.
        """
        if not is_stagiaires_host(url):
            raise StagiairesCollectionError(f"refusing off-domain request: {url}")
        if not robots_checked:
            self._require_robots_allows(url)
        current = url
        for _ in range(self.MAX_REDIRECTS + 1):
            response = self._get_once(current)
            if response.status_code in _REDIRECT_STATUSES:
                location = (response.headers.get("location") or "").strip()
                if not location:
                    raise StagiairesCollectionError(
                        f"Stagiaires redirect without a location: {current}"
                    )
                target = urljoin(current, location)
                if not is_stagiaires_host(target):
                    # Recorded in the error, never requested.
                    raise StagiairesCollectionError(
                        f"refusing off-domain redirect from {current} to "
                        f"{urlsplit(target).hostname}"
                    )
                self._require_robots_allows(target)
                current = target
                continue
            body = response.text
            if (barrier := access_barrier(response.status_code, current, body)):
                raise StagiairesCollectionError(
                    f"Stagiaires refused {current}: {barrier}"
                )
            return current, body
        raise StagiairesCollectionError(f"too many redirects from {url}")

    def _require_robots_allows(self, url: str) -> None:
        if self._robots_enforced and not robots_allows(self._robots, url):
            raise StagiairesCollectionError(f"robots.txt disallows {url}")

    def _resolve_robots(self) -> None:
        """Fetch robots.txt first, and fail closed when it cannot be resolved.

        A robots.txt we were *refused* tells us nothing about what is allowed,
        and an unknown rule is never read as a permissive one. A definitively
        absent file (404/410) means no explicit rule applies and collection may
        continue — a statement about robots.txt alone, and not permission of any
        kind.
        """
        response = self._get_once(ROBOTS_URL)
        disposition = classify_robots_response(
            response.status_code, ROBOTS_URL, response.text
        )
        if disposition == ROBOTS_OBEY:
            self._robots = parse_robots_txt(response.text)
            self._robots_enforced = True
            self._declarations = sitemap_declarations(response.text)
            return
        if disposition == ROBOTS_ABSENT:
            # No rules to obey, and equally no sitemap declared: without the
            # site's own pointer there is no official chain to follow, and
            # guessing one is not collection.
            self._robots_enforced = False
            self._declarations = ()
            return
        raise StagiairesCollectionError(
            f"Stagiaires robots.txt could not be resolved safely: {disposition}"
        )

    # -- BaseCollector ---------------------------------------------------
    def fetch(self) -> Any:
        """Walk the official chain and return the pages this run will parse.

        The payload is a list of `(entry, final_url, html)` triples: the detail
        pages already fetched, in the order they were selected.
        """
        self._pages_checked = 0
        self._resolve_robots()

        same_host = [item for item in self._declarations if is_stagiaires_host(item)]
        if not same_host:
            raise StagiairesCollectionError(
                "Stagiaires robots.txt declares no same-host sitemap"
            )
        index_url = same_host[0]
        _, index_body = self._fetch(index_url)
        offer_sitemaps = parse_sitemap_index(index_body, index_url)
        if not offer_sitemaps:
            raise StagiairesPayloadError(
                "Stagiaires sitemap index declares no offer sitemap"
            )
        if len(offer_sitemaps) > self.MAX_OFFER_SITEMAPS:
            # Reading the first twenty and returning candidates would present a
            # materially partial read as a complete one — the same failure as an
            # unreadable sitemap, only quieter, because nothing would look wrong.
            # The safety bound stays where it is; what changes is that exceeding
            # it is a refusal rather than a truncation. Nothing below this line
            # is requested: no offer sitemap, no detail page, no candidate.
            raise StagiairesPayloadError(
                f"Stagiaires sitemap index declares {len(offer_sitemaps)} offer "
                f"sitemaps, above the production bound of "
                f"{self.MAX_OFFER_SITEMAPS}; refusing to collect a partial "
                f"subset of the source"
            )

        entries: list[SitemapOfferEntry] = []
        # The full declared set, because the bound above proved it fits. Every
        # declared offer sitemap is required: the offer set is their union, so
        # one file we cannot read makes the result a silent subset of the truth.
        # Both `_fetch` and `parse_offer_sitemap` raise, and neither failure is
        # swallowed into an empty list.
        for sitemap_url in offer_sitemaps:
            time.sleep(self.DELAY_SECONDS)
            _, body = self._fetch(sitemap_url)
            entries.extend(parse_offer_sitemap(body, sitemap_url))

        targets = select_detail_targets(tuple(entries), self.detail_page_limit)
        pages: list[tuple[SitemapOfferEntry, str, str]] = []
        for entry in targets:
            time.sleep(self.DELAY_SECONDS)
            final_url, html = self._fetch(entry.canonical_url)
            pages.append((entry, final_url, html))
        return pages

    def parse(self, payload: Any) -> list[dict[str, Any]]:
        """Validate the fetched pages and return one raw record per offer."""
        if not isinstance(payload, list):
            raise StagiairesPayloadError("Stagiaires payload must be a list of pages")
        records: list[dict[str, Any]] = []
        for item in payload:
            entry, final_url, html = item
            posting = job_posting(html)
            if posting is None:
                raise StagiairesPayloadError(
                    f"Stagiaires offer {entry.canonical_url} publishes no JobPosting"
                )
            records.append(
                {"entry": entry, "final_url": final_url, "html": html, "posting": posting}
            )
        return records

    def normalize(self, item: dict[str, Any]) -> OpportunityCandidate:
        """Map explicit public signals into a candidate, inventing nothing.

        Title and organization are required: a candidate without them is not a
        usable opportunity, and storing a placeholder in either would put a fact
        in the database that no page ever stated. Everything else is optional and
        stays `None` when absent — UNKNOWN remains UNKNOWN.

        `source_url` is the **canonical** detail URL rather than whatever URL a
        redirect ended on, because opportunities are keyed on
        `(source_id, source_url)`: an identity that drifted between runs would
        re-create every offer as new on the next run.
        """
        entry: SitemapOfferEntry = item["entry"]
        posting: dict[str, Any] = item["posting"]
        detail_url = entry.canonical_url

        title = posting_title(posting)
        if not title:
            raise StagiairesPayloadError(
                f"Stagiaires offer {detail_url} is missing a title"
            )
        organization = posting_organization(posting)
        if not organization:
            raise StagiairesPayloadError(
                f"Stagiaires offer {detail_url} is missing a hiring organization"
            )

        return OpportunityCandidate(
            source_id=self.source.id,
            source_external_id=entry.source_external_id,
            canonical_title=title,
            organization=organization,
            location=posting_location(posting),
            description=posting_description(posting),
            # The posting's own datePosted, never the sitemap's <lastmod>.
            published_at=posting_published_at(posting),
            source_url=detail_url,
            application_url=application_action_url(item["html"], detail_url),
            canonical_url=detail_url,
        )


__all__ = [
    "StagiairesCollectionError",
    "StagiairesCollector",
    "StagiairesPayloadError",
    "canonical_url",
    "deduplicate_entries",
]
