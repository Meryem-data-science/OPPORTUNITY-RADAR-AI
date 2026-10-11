"""GET-only collection of published Lever postings available in Morocco.

The public API and its robots.txt are read without cookies or credentials.
Only a declared Morocco location is retained; worldwide/remote alone is not
proof of Morocco eligibility. Original job URLs come from the provider.
"""

from __future__ import annotations

from html import escape
import re
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from services.collector.collectors.base import BaseCollector
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.stagiaires import (
    COLLECTOR_USER_AGENT, ROBOTS_ABSENT, ROBOTS_OBEY,
    classify_robots_response, parse_robots_txt, robots_allows,
)
from services.collector.sources import SourceConfig

API_ORIGIN = "https://api.lever.co"
PAGE_ORIGIN = "https://jobs.lever.co"
PARSER_VERSION = "lever-public-ma-v1"
_MOROCCO_LOCATION = re.compile(r"\b(?:morocco|maroc)\b", re.IGNORECASE)


class LeverCollectionError(RuntimeError):
    """A public board could not be read safely and completely."""


class LeverPayloadError(ValueError):
    """The response no longer follows the public posting contract."""


def posting_identity(url: object, board: str) -> str | None:
    """Return the UUID only for this board's exact original posting URL."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        prefix = f"/{board}/"
        if (parts.scheme != "https" or parts.netloc != "jobs.lever.co"
                or parts.query or parts.fragment or not parts.path.startswith(prefix)):
            return None
        value = parts.path.removeprefix(prefix)
        return value if str(UUID(value)) == value else None
    except (ValueError, AttributeError):
        return None


def morocco_locations(item: dict[str, Any]) -> list[str]:
    """Use the provider's places, including explicit alternate locations."""
    categories = item.get("categories")
    if not isinstance(categories, dict):
        raise LeverPayloadError("Lever posting is missing categories")
    primary = categories.get("location")
    alternatives = categories.get("allLocations", [])
    if not isinstance(alternatives, list) or any(not isinstance(v, str) for v in alternatives):
        raise LeverPayloadError("Lever allLocations must be a list of strings")
    values = [*([primary] if isinstance(primary, str) else []), *alternatives]
    selected = [v.strip() for v in values if _MOROCCO_LOCATION.search(v)]
    # The country field describes the primary location, not every alternate one.
    if str(item.get("country", "")).upper() == "MA" and isinstance(primary, str) and primary.strip():
        selected.insert(0, primary.strip())
    return list(dict.fromkeys(selected))


