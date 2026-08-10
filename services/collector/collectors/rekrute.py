"""Conservative, public-only ReKrute discovery and detail collection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
import logging
import re
import time
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from services.collector.models.opportunity import OpportunityCandidate

USER_AGENT = "OpportunityRadarAI/0.1 (+educational-project; public-readonly)"
ORIGIN = "https://www.rekrute.com"
ROBOTS_URL = f"{ORIGIN}/robots.txt"
HARD_LIMIT = 20
OFFER_PATH = re.compile(r"^/(?:fr/)?offre-emploi-[^?#]*-(\d+)\.html$")
EXPIRED_TEXT = "cette offre d'emploi n'est plus d'actualité"


class ReKruteError(RuntimeError):
    """Base class for safe, classified probe failures."""


class RobotsDeniedError(ReKruteError):
    """Robots policy could not explicitly authorize the requested path."""


class SearchFetchError(ReKruteError):
    """The public search page could not be fetched."""


class DetailFetchError(ReKruteError):
    """A public offer page could not be fetched."""


class NoOfferLinksError(ReKruteError):
    """No conforming offer links were present on the search page."""


class ReKruteParseError(ValueError):
    """A detail page lacks a required reliable field."""


class ReKruteHttpClient:
    """Small injectable HTTP boundary with an identified agent and strict timeout."""

    def __init__(self, timeout: float = 15.0, max_redirects: int = 5) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
            max_redirects=max_redirects,
        )

    def fetch_html(self, url: str) -> str:
        response = self._client.get(url)
        response.raise_for_status()
        return response.text

    def close(self) -> None:
        self._client.close()


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list["_Node"] = field(default_factory=list)
    chunks: list[str] = field(default_factory=list)

    def text(self) -> str:
        return clean_text(" ".join(self.chunks + [child.text() for child in self.children]))


class _TreeParser(HTMLParser):
    VOID = {"meta", "link", "img", "br", "hr", "input", "source"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {k.lower(): v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag.lower() not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag.lower():
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.stack[-1].tag not in {"script", "style", "nav", "footer"}:
            self.stack[-1].chunks.append(data)


def clean_text(value: str) -> str:
    return " ".join(value.split())


def build_search_url(query: str) -> str:
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    return f"{ORIGIN}/offres.html?keyword={quote(query)}"


def canonicalize_offer_url(value: str, base_url: str = ORIGIN) -> str | None:
    parsed = urlsplit(urljoin(base_url, value))
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "www.rekrute.com":
        return None
    if not OFFER_PATH.fullmatch(parsed.path):
        return None
    return urlunsplit(("https", "www.rekrute.com", parsed.path, "", ""))


def offer_id(value: str) -> str | None:
    match = OFFER_PATH.fullmatch(urlsplit(value).path)
    return match.group(1) if match else None


def discover_offer_urls(html: str, search_url: str = ORIGIN) -> list[str]:
    parser = _LinkParser()
    parser.feed(html)
    seen: set[str] = set()
    found: list[str] = []
    for href in parser.links:
        canonical = canonicalize_offer_url(href, search_url)
        if canonical and canonical not in seen:
            seen.add(canonical)
            found.append(canonical)
    return found


def _walk(node: _Node):
    for child in node.children:
        yield child
        yield from _walk(child)


def _attribute_node(root: _Node, names: tuple[str, ...]) -> _Node | None:
    for node in _walk(root):
        marker = " ".join((node.attrs.get("id", ""), node.attrs.get("class", ""))).lower()
        if any(name in marker for name in names) and node.text():
            return node
    return None


def _label_value(root: _Node, labels: tuple[str, ...]) -> str | None:
    for node in _walk(root):
        text = node.text()
        lowered = text.lower()
        for label in labels:
            match = re.match(rf"^{re.escape(label)}\s*:\s*(.+)$", text, re.IGNORECASE)
            if match and len(text) < 300:
                return clean_text(match.group(1))
        if node.tag in {"dt", "th", "strong", "span"} and any(
            lowered.rstrip(" :") == label for label in labels
        ):
            siblings = node.children
            if siblings:
                value = clean_text(" ".join(child.text() for child in siblings))
                if value:
                    return value
    return None


def _section(root: _Node, heading_names: tuple[str, ...]) -> str | None:
    headings = {"h1", "h2", "h3", "h4", "h5", "strong"}
    for node in _walk(root):
        heading = node.text().lower().rstrip(" :")
        if node.tag in headings and heading in heading_names:
            # ReKrute-style fixtures and pages generally wrap heading + body together.
            parent = next((p for p in _walk(root) if node in p.children), None)
            if parent:
                pieces: list[str] = []
                passed = False
                for sibling in parent.children:
                    if sibling is node:
                        passed = True
                        continue
                    if passed and sibling.tag in headings:
                        break
                    if passed and sibling.text():
                        pieces.append(sibling.text())
                value = clean_text(" ".join(pieces))
                if value:
                    return value
    return None


def parse_offer(html: str, url: str, source_id: str = "rekrute_public") -> OpportunityCandidate | None:
    canonical = canonicalize_offer_url(url)
    external_id = offer_id(canonical or "")
    if canonical is None or external_id is None:
        raise ReKruteParseError("offer URL has no reliable numeric id")
    parser = _TreeParser()
    parser.feed(html)
    page_text = parser.root.text()
    if EXPIRED_TEXT in page_text.lower():
        return None

    title_node = _attribute_node(parser.root, ("job-title", "offer-title", "offre-title"))
    if title_node is None:
        title_node = next((n for n in _walk(parser.root) if n.tag == "h1" and n.text()), None)
    title = title_node.text() if title_node else ""
    if not title:
        raise ReKruteParseError("offer title is missing")

    organization_node = _attribute_node(parser.root, ("company-name", "recruiter-name", "organization"))
    organization = organization_node.text() if organization_node else _label_value(
        parser.root, ("entreprise", "société")
    )
    if not organization or organization.lower() == "rekrute":
        raise ReKruteParseError("offer organization is missing")

    location_node = _attribute_node(parser.root, ("location", "localisation", "ville"))
    location = location_node.text() if location_node else _label_value(
        parser.root, ("localisation", "lieu", "ville")
    )
    poste = _section(parser.root, ("poste", "description du poste"))
    profile = _section(parser.root, ("profil recherché", "profil recherche"))
    description = clean_text("\n\n".join(value for value in (poste, profile) if value)) or None
    if description is None:
        raise ReKruteParseError("offer description is missing")

    date_match = re.search(
        r"publication\s*:\s*(?:du\s+)?(\d{2}/\d{2}/\d{4})", page_text, re.IGNORECASE
    )
    published_at = None
    if date_match:
        try:
            published_at = datetime.strptime(date_match.group(1), "%d/%m/%Y").date().isoformat()
        except ValueError:
            published_at = None
    return OpportunityCandidate(
        source_id=source_id,
        source_external_id=external_id,
        canonical_title=title,
        organization=organization,
        location=location or None,
        description=description,
        published_at=published_at,
        source_url=canonical,
        application_url=canonical,
        canonical_url=canonical,
    )


@dataclass(frozen=True)
class ProbeResult:
    status: str
    candidates: list[OpportunityCandidate]
    detail_fetch_failed: int = 0
    parse_failed: int = 0


class ReKruteCollector:
    """Orchestrate one search page and a bounded set of its direct offer links."""

    def __init__(
        self,
        client: ReKruteHttpClient,
        *,
        source_id: str = "rekrute_public",
        pause_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.source_id = source_id
        self.pause_seconds = max(0.0, pause_seconds)
        self.sleep = sleep
        self.logger = logging.getLogger(__name__)

    def _robots_parser(self) -> RobotFileParser:
        try:
            body = self.client.fetch_html(ROBOTS_URL)
        except (httpx.HTTPError, OSError) as error:
            raise RobotsDeniedError("robots.txt could not be verified") from error
        if (
            not body.strip()
            or not re.search(r"^\s*user-agent\s*:", body, re.I | re.M)
            or not re.search(r"^\s*(?:allow|disallow)\s*:", body, re.I | re.M)
        ):
            raise RobotsDeniedError("robots.txt response is not usable")
        parser = RobotFileParser(ROBOTS_URL)
        parser.parse(body.splitlines())
        return parser

    @staticmethod
    def _require_allowed(parser: RobotFileParser, url: str) -> None:
        if not parser.can_fetch(USER_AGENT, url):
            raise RobotsDeniedError(f"robots.txt denies path: {urlsplit(url).path}")

    def collect(self, query: str, limit: int = 3) -> ProbeResult:
        if isinstance(limit, bool) or not 1 <= limit <= HARD_LIMIT:
            raise ValueError(f"limit must be between 1 and {HARD_LIMIT}")
        search_url = build_search_url(query)
        robots = self._robots_parser()
        self._require_allowed(robots, search_url)
        try:
            search_html = self.client.fetch_html(search_url)
        except (httpx.HTTPError, OSError) as error:
            raise SearchFetchError("search page fetch failed") from error
        links = discover_offer_urls(search_html, search_url)[:limit]
        if not links:
            raise NoOfferLinksError("search page contained no valid offer links")
        for link in links:
            self._require_allowed(robots, link)

        candidates: list[OpportunityCandidate] = []
        fetch_failures = parse_failures = 0
        for index, link in enumerate(links):
            if index and self.pause_seconds:
                self.sleep(self.pause_seconds)
            try:
                detail_html = self.client.fetch_html(link)
            except (httpx.HTTPError, OSError) as error:
                fetch_failures += 1
                self.logger.warning("detail_fetch_failed url=%s error=%s", link, type(error).__name__)
                continue
            try:
                candidate = parse_offer(detail_html, link, self.source_id)
            except ReKruteParseError as error:
                parse_failures += 1
                self.logger.warning("parse_failed offer_id=%s error=%s", offer_id(link), type(error).__name__)
                continue
            if candidate is not None:
                candidates.append(candidate)
        status = "success"
        if not candidates:
            status = "detail_fetch_failed" if fetch_failures and not parse_failures else "parse_failed"
        return ProbeResult(status, candidates, fetch_failures, parse_failures)
