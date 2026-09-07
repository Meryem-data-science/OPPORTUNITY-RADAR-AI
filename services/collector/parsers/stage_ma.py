"""Pure parsing for the Stage.ma collector. Opens no socket.

Phase 7C.5A audited what Stage.ma publicly exposes and found exactly one
browser-free discovery surface: the Informatique specialty page. This module is
the production half of that finding — it turns bytes the collector fetched into
listing entries and candidates, and every network decision lives in
`services.collector.collectors.stage_ma`.

It **re-states** the audited semantics rather than importing them.
`evaluation/morocco_pfe/stage_ma_access.py` is an audit trail — a record of what
we checked and when, free to change as evidence changes — and production
depending on it would make the runtime hostage to a layer whose job is to move.
The two are kept in step by tests, not by an import.

What the audit found, and what this module therefore has to be careful about:

* **the listing mixes named and deliberately anonymous employers.** An offer
  whose employer is "Anonyme" is not a broken page, and an organization is not
  something to fill in — an offer we cannot attribute is skipped, one at a time,
  while a *structural* failure still fails the whole source;
* **only 1 of 3 sampled detail pages carried a usable organization**, so the
  listing card is a real fallback rather than a nicety;
* **the numeric ID in `/offres-stage/<id>-<slug>` is a candidate identity.** It
  is never a date, a sort key, or evidence of freshness;
* **a displayed date is not a publication date.** `datePosted` is the only
  source of `published_at`, and epoch-zero and malformed values become `None`
  rather than a timestamp nobody published.

Card scoping is the other load-bearing idea. Organization and location must come
from the *same offer card*, and "card" is derived from the document's own
structure — the largest ancestor of an offer link that still contains exactly one
**distinct offer identity**, one canonical offer URL — never from CSS class
names, which are generated and would break on the next redesign.

Distinct identity, not link count, is the invariant, and the real listing is why:
it gives each offer both a title anchor and a "+ Voir Offre de Stage" anchor
pointing at the same URL, so a card holding two anchors is ordinarily still a
card holding one offer. Counting anchors made such a card look like two offers
and collapsed its boundary onto the anchor itself, which left every employer
outside its own card. Climbing therefore stops only when an ancestor contains two
or more distinct offer identities — and an organization anchor sitting in such a
container belongs to no card and is used for neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

#: The collector identifies itself honestly. No browser is impersonated: the
#: audit that made this integration possible ran under an honest agent, and the
#: collector keeps that promise.
COLLECTOR_USER_AGENT = (
    "OpportunityRadarAI-Collector/1.0 (+public opportunity collection; GET-only)"
)

#: The version of these parsing rules, reported in run metrics so a stored run
#: can be traced back to the logic that produced it.
PARSER_VERSION = "stage-ma-html-v1"

#: The public hosts this collector will talk to. `www` and the bare domain are
#: both accepted as published and are not rewritten into each other.
STAGE_MA_HOSTS = frozenset({"www.stage.ma", "stage.ma"})

ROBOTS_URL = "https://www.stage.ma/robots.txt"

#: The **only** discovery surface, frozen by the 7C.5A evidence. The homepage and
#: the generic /offres-stage listing were both reached and both exposed zero
#: offer links in ordinary server HTML; this page exposed ten. Coverage is
#: therefore the Informatique specialty and nothing else — deliberately not all
#: of Stage.ma — and no sitemap is guessed, because robots declares none.
LISTING_URL = "https://www.stage.ma/specialites/computer-science"

OFFER_PATH_PREFIX = "/offres-stage/"

#: `/offres-stage/<digits>` with an optional `-<slug>`.
_OFFER_PATH_PATTERN = re.compile(r"^/offres-stage/(\d+)(?:-[^/]*)?$")

#: `/organismes/<digits>-<slug>`: an employer profile. Its anchor *text* is
#: listing-organization evidence; the page itself is never fetched.
_ORGANISATION_PATH_PATTERN = re.compile(r"^/organismes/(\d+)(?:-[^/]*)?$")


class StageMaCollectionError(RuntimeError):
    """Raised when Stage.ma cannot be collected safely or completely."""


class StageMaPayloadError(ValueError):
    """Raised when a Stage.ma response does not follow the expected shape."""


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


def product_token(user_agent: str) -> str:
    """Return the REP product token of a user-agent string, lowercased."""
    return (user_agent or "").split("/", 1)[0].strip().lower()


def robots_target(url: str) -> str:
    """Return the string a robots rule is matched against: path **and** query.

    Matching the path alone ignores every rule keyed on a parameter, so
    `Disallow: /*?print=1` would not stop `/offres-stage/1-a?print=1`. One
    helper, used everywhere a URL is judged, so the two cannot drift apart.
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
    after it, including a `Disallow` a later record adds.
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


