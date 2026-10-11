"""Bounded, robots-aware collection from SQLI's own public career pages."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from services.collector.collectors.base import BaseCollector
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.sqli import (
    MAX_DOCUMENT_BYTES, PARSER_VERSION, ROBOTS_URL,
    SqliCollectionError, SqliPayloadError, SqliSitemapEntry,
    canonical_offer_url, is_sqli_url, parse_offer, parse_sitemap, select_detail_targets,
)
from services.collector.parsers.stagiaires import (
    COLLECTOR_USER_AGENT, ROBOTS_OBEY, RobotsGroup, access_barrier,
    classify_robots_response, parse_robots_txt, robots_allows, sitemap_declarations,
)
from services.collector.sources import SourceConfig

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class SqliCollector(BaseCollector):
    """Collect Moroccan SQLI descriptions without requesting its external ATS.

    At most ten declared sitemap files and fifty details are read, with a one
    second minimum between requests. No retries, browser impersonation, cookies
    or authentication. Sitemap presence is never a complete active registry.
    """

    TIMEOUT_SECONDS = 25.0
    DELAY_SECONDS = 1.0
    MAX_REDIRECTS = 3
    MAX_SITEMAPS = 10

    def __init__(self, source: SourceConfig, client: httpx.Client | None = None) -> None:
        if source.type != "sqli_careers":
            raise ValueError("SqliCollector requires a sqli_careers source")
        limit = source.detail_page_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("SQLI detail_page_limit must be between 1 and 50")
        self.source = source
        self.source_id = source.id
        self.detail_page_limit = limit
        self._client = client
        self._robots: tuple[RobotsGroup, ...] = ()
        self._pages_checked = 0
        self._last_request_at: float | None = None

    def run_metrics(self) -> dict[str, Any]:
        return {"pages_checked": self._pages_checked, "parser_version": PARSER_VERSION}

    def _request(self, url: str) -> tuple[int, dict[str, str], str]:
        if self._last_request_at is not None:
            remaining = self.DELAY_SECONDS - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()
        headers = {"User-Agent": COLLECTOR_USER_AGENT}
        try:
            if self._client is None:
                # Top-level streams do not retain cookies between GETs.
                stream = httpx.stream("GET", url, headers=headers,
                                      timeout=self.TIMEOUT_SECONDS, follow_redirects=False)
            else:
                self._client.cookies.clear()
                stream = self._client.stream("GET", url, headers=headers,
                                             timeout=self.TIMEOUT_SECONDS, follow_redirects=False)
            with stream as response:
                self._pages_checked += 1
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_DOCUMENT_BYTES:
                        raise SqliCollectionError(f"SQLI document exceeds the size limit: {url}")
                    chunks.append(chunk)
                body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                return response.status_code, dict(response.headers), body
        except httpx.RequestError as error:
            raise SqliCollectionError(f"SQLI request failed ({type(error).__name__}): {url}") from error

    def _fetch(self, url: str, *, robots_file: bool = False, allow_missing: bool = False) -> tuple[str, str] | None:
        current = url
        for _ in range(self.MAX_REDIRECTS + 1):
            if not is_sqli_url(current):
                raise SqliCollectionError(f"SQLI refuses an off-domain URL: {current}")
            if robots_file:
                if urlsplit(current).path != "/robots.txt":
                    raise SqliCollectionError("SQLI robots.txt redirected away from the rules file")
            elif not robots_allows(self._robots, current, COLLECTOR_USER_AGENT):
                raise SqliCollectionError(f"SQLI robots.txt disallows {current}")
            status, headers, body = self._request(current)
            if status in _REDIRECT_STATUSES:
                location = headers.get("location", "").strip()
                if not location:
                    raise SqliCollectionError(f"SQLI redirect has no location: {current}")
                target = urljoin(current, location)
                if allow_missing and canonical_offer_url(target) != canonical_offer_url(url):
                    raise SqliCollectionError("SQLI job redirect left the exact original posting")
                current = target
                continue
            if allow_missing and status in {404, 410}:
                return None
            if robots_file:
                sample = body[:2000].lower()
                disposition = classify_robots_response(status, current, body)
                if disposition != ROBOTS_OBEY or "<html" in sample or "<!doctype" in sample:
                    raise SqliCollectionError(f"SQLI robots.txt could not be resolved: {disposition}")
            elif barrier := access_barrier(status, current, body):
                raise SqliCollectionError(f"SQLI refused {current}: {barrier}")
            return current, body
        raise SqliCollectionError(f"SQLI exceeded the redirect limit: {url}")

    def fetch(self) -> list[tuple[SqliSitemapEntry, str, str]]:
        self._pages_checked = 0
        self._last_request_at = None
        self._robots = ()
        robots_result = self._fetch(ROBOTS_URL, robots_file=True)
        if robots_result is None:
            raise SqliCollectionError("SQLI robots.txt is unavailable")
        _, robots_body = robots_result
        self._robots = parse_robots_txt(robots_body)
        pending = [url for url in sitemap_declarations(robots_body) if is_sqli_url(url)]
        if not pending:
            raise SqliCollectionError("SQLI robots.txt declares no same-host sitemap")
        visited: set[str] = set()
        entries: list[SqliSitemapEntry] = []
        while pending:
            sitemap_url = pending.pop(0)
            if sitemap_url in visited:
                continue
            if len(visited) >= self.MAX_SITEMAPS:
                raise SqliPayloadError("SQLI declares more sitemap files than the collection bound")
            visited.add(sitemap_url)
            result = self._fetch(sitemap_url)
            if result is None:
                raise SqliCollectionError("SQLI required sitemap is unavailable")
            final_url, body = result
            children, discovered = parse_sitemap(body, final_url)
            pending.extend(child for child in children if child not in visited)
            entries.extend(discovered)
        pages: list[tuple[SqliSitemapEntry, str, str]] = []
        # Epoch hours are UTC. A changing window prevents an oversized global
        # catalogue from keeping older Moroccan pages outside the budget forever.
        targets = select_detail_targets(
            entries, self.detail_page_limit, rotation_window=int(time.time() // 3600)
        )
        for entry in targets:
            result = self._fetch(entry.canonical_url, allow_missing=True)
            if result is None:
                continue
            final_url, body = result
            if canonical_offer_url(final_url) is None:
                raise SqliPayloadError("SQLI job redirected to a non-job page")
            pages.append((entry, final_url, body))
        return pages

    def parse(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise SqliPayloadError("SQLI payload must be a list of detail pages")
        records: list[dict[str, Any]] = []
        for entry, final_url, body in payload:
            if (offer := parse_offer(body, final_url)) is not None:
                records.append({"entry": entry, "offer": offer})
        return records

    def normalize(self, item: dict[str, Any]) -> OpportunityCandidate:
        entry: SqliSitemapEntry = item["entry"]
        offer = item["offer"]
        return OpportunityCandidate(
            source_id=self.source.id, source_external_id=entry.source_external_id,
            canonical_title=offer["title"], organization="SQLI", location=offer["location"],
            description=offer["description"], published_at=None,
            source_url=entry.canonical_url, application_url=offer["application_url"],
            canonical_url=entry.canonical_url,
        )
