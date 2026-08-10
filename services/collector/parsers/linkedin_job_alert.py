"""Conservative, network-free parsing of LinkedIn job-alert emails."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parseaddr
from html import unescape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from services.collector.models.gmail_message import GmailMessageCandidate
from services.collector.models.opportunity import OpportunityCandidate

LINKEDIN_JOB_ALERT_SOURCE_ID = "linkedin_job_alert_email"
_JOB_PATH = re.compile(r"^/(?:comm/)?jobs/view/(\d+)(?:/)?$")
_TEXT_URL = re.compile(r"https?://[^\s<>\]\[()]+")
_BLOCK_TAGS = frozenset({"article", "div", "li", "table", "td", "tr"})
_LINE_TAGS = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"})
_NON_LOCATION = re.compile(
    r"(?:\brelations?\b|\bconnections?\b|^recrutement actif$|^actively recruiting$)",
    re.IGNORECASE,
)


def _clean(value: str) -> str:
    return " ".join(unescape(value).split())


def _is_plausible_location(value: str | None) -> bool:
    """Reject compact LinkedIn UI labels that are not geographic metadata."""
    return bool(value and not _NON_LOCATION.search(_clean(value)))


def _job_identity(url: str) -> tuple[str, str] | None:
    """Return a numeric job id and canonical URL for an explicit LinkedIn URL."""
    try:
        parsed = urlsplit(unescape(url).strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"linkedin.com", "www.linkedin.com"}:
        return None
    match = _JOB_PATH.fullmatch(parsed.path)
    if not match:
        return None
    job_id = match.group(1)
    return job_id, f"https://www.linkedin.com/jobs/view/{job_id}"


@dataclass
class _Node:
    tag: str
    parent: _Node | None
    attrs: dict[str, str] = field(default_factory=dict)
    parts: list[str | _Node] = field(default_factory=list)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", None)
        self.current = self.root
        self.anchors: list[_Node] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), self.current, {k.lower(): v or "" for k, v in attrs})
        self.current.parts.append(node)
        if node.tag == "a":
            self.anchors.append(node)
        if node.tag not in {"area", "base", "br", "hr", "img", "input", "link", "meta"}:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.current.tag == tag.lower():
            self.current = self.current.parent or self.root

    def handle_endtag(self, tag: str) -> None:
        cursor: _Node | None = self.current
        while cursor is not None and cursor is not self.root:
            if cursor.tag == tag.lower():
                self.current = cursor.parent or self.root
                return
            cursor = cursor.parent

    def handle_data(self, data: str) -> None:
        self.current.parts.append(data)


def _text(node: _Node) -> str:
    values: list[str] = []
    for part in node.parts:
        values.append(part if isinstance(part, str) else _text(part))
    return _clean(" ".join(values))


def _lines(node: _Node) -> list[str]:
    chunks: list[str] = []

    def walk(item: _Node) -> None:
        for part in item.parts:
            if isinstance(part, str):
                if cleaned := _clean(part):
                    chunks.append(cleaned)
            else:
                if part.tag in _LINE_TAGS:
                    chunks.append("\n")
                walk(part)
                if part.tag in _LINE_TAGS:
                    chunks.append("\n")

    walk(node)
    return [_clean(line) for line in " ".join(chunks).split("\n") if _clean(line)]


def _job_links(node: _Node) -> int:
    count = 0
    for part in node.parts:
        if isinstance(part, _Node):
            if part.tag == "a" and _job_identity(part.attrs.get("href", "")):
                count += 1
            count += _job_links(part)
    return count


def _single_job_block(anchor: _Node) -> _Node | None:
    """Find the nearest structural block containing this job and no other job."""
    node = anchor.parent
    while node is not None and node.tag != "document":
        if node.tag in _BLOCK_TAGS and _job_links(node) == 1:
            lines = _lines(node)
            if len(lines) >= 2:
                return node
        node = node.parent
    return None


def _metadata(block: _Node, title: str) -> tuple[str, str | None] | None:
    lines = _lines(block)
    try:
        title_index = lines.index(title)
    except ValueError:
        return None
    following = [line for line in lines[title_index + 1 :] if not _TEXT_URL.fullmatch(line)]
    following = [line for line in following if line != title]
    if not following:
        return None
    organization = following[0]
    location: str | None = None
    # Real alerts can render the company and geography in one metadata line.
    # Split only this structurally associated line, and only one separator.
    if organization.count("·") == 1:
        possible_organization, possible_location = map(
            _clean, organization.split("·", maxsplit=1)
        )
        if possible_organization and possible_location:
            organization = possible_organization
            if _is_plausible_location(possible_location):
                location = possible_location
    elif len(following) > 1 and _is_plausible_location(following[1]):
        location = following[1]
    return organization, location


def _from_html(body: str) -> list[tuple[str, str, str, str, str | None]]:
    parser = _TreeParser()
    parser.feed(body)
    found: list[tuple[str, str, str, str, str | None]] = []
    for anchor in parser.anchors:
        email_url = unescape(anchor.attrs.get("href", "")).strip()
        identity = _job_identity(email_url)
        title = _text(anchor)
        if not identity or not title:
            continue
        block = _single_job_block(anchor)
        metadata = _metadata(block, title) if block else None
        if metadata:
            # Never propagate email tracking parameters or tokens.
            found.append((identity[0], identity[1], identity[1], title, *metadata))
    return found


def _from_text(body: str) -> list[tuple[str, str, str, str, str | None]]:
    """Parse only an explicit Title/Company[/Location]/URL line sequence."""
    lines = [_clean(line) for line in body.splitlines() if _clean(line)]
    found: list[tuple[str, str, str, str, str | None]] = []
    for index, line in enumerate(lines):
        match = _TEXT_URL.fullmatch(line)
        identity = _job_identity(match.group(0)) if match else None
        if not identity or index < 2:
            continue
        title, organization = lines[index - 2 : index]
        if _TEXT_URL.search(title) or _TEXT_URL.search(organization):
            continue
        found.append((identity[0], identity[1], identity[1], title, organization, None))
    return found


def parse_linkedin_job_alert(message: GmailMessageCandidate) -> list[OpportunityCandidate]:
    """Return confidently identified jobs, without I/O, logging, or persistence."""
    address = parseaddr(message.sender or "")[1]
    domain = address.rpartition("@")[2].lower().rstrip(".")
    if domain != "linkedin.com":
        return []
    extracted = _from_html(message.body_html) if message.body_html else _from_text(message.body_text or "")
    candidates: list[OpportunityCandidate] = []
    seen: set[str] = set()
    for job_id, canonical, source_url, title, organization, location in extracted:
        if job_id in seen:
            continue
        seen.add(job_id)
        candidates.append(OpportunityCandidate(
            source_id=LINKEDIN_JOB_ALERT_SOURCE_ID,
            source_external_id=job_id,
            canonical_title=title,
            organization=organization,
            location=location,
            description=None,
            published_at=None,
            source_url=source_url,
            application_url=canonical,
            canonical_url=canonical,
        ))
    return candidates