#: What a robots.txt response means for the run. Four outcomes rather than two:
#: a robots.txt we were *refused* tells us nothing about what is allowed, and
#: reading that silence as permission is the failure this prevents.
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
_LOGIN_PATH_SEGMENTS = ("/login", "/signin", "/connexion", "/se-connecter", "/compte")


def classify_robots_response(status_code: int, final_url: str, body: str) -> str:
    """Decide what a robots.txt response permits.

    * **200** — parse and obey, unless the response is transparently a bot wall.
    * **404 / 410** — definitively absent, so no explicit rule applies and
      collection may continue. That is a statement about robots.txt alone and is
      **not** permission of any kind, legal or otherwise.
    * **401 / 403 / 407 / 429** — we were refused the file, so we do not know
      what it says. An unknown rule is never assumed permissive: the run fails.
    * **5xx and anything else** — unresolved. The run fails.
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

    A barrier is reported, never worked around. 404/410 is deliberately absent:
    a page that is gone is a lifecycle fact, and what it means depends on which
    page it was — the caller decides, which is why this refuses to.
    """
    if status_code == 403:
        return "HTTP_403_FORBIDDEN"
    if status_code == 429:
        return "HTTP_429_RATE_LIMITED"
    if status_code in {401, 407}:
        return "AUTHENTICATION_REQUIRED"
    sample = (body or "")[:20000].lower()
    if any(marker in sample for marker in _CHALLENGE_MARKERS):
        return "BOT_CHALLENGE_OR_CAPTCHA"
    if any(
        segment in urlsplit(final_url or "").path.lower()
        for segment in _LOGIN_PATH_SEGMENTS
    ):
        return "REDIRECTED_TO_LOGIN"
    if status_code >= 500:
        return f"HTTP_{status_code}_SERVER_ERROR"
    return None


# ------------------------------------------------------------------ URLs ----


