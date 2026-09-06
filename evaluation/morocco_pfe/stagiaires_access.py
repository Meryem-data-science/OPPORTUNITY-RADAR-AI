"""Network-free analysis for the Stagiaires.ma access audit (Phase 7C.4A).

7C.4A asks one question — *what does Stagiaires.ma publicly expose, and is its
public structure a workable basis for a future collector?* — and answers it
without writing one. This module is the pure half of that question: it turns
bytes somebody else fetched into evidence, and it opens no socket. The network
edge is `evaluation.morocco_pfe.cli.stagiaires_access_audit`, and it is the
only file in this slice that makes a request.

The discovery chain this module encodes is the site's **own official one**:

    robots.txt  ->  the Sitemap: it declares  ->  offre-sitemap*.xml
                ->  /stage-emploi-maroc/<numeric-id>-<slug>

That matters. The public PFE listing is a Next.js application whose initial
HTML does not carry the offer list as ordinary links, so scraping the rendered
listing would mean driving a browser. The sitemap chain is published by the
site for exactly this purpose, needs no browser, and is what this audit reads.
Nothing here renders JavaScript, and nothing here should ever need to.

Two invariants are load-bearing and are enforced by tests rather than by good
intentions:

* **`lastmod` is not a publication date.** A `<lastmod>` states when a *page*
  last changed, which is not when an *offer* was published: a re-rendered page,
  a template change or a view-counter bump all move it. `SitemapOfferEntry`
  therefore carries `sitemap_lastmod` and has no `published_at` field at all,
  so no caller can quietly promote one to the other;
* **the numeric ID is a candidate, not a contract.** `/stage-emploi-maroc/<id>`
  exposes an integer that looks like a stable per-offer identity, and this
  module extracts it as `source_external_id`. Whether it is stable across time
  is not something a single audit can establish, and this slice does not claim
  it. Where the published data contradicts the assumption — one ID under two
  different canonical URLs — that is surfaced as an audit problem instead of
  being silently deduplicated away.

Nothing in this slice collects. There is no `config/sources.yaml` row, no
`SourceConfig` type, no collector, no factory registration, no RadarAgent run,
no database write and no migration for Stagiaires.ma, and this module imports
nothing that could create one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

#: The audit identifies itself honestly. No browser is impersonated, because
#: impersonating one is exactly the evasion this phase forbids.
AUDIT_USER_AGENT = "OpportunityRadarAI-AccessAudit/1.0 (+public access audit; GET-only)"

#: The public hosts this audit will talk to. Anything else — a tracker, a CDN,
#: an off-domain apply link — is out of scope and is rejected rather than
#: followed. `www` and the bare domain are both accepted as *seen*, and are
#: deliberately **not** folded into each other: rewriting one to the other is a
#: guess about the site's canonical form, and this audit does not guess.
STAGIAIRES_HOSTS = frozenset({"www.stagiaires.ma", "stagiaires.ma"})

#: The entry point of the chain. Everything else is *discovered*, never assumed:
#: the sitemap index is read from robots.txt, and the offer sitemaps are read
#: from the index.
ROBOTS_URL = "https://www.stagiaires.ma/robots.txt"

#: The public PFE listing the product cares about. It is fetched once to record
#: that it is publicly reachable and what it structurally contains — it is *not*
#: the discovery source, and this audit never parses an offer list out of it.
PFE_TARGET_URL = (
    "https://www.stagiaires.ma/stage-emploi-type-stage/stage-de-fin-d-etudes"
)

#: The public offer-detail path family. A URL outside it is not an offer.
OFFER_PATH_PREFIX = "/stage-emploi-maroc/"

#: `/stage-emploi-maroc/<digits>` optionally followed by `-<slug>`. The slug is
#: decoration; the digits are the candidate identity. A path whose ID is absent,
#: non-numeric, or glued to the slug without the separator is malformed and is
#: surfaced, not repaired.
_OFFER_PATH_PATTERN = re.compile(r"^/stage-emploi-maroc/(\d+)(?:-[^/]*)?$")

#: The offer-sitemap family declared by the official sitemap index:
#: `offre-sitemap.xml`, `offre-sitemap2.xml`, and any further numbered sibling.
#: Written as a pattern rather than a fixed pair on purpose — the site may
#: publish a third file the day the second one fills up, and an audit that
#: hardcoded two would silently stop seeing a third of the offers.
_OFFER_SITEMAP_PATTERN = re.compile(r"^/offre-sitemap\d*\.xml$")

#: Bounded by policy, not by taste: this audit reads at most three real offer
#: pages to characterize their structure. It is not a crawler and must never
#: become one.
DEFAULT_DETAIL_LIMIT = 3
MAX_DETAIL_LIMIT = 3

#: A guard against a hostile or accidental multi-megabyte document. XML this
#: large is not a sitemap we should be parsing in an audit.
MAX_XML_BYTES = 20_000_000


class StagiairesAccessError(RuntimeError):
    """Raised when the audit cannot proceed honestly."""


def validate_detail_limit(value: object) -> int:
    """Return a detail-page limit within the audit's hard bound.

    `bool` is rejected explicitly: `True` is an `int` in Python, and a flag
    silently becoming "fetch 1 page" is the kind of quiet nonsense this
    repository's other limit validators already refuse.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise StagiairesAccessError("detail limit must be an integer")
    if not 1 <= value <= MAX_DETAIL_LIMIT:
        raise StagiairesAccessError(
            f"detail limit must be between 1 and {MAX_DETAIL_LIMIT}"
        )
    return value


