"""Pure parsing for the Stagiaires.ma sitemap collector. Opens no socket.

Phase 7C.4A audited what Stagiaires.ma publicly exposes and established the
site's own discovery chain:

    robots.txt -> the Sitemap: it declares -> offre-sitemap*.xml
               -> /stage-emploi-maroc/<numeric-id>-<slug>

This module is the production half of that finding: it turns bytes the collector
fetched into rules, offer entries and candidates. Every network decision lives in
`services.collector.collectors.stagiaires`; nothing here requests anything, so
every rule below is testable without a site.

It deliberately **re-states** the audited semantics rather than importing them.
`evaluation/` is evidence infrastructure — an audit trail of what we checked and
when — and production importing it would make the runtime depend on a layer whose
whole purpose is to be free to change as evidence changes. The two are kept in
step by tests, not by an import.

Three invariants carried forward from the audit, each load-bearing:

* **`<lastmod>` is not a publication date.** It says a page changed, not that an
  offer was posted. Here it earns exactly one privilege — deciding which pages
  are refreshed first — and `SitemapOfferEntry` has no `published_at` field for
  anything to quietly assign it to;
* **the numeric offer ID is a candidate identity, not a proven key.** It is
  carried as `source_external_id` because it is the best identifier the site
  publishes, and nothing in the database keys on it;
* **absence stays absence.** A field the page does not publish becomes `None`.
  No placeholder, no value inferred from the source's own configuration, no
  organization guessed from the domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

#: The collector identifies itself honestly. No browser is impersonated and no
#: crawler is imitated: the audit that made this integration possible was run
#: under an honest agent, and the collector keeps that promise.
COLLECTOR_USER_AGENT = "OpportunityRadarAI-Collector/1.0 (+public opportunity collection; GET-only)"

#: The version of these parsing rules, reported in run metrics so a stored run
#: can be traced back to the logic that produced it.
PARSER_VERSION = "stagiaires-jsonld-v1"

#: The public hosts this collector will talk to. `www` and the bare domain are
#: both accepted as published and are not rewritten into each other: which one
#: the site considers canonical is not something we have evidence for.
STAGIAIRES_HOSTS = frozenset({"www.stagiaires.ma", "stagiaires.ma"})

#: The entry point, and the only URL in the chain that is written down. The
#: sitemap index comes from robots, and the offer sitemaps from the index.
ROBOTS_URL = "https://www.stagiaires.ma/robots.txt"

#: The public offer-detail path family.
OFFER_PATH_PREFIX = "/stage-emploi-maroc/"

#: `/stage-emploi-maroc/<digits>` with an optional `-<slug>`.
_OFFER_PATH_PATTERN = re.compile(r"^/stage-emploi-maroc/(\d+)(?:-[^/]*)?$")

#: The offer-sitemap family. A pattern, not the two filenames observed in the
#: audit: the site may publish a third the day the second fills up, and a
#: collector that hardcoded two would silently stop seeing a third of the offers.
_OFFER_SITEMAP_PATTERN = re.compile(r"^/offre-sitemap\d*\.xml$")

#: A guard against a hostile or accidental multi-megabyte document.
MAX_XML_BYTES = 20_000_000


class StagiairesCollectionError(RuntimeError):
    """Raised when Stagiaires.ma cannot be collected safely or completely."""


class StagiairesPayloadError(ValueError):
    """Raised when a Stagiaires.ma response does not follow the expected shape."""


# ---------------------------------------------------------------- robots ----


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    pattern: str


@dataclass(frozen=True)
class RobotsGroup:
    agents: tuple[str, ...]
    rules: tuple[RobotsRule, ...]


def parse_robots_txt(text: str) -> tuple[RobotsGroup, ...]:
    """Parse robots.txt into groups, tolerating comments and stray lines.

    An empty `Disallow:` imposes **no rule**. It must never become `Allow: /`:
    given `Disallow: /` followed by `Disallow:`, a synthesized allow would tie
    with the real refusal on pattern length, win the tie-break, and silently turn
    "nothing is allowed" into "everything is".
    """
    groups: list[RobotsGroup] = []
    agents: list[str] = []
    rules: list[RobotsRule] = []
    expecting_agents = False

    def flush() -> None:
        if agents:
            groups.append(RobotsGroup(tuple(agents), tuple(rules)))

    for raw_line in (text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name, value = field_name.strip().lower(), value.strip()
        if field_name == "user-agent":
            if not expecting_agents:
                flush()
                agents, rules = [], []
                expecting_agents = True
            agents.append(value.lower())
        elif field_name in {"allow", "disallow"}:
            expecting_agents = False
            if agents and value:
                rules.append(RobotsRule(allow=field_name == "allow", pattern=value))
    flush()
    return tuple(groups)


def sitemap_declarations(text: str) -> tuple[str, ...]:
    """Return the absolute `Sitemap:` URLs robots.txt declares, in file order.

    Group-independent, so it is read from the whole file. This is the collector's
    only sanctioned way to learn where the sitemap is: a URL we wrote down
    ourselves would not be "the official sitemap" in any meaningful sense.
    """
    seen: set[str] = set()
    declared: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        if field_name.strip().lower() != "sitemap":
            continue
        candidate = value.strip()
        if urlsplit(candidate).scheme not in {"http", "https"}:
            continue
        if candidate not in seen:
            seen.add(candidate)
            declared.append(candidate)
    return tuple(declared)


def product_token(user_agent: str) -> str:
    """Return the REP product token of a user-agent string, lowercased."""
    return (user_agent or "").split("/", 1)[0].strip().lower()


def robots_target(url: str) -> str:
    """Return the string a robots rule is matched against for ``url``.

    The REP matches the request path **and its query**. Matching the path alone
    ignores every rule that discriminates on a parameter — `Disallow:
    /*?download=true` would not stop `/allowed?download=true`. One helper, used
    everywhere a URL is judged, so the two cannot drift apart.
    """
    parts = urlsplit(url or "")
    target = parts.path or "/"
    return f"{target}?{parts.query}" if parts.query else target


def _rule_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    expression = "".join(
        ".*" if character == "*" else re.escape(character) for character in body
    )
    return re.compile(f"^{expression}$" if anchored else f"^{expression}")


def _agent_applies(agent: str, token: str) -> bool:
    """Exact match, or the file naming a shorter token we extend.

    Deliberately not a substring test: an unrelated group whose short name
    happened to occur inside ours could just as easily be permissive as strict.
    """
    return bool(agent) and (agent == token or token.startswith(agent))


def _applicable_rules(
    groups: tuple[RobotsGroup, ...], user_agent: str
) -> tuple[RobotsRule, ...]:
    """Every rule addressing this agent, merged across records.

    All matching groups are merged rather than only the first: a file may name
    the same agent twice, and stopping at the first record discards every rule
    after it, including a `Disallow` a later record adds. The wildcard fallback
    merges all `*` records, and applies only when no group names us.
    """
    token = product_token(user_agent)
    rules: list[RobotsRule] = []
    matched = False
    for group in groups:
        if any(_agent_applies(agent, token) for agent in group.agents):
            matched = True
            rules.extend(group.rules)
    if matched:
        return tuple(rules)
    for group in groups:
        if "*" in group.agents:
            rules.extend(group.rules)
    return tuple(rules)


def robots_allows(
    groups: tuple[RobotsGroup, ...],
    url: str,
    user_agent: str = COLLECTOR_USER_AGENT,
) -> bool:
    """Whether robots permits ``url``: longest match wins, Allow breaks ties."""
    target = robots_target(url)
    best: RobotsRule | None = None
    for rule in _applicable_rules(groups, user_agent):
        if not _rule_regex(rule.pattern).match(target):
            continue
        if (
            best is None
            or len(rule.pattern) > len(best.pattern)
            or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
        ):
            best = rule
    return True if best is None else best.allow


#: What a robots.txt response means for the rest of the run. Four outcomes, not
#: two: a robots.txt we were *refused* tells us nothing about what is allowed,
#: and reading that silence as permission is the failure this prevents.
ROBOTS_OBEY = "OBEY"
ROBOTS_ABSENT = "ABSENT"
ROBOTS_REFUSED = "REFUSED"
ROBOTS_UNRESOLVED = "UNRESOLVED"

_CHALLENGE_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "cf-challenge",
    "cf_chl",
    "attention required",
    "checking your browser",
    "access denied",
    "just a moment",
    "bot detection",
    "datadome",
)
_LOGIN_PATH_SEGMENTS = ("/login", "/signin", "/connexion", "/se-connecter")


def classify_robots_response(status_code: int, final_url: str, body: str) -> str:
    """Decide what a robots.txt response permits.

    * **200** — parse and obey, unless the response is transparently a bot wall.
    * **404 / 410** — definitively absent, so no explicit rule applies and
      collection may continue. That is a statement about robots.txt alone and
      **not** permission of any kind.
    * **401 / 403 / 407 / 429** — we were refused the file, so we do not know
      what it says. An unknown rule is never assumed permissive: collection
      fails rather than proceeding blind.
    * **5xx and anything else** — unresolved. Collection fails.
    """
    if status_code in {401, 403, 407, 429}:
        return ROBOTS_REFUSED
    if status_code in {404, 410}:
        return ROBOTS_ABSENT
    if status_code != 200:
        return ROBOTS_UNRESOLVED
    sample = (body or "")[:20000].lower()
    if any(marker in sample for marker in _CHALLENGE_MARKERS):
        return ROBOTS_REFUSED
    if any(
        segment in urlsplit(final_url or "").path.lower()
        for segment in _LOGIN_PATH_SEGMENTS
    ):
        return ROBOTS_REFUSED
    return ROBOTS_OBEY


def access_barrier(status_code: int, final_url: str, body: str) -> str | None:
    """Name the barrier a response represents, or ``None`` if it looks public.

    A barrier is reported and the run fails. Nothing here works around one.
    """
    if status_code == 403:
        return "HTTP_403_FORBIDDEN"
    if status_code == 429:
        return "HTTP_429_RATE_LIMITED"
    if status_code in {401, 407}:
        return "AUTHENTICATION_REQUIRED"
    if status_code >= 500:
        return f"HTTP_{status_code}_SERVER_ERROR"
    sample = (body or "")[:20000].lower()
    if any(marker in sample for marker in _CHALLENGE_MARKERS):
        return "BOT_CHALLENGE_OR_CAPTCHA"
    if any(
        segment in urlsplit(final_url or "").path.lower()
        for segment in _LOGIN_PATH_SEGMENTS
    ):
        return "REDIRECTED_TO_LOGIN"
    if status_code != 200:
        return f"HTTP_{status_code}"
    return None


# ------------------------------------------------------------------ URLs ----


def canonical_url(url: str) -> str:
    """Normalize a public Stagiaires.ma URL conservatively.

    Only meaningless parts are dropped: scheme and host lowercased, default port
    removed, fragment and query dropped, trailing slash trimmed. Path case is
    left as published and `www` is not rewritten to the bare domain or back.

    This is also the **persistence identity**. Opportunities are keyed on
    `(source_id, source_url)`, so this must return the same string for the same
    offer on every run — which is why the collector stores this rather than
    whatever URL a redirect happened to end on.
    """
    if not isinstance(url, str) or not url.strip():
        raise StagiairesPayloadError("URL must be a non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"}:
        raise StagiairesPayloadError(f"not an http(s) URL: {url!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise StagiairesPayloadError(f"URL has no host: {url!r}")
    if parts.port and parts.port not in {80, 443}:
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def is_stagiaires_host(url: str) -> bool:
    """Whether ``url`` points at one of the public Stagiaires.ma hosts."""
    try:
        host = urlsplit(canonical_url(url)).hostname or ""
    except StagiairesPayloadError:
        return False
    return host in STAGIAIRES_HOSTS


def extract_external_id(url: str) -> str | None:
    """Return the numeric offer ID in ``/stage-emploi-maroc/<id>-<slug>``.

    Returned as the digits the site published, not as an `int`: `007` and `7`
    are different published identities and normalizing them together would
    invent an equality the site never stated. `None` means this URL carries no
    offer ID in the known shape — a finding, never a zero.
    """
    try:
        path = urlsplit(canonical_url(url)).path
    except StagiairesPayloadError:
        return None
    match = _OFFER_PATH_PATTERN.match(path)
    return match.group(1) if match else None


# --------------------------------------------------------------- sitemaps ---


@dataclass(frozen=True)
class SitemapOfferEntry:
    """One public offer URL as the sitemap published it.

    There is no `published_at` here and there must never be one derived from
    `sitemap_lastmod`. The lastmod's only job is `freshness_key`.
    """

    source_external_id: str
    canonical_url: str
    sitemap_lastmod: str | None = None


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].strip().lower()


def _parse_xml(text: str, expected_root: str) -> ElementTree.Element:
    """Parse sitemap XML, insisting the document really is what we asked for.

    A fetch that answers with an HTML error page is not a sitemap, and parsing
    it leniently would turn a failed request into an empty-but-successful
    result. A collector reporting zero offers because it read the wrong document
    is worse than one that fails.
    """
    if len(text or "") > MAX_XML_BYTES:
        raise StagiairesPayloadError(
            f"sitemap XML exceeds the {MAX_XML_BYTES}-byte limit"
        )
    try:
        root = ElementTree.fromstring((text or "").strip())
    except ElementTree.ParseError as error:
        raise StagiairesPayloadError(f"document is not well-formed XML: {error}")
    if _local_name(root.tag) != expected_root:
        raise StagiairesPayloadError(
            f"expected a <{expected_root}> document, got <{_local_name(root.tag)}>"
        )
    return root


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            value = (child.text or "").strip()
            return value or None
    return None


def parse_sitemap_index(xml_text: str, base_url: str) -> tuple[str, ...]:
    """Read the official `<sitemapindex>` and return the offer sitemaps.

    Only same-host entries in the `offre-sitemap*.xml` family are returned, each
    once. Off-domain entries are dropped rather than followed: "the index told
    us to" is not a reason to fetch someone else's host.
    """
    root = _parse_xml(xml_text, "sitemapindex")
    offers: list[str] = []
    seen: set[str] = set()
    for node in root:
        if _local_name(node.tag) != "sitemap":
            continue
        raw = _child_text(node, "loc")
        if not raw:
            continue
        try:
            candidate = canonical_url(urljoin(base_url, raw))
        except StagiairesPayloadError:
            continue
        if candidate in seen or not is_stagiaires_host(candidate):
            continue
        seen.add(candidate)
        if _OFFER_SITEMAP_PATTERN.match(urlsplit(candidate).path):
            offers.append(candidate)
    return tuple(offers)


def parse_offer_sitemap(xml_text: str, base_url: str) -> tuple[SitemapOfferEntry, ...]:
    """Read one `<urlset>` into offer entries, dropping what is not an offer.

    `<lastmod>` is carried through as the string the site published: not parsed
    here, not compared to anything, and above all not turned into a publication
    date. Off-domain, wrong-path-family and malformed-ID rows are rejected — a
    URL we cannot identify is not an offer we can collect.
    """
    root = _parse_xml(xml_text, "urlset")
    entries: list[SitemapOfferEntry] = []
    for node in root:
        if _local_name(node.tag) != "url":
            continue
        raw = _child_text(node, "loc")
        if not raw:
            continue
        lastmod = _child_text(node, "lastmod")
        try:
            candidate = canonical_url(urljoin(base_url, raw))
        except StagiairesPayloadError:
            continue
        if not is_stagiaires_host(candidate):
            continue
        if not urlsplit(candidate).path.startswith(OFFER_PATH_PREFIX):
            continue
        external_id = extract_external_id(candidate)
        if external_id is None:
            continue
        entries.append(
            SitemapOfferEntry(
                source_external_id=external_id,
                canonical_url=candidate,
                sitemap_lastmod=lastmod,
            )
        )
    return tuple(entries)


def deduplicate_entries(
    entries: tuple[SitemapOfferEntry, ...],
) -> tuple[SitemapOfferEntry, ...]:
    """Collapse the same canonical URL listed in more than one sitemap file."""
    seen: set[str] = set()
    unique: list[SitemapOfferEntry] = []
    for entry in entries:
        if entry.canonical_url in seen:
            continue
        seen.add(entry.canonical_url)
        unique.append(entry)
    return tuple(unique)


def parse_lastmod(value: str | None) -> datetime | None:
    """Read an ISO-8601 sitemap `<lastmod>`, or return ``None``.

    Conservative on purpose. A value we cannot read is not repaired and not
    replaced with "now": it simply loses freshness priority, which costs that
    entry a place in the queue and invents nothing. Naive timestamps are read as
    UTC so that all entries compare on one scale.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.combine(
                date.fromisoformat(text), datetime.min.time(), tzinfo=timezone.utc
            )
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def freshness_key(entry: SitemapOfferEntry) -> tuple[int, float, str]:
    """Order offers for fetching: freshest first, deterministically.

    `<lastmod>` earns exactly one privilege here — deciding which pages a
    bounded run refreshes first — and never becomes data. Entries whose lastmod
    is absent or unreadable sort after every dated entry rather than being given
    a fabricated timestamp, and both groups break ties on canonical URL so the
    order never depends on sitemap file order or dictionary iteration.

    The numeric ID is deliberately absent from this key: a larger ID is not
    evidence of a later publication, and using it as one would smuggle a recency
    claim the audit refused to make.
    """
    moment = parse_lastmod(entry.sitemap_lastmod)
    if moment is None:
        return (1, 0.0, entry.canonical_url)
    return (0, -moment.timestamp(), entry.canonical_url)