def canonical_url(url: str) -> str:
    """Normalize a public Stage.ma URL conservatively.

    Only meaningless parts are dropped: scheme and host lowercased, default port
    removed, fragment and query dropped, trailing slash trimmed. Path case is
    left as published, and `www.stage.ma` is not rewritten to `stage.ma`.

    This is also the **persistence identity**. Opportunities are keyed on
    `(source_id, source_url)`, so it must return the same string for the same
    offer on every run — which is why the collector stores this rather than
    whatever URL a redirect happened to end on.
    """
    if not isinstance(url, str) or not url.strip():
        raise StageMaPayloadError("URL must be a non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"}:
        raise StageMaPayloadError(f"not an http(s) URL: {url!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise StageMaPayloadError(f"URL has no host: {url!r}")
    if parts.port and parts.port not in {80, 443}:
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def is_stage_ma_host(url: str) -> bool:
    """Whether ``url`` points at one of the public Stage.ma hosts."""
    try:
        host = urlsplit(canonical_url(url)).hostname or ""
    except StageMaPayloadError:
        return False
    return host in STAGE_MA_HOSTS


def extract_candidate_id(url: str) -> str | None:
    """Return the numeric component of a Stage.ma ``/offres-stage/<id>-<slug>``.

    A **candidate** `source_external_id` and nothing more. The digits are
    returned as the site published them, not as an `int`: `007` and `7` are
    different published identities. The host is part of the question — another
    site can publish an identical-looking path, and this collector has no
    business treating someone else's URL as a Stage.ma offer.

    Nothing anywhere reads this value as a date, a sort key, or freshness.
    """
    try:
        parts = urlsplit(canonical_url(url))
    except StageMaPayloadError:
        return None
    if (parts.hostname or "") not in STAGE_MA_HOSTS:
        return None
    match = _OFFER_PATH_PATTERN.match(parts.path)
    return match.group(1) if match else None


def is_offer_detail_url(url: str) -> bool:
    return extract_candidate_id(url) is not None


def is_organisation_url(url: str) -> bool:
    """Whether ``url`` is a Stage.ma employer profile — evidence, never a target."""
    try:
        parts = urlsplit(canonical_url(url))
    except StageMaPayloadError:
        return False
    if (parts.hostname or "") not in STAGE_MA_HOSTS:
        return False
    return bool(_ORGANISATION_PATH_PATTERN.match(parts.path))


def normalize_text(value: object) -> str:
    """Collapse whitespace in a string, returning '' for anything else."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


# ------------------------------------------------------------ the listing ---


class _Element:
    """One node of a minimal document tree: tag, attributes, children."""

    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "_Element | None"):
        self.tag = tag
        self.attrs = attrs
        self.children: list[Any] = []
        self.parent = parent


#: Void elements never close, so they must not be pushed as open containers: a
#: `<br>` or `<img>` that pushed and never popped would swallow the rest of the
#: document into itself and destroy every card boundary below it.
_VOID_ELEMENTS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)


class _DocumentBuilder(HTMLParser):
    """Build a small tree from HTML, tolerating the malformed markup of the wild."""

    _SKIP_TEXT = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("#document", {}, None)
        self._current = self.root
        self.jsonld: list[str] = []
        self._in_jsonld = False
        self._jsonld_buffer: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if name == "script" and "ld+json" in attributes.get("type", "").lower():
            self._in_jsonld = True
            self._jsonld_buffer = []
        if name in self._SKIP_TEXT:
            self._skip_depth += 1
        element = _Element(name, attributes, self._current)
        self._current.children.append(element)
        if name not in _VOID_ELEMENTS:
            self._current = element

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        self.handle_starttag(tag, attrs)
        if name not in _VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "script" and self._in_jsonld:
            self._in_jsonld = False
            if (payload := "".join(self._jsonld_buffer).strip()):
                self.jsonld.append(payload)
        if name in self._SKIP_TEXT and self._skip_depth:
            self._skip_depth -= 1
        if name in _VOID_ELEMENTS:
            return
        # Close the nearest matching ancestor. Unbalanced markup closes nothing
        # rather than unwinding the whole tree.
        node: _Element | None = self._current
        while node is not None and node.tag != name:
            node = node.parent
        if node is not None and node.parent is not None:
            self._current = node.parent

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_buffer.append(data)
            return
        if not self._skip_depth:
            self._current.children.append(data)


def _element_text(element: _Element) -> str:
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
            return
        for child in node.children:
            walk(child)

    walk(element)
    return normalize_text("".join(parts))


#: Apostrophe variants a French page mixes freely. Normalized to one form so a
#: marker list stays readable instead of doubling for typography.
_APOSTROPHES = "\u2019\u2018\u201b\u00b4"


def _matchable(text: str) -> str:
    """Lowercase text with its apostrophes normalized, for marker matching."""
    lowered = (text or "").casefold()
    for character in _APOSTROPHES:
        lowered = lowered.replace(character, "'")
    return lowered


def _anchors(element: _Element) -> list[_Element]:
    found: list[_Element] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            return
        if node.tag == "a" and node.attrs.get("href", "").strip():
            found.append(node)
        for child in node.children:
            walk(child)

    walk(element)
    return found


def _resolved_href(anchor: _Element, base_url: str) -> str | None:
    href = anchor.attrs.get("href", "").strip()
    if not href or href.lower().startswith(("javascript:", "#", "mailto:", "tel:")):
        return None
    try:
        return canonical_url(urljoin(base_url, href))
    except StageMaPayloadError:
        return None


def _distinct_offer_urls(element: _Element, base_url: str) -> set[str]:
    """The set of distinct canonical offer URLs beneath ``element``.

    A **set**, because the real listing links the same offer more than once from
    one card — a title link and a "+ Voir Offre de Stage" link both pointing at
    `/offres-stage/9355-…`. Counting anchors would make such a card look like
    two offers and collapse it, which is exactly the bug this replaces.
    """
    found: set[str] = set()
    for anchor in _anchors(element):
        resolved = _resolved_href(anchor, base_url)
        if resolved and is_offer_detail_url(resolved):
            found.add(resolved)
    return found


def _card_for(anchor: _Element, base_url: str) -> _Element:
    """Return the offer card containing ``anchor``.

    The card is the largest ancestor that still contains exactly **one distinct
    offer identity** — one canonical URL, however many anchors point at it. That
    is the real invariant: the live listing gives each offer both a title link
    and a "+ Voir Offre de Stage" link, so a card holding two anchors is
    ordinarily still a card holding one offer, and climbing stops only when a
    *second offer* appears.

    Deriving the boundary from the document's own structure rather than from CSS
    class names matters twice over: generated class names change on the next
    redesign, and this definition makes cross-card leakage impossible by
    construction — an organization anchor sitting beside two distinct offers
    belongs to no card, so it is attributed to neither.
    """
    card = anchor
    node = anchor.parent
    while node is not None and node.tag != "#document":
        if len(_distinct_offer_urls(node, base_url)) != 1:
            break
        card = node
        node = node.parent
    return card


@dataclass(frozen=True)
class StageMaListingEntry:
    """One offer as the approved specialty listing published it.

    `listing_organization` and `listing_location` are *card-scoped evidence*,
    used only when the detail page does not supply better. Both may be `None`:
    the listing openly carries anonymous offers, and an absent value stays
    absent rather than becoming a placeholder.
    """

    source_external_id: str
    canonical_url: str
    listing_title: str | None = None
    listing_organization: str | None = None
    listing_location: str | None = None


#: Characters a "value" can consist entirely of while carrying no information —
#: the dash runs a listing uses where an employer would go.
_SEPARATOR_CHARACTERS = frozenset(" -–—_.·•*/\\|:,;")

#: The site's own word for a deliberately unattributed employer. Matched exactly
#: (case-insensitively) rather than by substring, so a real company whose name
#: happens to contain it is never discarded.
_ANONYMOUS_ORGANISATION_NAMES = frozenset({"anonyme", "anonymous", "confidentiel"})


def is_usable_organization(value: object) -> bool:
    """Whether a string is a real employer name rather than a stand-in.

    Deliberately narrow. Three things disqualify a value — it is empty, it is
    the site's explicit anonymity marker, or it is nothing but separators — and
    nothing else does. A broader heuristic would eventually throw away a
    legitimate company, and a wrong employer is worse than a missing one only
    because a missing one is honest.
    """
    text = normalize_text(value)
    if not text:
        return False
    if text.casefold() in _ANONYMOUS_ORGANISATION_NAMES:
        return False
    return not all(character in _SEPARATOR_CHARACTERS for character in text)


#: Explicit machine-readable carriers of a location inside a card. Deliberately
#: narrow: the audit found no reliable location structure on these pages, and
#: guessing a city from neighbouring text would attach the site footer's address
#: to every offer on the page.
_LOCATION_ITEMPROPS = frozenset(
    {"addresslocality", "joblocation", "addressregion", "location"}
)


def _card_organization(card: _Element, base_url: str) -> str | None:
    """The employer named by this card's own organization-profile anchor.

    The anchor's visible text is the evidence; the profile page is never
    fetched. An anchor from another card cannot appear here, because the card
    boundary is what `_card_for` computed.
    """
    for anchor in _anchors(card):
        resolved = _resolved_href(anchor, base_url)
        if resolved and is_organisation_url(resolved):
            text = _element_text(anchor) or normalize_text(anchor.attrs.get("title"))
            if is_usable_organization(text):
                return normalize_text(text)
    return None


def _card_location(card: _Element) -> str | None:
    """An explicitly marked location inside this card, or ``None``.

    Only machine-readable markers count. There is no fallback to nearby text: a
    listing page carries a footer address, a newsletter blurb and other cards,
    and "the nearest text that looks like a city" would confidently attach the
    wrong one.
    """

    def walk(node: Any) -> str | None:
        if isinstance(node, str):
            return None
        itemprop = node.attrs.get("itemprop", "").strip().lower()
        if itemprop in _LOCATION_ITEMPROPS:
            value = normalize_text(node.attrs.get("content")) or _element_text(node)
            if value:
                return value
        label = node.attrs.get("aria-label", "").strip().lower()
        if label in {"lieu", "ville", "location", "localisation"}:
            if (value := _element_text(node)):
                return value
        for child in node.children:
            if (found := walk(child)) is not None:
                return found
        return None

    return walk(card)


#: Phrases with which the site states, in its own words, that it has nothing to
#: show. Only an explicit statement makes an empty listing a valid observation;
#: silence does not, because silence is what a redesign also looks like.
_EMPTY_STATE_MARKERS = (
    "aucune offre",
    "aucun offre",
    "aucune annonce",
    "aucun résultat",
    "aucun resultat",
    "pas d'offre",
    "no offers",
    "no results found",
)


def has_explicit_empty_state(html: str) -> bool:
    """Whether the page says, in its own words, that it holds no offers."""
    builder = _DocumentBuilder()
    builder.feed(html or "")
    text = _matchable(_element_text(builder.root))
    return any(marker in text for marker in _EMPTY_STATE_MARKERS)


def parse_listing(html: str, base_url: str = LISTING_URL) -> tuple[StageMaListingEntry, ...]:
    """Read the approved specialty page into listing entries, in document order.

    Order is the page's, deduplication keeps the first occurrence, and the
    numeric ID plays no part in either — sorting by it would smuggle in a
    recency claim the audit explicitly refused to make. Only same-host offer
    URLs in the known path family are accepted.
    """
    builder = _DocumentBuilder()
    builder.feed(html or "")
    entries: list[StageMaListingEntry] = []
    seen: set[str] = set()
    for anchor in _anchors(builder.root):
        resolved = _resolved_href(anchor, base_url)
        if not resolved or not is_offer_detail_url(resolved):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        external_id = extract_candidate_id(resolved)
        if external_id is None:  # pragma: no cover - guarded by is_offer_detail_url
            continue
        card = _card_for(anchor, base_url)
        title = _element_text(anchor) or normalize_text(anchor.attrs.get("title"))
        entries.append(
            StageMaListingEntry(
                source_external_id=external_id,
                canonical_url=resolved,
                listing_title=title or None,
                listing_organization=_card_organization(card, base_url),
                listing_location=_card_location(card),
            )
        )
    return tuple(entries)


def select_listing_targets(
    entries: tuple[StageMaListingEntry, ...], limit: int
) -> tuple[StageMaListingEntry, ...]:
    """Take the first ``limit`` entries in the page's own order.

    Deduplication has already happened, so the bound applies to distinct offers.
    The order is the document's and nothing else: not the numeric ID, which is
    not evidence of recency, and not any score, which is a later layer's job.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise StageMaPayloadError("detail page limit must be a positive integer")
    return entries[:limit]


# ------------------------------------------------------------ detail page ---


def _iter_nodes(value: object) -> list[dict[str, Any]]:
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
    """Return the page's first schema.org `JobPosting` node, or ``None``.

    A malformed block does not condemn the page — a later block may still carry
    the posting — but a page with no parseable posting at all is a structural
    contract failure for the caller, not something to work around here.
    """
    builder = _DocumentBuilder()
    builder.feed(html or "")
    for block in builder.jsonld:
        try:
            document = json.loads(block)
        except ValueError:
            continue
        for node in _iter_nodes(document):
            if "JobPosting" in _node_types(node):
                return node
    return None


def _dig(node: object, *path: str) -> object:
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
    """The employer the posting names, when it names a usable one.

    "Anonyme" is filtered here rather than passed on, so the listing fallback
    gets its turn: the site publishes anonymous and named offers side by side,
    and an anonymous *detail* page sometimes sits behind a named listing card.
    """
    name = _dig(posting, "hiringOrganization", "name")
    return normalize_text(name) if is_usable_organization(name) else None


def posting_location(posting: dict[str, Any]) -> str | None:
    """Structured location only: locality, else region, else country.

    Nothing is derived from the source's configured country. `country: MA` is a
    statement about the source, not about where a particular internship is.
    """
    address = _dig(posting, "jobLocation", "address")
    if address is None:
        return None
    for key in ("addressLocality", "addressRegion", "addressCountry"):
        if (value := _text_or_none(_dig(address, key))):
            return value
    return None


def posting_description(posting: dict[str, Any]) -> str | None:
    return _text_or_none(posting.get("description"))


# ------------------------------------------------------------- lifecycle ---

#: A page's publication state, asserted only when the page's own text supports
#: it. There is no inference from an HTTP status: a 200 does not mean an offer
#: is open, and UNKNOWN is not EXPIRED.
STATE_PUBLISHED = "PUBLISHED"
STATE_UNPUBLISHED = "UNPUBLISHED"
STATE_EXPIRED = "EXPIRED"
STATE_UNKNOWN = "UNKNOWN"

#: Markers in the page's visible text, French first because the site is
#: Moroccan and francophone.
_EXPIRED_MARKERS = (
    "offre expirée",
    "offre expiree",
    "annonce expirée",
    "annonce expiree",
    "expirée",
    "expiree",
    "expired",
    "cette offre n'est plus",
    "n'est plus disponible",
    "date limite dépassée",
    "date limite depassee",
)
#: Including the sentence the live Stage.ma detail page really renders:
#: "Cette offre de stage n'est pas publiée. Seul le recruteur et
#: l'administrateur peuvent la visualiser." That same page also prints
#: "Publiée le 01/01/1970", so a parser that knew only "non publiée" fell
#: through to the PUBLISHED marker and would have stored a hidden offer as live.
#: Apostrophes are normalized before matching, so the typographic and ASCII
#: forms need not both be listed.
_UNPUBLISHED_MARKERS = (
    "n'est pas publiée",
    "n'est pas publiee",
    "n'est pas publié",
    "n'est pas publie",
    "non publiée",
    "non publiee",
    "non publié",
    "non publie",
    "unpublished",
    "en attente de validation",
    "en cours de validation",
)


_PUBLISHED_MARKERS = (
    "publiée le",
    "publiee le",
    "publié le",
    "publie le",
    "date de publication",
    "published on",
)


def publication_state(html: str) -> str:
    """Read the publication state from the page's own visible words.

    Precedence is `EXPIRED > UNPUBLISHED > PUBLISHED`, and it matters: a page
    reading "publiée le 3 mars — offre expirée" describes a *closed* offer, and
    a collector that stopped at "publiée" would store it as open. A page whose
    text supports nothing is `UNKNOWN`, which is not the same as expired and is
    not a reason to discard the offer.
    """
    builder = _DocumentBuilder()
    builder.feed(html or "")
    text = _matchable(_element_text(builder.root))
    if any(marker in text for marker in _EXPIRED_MARKERS):
        return STATE_EXPIRED
    if any(marker in text for marker in _UNPUBLISHED_MARKERS):
        return STATE_UNPUBLISHED
    if any(marker in text for marker in _PUBLISHED_MARKERS):
        return STATE_PUBLISHED
    return STATE_UNKNOWN


# ------------------------------------------------------------------ dates ---

#: Epoch zero in the spellings a site actually renders it in. A date column that
#: was never set is displayed like this far more often than any site admits, and
#: it is the single most dangerous value to mistake for a publication date.
_EPOCH_SPELLINGS = ("1970-01-01", "01/01/1970", "1/1/1970", "01-01-1970", "1970/01/01")

_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})"), "ymd"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})"), "dmy"),
    (re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})"), "dmy"),
)