# --------------------------------------------------------------- robots ----


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    pattern: str


@dataclass(frozen=True)
class RobotsGroup:
    agents: tuple[str, ...]
    rules: tuple[RobotsRule, ...]


@dataclass(frozen=True)
class RobotsVerdict:
    """Whether a path may be fetched, and which line decided it."""

    allowed: bool
    matched_rule: str | None
    matched_agent: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "matched_rule": self.matched_rule,
            "matched_agent": self.matched_agent,
        }


def parse_robots_txt(text: str) -> tuple[RobotsGroup, ...]:
    """Parse robots.txt into groups, tolerating comments and stray lines."""
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
            elif agents and field_name == "disallow":
                # "Disallow:" with an empty value allows everything; it is a
                # real REP idiom and dropping it would misread the file.
                rules.append(RobotsRule(allow=True, pattern="/"))
    flush()
    return tuple(groups)


def sitemap_declarations(text: str) -> tuple[str, ...]:
    """Return the absolute `Sitemap:` URLs robots.txt declares, in file order.

    `Sitemap` is a group-independent directive, so it is read from the whole
    file rather than from the group that happens to precede it. Duplicates are
    collapsed and relative values dropped: a sitemap reference that is not an
    absolute http(s) URL is not something this audit will resolve by guessing.
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


def _rule_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    expression = "".join(
        ".*" if character == "*" else re.escape(character) for character in body
    )
    return re.compile(f"^{expression}$" if anchored else f"^{expression}")


def _select_group(
    groups: tuple[RobotsGroup, ...], user_agent: str
) -> RobotsGroup | None:
    """Prefer a group naming this agent; fall back to the wildcard group."""
    token = user_agent.split("/", 1)[0].strip().lower()
    for group in groups:
        if any(agent and (agent == token or agent in token) for agent in group.agents):
            return group
    for group in groups:
        if "*" in group.agents:
            return group
    return None


def robots_verdict(
    groups: tuple[RobotsGroup, ...], path: str, user_agent: str = AUDIT_USER_AGENT
) -> RobotsVerdict:
    """Decide whether ``path`` is fetchable: longest match wins, Allow breaks ties."""
    group = _select_group(groups, user_agent)
    if group is None:
        return RobotsVerdict(allowed=True, matched_rule=None, matched_agent=None)
    target = path or "/"
    best: RobotsRule | None = None
    for rule in group.rules:
        if not _rule_regex(rule.pattern).match(target):
            continue
        if (
            best is None
            or len(rule.pattern) > len(best.pattern)
            or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
        ):
            best = rule
    if best is None:
        return RobotsVerdict(True, None, ", ".join(group.agents) or None)
    return RobotsVerdict(
        allowed=best.allow,
        matched_rule=f"{'Allow' if best.allow else 'Disallow'}: {best.pattern}",
        matched_agent=", ".join(group.agents) or None,
    )


# --------------------------------------------------------- robots policy ----

#: What a robots.txt response means for the rest of the audit. Deliberately
#: four outcomes rather than "200 or not": a robots.txt we were *refused*
#: (403/429) tells us nothing about what is allowed, and treating that silence
#: as permission is the failure mode this policy exists to prevent.
ROBOTS_OBEY = "OBEY"
ROBOTS_ABSENT = "ABSENT"
ROBOTS_BARRIER = "BARRIER"
ROBOTS_UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class RobotsDisposition:
    """Whether robots.txt can be honoured, and what to do when it cannot."""

    disposition: str
    barrier: str | None = None
    detail: str | None = None

    @property
    def may_proceed(self) -> bool:
        """Only a parsed file or a definitively absent one lets the audit continue."""
        return self.disposition in {ROBOTS_OBEY, ROBOTS_ABSENT}

    def as_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "barrier": self.barrier,
            "detail": self.detail,
        }


#: Markers that a response is a bot wall or a login redirect rather than the
#: public page. Matched case-insensitively against a bounded prefix of the body.
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


def classify_robots_response(
    status_code: int, final_url: str, body: str
) -> RobotsDisposition:
    """Decide what a robots.txt response permits.

    * **200** — parse it and obey it, unless the response is transparently a
      bot wall or a login page rather than the file itself.
    * **404 / 410** — the file is definitively absent, so no explicit rule
      exists and this audit's own policy allows it to continue. That is a
      statement about robots.txt only; it is **not** permission of any kind,
      legal or otherwise.
    * **401 / 403 / 407 / 429** — we were refused the file. We therefore do not
      know what it says, and an unknown rule is never assumed to be permissive:
      the audit stops before touching any other page.
    * **5xx and anything else** — unresolved. Stop.

    Emptiness is deliberately *not* a barrier here: a valid robots.txt is often
    only a couple of lines long, so the generic short-body heuristic used for
    HTML pages would misread a real file as a wall.
    """
    if status_code in {401, 403, 407}:
        return RobotsDisposition(
            ROBOTS_BARRIER, f"ROBOTS_HTTP_{status_code}", "robots.txt was refused"
        )
    if status_code == 429:
        return RobotsDisposition(
            ROBOTS_BARRIER,
            "ROBOTS_HTTP_429_RATE_LIMITED",
            "robots.txt was rate limited",
        )
    if status_code in {404, 410}:
        return RobotsDisposition(
            ROBOTS_ABSENT, None, f"robots.txt is absent (HTTP {status_code})"
        )
    if status_code >= 500:
        return RobotsDisposition(
            ROBOTS_UNRESOLVED,
            f"ROBOTS_HTTP_{status_code}_SERVER_ERROR",
            "robots.txt could not be resolved",
        )
    if status_code != 200:
        return RobotsDisposition(
            ROBOTS_UNRESOLVED,
            f"ROBOTS_HTTP_{status_code}",
            "unexpected robots.txt status",
        )
    sample = (body or "")[:20000].lower()
    if any(marker in sample for marker in _CHALLENGE_MARKERS):
        return RobotsDisposition(
            ROBOTS_BARRIER,
            "ROBOTS_BOT_CHALLENGE_OR_CAPTCHA",
            "robots.txt returned a challenge page",
        )
    if any(
        segment in urlsplit(final_url or "").path.lower()
        for segment in _LOGIN_PATH_SEGMENTS
    ):
        return RobotsDisposition(
            ROBOTS_BARRIER,
            "ROBOTS_REDIRECTED_TO_LOGIN",
            "robots.txt redirected to login",
        )
    return RobotsDisposition(ROBOTS_OBEY, None, "robots.txt retrieved")


def detect_access_barrier(status_code: int, final_url: str, body: str) -> str | None:
    """Name the barrier a response represents, or ``None`` if it looks public.

    A barrier is reported, never worked around. 7C.4A stops at the wall.
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
    for marker in _CHALLENGE_MARKERS:
        if marker in sample:
            return "BOT_CHALLENGE_OR_CAPTCHA"
    path = urlsplit(final_url or "").path.lower()
    if any(segment in path for segment in _LOGIN_PATH_SEGMENTS):
        return "REDIRECTED_TO_LOGIN"
    if status_code == 200 and len(sample.strip()) < 200:
        return "EMPTY_OR_JAVASCRIPT_ONLY_BODY"
    if status_code != 200:
        return f"HTTP_{status_code}"
    return None