def select_detail_targets(
    entries: tuple[SitemapOfferEntry, ...], limit: int
) -> tuple[SitemapOfferEntry, ...]:
    """Choose which offers this bounded run will fetch in full."""
    return tuple(sorted(deduplicate_entries(entries), key=freshness_key)[:limit])


# ------------------------------------------------------------ detail page ---


def normalize_text(value: object) -> str:
    """Collapse whitespace in a string, returning '' for anything else."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


#: Words that mark a control as an *application action*. French first, because
#: the site is Moroccan and francophone.
APPLICATION_INTENT_MARKERS = (
    "postuler",
    "postulez",
    "postule",
    "candidater",
    "candidature",
    "apply",
)

_VOID_ELEMENTS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)


class _DetailCollector(HTMLParser):
    """Collect JSON-LD blocks and the page's interactive controls.

    Deliberately site-agnostic: it knows about `<script type="application/ld+json">`
    and about anchors and buttons, mechanisms any page may use, and knows nothing
    about Stagiaires.ma's own class names. A collector built on generated CSS
    classes breaks the next time the frontend is rebuilt; one built on standard
    markup does not.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        #: (element text, aria-label/title, href or None) per control.
        self.controls: list[tuple[str, str, str | None]] = []
        self._in_jsonld = False
        self._buffer: list[str] = []
        self._control_stack: list[list[Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if name == "script" and "ld+json" in attributes.get("type", "").lower():
            self._in_jsonld = True
            self._buffer = []
        elif name in {"a", "button"}:
            self._control_stack.append(
                [
                    attributes.get("aria-label", "").strip()
                    or attributes.get("title", "").strip(),
                    attributes.get("href", "").strip() or None,
                    [],
                ]
            )
        elif name == "input" and attributes.get("type", "").lower() in {
            "submit",
            "button",
        }:
            # Void, so it has no text: its label is the value or aria-label.
            label = (
                attributes.get("value", "").strip()
                or attributes.get("aria-label", "").strip()
            )
            if label:
                self.controls.append(("", normalize_text(label), None))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "script" and self._in_jsonld:
            self._in_jsonld = False
            if (payload := "".join(self._buffer).strip()):
                self.jsonld.append(payload)
        elif name in {"a", "button"} and self._control_stack:
            labelled, href, parts = self._control_stack.pop()
            self.controls.append(
                (normalize_text("".join(parts)), normalize_text(labelled), href)
            )

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._buffer.append(data)
        elif self._control_stack:
            self._control_stack[-1][2].append(data)


def _iter_nodes(value: object) -> list[dict[str, Any]]:
    """Walk JSON-LD, following lists and `@graph`, yielding object nodes."""
    found: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_iter_nodes(item))
    elif isinstance(value, dict):
        found.append(value)
        for key in ("@graph", "itemListElement", "item"):
            if key in value:
                found.extend(_iter_nodes(value[key]))
    return found


def _node_types(node: dict[str, Any]) -> list[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


def job_posting(html: str) -> dict[str, Any] | None:
    """Return the page's first schema.org `JobPosting` node, or ``None``."""
    collector = _DetailCollector()
    collector.feed(html or "")
    for block in collector.jsonld:
        try:
            document = json.loads(block)
        except ValueError:
            # One malformed block does not condemn the page; a later block may
            # still carry the posting.
            continue
        for node in _iter_nodes(document):
            if "JobPosting" in _node_types(node):
                return node
    return None


def _dig(node: object, *path: str) -> object:
    """Follow a key path through JSON-LD, stepping into the first list item."""
    current = node
    for key in path:
        if isinstance(current, list):
            current = current[0] if current else None
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, list):
        current = current[0] if current else None
    return current


def _text_or_none(value: object) -> str | None:
    text = normalize_text(value)
    return text or None


def posting_title(posting: dict[str, Any]) -> str | None:
    return _text_or_none(posting.get("title"))


def posting_organization(posting: dict[str, Any]) -> str | None:
    """The employer the posting names — never inferred from the board or domain.

    Stagiaires.ma publishes other people's vacancies. Falling back to the site's
    own name would record "Stagiaires.ma" as the employer of every offer, which
    is not a missing value but a wrong one.
    """
    return _text_or_none(_dig(posting, "hiringOrganization", "name"))


def posting_location(posting: dict[str, Any]) -> str | None:
    """Structured location only: locality, else region, else country.

    Nothing is derived from the source's configured country. `country: MA` is a
    statement about the source, not about where a particular internship is.
    """
    address = _dig(posting, "jobLocation", "address")
    for key in ("addressLocality", "addressRegion", "addressCountry"):
        if (value := _text_or_none(_dig(address, key) if address else None)):
            return value
    return None


def posting_description(posting: dict[str, Any]) -> str | None:
    return _text_or_none(posting.get("description"))


def posting_published_at(posting: dict[str, Any]) -> str | None:
    """The posting's own `datePosted`, and nothing else.

    Never the sitemap's `<lastmod>`, which says a page changed rather than that
    an offer was published. A page that states no date yields `None`.
    """
    return _text_or_none(posting.get("datePosted"))


def application_action_url(html: str, base_url: str) -> str | None:
    """The href of an explicit application control, or ``None``.

    Deliberately not `JobPosting.url`, `itemprop="url"` or `og:url`: those name
    the posting — the page we are already on — and every offer has a canonical
    URL, so reading one as an application link would claim an application route
    for every offer ever published, including ones whose only route is an email
    address or which have already closed.

    What counts is a control that *says* it applies. A relative href resolves
    against the detail page. The result may point off-domain at an employer's
    ATS: that is recorded, and the collector never requests it. A control with
    only a `javascript:` or fragment href yields `None` rather than a URL that
    would not work.
    """
    collector = _DetailCollector()
    collector.feed(html or "")
    for text, labelled, href in collector.controls:
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            continue
        if not any(
            marker in name.lower()
            for name in (text, labelled)
            if name
            for marker in APPLICATION_INTENT_MARKERS
        ):
            continue
        resolved = urljoin(base_url, href)
        if urlsplit(resolved).scheme not in {"http", "https"}:
            continue
        return resolved
    return None


__all__ = [
    "APPLICATION_INTENT_MARKERS",
    "COLLECTOR_USER_AGENT",
    "MAX_XML_BYTES",
    "OFFER_PATH_PREFIX",
    "PARSER_VERSION",
    "ROBOTS_ABSENT",
    "ROBOTS_OBEY",
    "ROBOTS_REFUSED",
    "ROBOTS_UNRESOLVED",
    "ROBOTS_URL",
    "STAGIAIRES_HOSTS",
    "RobotsGroup",
    "RobotsRule",
    "SitemapOfferEntry",
    "StagiairesCollectionError",
    "StagiairesPayloadError",
    "access_barrier",
    "application_action_url",
    "canonical_url",
    "classify_robots_response",
    "deduplicate_entries",
    "extract_external_id",
    "freshness_key",
    "is_stagiaires_host",
    "job_posting",
    "normalize_text",
    "parse_lastmod",
    "parse_offer_sitemap",
    "parse_robots_txt",
    "parse_sitemap_index",
    "posting_description",
    "posting_location",
    "posting_organization",
    "posting_published_at",
    "posting_title",
    "product_token",
    "robots_allows",
    "robots_target",
    "select_detail_targets",
    "sitemap_declarations",
]