def posting_published_at(posting: dict[str, Any]) -> str | None:
    """The offer's publication date, from `JobPosting.datePosted` and nowhere else.

    Returned as an ISO date, or `None`. `None` is a real answer here: a value we
    cannot read is not repaired, and nothing stands in for a missing one — not
    the crawl time, not `validThrough`, not an "À partir du" start date, not a
    `<time>` element, and not the numeric URL id, which is not a date in any
    sense. Epoch-zero and impossible dates return `None` for the same reason:
    storing one would put a 1970 timestamp on a 2026 internship.
    """
    raw = normalize_text(posting.get("datePosted"))
    if not raw:
        return None
    lowered = raw.casefold()
    if any(spelling in lowered for spelling in _EPOCH_SPELLINGS):
        return None
    for pattern, order in _DATE_PATTERNS:
        match = pattern.match(raw)
        if not match:
            continue
        first, second, third = (int(part) for part in match.groups())
        year, month, day = (
            (first, second, third) if order == "ymd" else (third, second, first)
        )
        try:
            parsed = date(year, month, day)
        except ValueError:
            return None
        if parsed.year <= 1970:
            return None
        return parsed.isoformat()
    return None


# ------------------------------------------------------- application action --

#: Words with which a control says it applies. French first.
APPLICATION_INTENT_MARKERS = (
    "postuler",
    "postulez",
    "candidater",
    "candidature",
    "apply",
)