# ------------------------------------------------------------------ URLs ----


def canonical_url(url: str) -> str:
    """Normalize a public Stagiaires.ma URL conservatively.

    Conservative means: only the parts of a URL that carry no meaning are
    dropped. The scheme and host are lowercased, a default port is removed, the
    fragment goes (it is client-side only), the query goes (no observed offer
    URL carries one, and a tracking parameter is not a different offer) and a
    trailing slash is trimmed. The path's case is left exactly as published, and
    `www.stagiaires.ma` is **not** rewritten to `stagiaires.ma` or back —
    deciding which of those the site considers canonical is a claim this audit
    has no evidence for. Where that leaves two spellings of one offer, the ID
    collision report says so rather than a silent merge hiding it.
    """
    if not isinstance(url, str) or not url.strip():
        raise StagiairesAccessError("URL must be a non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"}:
        raise StagiairesAccessError(f"not an http(s) URL: {url!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise StagiairesAccessError(f"URL has no host: {url!r}")
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
    except StagiairesAccessError:
        return False
    return host in STAGIAIRES_HOSTS


def extract_external_id(url: str) -> str | None:
    """Return the numeric offer ID in ``/stage-emploi-maroc/<id>-<slug>``.

    The digits are returned as the string the site published them as, not as an
    `int`: `007` and `7` are different published identities, and normalizing
    them together would invent an equality the site never stated. `None` means
    this URL does not carry an offer ID in the known shape — a caller must
    treat that as a finding, not as a zero.
    """
    try:
        path = urlsplit(canonical_url(url)).path
    except StagiairesAccessError:
        return None
    match = _OFFER_PATH_PATTERN.match(path)
    return match.group(1) if match else None


#: Why a sitemap URL was not accepted as a public offer detail URL. These are
#: audit findings, deliberately named rather than counted, so a reviewer can
#: tell "someone linked another domain" from "the path family changed".
REJECT_NOT_A_URL = "MALFORMED_URL"
REJECT_OFF_DOMAIN = "OFF_DOMAIN"
REJECT_WRONG_PATH_FAMILY = "WRONG_PATH_FAMILY"
REJECT_MISSING_NUMERIC_ID = "MISSING_OR_MALFORMED_NUMERIC_ID"


@dataclass(frozen=True)
class RejectedSitemapUrl:
    """A `<loc>` the audit refused, and the reason it refused it."""

    url: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"url": self.url, "reason": self.reason}


@dataclass(frozen=True)
class SitemapOfferEntry:
    """One public offer URL as the sitemap published it.

    There is no `published_at` here, and there must never be one derived from
    `sitemap_lastmod`. `<lastmod>` is page-modification metadata: it says a URL
    changed, not that an offer was posted. Whether Stagiaires.ma states a real
    publication date is a question about the *detail page*, which the detail
    audit answers separately and may well answer with "it does not".
    """

    source_external_id: str
    canonical_url: str
    sitemap_lastmod: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "source_external_id": self.source_external_id,
            "canonical_url": self.canonical_url,
            "sitemap_lastmod": self.sitemap_lastmod,
        }


def _local_name(tag: object) -> str:
    """Return an XML tag without its namespace, lowercased."""
    text = str(tag)
    return text.rsplit("}", 1)[-1].strip().lower()


