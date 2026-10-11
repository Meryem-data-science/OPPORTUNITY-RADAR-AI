"""Pure parsing of public SQLI career pages and their declared sitemaps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

PARSER_VERSION = "sqli-careers-html-v1"
ROBOTS_URL = "https://www.sqli.com/robots.txt"
SQLI_HOST = "www.sqli.com"
OFFER_PATH_PREFIX = "/fr-fr/carriere/nos-offres-demploi/"
MAX_DOCUMENT_BYTES = 8_000_000
_OFFER_PATH = re.compile(r"^/fr-fr/carriere/nos-offres-demploi/([^/]+)$")
_DISCOVERY_WORDS = re.compile(
    r"(?:^|[-_])(?:data|databricks|analytics|analytique|intelligence|artificielle|"
    r"machine|learning|ml|ai|ia|bi|decisionnel|python|stage|stagiaire|pfe|"
    r"alternance|apprentissage|apprentice|trainee|internship)(?:[-_]|$)"
)
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_CLOSED_MARKERS = (
    "cette offre n'est plus disponible", "cette offre n’est plus disponible",
    "ce poste n'est plus disponible", "ce poste n’est plus disponible",
    "this job is no longer available", "this position has been filled",
)


class SqliCollectionError(RuntimeError):
    """A required public document could not be collected safely."""


class SqliPayloadError(ValueError):
    """The source did not expose the expected sitemap or job-page structure."""


@dataclass(frozen=True)
class SqliSitemapEntry:
    canonical_url: str
    source_external_id: str
    sitemap_lastmod: str | None = None


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def _fold(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(char))


def is_sqli_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname == SQLI_HOST
                and parts.port in {None, 443} and parts.username is None
                and parts.password is None)
    except ValueError:
        return False


def canonical_offer_url(url: str) -> str | None:
    if not is_sqli_url(url):
        return None
    path = urlsplit(url).path.rstrip("/")
    if not _OFFER_PATH.fullmatch(path):
        return None
    return urlunsplit(("https", SQLI_HOST, path, "", ""))


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def parse_sitemap(body: str, sitemap_url: str) -> tuple[tuple[str, ...], tuple[SqliSitemapEntry, ...]]:
    """Read child sitemaps or candidate job URLs; lastmod only orders reads.

    The discovery words bound the network budget. Existing qualification still
    determines relevance after the full public job description is collected.
    """
    if len(body.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise SqliPayloadError("SQLI sitemap exceeds the document size limit")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.IGNORECASE):
        raise SqliPayloadError("SQLI sitemap contains unsupported XML declarations")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise SqliPayloadError(f"SQLI returned invalid sitemap XML: {sitemap_url}") from error
    root_name = _local_name(root.tag)
    if root_name not in {"sitemapindex", "urlset"}:
        raise SqliPayloadError(f"SQLI returned an unexpected sitemap root: {root_name}")
    children: list[str] = []
    entries: list[SqliSitemapEntry] = []
    for node in root:
        values = {_local_name(child.tag): (child.text or "").strip() for child in node}
        loc = values.get("loc", "")
        if not loc:
            raise SqliPayloadError("SQLI sitemap entry is missing its URL")
        if root_name == "sitemapindex":
            if not is_sqli_url(loc) or not urlsplit(loc).path.endswith(".xml"):
                raise SqliPayloadError("SQLI sitemap references an unsupported child URL")
            children.append(loc)
            continue
        canonical = canonical_offer_url(loc)
        if canonical is None:
            continue
        slug = urlsplit(canonical).path.rsplit("/", 1)[-1]
        if not _DISCOVERY_WORDS.search(_fold(slug)):
            continue
        match = re.search(r"-(\d+)$", slug)
        entries.append(SqliSitemapEntry(canonical, match.group(1) if match else slug, values.get("lastmod") or None))
    return tuple(dict.fromkeys(children)), tuple(entries)


def select_detail_targets(
    entries: list[SqliSitemapEntry], limit: int, *, rotation_window: int = 0
) -> tuple[SqliSitemapEntry, ...]:
    """Keep fresh pages and rotate a bounded older window without local state.

    With limit=50 an oversized catalogue gets forty recent pages plus ten
    rotating pages. A stable backlog is covered over consecutive UTC hours;
    absence from one bounded read never establishes an offer's closure.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise SqliPayloadError("SQLI detail-page limit must be between 1 and 50")
    if isinstance(rotation_window, bool) or not isinstance(rotation_window, int) or rotation_window < 0:
        raise SqliPayloadError("SQLI rotation window must be a non-negative integer")
    def priority(entry: SqliSitemapEntry) -> tuple[float, str]:
        try:
            value = datetime.fromisoformat((entry.sitemap_lastmod or "").replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            timestamp = value.timestamp()
        except (ValueError, OverflowError, OSError):
            timestamp = float("-inf")
        return (-timestamp, entry.canonical_url)
    by_url: dict[str, SqliSitemapEntry] = {}
    for entry in entries:
        previous = by_url.get(entry.canonical_url)
        if previous is None or priority(entry) < priority(previous):
            by_url[entry.canonical_url] = entry
    ordered = sorted(by_url.values(), key=priority)
    if len(ordered) <= limit:
        return tuple(ordered)
    # The same proportional policy works for lower configured reading limits.
    rotating_slots = min(10, max(1, limit // 5))
    recent_slots = limit - rotating_slots
    backlog = ordered[recent_slots:]
    window_count = (len(backlog) + rotating_slots - 1) // rotating_slots
    offset = (rotation_window % window_count) * rotating_slots
    rotated = [backlog[(offset + index) % len(backlog)] for index in range(rotating_slots)]
    return tuple(ordered[:recent_slots] + rotated)


class _OfferParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.title_parts: list[str] = []
        self.description_parts: list[str] = []
        self.article_parts: list[str] = []
        self.metadata: list[dict[str, str]] = []
        self.capture_title: int | None = None
        self.capture_description: int | None = None
        self.capture_article: int | None = None
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag not in _VOID_TAGS:
            self.stack.append(tag)
        depth = len(self.stack)
        if tag in {"script", "style"}:
            self.suppressed += 1
        if tag == "article" and self.capture_article is None:
            self.capture_article = depth
        if tag == "h1" and self.capture_article is not None:
            self.capture_title = depth
        if classes.intersection({"node-job-page__description__title", "node-job-page__description__content"}):
            self.capture_description = depth
            self.description_parts.append("\n")
        elif self.capture_description is not None and tag in {"p", "li", "br", "h2", "h3"}:
            self.description_parts.append("\n")
        if tag == "a" and "js-gtm-apply-job" in classes:
            self.metadata.append({
                "title": values.get("data-job-title", ""),
                "contract": values.get("data-job-contract", ""),
                "city": values.get("data-agency", ""),
                "country": values.get("data-country", ""),
                "application_url": values.get("href", ""),
            })

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or tag not in self.stack:
            return
        while self.stack:
            depth = len(self.stack)
            current = self.stack.pop()
            if current in {"script", "style"}:
                self.suppressed = max(0, self.suppressed - 1)
            if self.capture_title == depth:
                self.capture_title = None
            if self.capture_description == depth:
                self.capture_description = None
                self.description_parts.append("\n")
            if self.capture_article == depth:
                self.capture_article = None
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            if self.capture_title is not None:
                self.title_parts.append(data)
            if self.capture_description is not None:
                self.description_parts.append(data)
            if self.capture_article is not None:
                self.article_parts.append(data)


def parse_offer(html: str, detail_url: str) -> dict[str, str] | None:
    """Return confirmed Moroccan SQLI metadata, or skip a foreign/closed job."""
    parser = _OfferParser()
    parser.feed(html)
    article_text = normalize_text(" ".join(parser.article_parts)).casefold()
    if any(marker in article_text for marker in _CLOSED_MARKERS):
        return None
    if not parser.metadata:
        raise SqliPayloadError(f"SQLI job metadata is missing: {detail_url}")
    metadata = parser.metadata[0]
    if any(item != metadata for item in parser.metadata[1:]):
        raise SqliPayloadError(f"SQLI job metadata is inconsistent: {detail_url}")
    country = _fold(normalize_text(metadata["country"]))
    if not country:
        raise SqliPayloadError(f"SQLI job country is missing: {detail_url}")
    if country not in {"maroc", "morocco", "ma"}:
        return None
    title = normalize_text(" ".join(parser.title_parts))
    metadata_title = normalize_text(metadata["title"])
    if not title or not metadata_title or _fold(title) != _fold(metadata_title):
        raise SqliPayloadError(f"SQLI job title is missing or inconsistent: {detail_url}")
    description = "\n".join(normalize_text(line) for line in "".join(parser.description_parts).splitlines() if normalize_text(line))
    if not description:
        raise SqliPayloadError(f"SQLI job description is missing: {detail_url}")
    application_url = urljoin(detail_url, metadata["application_url"].strip())
    parts = urlsplit(application_url)
    if (not metadata["application_url"].strip() or parts.scheme not in {"https", "http"}
            or not parts.hostname or parts.username is not None or parts.password is not None):
        raise SqliPayloadError(f"SQLI application link is missing or invalid: {detail_url}")
    contract = normalize_text(metadata["contract"])
    if contract:
        description = f"Type de contrat : {contract}\n\n{description}"
    city = normalize_text(metadata["city"])
    return {"title": title, "location": f"{city}, Maroc" if city else "Maroc",
            "description": description, "application_url": application_url}
