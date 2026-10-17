"""Conservative, network-free parsing of official Indeed job-alert emails.

Indeed documents ``alert@indeed.com`` as its alert sender:
https://support.indeed.com/hc/en-nz/articles/204288824-I-m-not-receiving-alerts
Only explicit job keys are used; CTS payloads are decoded locally and no
tracking link is followed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import getaddresses
from html import unescape
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlsplit

from services.collector.models.gmail_message import GmailMessageCandidate
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.indeed_links import unwrap_indeed_url

INDEED_JOB_ALERT_SOURCE_ID = "indeed_job_alert_email"
PARSER_VERSION = "indeed-alert-email-v2"
INDEED_ALERT_SENDERS = frozenset({
    "alert@indeed.com",
    "donotreply@jobalert.indeed.com",
    "donotreply@match.indeed.com",
})
_ALLOWED_HOSTS = frozenset({"ma.indeed.com", "www.indeed.com", "indeed.com"})
_JOB_PATH_KEYS = {
    "/viewjob": "jk",
    "/rc/clk": "jk",
    "/pagead/clk": "jk",
    "/jobs": "vjk",
}
_JOB_KEY = re.compile(r"[0-9a-fA-F]{16}")
_TEXT_URL = re.compile(r"https?://[^\s<>\]\[()]+")
_BLOCK_TAGS = frozenset({"article", "div", "li", "table", "td", "tr", "section"})
_LINE_TAGS = frozenset({"br", "p", "div", "li", "table", "td", "tr", "h1", "h2", "h3", "h4"})
_IGNORED_TAGS = frozenset({"script", "style", "head", "title"})
_UI_TEXT = re.compile(
    r"^(?:apply(?: now)?|apply on indeed|postuler(?: maintenant)?|candidater|"
    r"voir (?:l['’]offre|le poste|les offres|toutes les offres)|"
    r"view (?:job|jobs|all jobs|job details)|see (?:job|all jobs)|"
    r"en savoir plus|learn more|save(?: job)?|enregistrer(?: l['’]offre)?|"
    r"unsubscribe|se désabonner|désabonnement|manage (?:alerts|preferences)|"
    r"gérer (?:mes |les )?(?:alertes|préférences)|job alerts?|alertes? emploi|"
    r"indeed|nouveau|new|sponsored|sponsorisé|recrutement actif|actively hiring|"
    r"vous désinscrire(?: ici)?|politique de confidentialité|privacy policy|"
    r"conditions d['’]utilisation|terms of (?:use|service)|centre d['’]aide|"
    r"help cent(?:er|re)|this is a bad match)"
    r"[\s.!›»→]*$",
    re.IGNORECASE,
)
_CTA_PREFIX = re.compile(
    r"^(?:apply\b|postuler\b|candidater\b|"
    r"(?:view|see) (?:this |the |full |all |your |more )?(?:\d+ )?(?:jobs?|matches|opportunities)\b|"
    r"(?:voir|afficher|consulter) (?:cette |ce |cet |ces |l['’]|le |les |toutes? les |plus d['’])?"
    r"(?:offres?|postes?|emplois?)\b)",
    re.IGNORECASE,
)
_CONTACT_TEXT = re.compile(
    r"(?:[^\s@]+@[^\s@]+|\b(?:mailto|tel):|"
    r"^(?:contact(?:ez)?|email|e-mail|courriel|téléphone|telephone|phone|"
    r"sent to|envoyé à|envoyée à|destinataire)\s*[:：])",
    re.IGNORECASE,
)
_NON_COMPANY = re.compile(
    r"(?:^\d[\d.,\s]*(?:/\s*5|étoiles?|stars?)?$|"
    r"^(?:CDI|CDD|stage|internship|full[- ]time|part[- ]time|temps plein|temps partiel)$|"
    r"^(?:il y a|posted|salary|salaire)\b|[€$£]|\b(?:MAD|DH|DHS|USD|EUR)\b)",
    re.IGNORECASE,
)
_NON_LOCATION = re.compile(
    r"(?:\b(?:salaire|salary|salaires|salaires? estimés?|reviews?|avis|"
    r"ratings?|étoiles?|full[- ]time|part[- ]time|temps plein|temps partiel|"
    r"CDI|CDD|internship|stage|contract|contrat|benefits|avantages)\b|"
    r"\b(?:il y a|posted|publiée?|publié|urgent|nouveau|new)\b|"
    r"[€$£]|\b(?:MAD|DH|DHS|USD|EUR)\b|^\d[\d.,\s]*$)",
    re.IGNORECASE,
)


class IndeedJobAlertParseError(ValueError):
    """An identifiable alert has a card layout we cannot safely extract."""


def _clean(value: str) -> str:
    return " ".join(unescape(value).split())


def _is_trusted_sender(sender: str | None) -> bool:
    addresses = getaddresses([sender or ""])
    return len(addresses) == 1 and addresses[0][1].lower() in INDEED_ALERT_SENDERS


def _job_identity(url: str) -> tuple[str, str] | None:
    """Accept a single explicit hexadecimal job key on an allowlisted host."""
    value = unwrap_indeed_url(url)
    if value is None:
        return None
    if re.search(r"[\x00-\x20\x7f]", value):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or host not in _ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            return None
        key_name = _JOB_PATH_KEYS.get(parsed.path.removesuffix("/"))
        if key_name is None:
            return None
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        return None
    values = query.get(key_name, [])
    # Duplicate or conflicting identity parameters are not a safe identity.
    other_key = "vjk" if key_name == "jk" else "jk"
    if len(values) != 1 or other_key in query or not _JOB_KEY.fullmatch(values[0]):
        return None
    job_key = values[0].lower()
    # The database also keys jobs by source_url. One canonical host keeps a job
    # stable across Moroccan and global variants of successive alert emails.
    # This is a URL identity choice; it provides no evidence about job location.
    return job_key, f"https://ma.indeed.com/viewjob?jk={job_key}"


@dataclass
class _Node:
    tag: str
    parent: _Node | None
    attrs: dict[str, str] = field(default_factory=dict)
    parts: list[str | _Node] = field(default_factory=list)


def _hidden(node: _Node) -> bool:
    return (
        node.tag in _IGNORED_TAGS
        or "hidden" in node.attrs
        or node.attrs.get("aria-hidden", "").lower() == "true"
        or bool(re.search(r"display\s*:\s*none", node.attrs.get("style", ""), re.IGNORECASE))
    )


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", None)
        self.current = self.root
        self.anchors: list[_Node] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), self.current, {key.lower(): value or "" for key, value in attrs})
        self.current.parts.append(node)
        if node.tag == "a":
            self.anchors.append(node)
        if node.tag not in {"area", "base", "br", "hr", "img", "input", "link", "meta", "wbr"}:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.current.tag == tag.lower():
            self.current = self.current.parent or self.root

    def handle_endtag(self, tag: str) -> None:
        cursor = self.current
        while cursor is not self.root:
            if cursor.tag == tag.lower():
                self.current = cursor.parent or self.root
                return
            cursor = cursor.parent or self.root

    def handle_data(self, data: str) -> None:
        self.current.parts.append(data)


def _text(node: _Node) -> str:
    if _hidden(node):
        return ""
    return _clean(" ".join(part if isinstance(part, str) else _text(part) for part in node.parts))


def _visible(node: _Node) -> bool:
    cursor: _Node | None = node
    while cursor is not None:
        if _hidden(cursor):
            return False
        cursor = cursor.parent
    return True


def _lines(node: _Node) -> list[str]:
    chunks: list[str] = []

    def walk(item: _Node) -> None:
        if _hidden(item):
            return
        for part in item.parts:
            if isinstance(part, str):
                chunks.append(part)
            elif part.tag == "a":
                # A nested span must not split the title into separate lines.
                chunks.extend(("\n", _text(part), "\n"))
            else:
                if part.tag in _LINE_TAGS:
                    chunks.append("\n")
                walk(part)
                if part.tag in _LINE_TAGS:
                    chunks.append("\n")

    walk(node)
    return [cleaned for line in " ".join(chunks).split("\n") if (cleaned := _clean(line))]


def _job_keys(node: _Node) -> set[str]:
    if _hidden(node):
        return set()
    keys: set[str] = set()
    if node.tag == "a" and (identity := _job_identity(node.attrs.get("href", ""))):
        keys.add(identity[0])
    for part in node.parts:
        if isinstance(part, _Node):
            keys.update(_job_keys(part))
    return keys


def _content_label(value: str, *, maximum: int = 200) -> bool:
    return bool(
        value
        and len(value) <= maximum
        and any(character.isalpha() for character in value)
        and not _UI_TEXT.fullmatch(value)
        and not _CTA_PREFIX.search(value)
        and not _CONTACT_TEXT.search(value)
        and not _TEXT_URL.search(value)
    )


def _location(value: str) -> str | None:
    return value if _content_label(value, maximum=160) and not _NON_LOCATION.search(value) else None


def _company(value: str) -> bool:
    return _content_label(value) and not _NON_COMPANY.search(value)


def _semantic_values(node: _Node, field_name: str) -> list[str]:
    """Use explicit company/location markup, when present, within this card."""
    tokens = {
        "company": {"company", "companyname", "company-name", "company_name", "employer"},
        "location": {"location", "joblocation", "job-location", "job_location", "companylocation"},
        "title": {"jobtitle", "job-title", "job_title", "jcs-jobtitle"},
    }[field_name]
    values: list[str] = []
    if _hidden(node):
        return values
    identifiers = " ".join(node.attrs.get(name, "") for name in ("class", "id", "itemprop", "data-testid"))
    if tokens.intersection(identifiers.lower().split()):
        if value := _text(node):
            values.append(value)
        return values
    for part in node.parts:
        if isinstance(part, _Node):
            values.extend(_semantic_values(part, field_name))
    return list(dict.fromkeys(values))


def _title(anchor: _Node) -> str:
    titles = _semantic_values(anchor, "title")
    if len(titles) == 1:
        return titles[0]
    if titles or _semantic_values(anchor, "company") or _semantic_values(anchor, "location"):
        # A whole-card link is not itself a title. Keep its explicit job-title
        # field when available; otherwise leave the unsupported card visible.
        return ""
    return _text(anchor)


def _metadata(node: _Node, title: str) -> tuple[str, str | None] | None:
    companies = _semantic_values(node, "company")
    locations = _semantic_values(node, "location")
    if len(companies) > 1 or len(locations) > 1:
        return None
    if companies:
        company = companies[0]
        if not _company(company) or company == title:
            return None
        return company, _location(locations[0]) if locations else None
    if locations:
        # A labelled location cannot become the employer in a partial wrapper.
        return None

    lines = _lines(node)
    # Unlabelled metadata is accepted only in a compact card. A larger wrapper
    # may also contain a subject/header, snippets or footer contact information;
    # scanning through those would invent a company from unrelated content.
    if not lines or lines[0] != title:
        return None
    following = lines[1:]
    # Skip only a duplicate title, never arbitrary intervening text: doing so
    # could attach a location, footer or another field as the company.
    while following and following[0] == title:
        following = following[1:]
    while following and (_UI_TEXT.fullmatch(following[-1]) or _CTA_PREFIX.search(following[-1])):
        following = following[:-1]
    if len(following) not in {1, 2} or not _company(following[0]):
        return None
    company = following[0]
    location = _location(following[1]) if len(following) > 1 else None
    # Some alert cards explicitly combine the two fields on a single line.
    if company.count("·") == 1:
        company, possible_location = map(_clean, company.split("·", maxsplit=1))
        if not _company(company):
            return None
        location = _location(possible_location)
    return company, location


def _from_html(body: str) -> tuple[list[tuple[str, str, str, str, str | None]], set[str]]:
    parser = _TreeParser()
    parser.feed(body)
    found: list[tuple[str, str, str, str, str | None]] = []
    identified_keys: set[str] = set()
    for anchor in parser.anchors:
        identity = _job_identity(anchor.attrs.get("href", ""))
        if not identity and _visible(anchor):
            # Observed alert templates identify their title links by these
            # containers. An opaque title link must not disappear silently
            # when another card in the same email happens to be decodable.
            parent = anchor.parent
            title_container = bool(parent and (
                (parent.tag == "td" and "r-l" in parent.attrs.get("class", "").split())
                or parent.tag in {"h1", "h2", "h3"}
            ))
            try:
                host = urlsplit(anchor.attrs.get("href", "")).hostname
            except ValueError:
                host = None
            if title_container and host in {"cts.indeed.com", "engage.indeed.com"}:
                identified_keys.add("unsupported-job-card")
        if not identity or not _visible(anchor):
            continue
        identified_keys.add(identity[0])
        title = _title(anchor)
        if not _content_label(title) or _CTA_PREFIX.search(title):
            continue
        blocks: list[_Node] = []
        node = anchor.parent
        while node is not None and node.tag != "document":
            if node.tag in _BLOCK_TAGS:
                keys = _job_keys(node)
                if keys != {identity[0]}:
                    # A broader parent also contains the other jobs. Stop here.
                    break
                blocks.append(node)
            node = node.parent
        metadata: tuple[str, str | None] | None = None
        # Prefer explicit fields from the complete single-job card. The nearest
        # title/company wrapper may omit a location held in a sibling element.
        for block in blocks:
            if not _semantic_values(block, "company"):
                continue
            possible = _metadata(block, title)
            if possible and (metadata is None or possible[0] == metadata[0]):
                metadata = possible
                if possible[1] is not None:
                    break
        if metadata is None:
            for block in blocks:
                possible = _metadata(block, title)
                if possible and (metadata is None or possible[0] == metadata[0]):
                    metadata = possible
                    if possible[1] is not None:
                        break
        if metadata is not None:
            found.append((identity[0], identity[1], title, *metadata))
    return found, identified_keys


def _from_text(body: str) -> tuple[list[tuple[str, str, str, str, str | None]], set[str]]:
    """Accept only a blank-delimited Title/Company[/Location]/URL card."""
    found: list[tuple[str, str, str, str, str | None]] = []
    identified_keys = {
        identity[0]
        for match in _TEXT_URL.finditer(body)
        if (identity := _job_identity(match.group(0)))
    }
    for block in re.split(r"\n\s*\n", body):
        lines = [_clean(line) for line in block.splitlines() if _clean(line)]
        if len(lines) not in {3, 4}:
            continue
        if not all(_content_label(line) for line in lines[:-1]):
            continue
        title, company = lines[:2]
        if _CTA_PREFIX.search(title) or not _company(company):
            continue
        url = lines[-1].strip("<>")
        identity = _job_identity(url)
        if not identity:
            try:
                host = urlsplit(url).hostname
            except ValueError:
                host = None
            if host in {"cts.indeed.com", "engage.indeed.com"}:
                identified_keys.add("unsupported-job-card")
            continue
        location = _location(lines[2]) if len(lines) == 4 else None
        found.append((identity[0], identity[1], title, company, location))
    return found, identified_keys


def parse_indeed_job_alert(message: GmailMessageCandidate) -> list[OpportunityCandidate]:
    """Extract structurally associated jobs without I/O or personal email data."""
    if not _is_trusted_sender(message.sender):
        return []
    extracted, identified_keys = _from_html(message.body_html or "")
    if message.body_text:
        text_records, text_keys = _from_text(message.body_text)
        identified_keys.update(text_keys)
        # Text may rescue an unsupported HTML card, but never overwrite a card
        # whose HTML fields were already extracted (including any ambiguity).
        html_keys = {record[0] for record in extracted}
        extracted.extend(record for record in text_records if record[0] not in html_keys)
    # Conflicting cards for one job key are ambiguous; do not merge their fields.
    unique: dict[str, tuple[str, str, str, str, str | None]] = {}
    ambiguous: set[str] = set()
    for record in extracted:
        job_key, _, title, company, location = record
        previous = unique.get(job_key)
        if previous and (
            previous[2:4] != (title, company)
            or (previous[4] is not None and location is not None and previous[4] != location)
        ):
            ambiguous.add(job_key)
        elif previous is None or (previous[4] is None and location is not None):
            unique[job_key] = record
    candidates = [
        OpportunityCandidate(
            source_id=INDEED_JOB_ALERT_SOURCE_ID,
            source_external_id=job_key,
            canonical_title=title,
            organization=company,
            location=location,
            description=None,
            published_at=None,
            source_url=canonical,
            application_url=canonical,
            canonical_url=canonical,
        )
        for job_key, canonical, title, company, location in unique.values()
        if job_key not in ambiguous
    ]
    accepted_keys = {candidate.source_external_id for candidate in candidates}
    if identified_keys - accepted_keys:
        raise IndeedJobAlertParseError(
            "Indeed alert contains job links without safely identifiable job cards; "
            "its email layout may require an update (partial collection refused)"
        )
    if not candidates:
        raise IndeedJobAlertParseError(
            "Indeed alert contains no supported job cards; direct job links and "
            "a supported email layout are required"
        )
    return candidates