def _parse_xml(text: str, expected_root: str) -> ElementTree.Element:
    """Parse sitemap XML, insisting the document really is what we asked for.

    A sitemap fetch that answers with an HTML error page, a redirect notice or
    a truncated body is not a sitemap, and parsing it "leniently" would turn a
    failed request into an empty-but-successful result — an audit reporting
    zero offers because it silently read the wrong document is worse than one
    that stops.
    """
    if len(text or "") > MAX_XML_BYTES:
        raise StagiairesAccessError(
            f"sitemap XML exceeds the {MAX_XML_BYTES}-byte audit limit"
        )
    try:
        root = ElementTree.fromstring((text or "").strip())
    except ElementTree.ParseError as error:
        raise StagiairesAccessError(f"document is not well-formed XML: {error}")
    if _local_name(root.tag) != expected_root:
        raise StagiairesAccessError(
            f"expected a <{expected_root}> document, got <{_local_name(root.tag)}>"
        )
    return root


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            value = (child.text or "").strip()
            return value or None
    return None


@dataclass(frozen=True)
class SitemapIndexParse:
    """What the official sitemap index declares, split by what we will read.

    `offer_sitemaps` is what this audit fetches. `other_sitemaps` is recorded
    but not followed: the index declares page and category sitemaps too, and an
    audit of offer structure has no business walking them.
    """

    offer_sitemaps: tuple[str, ...]
    other_sitemaps: tuple[str, ...]
    rejected: tuple[RejectedSitemapUrl, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "offer_sitemaps": list(self.offer_sitemaps),
            "offer_sitemap_count": len(self.offer_sitemaps),
            "other_sitemap_count": len(self.other_sitemaps),
            "other_sitemaps": list(self.other_sitemaps),
            "rejected": [item.as_dict() for item in self.rejected],
        }


def parse_sitemap_index(xml_text: str, base_url: str = ROBOTS_URL) -> SitemapIndexParse:
    """Read the official `<sitemapindex>` and pick out the offer sitemaps.

    Only same-host entries in the `offre-sitemap*.xml` family are accepted, and
    each is taken once even if the index lists it twice. The family is matched
    by pattern rather than by an enumerated pair, so a future
    `offre-sitemap3.xml` is discovered the day it appears — while a `<loc>`
    pointing at another domain is rejected outright, because "the index told us
    to" is not a reason to fetch someone else's host.
    """
    root = _parse_xml(xml_text, "sitemapindex")
    offers: list[str] = []
    others: list[str] = []
    rejected: list[RejectedSitemapUrl] = []
    seen: set[str] = set()
    for node in root:
        if _local_name(node.tag) != "sitemap":
            continue
        raw = _child_text(node, "loc")
        if not raw:
            continue
        try:
            canonical = canonical_url(urljoin(base_url, raw))
        except StagiairesAccessError:
            rejected.append(RejectedSitemapUrl(raw, REJECT_NOT_A_URL))
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        if not is_stagiaires_host(canonical):
            rejected.append(RejectedSitemapUrl(canonical, REJECT_OFF_DOMAIN))
            continue
        if _OFFER_SITEMAP_PATTERN.match(urlsplit(canonical).path):
            offers.append(canonical)
        else:
            others.append(canonical)
    return SitemapIndexParse(tuple(offers), tuple(others), tuple(rejected))


@dataclass(frozen=True)
class OfferSitemapParse:
    """One `<urlset>` file, split into accepted offers and named rejections."""

    entries: tuple[SitemapOfferEntry, ...]
    rejected: tuple[RejectedSitemapUrl, ...]
    url_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "url_count": self.url_count,
            "accepted_count": len(self.entries),
            "rejected_count": len(self.rejected),
            "rejected_reasons": dict(
                sorted(Counter(item.reason for item in self.rejected).items())
            ),
            "with_lastmod": sum(1 for item in self.entries if item.sitemap_lastmod),
        }


def parse_offer_sitemap(
    xml_text: str, base_url: str = ROBOTS_URL
) -> OfferSitemapParse:
    """Read one `<urlset>` into offer entries, naming everything it refuses.

    `<lastmod>` is carried through untouched, as the string the site published.
    It is not parsed into a date, not compared to anything, and above all not
    turned into a publication date: see `SitemapOfferEntry`.
    """
    root = _parse_xml(xml_text, "urlset")
    entries: list[SitemapOfferEntry] = []
    rejected: list[RejectedSitemapUrl] = []
    url_count = 0
    for node in root:
        if _local_name(node.tag) != "url":
            continue
        raw = _child_text(node, "loc")
        if not raw:
            continue
        url_count += 1
        lastmod = _child_text(node, "lastmod")
        try:
            canonical = canonical_url(urljoin(base_url, raw))
        except StagiairesAccessError:
            rejected.append(RejectedSitemapUrl(raw, REJECT_NOT_A_URL))
            continue
        if not is_stagiaires_host(canonical):
            rejected.append(RejectedSitemapUrl(canonical, REJECT_OFF_DOMAIN))
            continue
        if not urlsplit(canonical).path.startswith(OFFER_PATH_PREFIX):
            rejected.append(RejectedSitemapUrl(canonical, REJECT_WRONG_PATH_FAMILY))
            continue
        external_id = extract_external_id(canonical)
        if external_id is None:
            rejected.append(RejectedSitemapUrl(canonical, REJECT_MISSING_NUMERIC_ID))
            continue
        entries.append(
            SitemapOfferEntry(
                source_external_id=external_id,
                canonical_url=canonical,
                sitemap_lastmod=lastmod,
            )
        )
    return OfferSitemapParse(tuple(entries), tuple(rejected), url_count)