def application_url(html: str, base_url: str) -> str | None:
    """The href of an explicit application control, or ``None``.

    Deliberately not the canonical URL, `og:url`, `JobPosting.url`, a link found
    in the description or the employer's profile. Those name the posting or the
    company — the page we are already on, or who published it — and every offer
    has them, so reading one as an application route would claim a way to apply
    for every offer ever published, including closed ones.

    What counts is a control that *says* it applies **and** carries a usable
    http(s) href. A JavaScript handler, a bare `#`, a modal or a login-only
    control yields `None`: reporting a URL that does not work is worse than
    reporting none. An off-domain href may be recorded — an employer's own ATS
    is a real destination — and is never fetched.
    """
    builder = _DocumentBuilder()
    builder.feed(html or "")
    for anchor in _anchors(builder.root):
        label = _element_text(anchor) or normalize_text(
            anchor.attrs.get("aria-label") or anchor.attrs.get("title")
        )
        if not label:
            continue
        if not any(
            marker in label.casefold() for marker in APPLICATION_INTENT_MARKERS
        ):
            continue
        href = anchor.attrs.get("href", "").strip()
        if not href or href.lower().startswith(("javascript:", "#", "mailto:", "tel:")):
            return None
        resolved = urljoin(base_url, href)
        if urlsplit(resolved).scheme not in {"http", "https"}:
            return None
        return resolved
    return None