class LeverCollector(BaseCollector):
    """Read a bounded public board, then keep explicitly Moroccan postings."""

    TIMEOUT_SECONDS = 20.0
    DELAY_SECONDS = 1.0
    PAGE_SIZE = 100
    MAX_LIST_PAGES = 20
    MAX_RESPONSE_BYTES = 8_000_000

    def __init__(self, source: SourceConfig, client: httpx.Client | None = None) -> None:
        if source.type != "lever" or source.country != "MA":
            raise ValueError("LeverCollector requires a lever source restricted to MA")
        if not source.board_token or not re.fullmatch(r"[A-Za-z0-9_-]+", source.board_token):
            raise ValueError("Lever source requires a valid board_token")
        self.source = source
        self.source_id = source.id
        self.endpoint = f"{API_ORIGIN}/v0/postings/{source.board_token}"
        self._client = client
        self._pages_checked = 0
        self._last_request_at = time.monotonic()

    def run_metrics(self) -> dict[str, Any]:
        return {"pages_checked": self._pages_checked, "parser_version": PARSER_VERSION}

    def _get(self, client: httpx.Client, url: str) -> httpx.Response:
        wait = self.DELAY_SECONDS - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()
        client.cookies.clear()
        try:
            with client.stream("GET", url, timeout=self.TIMEOUT_SECONDS,
                               follow_redirects=False) as response:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.MAX_RESPONSE_BYTES:
                        raise LeverPayloadError("Lever response exceeded its size bound")
                    chunks.append(chunk)
                self._pages_checked += 1
                headers = {k: v for k, v in response.headers.items()
                           if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
                return httpx.Response(response.status_code, content=b"".join(chunks),
                                      headers=headers, request=response.request)
        except httpx.HTTPError as error:
            raise LeverCollectionError(f"Lever public request failed: {type(error).__name__}") from error

    def _fetch_with(self, client: httpx.Client) -> list[dict[str, Any]]:
        response = self._get(client, API_ORIGIN + "/robots.txt")
        disposition = classify_robots_response(response.status_code, str(response.url), response.text)
        if disposition == ROBOTS_OBEY:
            rules = parse_robots_txt(response.text)
        elif disposition == ROBOTS_ABSENT:
            rules = ()
        else:
            raise LeverCollectionError("Lever robots.txt could not be resolved safely")
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(self.MAX_LIST_PAGES):
            url = f"{self.endpoint}?mode=json&skip={page * self.PAGE_SIZE}&limit={self.PAGE_SIZE}"
            if not robots_allows(rules, url):
                raise LeverCollectionError("Lever robots.txt disallows the public posting endpoint")
            response = self._get(client, url)
            if response.status_code != 200:
                raise LeverCollectionError(f"Lever returned HTTP {response.status_code}")
            try:
                jobs = response.json()
            except ValueError as error:
                raise LeverPayloadError("Lever returned invalid JSON") from error
            if not isinstance(jobs, list) or len(jobs) > self.PAGE_SIZE:
                raise LeverPayloadError("Lever posting page must be a bounded list")
            for item in jobs:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise LeverPayloadError("Lever posting is missing a string id")
                if item["id"] in seen:
                    raise LeverPayloadError("Lever pagination repeated a posting")
                seen.add(item["id"])
                collected.append(item)
            if len(jobs) < self.PAGE_SIZE:
                return collected
        raise LeverCollectionError("Lever catalogue exceeded the bounded pagination limit")

    def fetch(self) -> Any:
        self._pages_checked = 0
        self._last_request_at = time.monotonic()
        if self._client is not None:
            return self._fetch_with(self._client)
        with httpx.Client(headers={"User-Agent": COLLECTOR_USER_AGENT,
                                   "Accept": "application/json"}, follow_redirects=False) as client:
            return self._fetch_with(client)

    def parse(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or any(not isinstance(v, dict) for v in payload):
            raise LeverPayloadError("Lever catalogue must be a list of objects")
        return [item for item in payload if morocco_locations(item)]

    def normalize(self, item: dict[str, Any]) -> OpportunityCandidate:
        title, posting_id, url = item.get("text"), item.get("id"), item.get("hostedUrl")
        if not isinstance(title, str) or not title.strip():
            raise LeverPayloadError("Lever posting is missing its title")
        if posting_identity(url, self.source.board_token or "") != posting_id:
            raise LeverPayloadError("Lever original URL does not identify the collected posting")
        locations = morocco_locations(item)
        if not locations:
            raise LeverPayloadError("Lever posting has no confirmed Morocco location")
        categories = item["categories"]
        metadata: list[str] = []
        for label, value in (("Type de mission", categories.get("commitment")),
                             ("Équipe", categories.get("team")),
                             ("Organisation du travail", item.get("workplaceType"))):
            if isinstance(value, str) and value.strip():
                metadata.append(f"<p>{label} : {escape(value)}</p>")
        sections = [item.get("description", "")]
        lists = item.get("lists", [])
        if not isinstance(lists, list) or any(not isinstance(v, dict) for v in lists):
            raise LeverPayloadError("Lever lists must be a list of sections")
        for section in lists:
            heading, content = section.get("text", ""), section.get("content", "")
            if not isinstance(heading, str) or not isinstance(content, str):
                raise LeverPayloadError("Lever description section must contain strings")
            sections.append(f"<h2>{escape(heading)}</h2>{content}")
        sections.append(item.get("additional", ""))
        if any(not isinstance(value, str) for value in sections):
            raise LeverPayloadError("Lever description must contain strings")
        apply_url = item.get("applyUrl")
        if apply_url != url + "/apply":
            raise LeverPayloadError("Lever application URL does not identify the collected posting")
        # createdAt is a creation timestamp, not a verified publication date.
        # Keep publication unknown rather than making an old imported job new.
        location = "; ".join(
            value if _MOROCCO_LOCATION.search(value) else value + ", Morocco"
            for value in locations
        )
        return OpportunityCandidate(
            source_id=self.source_id, source_external_id=posting_id,
            canonical_title=title.strip(), organization=self.source.organization or "",
            location=location, description="\n".join([*metadata, *sections]),
            published_at=None, source_url=url, application_url=apply_url, canonical_url=url,
        )