@dataclass(frozen=True)
class IdCollision:
    """One numeric ID published under more than one canonical URL.

    This is the finding that decides whether the numeric ID may ever be used as
    a `source_external_id` in production. A collision means the ID is not, in
    the data as published, a unique key — and a deduplicating collector keyed
    on it would merge two different offers. It is surfaced loudly and is never
    resolved by picking a winner.
    """

    source_external_id: str
    canonical_urls: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "source_external_id": self.source_external_id,
            "canonical_urls": list(self.canonical_urls),
        }


@dataclass(frozen=True)
class SitemapAudit:
    """Every offer sitemap, combined, with the problems left visible."""

    entries: tuple[SitemapOfferEntry, ...] = ()
    rejected: tuple[RejectedSitemapUrl, ...] = ()
    total_urls: int = 0
    duplicate_canonical_urls: int = 0
    id_collisions: tuple[IdCollision, ...] = ()

    @property
    def unique_ids(self) -> int:
        return len({entry.source_external_id for entry in self.entries})

    def as_dict(self) -> dict[str, object]:
        return {
            "total_sitemap_urls": self.total_urls,
            "accepted_offer_urls": len(self.entries),
            "unique_canonical_urls": len(
                {entry.canonical_url for entry in self.entries}
            ),
            "unique_source_external_ids": self.unique_ids,
            "duplicate_canonical_urls": self.duplicate_canonical_urls,
            "entries_with_lastmod": sum(
                1 for entry in self.entries if entry.sitemap_lastmod
            ),
            "rejected_count": len(self.rejected),
            "rejected_reasons": dict(
                sorted(Counter(item.reason for item in self.rejected).items())
            ),
            "rejected_sample": [item.as_dict() for item in self.rejected[:20]],
            "id_collision_count": len(self.id_collisions),
            "id_collisions": [item.as_dict() for item in self.id_collisions],
            "lastmod_is_publication_date": False,
            "lastmod_note": (
                "sitemap <lastmod> is page-modification metadata and is NOT "
                "mapped to published_at anywhere in this audit"
            ),
        }


def combine_offer_sitemaps(parses: list[OfferSitemapParse]) -> SitemapAudit:
    """Merge per-file parses into one audit, deduplicating and finding collisions.

    Deduplication is by canonical URL and is order-preserving, so the same offer
    listed in two sitemap files is one logical record rather than two. The
    *count* of those duplicates is kept, because "the site lists 1710 URLs and
    1710 of them are distinct" and "it lists 1710 and 40 are repeats" are
    different facts about the source.
    """
    entries: list[SitemapOfferEntry] = []
    rejected: list[RejectedSitemapUrl] = []
    seen: set[str] = set()
    duplicates = 0
    total = 0
    for parse in parses:
        total += parse.url_count
        rejected.extend(parse.rejected)
        for entry in parse.entries:
            if entry.canonical_url in seen:
                duplicates += 1
                continue
            seen.add(entry.canonical_url)
            entries.append(entry)

    by_id: dict[str, list[str]] = {}
    for entry in entries:
        by_id.setdefault(entry.source_external_id, []).append(entry.canonical_url)
    collisions = tuple(
        IdCollision(identifier, tuple(urls))
        for identifier, urls in sorted(by_id.items())
        if len(urls) > 1
    )
    return SitemapAudit(
        entries=tuple(entries),
        rejected=tuple(rejected),
        total_urls=total,
        duplicate_canonical_urls=duplicates,
        id_collisions=collisions,
    )


#: How the bounded detail sample is chosen, stated in the report so a reviewer
#: never has to read the code to know what they are looking at.
DETAIL_SAMPLE_STRATEGY = (
    "highest numeric source_external_id first, ties broken by canonical_url "
    "ascending; deterministic and independent of sitemap file order"
)


def select_detail_sample(
    entries: tuple[SitemapOfferEntry, ...], limit: int = DEFAULT_DETAIL_LIMIT
) -> tuple[SitemapOfferEntry, ...]:
    """Choose the offer pages the audit will actually fetch.

    Highest numeric ID first, because the newest offers are the ones whose
    structure a future collector would meet, and because "highest" is a total
    order the site publishes rather than a taste. Ties — the same ID under two
    URLs, which is itself a reported problem — fall back to the canonical URL so
    the choice cannot depend on dictionary or file ordering.

    The limit is validated here rather than by the caller, so there is exactly
    one place where the hard bound of three pages can be enforced, and no path
    through the audit that reaches the network without passing it.
    """
    bounded = validate_detail_limit(limit)
    ordered = sorted(
        entries, key=lambda item: (-int(item.source_external_id), item.canonical_url)
    )
    return tuple(ordered[:bounded])


# ------------------------------------------------- detail page structure ----

#: The signals a future collector would need from an offer page, in report
#: order. This slice does not normalize them into an opportunity — it only
#: answers, per signal, "does the public HTML actually carry this, and through
#: which generic mechanism?". A signal nothing carries is reported as absent.
DETAIL_SIGNALS = (
    "title",
    "organization",
    "location",
    "contract_type",
    "internship_type",
    "work_mode",
    "description",
    "publication_date",
    "deadline",
    "application_url",
)