__all__ = [
    "APPLICATION_INTENT_MARKERS",
    "COLLECTOR_USER_AGENT",
    "LISTING_URL",
    "OFFER_PATH_PREFIX",
    "PARSER_VERSION",
    "ROBOTS_ABSENT",
    "ROBOTS_OBEY",
    "ROBOTS_REFUSED",
    "ROBOTS_UNRESOLVED",
    "ROBOTS_URL",
    "STAGE_MA_HOSTS",
    "STATE_EXPIRED",
    "STATE_PUBLISHED",
    "STATE_UNKNOWN",
    "STATE_UNPUBLISHED",
    "RobotsGroup",
    "RobotsRule",
    "StageMaCollectionError",
    "StageMaListingEntry",
    "StageMaPayloadError",
    "access_barrier",
    "application_url",
    "canonical_url",
    "classify_robots_response",
    "extract_candidate_id",
    "has_explicit_empty_state",
    "is_offer_detail_url",
    "is_organisation_url",
    "is_stage_ma_host",
    "is_usable_organization",
    "job_posting",
    "normalize_text",
    "parse_listing",
    "parse_robots_txt",
    "posting_description",
    "posting_location",
    "posting_organization",
    "posting_published_at",
    "posting_title",
    "product_token",
    "publication_state",
    "robots_allows",
    "robots_target",
    "select_listing_targets",
]