#: The longest normalized value the report will carry for a text signal. Enough
#: to prove a field was really extracted; far too little to be a copy of the
#: page. Descriptions are never emitted as text at all — see `DetailSignal`.
MAX_SIGNAL_VALUE_CHARS = 200


class _HtmlCollector(HTMLParser):
    """Collect the *generic* carriers of meaning a public HTML page can have.

    Deliberately site-agnostic. It knows about JSON-LD, OpenGraph and other
    `<meta>`, microdata `itemprop`, `<time datetime>`, `<title>`, `<h1>` and
    `<a href>` — mechanisms any page may use — and it knows nothing whatsoever
    about Stagiaires.ma's own class names or DOM. That is the point: this audit
    reports what the site publishes through standard means, so that a reviewer
    can decide whether a collector is feasible, rather than reporting the
    success of selectors somebody guessed.
    """

    _TEXT_SKIP = frozenset({"script", "style", "noscript", "template"})
    #: Void elements never close, so they must not push a microdata scope: a
    #: `<meta>` or `<br>` that pushed and never popped would leave the itemprop
    #: stack permanently misaligned and attribute later text to the wrong
    #: element.
    _VOID = frozenset(
        {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self.itemprops: dict[str, str] = {}
        self.times: list[str] = []
        self.hrefs: list[str] = []
        self.title: str | None = None
        self.first_h1: str | None = None
        self.has_next_data = False
        self._in_jsonld = False
        self._buffer: list[str] = []
        self._capture: str | None = None
        self._capture_buffer: list[str] = []
        self._itemprop_stack: list[str | None] = []
        self._skip_depth = 0

    # -- collection ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if name in self._TEXT_SKIP:
            self._skip_depth += 1
        if name == "script":
            script_type = attributes.get("type", "").lower()
            if "ld+json" in script_type:
                self._in_jsonld = True
                self._buffer = []
            if attributes.get("id", "") == "__NEXT_DATA__":
                self.has_next_data = True
        elif name == "meta":
            key = attributes.get("property") or attributes.get("name")
            content = attributes.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key.strip().lower(), content)
        elif name == "time":
            if (stamp := attributes.get("datetime", "").strip()):
                self.times.append(stamp)
        elif name == "a":
            if (href := attributes.get("href", "").strip()):
                self.hrefs.append(href)
        elif name in {"title", "h1"} and self._capture is None:
            if name == "title" or self.first_h1 is None:
                self._capture = name
                self._capture_buffer = []

        prop = attributes.get("itemprop", "").strip().lower() or None
        if prop:
            # `content`/`datetime` win over element text: microdata puts the
            # machine-readable value there when the two differ.
            inline = (
                attributes.get("content", "").strip()
                or attributes.get("datetime", "").strip()
            )
            if inline:
                self.itemprops.setdefault(prop, inline)
        if name not in self._VOID:
            self._itemprop_stack.append(prop)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in self._TEXT_SKIP and self._skip_depth:
            self._skip_depth -= 1
        if name == "script" and self._in_jsonld:
            self._in_jsonld = False
            if (payload := "".join(self._buffer).strip()):
                self.jsonld.append(payload)
        if self._capture == name:
            text = normalize_text("".join(self._capture_buffer))
            if name == "title" and self.title is None:
                self.title = text or None
            elif name == "h1" and self.first_h1 is None:
                self.first_h1 = text or None
            self._capture = None
            self._capture_buffer = []
        if name not in self._VOID and self._itemprop_stack:
            self._itemprop_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._buffer.append(data)
            return
        if self._skip_depth:
            return
        if self._capture is not None:
            self._capture_buffer.append(data)
        for prop in reversed(self._itemprop_stack):
            if prop and prop not in self.itemprops:
                if (text := normalize_text(data)):
                    self.itemprops[prop] = text
                break


def normalize_text(value: object) -> str:
    """Collapse whitespace in a string, returning '' for anything else."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _iter_nodes(value: object) -> list[dict[str, object]]:
    """Walk JSON-LD, following lists and `@graph`, yielding object nodes."""
    found: list[dict[str, object]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_iter_nodes(item))
    elif isinstance(value, dict):
        found.append(value)
        for key in ("@graph", "itemListElement", "item"):
            if key in value:
                found.extend(_iter_nodes(value[key]))
    return found


def _node_types(node: dict[str, object]) -> list[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


@dataclass(frozen=True)
class JsonLdSummary:
    """What JSON-LD a page really carries — types and key names, never values."""

    block_count: int
    invalid_block_count: int
    types: tuple[str, ...]
    job_posting_count: int
    job_posting_keys: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "block_count": self.block_count,
            "invalid_block_count": self.invalid_block_count,
            "types": list(self.types),
            "job_posting_count": self.job_posting_count,
            "job_posting_keys": list(self.job_posting_keys),
        }


def _jsonld_nodes(html: str) -> tuple[list[dict[str, object]], int, int]:
    collector = _HtmlCollector()
    collector.feed(html or "")
    nodes: list[dict[str, object]] = []
    invalid = 0
    for block in collector.jsonld:
        try:
            document = json.loads(block)
        except ValueError:
            invalid += 1
            continue
        nodes.extend(_iter_nodes(document))
    return nodes, len(collector.jsonld), invalid


def summarize_jsonld(html: str) -> JsonLdSummary:
    """Report which schema.org types and JobPosting keys a page publishes.

    Reports *key names*, never values: the point is to learn whether the site
    exposes structured `JobPosting` data and which fields it fills, without
    copying third-party job content into this repository.
    """
    nodes, blocks, invalid = _jsonld_nodes(html)
    types: Counter[str] = Counter()
    job_keys: set[str] = set()
    job_count = 0
    for node in nodes:
        node_types = _node_types(node)
        types.update(node_types)
        if "JobPosting" in node_types:
            job_count += 1
            job_keys.update(str(key) for key in node)
    return JsonLdSummary(
        block_count=blocks,
        invalid_block_count=invalid,
        types=tuple(sorted(types)),
        job_posting_count=job_count,
        job_posting_keys=tuple(sorted(job_keys)),
    )


@dataclass(frozen=True)
class DetailSignal:
    """Whether one field is really carried by a page, and by what.

    `carrier` is the honest part of this record: it names the generic mechanism
    that supplied the value, so "we can read the title" is never confused with
    "some selector matched something". A signal with `found=False` and
    `carrier=None` means no generic carrier on the page supplied it — reported
    as an absence, which is a finding, rather than filled in with a guess.

    `value` is a short normalized excerpt for verification only, and is always
    `None` for the description: a job description is third-party content and
    this repository stores its *length*, not its text.
    """

    found: bool
    carrier: str | None = None
    value: str | None = None
    chars: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "found": self.found,
            "carrier": self.carrier,
            "value": self.value,
            "chars": self.chars,
        }


def _short(value: object) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    return text[:MAX_SIGNAL_VALUE_CHARS]


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


def _flatten(value: object) -> str:
    """Render a JSON-LD scalar or shallow list of scalars as text."""
    if isinstance(value, list):
        return ", ".join(
            normalize_text(item) for item in value if isinstance(item, (str, int))
        )
    if isinstance(value, (str, int)):
        return normalize_text(str(value))
    return ""


def detail_signals(html: str) -> dict[str, DetailSignal]:
    """Report, per signal, whether the public HTML carries it and through what.

    The lookup order per signal runs from most to least structured — JSON-LD
    `JobPosting`, then microdata, then OpenGraph/`<meta>`, then the document's
    own `<title>`/`<h1>`/`<time>` — and stops at the first carrier that really
    supplies a value. Nothing site-specific is consulted, so a `False` here says
    "the page does not publish this through any standard mechanism", which is
    exactly the structural finding 7C.4A is for. It does not say the field is
    invisible to a human, and it is not an instruction to go and invent a
    selector for it.

    `publication_date` deliberately has no sitemap input of any kind. If the
    page states no date, the answer is "not found" — never the `<lastmod>` of
    the sitemap row that led us here.
    """
    collector = _HtmlCollector()
    collector.feed(html or "")
    meta, props = collector.meta, collector.itemprops
    nodes, _, _ = _jsonld_nodes(html)
    posting: dict[str, object] = {}
    for node in nodes:
        if "JobPosting" in _node_types(node):
            posting = node
            break

    def from_jsonld(*path: str) -> tuple[str | None, str | None]:
        if not posting:
            return None, None
        text = _flatten(_dig(posting, *path)) if len(path) > 1 else _flatten(
            posting.get(path[0])
        )
        if not text:
            return None, None
        return text, f"jsonld:JobPosting.{'.'.join(path)}"

    def from_microdata(*names: str) -> tuple[str | None, str | None]:
        for name in names:
            if (text := normalize_text(props.get(name, ""))):
                return text, f"microdata:itemprop={name}"
        return None, None

    def from_meta(*keys: str) -> tuple[str | None, str | None]:
        for key in keys:
            if (text := normalize_text(meta.get(key, ""))):
                return text, f"meta:{key}"
        return None, None

    def first(*candidates: tuple[str | None, str | None]) -> tuple[str | None, str | None]:
        for text, carrier in candidates:
            if text:
                return text, carrier
        return None, None

    def signal(*candidates: tuple[str | None, str | None]) -> DetailSignal:
        text, carrier = first(*candidates)
        if not text:
            return DetailSignal(found=False)
        return DetailSignal(True, carrier, _short(text), len(text))

    document_title = (
        (collector.first_h1, "html:h1"),
        (collector.title, "html:title"),
    )
    findings = {
        "title": signal(
            from_jsonld("title"), from_microdata("title"), from_meta("og:title"),
            *document_title,
        ),
        "organization": signal(
            from_jsonld("hiringOrganization", "name"),
            from_microdata("hiringorganization", "organization"),
        ),
        "location": signal(
            from_jsonld("jobLocation", "address", "addressLocality"),
            from_jsonld("jobLocation", "address", "addressRegion"),
            from_microdata("addresslocality", "joblocation"),
        ),
        "contract_type": signal(
            from_jsonld("employmentType"), from_microdata("employmenttype")
        ),
        "internship_type": signal(
            from_jsonld("occupationalCategory"),
            from_microdata("occupationalcategory"),
        ),
        "work_mode": signal(
            from_jsonld("jobLocationType"), from_microdata("joblocationtype")
        ),
        "publication_date": signal(
            from_jsonld("datePosted"),
            from_microdata("dateposted", "datepublished"),
            from_meta("article:published_time"),
            (collector.times[0] if collector.times else None, "html:time[datetime]"),
        ),
        "deadline": signal(
            from_jsonld("validThrough"),
            from_microdata("validthrough"),
            from_meta("article:expiration_time"),
        ),
        "application_url": signal(
            from_jsonld("url"), from_microdata("url"), from_meta("og:url")
        ),
    }

    # The description is measured, never quoted: a full job description is
    # third-party content, and this audit reports that the page carries one and
    # how long it is, not what it says.
    text, carrier = first(
        from_jsonld("description"),
        from_microdata("description"),
        from_meta("og:description", "description"),
    )
    findings["description"] = (
        DetailSignal(True, carrier, None, len(text)) if text else DetailSignal(False)
    )
    return {name: findings[name] for name in DETAIL_SIGNALS}


@dataclass
class DetailObservation:
    """One sampled offer page, described structurally and never quoted."""

    source_external_id: str
    canonical_url: str
    sitemap_lastmod: str | None = None
    status_code: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    body_bytes: int = 0
    barrier: str | None = None
    error: str | None = None
    has_next_data: bool = False
    jsonld: dict[str, object] = field(default_factory=dict)
    signals: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "source_external_id": self.source_external_id,
            "canonical_url": self.canonical_url,
            # Carried alongside, never merged into a publication date.
            "sitemap_lastmod": self.sitemap_lastmod,
            "status_code": self.status_code,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "body_bytes": self.body_bytes,
            "barrier": self.barrier,
            "error": self.error,
            "has_next_data_script": self.has_next_data,
            "jsonld": self.jsonld,
            "signals": self.signals,
        }


def describe_detail_page(html: str) -> dict[str, object]:
    """Summarize one offer page: JSON-LD shape, signal presence, nothing more."""
    collector = _HtmlCollector()
    collector.feed(html or "")
    return {
        "has_next_data_script": collector.has_next_data,
        "jsonld": summarize_jsonld(html).as_dict(),
        "signals": {
            name: found.as_dict() for name, found in detail_signals(html).items()
        },
    }


def signal_support(observations: list[DetailObservation]) -> dict[str, object]:
    """Across the sample, how many pages carried each signal, and by what.

    The aggregate a reviewer actually reads: "3/3 pages published a title
    through JSON-LD, 0/3 published a deadline at all". A zero is a result, and
    the right response to it is to record that the field is not available
    through standard markup — not to go hunting for a bespoke selector.
    """
    usable = [item for item in observations if item.signals]
    summary: dict[str, object] = {}
    for name in DETAIL_SIGNALS:
        carriers = Counter(
            str(item.signals[name]["carrier"])
            for item in usable
            if item.signals.get(name, {}).get("found")
        )
        summary[name] = {
            "pages_with_signal": sum(carriers.values()),
            "pages_sampled": len(usable),
            "carriers": dict(sorted(carriers.items())),
        }
    return summary


# ------------------------------------------------------------------ terms ----

#: No terms URL is hardcoded, because none has been evidenced. 7C.3 could name
#: ReKrute's because somebody had actually opened it; inventing a
#: plausible-looking `/conditions-generales` for this site would be fabrication
#: of exactly the kind this evaluation layer exists to prevent. An operator who
#: can see the site passes the real URL with `--terms-url`, and until then the
#: report says the check was not attempted and why.
TERMS_NOT_ATTEMPTED = "NOT_ATTEMPTED_NO_EVIDENCED_TERMS_URL"

TERMS_REVIEW_NOTE = (
    "Structural check only: URL, status and reachability. This audit does not "
    "read, quote, classify or interpret the text, and makes no legal "
    "determination in either direction. Reachability is not permission and "
    "silence is not permission; a human must read the terms in full. If the "
    "only public page of this kind is commercial or payment terms rather than "
    "site-use terms, that is a limitation of this check, not a finding."
)


__all__ = [
    "AUDIT_USER_AGENT",
    "DEFAULT_DETAIL_LIMIT",
    "DETAIL_SAMPLE_STRATEGY",
    "DETAIL_SIGNALS",
    "MAX_DETAIL_LIMIT",
    "MAX_SIGNAL_VALUE_CHARS",
    "MAX_XML_BYTES",
    "OFFER_PATH_PREFIX",
    "PFE_TARGET_URL",
    "REJECT_MISSING_NUMERIC_ID",
    "REJECT_NOT_A_URL",
    "REJECT_OFF_DOMAIN",
    "REJECT_WRONG_PATH_FAMILY",
    "ROBOTS_ABSENT",
    "ROBOTS_BARRIER",
    "ROBOTS_OBEY",
    "ROBOTS_UNRESOLVED",
    "ROBOTS_URL",
    "STAGIAIRES_HOSTS",
    "TERMS_NOT_ATTEMPTED",
    "TERMS_REVIEW_NOTE",
    "DetailObservation",
    "DetailSignal",
    "IdCollision",
    "JsonLdSummary",
    "OfferSitemapParse",
    "RejectedSitemapUrl",
    "RobotsDisposition",
    "RobotsGroup",
    "RobotsRule",
    "RobotsVerdict",
    "SitemapAudit",
    "SitemapIndexParse",
    "SitemapOfferEntry",
    "StagiairesAccessError",
    "canonical_url",
    "classify_robots_response",
    "combine_offer_sitemaps",
    "describe_detail_page",
    "detail_signals",
    "detect_access_barrier",
    "extract_external_id",
    "is_stagiaires_host",
    "normalize_text",
    "parse_offer_sitemap",
    "parse_robots_txt",
    "parse_sitemap_index",
    "robots_verdict",
    "select_detail_sample",
    "signal_support",
    "sitemap_declarations",
    "summarize_jsonld",
    "validate_detail_limit",
]
