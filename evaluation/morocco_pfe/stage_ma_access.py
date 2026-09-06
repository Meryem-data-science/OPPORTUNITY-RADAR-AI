"""Network-free analysis for the Stage.ma public-access audit (Phase 7C.5A).

Stage.ma is the source of both seed rows of `morocco_pfe_gold_v1`, which makes
it evidence we already trust — and evidence about two postings in the past is
not evidence that the site can be collected today. 7C.5A asks the narrower
question a collector would have to answer first: *does Stage.ma expose a stable,
browser-free discovery and detail structure?* This module is the pure half of
that question. It opens no socket; the network edge is
`evaluation.morocco_pfe.cli.stage_ma_access_audit`.

**A negative answer is a real answer.** This audit is allowed to conclude that
discovery is insufficient or that the structure will not support a collector,
and the feasibility vocabulary below has words for exactly that. Nothing here
tries to make a production design out of weak evidence.

The robots, redirect and fail-closed rules are **re-stated** here rather than
imported from the Stagiaires audit. Each audit is a record of the rules that
were applied when its evidence was produced, and one source's module quietly
changing another source's findings is the coupling that record exists to avoid.
A test asserts the two modules still agree on the shared robots semantics, so
they cannot drift apart unnoticed either.

Three rules carried over from 7C.4A and one new to this slice:

* **UNKNOWN stays UNKNOWN.** A field the page does not publish is absent, not a
  placeholder and not a value borrowed from somewhere else;
* **the numeric ID in `/offres-stage/<id>-<slug>` is a candidate identity**, not
  a proven key, and never a proxy for recency or for a publication date;
* **reachability is not permission.** A public HTTP 200 is a fact about a
  request, and this audit makes no legal claim in either direction;
* **a displayed date is a candidate, not a publication timestamp.** Stage.ma is
  known to show dates; `01/01/1970` and its relatives are flagged as sentinels
  rather than proposed as real ones, and nothing is fabricated to replace them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

#: Identifies this audit in the report, so a stored finding can be traced back
#: to the rules that produced it.
AUDIT_VERSION = "stage-ma-access-v1"

#: The audit identifies itself honestly. No browser is impersonated, because
#: impersonating one is exactly the evasion this phase forbids.
AUDIT_USER_AGENT = "OpportunityRadarAI-AccessAudit/1.0 (+public access audit; GET-only)"

#: The public hosts this audit will talk to. `www` and the bare domain are both
#: accepted as published and are not rewritten into each other: which one the
#: site considers canonical is not something we have evidence for.
STAGE_MA_HOSTS = frozenset({"www.stage.ma", "stage.ma"})

TARGET_HOST = "www.stage.ma"

#: The one URL written down. Everything else is either a bounded, named audit
#: candidate below or is discovered from what the site itself declares.
ROBOTS_URL = "https://www.stage.ma/robots.txt"

#: The bounded set of public surfaces this audit examines. These are
#: **candidates to inspect**, not a production discovery contract: the audit's
#: job is to find out what they really expose, and a name that looks right is
#: not evidence that a page is useful. There is deliberately no flag to add
#: more — an audit that fetches whatever it is told is not an audit of this site.
SURFACE_HOME = "https://www.stage.ma/"
SURFACE_LISTING = "https://www.stage.ma/offres-stage"
SURFACE_SPECIALTY = "https://www.stage.ma/specialites/computer-science"
SURFACE_URLS = (SURFACE_HOME, SURFACE_LISTING, SURFACE_SPECIALTY)

#: The offer-detail path family observed in the committed benchmark rows.
OFFER_PATH_PREFIX = "/offres-stage/"

#: `/offres-stage/<digits>` with an optional `-<slug>`.
_OFFER_PATH_PATTERN = re.compile(r"^/offres-stage/(\d+)(?:-[^/]*)?$")

#: `/specialites/<something>`: a category surface, recorded so the audit can say
#: whether specialty pages are materially more useful than the generic listing.
_SPECIALTY_PATH_PATTERN = re.compile(r"^/specialites/[^/]+/?$")

DEFAULT_DETAIL_LIMIT = 3
MAX_DETAIL_LIMIT = 5

#: A guard against a hostile or accidental multi-megabyte document.
MAX_XML_BYTES = 20_000_000

#: How a sampled detail page came to be sampled. The distinction is the point:
#: a benchmark row proves a page existed once, and can never stand in as
#: evidence that discovery works *today*.
DISCOVERY_LIVE = "LIVE_DISCOVERED"
DISCOVERY_CANARY = "BENCHMARK_CANARY"

#: The two real Stage.ma rows committed in `morocco_pfe_gold_v1`. They are
#: structure canaries only: fetched, if at all, to learn whether a detail page
#: still parses, never to claim that anything discovered them.
BENCHMARK_CANARY_URLS = (
    "https://www.stage.ma/offres-stage/9279-stage-pfe-intelligence-artificielle",
    "https://www.stage.ma/offres-stage/"
    "9233-stagepfe-ingenieur-ia-automatisation-nlp-optimisation-pour-un-saas",
)


class StageMaAccessError(RuntimeError):
    """Raised when the audit cannot proceed honestly."""


def validate_detail_limit(value: object) -> int:
    """Return a detail-page limit within the audit's hard bound.

    `bool` is rejected explicitly: `True` is an `int` in Python, and a flag
    silently becoming "fetch 1 page" is the kind of quiet nonsense this
    repository's other limit validators already refuse.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise StageMaAccessError("detail limit must be an integer")
    if not 1 <= value <= MAX_DETAIL_LIMIT:
        raise StageMaAccessError(
            f"detail limit must be between 1 and {MAX_DETAIL_LIMIT}"
        )
    return value


# ---------------------------------------------------------------- robots ----


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
    """Whether a URL may be fetched, and which line decided it."""

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
    """Parse robots.txt into groups, tolerating comments and stray lines.

    An empty `Disallow:` imposes **no rule**. It must never become `Allow: /`:
    given `Disallow: /` followed by `Disallow:`, a synthesized allow would tie
    with the real refusal on pattern length, win the tie-break, and silently
    turn "nothing is allowed" into "everything is".
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

    Group-independent, so read from the whole file. This is the audit's only
    sanctioned way to learn that a sitemap exists. Guessing `sitemap.xml` and
    treating a lucky 200 as an official discovery contract is exactly the
    fabrication this evaluation layer exists to prevent.
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
    ignores every rule keyed on a parameter, so `Disallow: /*?print=1` would not
    stop `/offres-stage?print=1`. One helper, used everywhere a URL is judged.
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

    Deliberately not a substring test in either direction: an unrelated group
    whose short name happened to occur inside ours could just as easily be a
    permissive group as a restrictive one.
    """
    return bool(agent) and (agent == token or token.startswith(agent))


def _applicable_rules(
    groups: tuple[RobotsGroup, ...], user_agent: str
) -> tuple[tuple[RobotsRule, ...], tuple[str, ...]]:
    """Every rule addressing this agent, merged across records.

    All matching groups are merged rather than only the first: a file may name
    the same agent twice, and stopping at the first record discards every rule
    after it, including a `Disallow` a later record adds. The wildcard fallback
    merges all `*` records and applies only when no group names us.
    """
    token = product_token(user_agent)
    rules: list[RobotsRule] = []
    matched: list[str] = []
    for group in groups:
        if any(_agent_applies(agent, token) for agent in group.agents):
            rules.extend(group.rules)
            matched.extend(group.agents)
    if rules or matched:
        return tuple(rules), tuple(dict.fromkeys(matched))
    for group in groups:
        if "*" in group.agents:
            rules.extend(group.rules)
            matched.extend(group.agents)
    return tuple(rules), tuple(dict.fromkeys(matched))


def robots_verdict(
    groups: tuple[RobotsGroup, ...], url: str, user_agent: str = AUDIT_USER_AGENT
) -> RobotsVerdict:
    """Decide whether ``url`` is fetchable: longest match wins, Allow breaks ties.

    Takes a full URL rather than a path, so `robots_target` is applied here and
    a caller cannot forget the query half of the rule.
    """
    rules, agents = _applicable_rules(groups, user_agent)
    matched_agent = ", ".join(agents) or None
    target = robots_target(url)
    best: RobotsRule | None = None
    for rule in rules:
        if not _rule_regex(rule.pattern).match(target):
            continue
        if (
            best is None
            or len(rule.pattern) > len(best.pattern)
            or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
        ):
            best = rule
    if best is None:
        return RobotsVerdict(True, None, matched_agent)
    return RobotsVerdict(
        allowed=best.allow,
        matched_rule=f"{'Allow' if best.allow else 'Disallow'}: {best.pattern}",
        matched_agent=matched_agent,
    )


# --------------------------------------------------------- robots policy ----

#: Four outcomes rather than "200 or not": a robots.txt we were *refused* tells
#: us nothing about what is allowed, and reading that silence as permission is
#: the failure this policy exists to prevent.
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


def classify_robots_response(
    status_code: int, final_url: str, body: str
) -> RobotsDisposition:
    """Decide what a robots.txt response permits.

    * **200** — parse it and obey it, unless the response is transparently a bot
      wall or a login page rather than the file itself.
    * **404 / 410** — the file is definitively absent, so no explicit rule
      applies and the audit may continue. That is a statement about robots.txt
      alone and is **not** permission of any kind, legal or otherwise.
    * **401 / 403 / 407 / 429** — we were refused the file, so we do not know
      what it says. An unknown rule is never assumed permissive: the audit stops
      before touching any discovery surface.
    * **5xx, anything else, or an unreadable response** — unresolved. The audit
      is incomplete rather than permitted.
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


#: Markers that a page is unavailable rather than refused — a deleted or expired
#: offer, which is a *finding about the source's lifecycle*, not a barrier.
_UNAVAILABLE_MARKERS = (
    "page introuvable",
    "page non trouv",
    "404",
    "n'existe plus",
    "no longer available",
    "not found",
)


#: What kind of problem a response represents. Three kinds, not one, because
#: they mean different things about the source and must not share an outcome:
#:
#: * `BARRIER` — we were **refused**. A wall, and the audit stops at it.
#: * `UNREADABLE` — we could not read something: a timeout, a 5xx, a document
#:   that is not what it claims to be. Nothing was refused; the audit simply
#:   does not know, so it is incomplete rather than permitted.
#: * `UNAVAILABLE` — the page is gone (404/410). That is a fact about the
#:   *source's lifecycle*, not about our access, and what it means depends
#:   entirely on which page: a deleted offer is an ordinary observation, while a
#:   robots-declared sitemap that 404s is a required document we could not read.
#:   The caller decides, which is why this classifier refuses to.
ISSUE_BARRIER = "BARRIER"
ISSUE_UNREADABLE = "UNREADABLE"
ISSUE_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ResponseIssue:
    """A named problem with one response, and the kind of problem it is."""

    kind: str
    code: str

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "code": self.code}


def classify_response(
    status_code: int, final_url: str, body: str
) -> ResponseIssue | None:
    """Name what is wrong with a response, or ``None`` if it looks public.

    Nothing here is worked around. Note what is deliberately **not** a barrier:
    a 5xx is a server that failed, not a refusal; a 404 is a page that is gone;
    and a thin or JavaScript-only body is a site that did not send the data in
    its HTML, which is the *discovery* finding this whole audit exists to
    report. Calling any of those a refusal would turn "this source needs a
    browser" into a failed run, and 7C.5A must be able to complete and say
    exactly that.
    """
    if status_code == 403:
        return ResponseIssue(ISSUE_BARRIER, "HTTP_403_FORBIDDEN")
    if status_code == 429:
        return ResponseIssue(ISSUE_BARRIER, "HTTP_429_RATE_LIMITED")
    if status_code in {401, 407}:
        return ResponseIssue(ISSUE_BARRIER, "AUTHENTICATION_REQUIRED")
    sample = (body or "")[:20000].lower()
    if any(marker in sample for marker in _CHALLENGE_MARKERS):
        return ResponseIssue(ISSUE_BARRIER, "BOT_CHALLENGE_OR_CAPTCHA")
    path = urlsplit(final_url or "").path.lower()
    if any(segment in path for segment in _LOGIN_PATH_SEGMENTS):
        return ResponseIssue(ISSUE_BARRIER, "REDIRECTED_TO_LOGIN")
    if status_code >= 500:
        return ResponseIssue(ISSUE_UNREADABLE, f"HTTP_{status_code}_SERVER_ERROR")
    if status_code in {404, 410}:
        return ResponseIssue(ISSUE_UNAVAILABLE, "PAGE_UNAVAILABLE")
    if status_code != 200:
        return ResponseIssue(ISSUE_UNREADABLE, f"HTTP_{status_code}")
    return None


# ------------------------------------------------------------------ URLs ----


def canonical_url(url: str) -> str:
    """Normalize a public Stage.ma URL conservatively.

    Only meaningless parts are dropped: scheme and host lowercased, default port
    removed, fragment and query dropped, trailing slash trimmed. Path case is
    left as published, and `www.stage.ma` is not rewritten to `stage.ma` or back
    — deciding which the site considers canonical is a claim this audit has no
    evidence for.
    """
    if not isinstance(url, str) or not url.strip():
        raise StageMaAccessError("URL must be a non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"}:
        raise StageMaAccessError(f"not an http(s) URL: {url!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise StageMaAccessError(f"URL has no host: {url!r}")
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
    except StageMaAccessError:
        return False
    return host in STAGE_MA_HOSTS


def extract_candidate_id(url: str) -> str | None:
    """Return the numeric component of a Stage.ma ``/offres-stage/<id>-<slug>``.

    A **candidate** `source_external_id` and nothing more. The digits are
    returned as the site published them, not as an `int`: `007` and `7` are
    different published identities. `None` means this URL is not a Stage.ma
    offer URL in the known shape — a finding, never a zero. Nothing anywhere
    reads this value as a date or as a position in time.

    The host is part of the question. Another site can publish a path that looks
    identical, and an audit of Stage.ma has no business treating someone else's
    URL as one of its offers just because the path matches.
    """
    try:
        parts = urlsplit(canonical_url(url))
    except StageMaAccessError:
        return None
    if (parts.hostname or "") not in STAGE_MA_HOSTS:
        return None
    match = _OFFER_PATH_PATTERN.match(parts.path)
    return match.group(1) if match else None


def is_offer_detail_url(url: str) -> bool:
    return extract_candidate_id(url) is not None


def is_specialty_url(url: str) -> bool:
    """Whether ``url`` looks like a `/specialites/<name>` category surface."""
    try:
        parts = urlsplit(canonical_url(url))
    except StageMaAccessError:
        return False
    if (parts.hostname or "") not in STAGE_MA_HOSTS:
        return False
    return bool(_SPECIALTY_PATH_PATTERN.match(parts.path))


# --------------------------------------------------------------- surfaces ---


class _LinkCollector(HTMLParser):
    """Collect hrefs, the document title, any canonical link, and JSON-LD.

    Deliberately site-agnostic: it reads `<a href>`, `<title>`,
    `<link rel=canonical>`, `<meta>` and `<script type="application/ld+json">` —
    mechanisms any page may use — and knows nothing about Stage.ma's own class
    names. Reporting that a selector somebody guessed matched something is not
    evidence about a site; reporting what the site publishes through standard
    means is.
    """

    _SKIP = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.title: str | None = None
        #: (datetime value, the visible text just before it, itemprop) per
        #: `<time>`. The context is the point: a bare `<time>` says *a* date,
        #: never *which* date, and this audit only accepts one it can tie to a
        #: publication label.
        self.times: list[tuple[str, str, str]] = []
        #: (element text, aria-label/title, href or None) per anchor/button.
        self.controls: list[tuple[str, str, str | None]] = []
        self.text_parts: list[str] = []
        self.framework_markers: list[str] = []
        self._in_jsonld = False
        self._buffer: list[str] = []
        self._capture_title = False
        self._title_parts: list[str] = []
        self._control_stack: list[list[Any]] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if name in self._SKIP:
            self._skip_depth += 1
        if name == "script":
            if "ld+json" in attributes.get("type", "").lower():
                self._in_jsonld = True
                self._buffer = []
            if attributes.get("id", "") in {"__NEXT_DATA__", "__NUXT_DATA__"}:
                self.framework_markers.append(attributes["id"])
        elif name == "a":
            if (href := attributes.get("href", "").strip()):
                self.hrefs.append(href)
        elif name == "link":
            if "canonical" in attributes.get("rel", "").lower():
                if (href := attributes.get("href", "").strip()):
                    self.canonical = href
        elif name == "meta":
            key = attributes.get("property") or attributes.get("name")
            content = attributes.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key.strip().lower(), content)
        elif name == "time":
            if (stamp := attributes.get("datetime", "").strip()):
                preceding = "".join(self.text_parts)[-160:]
                self.times.append(
                    (
                        stamp,
                        normalize_text(preceding),
                        attributes.get("itemprop", "").strip().lower(),
                    )
                )
        elif name == "title":
            self._capture_title = True
            self._title_parts = []

        for marker in ("data-reactroot", "ng-app", "v-app", "data-vue-meta"):
            if marker in attributes:
                self.framework_markers.append(marker)
        if name in {"div", "main"} and attributes.get("id", "") in {"root", "app", "__nuxt"}:
            self.framework_markers.append(f"#{attributes['id']}")

        if name in {"a", "button"}:
            self._control_stack.append(
                [
                    attributes.get("aria-label", "").strip()
                    or attributes.get("title", "").strip(),
                    attributes.get("href", "").strip() or None,
                    [],
                ]
            )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in {"meta", "link", "br", "img", "input", "hr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if name == "script" and self._in_jsonld:
            self._in_jsonld = False
            if (payload := "".join(self._buffer).strip()):
                self.jsonld.append(payload)
        elif name == "title" and self._capture_title:
            self._capture_title = False
            self.title = normalize_text("".join(self._title_parts)) or None
        elif name in {"a", "button"} and self._control_stack:
            labelled, href, parts = self._control_stack.pop()
            self.controls.append(
                (normalize_text("".join(parts)), normalize_text(labelled), href)
            )

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._buffer.append(data)
            return
        if self._capture_title:
            self._title_parts.append(data)
        if not self._skip_depth:
            self.text_parts.append(data)
        if self._control_stack:
            self._control_stack[-1][2].append(data)

    @property
    def visible_text(self) -> str:
        return " ".join("".join(self.text_parts).split())


def normalize_text(value: object) -> str:
    """Collapse whitespace in a string, returning '' for anything else."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def surface_identity(html: str) -> dict[str, object]:
    """A page's own title and canonical link, where it publishes them.

    Both are recorded because a discovery surface that states neither is weaker
    evidence than one that states both: a canonical link is the site telling us
    which URL it considers the real one, which is exactly what an identity for a
    later collector would be built on.
    """
    collector = _LinkCollector()
    collector.feed(html or "")
    return {
        "title_present": collector.title is not None,
        "title": collector.title[:MAX_EVIDENCE_CHARS] if collector.title else None,
        "canonical_present": collector.canonical is not None,
        "canonical_url": collector.canonical,
    }


@dataclass(frozen=True)
class SurfaceLinks:
    """What one public page's ordinary server HTML actually exposes."""

    offer_urls: tuple[str, ...]
    specialty_urls: tuple[str, ...]
    off_domain_links: int
    total_links: int

    def as_dict(self) -> dict[str, object]:
        return {
            "offer_detail_links": len(self.offer_urls),
            "specialty_links": len(self.specialty_urls),
            "off_domain_links": self.off_domain_links,
            "total_links": self.total_links,
            "offer_url_sample": list(self.offer_urls[:5]),
        }


def collect_surface_links(html: str, base_url: str) -> SurfaceLinks:
    """Read a page's links into offer-detail and specialty groups.

    Deduplicated by canonical URL and order-preserving, so "the first five offer
    links" is reproducible. Off-domain links are counted, never followed.
    """
    collector = _LinkCollector()
    collector.feed(html or "")
    offers: list[str] = []
    specialties: list[str] = []
    seen: set[str] = set()
    off_domain = 0
    total = 0
    for href in collector.hrefs:
        total += 1
        try:
            resolved = canonical_url(urljoin(base_url, href.strip()))
        except StageMaAccessError:
            continue
        if not is_stage_ma_host(resolved):
            off_domain += 1
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if is_offer_detail_url(resolved):
            offers.append(resolved)
        elif is_specialty_url(resolved):
            specialties.append(resolved)
    return SurfaceLinks(tuple(offers), tuple(specialties), off_domain, total)


#: Reported when a page returns HTML but the offers are not in it. A heuristic,
#: and labelled as one: it says "the server did not send the offer list", which
#: is the fact a browser-free collector cares about, and never claims to know
#: how the site is built.
def looks_browser_rendered(html: str, links: SurfaceLinks) -> dict[str, object]:
    """Whether the offers appear to be absent from the server-returned HTML."""
    collector = _LinkCollector()
    collector.feed(html or "")
    markers = tuple(dict.fromkeys(collector.framework_markers))
    text_length = len(collector.visible_text)
    return {
        "offer_links_in_server_html": len(links.offer_urls) > 0,
        "framework_markers": list(markers),
        "visible_text_chars": text_length,
        # Only a suspicion, and only when the offers really are missing.
        "appears_to_require_browser": bool(
            not links.offer_urls and (markers or text_length < 400)
        ),
        "note": (
            "Heuristic. It reports that the server-returned HTML did not carry "
            "offer links, which is what a browser-free collector cares about; "
            "it does not claim to know how the page is built."
        ),
    }


# --------------------------------------------------------------- sitemaps ---


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].strip().lower()


def _parse_xml(text: str) -> ElementTree.Element:
    """Parse a sitemap document, refusing anything that is not well-formed XML.

    A fetch that answers with an HTML error page is not a sitemap, and parsing
    it leniently would turn a failed request into an empty-but-successful
    result — an audit reporting "no URLs" because it read the wrong document is
    worse than one that says it could not read it.
    """
    if len(text or "") > MAX_XML_BYTES:
        raise StageMaAccessError(
            f"sitemap XML exceeds the {MAX_XML_BYTES}-byte audit limit"
        )
    try:
        return ElementTree.fromstring((text or "").strip())
    except ElementTree.ParseError as error:
        raise StageMaAccessError(f"document is not well-formed XML: {error}")


@dataclass(frozen=True)
class SitemapParse:
    """What one declared sitemap document turned out to contain."""

    root_tag: str
    offer_urls: tuple[str, ...]
    specialty_urls: tuple[str, ...]
    nested_sitemaps: tuple[str, ...]
    other_same_host_urls: int
    off_domain_urls: int

    def as_dict(self) -> dict[str, object]:
        return {
            "root": self.root_tag,
            "offer_detail_urls": len(self.offer_urls),
            "specialty_urls": len(self.specialty_urls),
            "nested_sitemaps": len(self.nested_sitemaps),
            "other_same_host_urls": self.other_same_host_urls,
            "off_domain_urls_ignored": self.off_domain_urls,
            "offer_url_sample": list(self.offer_urls[:5]),
        }


def parse_sitemap_document(xml_text: str, base_url: str) -> SitemapParse:
    """Read a `<urlset>` or `<sitemapindex>` and classify what it lists.

    Both roots are accepted because the audit's question is "what does the
    declared sitemap contain?", not "does it match a shape we assumed". Only
    same-host URLs are kept; an off-domain entry is counted and ignored, because
    "the sitemap told us to" is not a reason to fetch someone else's host.
    """
    root = _parse_xml(xml_text)
    root_tag = _local_name(root.tag)
    if root_tag not in {"urlset", "sitemapindex"}:
        raise StageMaAccessError(
            f"expected a <urlset> or <sitemapindex> document, got <{root_tag}>"
        )
    offers: list[str] = []
    specialties: list[str] = []
    nested: list[str] = []
    other = 0
    off_domain = 0
    seen: set[str] = set()
    for node in root:
        if _local_name(node.tag) not in {"url", "sitemap"}:
            continue
        raw = next(
            (
                (child.text or "").strip()
                for child in node
                if _local_name(child.tag) == "loc"
            ),
            "",
        )
        if not raw:
            continue
        try:
            resolved = canonical_url(urljoin(base_url, raw))
        except StageMaAccessError:
            continue
        if not is_stage_ma_host(resolved):
            off_domain += 1
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _local_name(node.tag) == "sitemap":
            nested.append(resolved)
        elif is_offer_detail_url(resolved):
            offers.append(resolved)
        elif is_specialty_url(resolved):
            specialties.append(resolved)
        else:
            other += 1
    return SitemapParse(
        root_tag, tuple(offers), tuple(specialties), tuple(nested), other, off_domain
    )


# ------------------------------------------- publication state and dates ----

#: A page's publication state, asserted only when the page's own text supports
#: it. There is no inference from HTTP status alone: a 200 does not mean an
#: offer is open, and a slow server does not mean it expired.
STATE_PUBLISHED = "PUBLISHED"
STATE_UNPUBLISHED = "UNPUBLISHED"
STATE_EXPIRED = "EXPIRED"
STATE_UNKNOWN = "UNKNOWN"

#: The words a site uses to say that a date is *the publication date*. They are
#: what separates a date we may propose as a candidate from one we may not: a
#: page can carry a deadline, a start date and a modification date, and a reader
#: that took the first of them would attach an arbitrary one to the offer.
PUBLICATION_LABELS = (
    "publiée le",
    "publiee le",
    "publié le",
    "publie le",
    "date de publication",
    "published on",
    "date de parution",
)

#: `itemprop`/schema names that identify a `<time>` as the posting date itself.
_PUBLICATION_ITEMPROPS = frozenset({"dateposted", "datepublished", "datecreated"})

#: Case-insensitive text markers, French first because the site is Moroccan and
#: francophone. Each maps to the state its wording actually supports.
_STATE_MARKERS: tuple[tuple[str, str], ...] = (
    ("offre expirée", STATE_EXPIRED),
    ("offre expiree", STATE_EXPIRED),
    ("annonce expirée", STATE_EXPIRED),
    ("expirée", STATE_EXPIRED),
    ("expiree", STATE_EXPIRED),
    ("expired", STATE_EXPIRED),
    ("cette offre n'est plus", STATE_EXPIRED),
    ("date limite dépassée", STATE_EXPIRED),
    ("non publiée", STATE_UNPUBLISHED),
    ("non publiee", STATE_UNPUBLISHED),
    ("unpublished", STATE_UNPUBLISHED),
    ("en attente de validation", STATE_UNPUBLISHED),
) + tuple((label, STATE_PUBLISHED) for label in PUBLICATION_LABELS)

#: The longest excerpt the report will carry for a state signal. Enough to show
#: which words decided it; far too little to be a copy of the page.
MAX_EVIDENCE_CHARS = 120


@dataclass(frozen=True)
class PublicationState:
    """The state a page's own words support, and the words that supported it."""

    state: str
    evidence: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {"state": self.state, "evidence": self.evidence}


def detect_publication_state(html: str) -> PublicationState:
    """Read the publication state from the page's visible text.

    Expiry and non-publication are checked before publication, because a page
    that says "publiée le 3 mars — offre expirée" is expired: the later fact
    overrides the earlier one, and a collector that read only "publiée" would
    store a closed offer as open. A page whose text supports nothing returns
    `UNKNOWN`, which is a result, not a gap to be filled by guessing.
    """
    collector = _LinkCollector()
    collector.feed(html or "")
    text = collector.visible_text.lower()
    for state in (STATE_EXPIRED, STATE_UNPUBLISHED, STATE_PUBLISHED):
        for marker, marked_state in _STATE_MARKERS:
            if marked_state != state or marker not in text:
                continue
            index = text.index(marker)
            start = max(index - 30, 0)
            excerpt = collector.visible_text[start : index + MAX_EVIDENCE_CHARS - 30]
            return PublicationState(state, normalize_text(excerpt) or None)
    return PublicationState(STATE_UNKNOWN)


#: How a date candidate was judged. `VALID_CANDIDATE` means "the site displayed
#: this and it parses" — a candidate for Phase 7C.5B to consider, never a
#: publication timestamp this audit asserts.
DATE_VALID_CANDIDATE = "VALID_CANDIDATE"
DATE_SENTINEL_OR_INVALID = "SENTINEL_OR_INVALID_DATE"
DATE_UNKNOWN = "UNKNOWN"

#: Epoch zero in the spellings a site actually renders it in. A date field that
#: was never set is very often displayed as this, and it is the single most
#: dangerous value to mistake for a real publication date.
_EPOCH_SPELLINGS = frozenset(
    {
        "01/01/1970",
        "1/1/1970",
        "01-01-1970",
        "1970-01-01",
        "1970/01/01",
        "01.01.1970",
        "thu jan 01 1970",
        "0",
    }
)

_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b"), "dmy"),
)


@dataclass(frozen=True)
class DateCandidate:
    """A displayed date, judged but never asserted as a publication timestamp."""

    status: str
    raw: str | None = None
    normalized: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "raw": self.raw,
            "normalized": self.normalized,
            "is_published_at": False,
        }


def classify_date_candidate(value: object) -> DateCandidate:
    """Judge a displayed date without ever repairing or replacing it.

    Three outcomes and no fourth: the site showed nothing (`UNKNOWN`), it showed
    something that reads as a real date (`VALID_CANDIDATE`, normalized to ISO for
    comparison only), or it showed a sentinel or something unparseable
    (`SENTINEL_OR_INVALID_DATE`).

    Epoch zero is the case this exists for. A date column that was never set
    renders as `01/01/1970` far more often than any site admits, and storing it
    as a publication date would put a 1970 timestamp on a 2026 internship. It is
    flagged, and **nothing is invented to stand in its place** — no crawl time,
    no sitemap modification time, no date derived from the numeric URL ID.
    """
    text = normalize_text(value)
    if not text:
        return DateCandidate(DATE_UNKNOWN)
    lowered = text.lower()
    if lowered in _EPOCH_SPELLINGS or any(
        spelling in lowered for spelling in ("1970-01-01", "01/01/1970", "01-01-1970")
    ):
        return DateCandidate(DATE_SENTINEL_OR_INVALID, text[:MAX_EVIDENCE_CHARS])
    for pattern, order in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        first, second, third = (int(part) for part in match.groups())
        year, month, day = (
            (first, second, third) if order == "ymd" else (third, second, first)
        )
        try:
            parsed = date(year, month, day)
        except ValueError:
            return DateCandidate(DATE_SENTINEL_OR_INVALID, text[:MAX_EVIDENCE_CHARS])
        if parsed.year <= 1970:
            return DateCandidate(
                DATE_SENTINEL_OR_INVALID, text[:MAX_EVIDENCE_CHARS], parsed.isoformat()
            )
        return DateCandidate(
            DATE_VALID_CANDIDATE, text[:MAX_EVIDENCE_CHARS], parsed.isoformat()
        )
    return DateCandidate(DATE_SENTINEL_OR_INVALID, text[:MAX_EVIDENCE_CHARS])


def publication_date_candidate(html: str) -> DateCandidate:
    """Find a date the page presents **as its publication date**, and judge it.

    A date is only a candidate when the page says it is the publication date.
    Three carriers qualify, in descending order of explicitness:

    1. schema.org `JobPosting.datePosted`;
    2. a `<time datetime>` whose own `itemprop` names it as the posting date, or
       whose immediately preceding text carries a publication label;
    3. text following an explicit label — "Publiée le", "Date de publication".

    Two and three are the same evidence in two forms, and the machine-readable
    one is preferred where a page offers both.

    A **bare `<time>` does not qualify**, and this is the correction that
    matters. An offer page routinely carries several dates — an application
    deadline, an internship start date, a last-modified stamp — and taking the
    first one on the page would attach an arbitrary date to the offer while
    looking entirely principled. When no carrier ties a date to publication the
    answer is `UNKNOWN`, and nothing fills it in: not a deadline, not a start
    date, not a sitemap timestamp, not the crawl time, and nothing derived from
    the numeric URL id.
    """
    posting = job_posting_node(html)
    if posting and (posted := normalize_text(posting.get("datePosted"))):
        return classify_date_candidate(posted)

    collector = _LinkCollector()
    collector.feed(html or "")

    # A `<time>` the page ties to publication is read before the label's own
    # rendered text: "Publiée le <time datetime="2026-03-15">15 mars</time>"
    # states the date twice, and the machine-readable half is the one that
    # parses. Reading "15 mars" instead would flag a perfectly good date as
    # malformed.
    for stamp, preceding, itemprop in collector.times:
        context = preceding.lower()
        if itemprop in _PUBLICATION_ITEMPROPS or any(
            label in context for label in PUBLICATION_LABELS
        ):
            return classify_date_candidate(stamp)

    text = collector.visible_text
    lowered = text.lower()
    for label in PUBLICATION_LABELS:
        if label in lowered:
            index = lowered.index(label) + len(label)
            return classify_date_candidate(text[index : index + 40])
    return DateCandidate(DATE_UNKNOWN)


# ------------------------------------------------------- detail structure ---


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


def summarize_structured_data(html: str) -> dict[str, object]:
    """Report which schema.org types and key names a page publishes.

    Key *names*, never values: the point is to learn whether Stage.ma exposes
    structured data a later collector could rely on, without copying third-party
    job content into this repository.
    """
    collector = _LinkCollector()
    collector.feed(html or "")
    types: Counter[str] = Counter()
    posting_keys: set[str] = set()
    invalid = 0
    postings = 0
    for block in collector.jsonld:
        try:
            document = json.loads(block)
        except ValueError:
            invalid += 1
            continue
        for node in _iter_nodes(document):
            node_types = _node_types(node)
            types.update(node_types)
            if "JobPosting" in node_types:
                postings += 1
                posting_keys.update(str(key) for key in node)
    return {
        "block_count": len(collector.jsonld),
        "invalid_block_count": invalid,
        "types": sorted(types),
        "job_posting_count": postings,
        "job_posting_keys": sorted(posting_keys),
    }


def job_posting_node(html: str) -> dict[str, Any] | None:
    """Return the page's first schema.org `JobPosting` node, or ``None``."""
    collector = _LinkCollector()
    collector.feed(html or "")
    for block in collector.jsonld:
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


#: The fields a later collector would need, in report order. 7C.5A reports
#: whether the page carries each and through which generic mechanism; it does
#: not design a parser, and a field nothing supplies is reported absent.
DETAIL_FIELDS = (
    "title",
    "organization",
    "location",
    "specialty",
    "internship_type",
    "work_mode",
    "duration",
    "description",
)


@dataclass(frozen=True)
class FieldEvidence:
    """Whether one field is carried, by what, and a bounded sample of it."""

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


def detail_field_evidence(html: str) -> dict[str, FieldEvidence]:
    """Report, per field, whether the public HTML carries it and through what.

    The lookup runs from most to least structured — JSON-LD `JobPosting`, then
    OpenGraph/`<meta>`, then the document's own `<title>` — and stops at the
    first carrier that really supplies a value. Nothing site-specific is
    consulted, so `found=False` means the page does not publish that field
    through any standard mechanism. That is the structural finding this audit is
    for; it is not an instruction to go and invent a selector.

    The description is **measured, never quoted**: a job description is
    third-party content, and this repository stores its length.
    """
    posting = job_posting_node(html) or {}
    collector = _LinkCollector()
    collector.feed(html or "")
    meta = collector.meta

    def from_posting(*path: str) -> tuple[str | None, str | None]:
        value = _dig(posting, *path) if len(path) > 1 else posting.get(path[0])
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value if isinstance(item, (str, int)))
        text = normalize_text(value if isinstance(value, str) else str(value or ""))
        return (text, f"jsonld:JobPosting.{'.'.join(path)}") if text else (None, None)

    def from_meta(*keys: str) -> tuple[str | None, str | None]:
        for key in keys:
            if (text := normalize_text(meta.get(key, ""))):
                return text, f"meta:{key}"
        return None, None

    def first(*candidates: tuple[str | None, str | None]) -> FieldEvidence:
        for text, carrier in candidates:
            if text:
                return FieldEvidence(
                    True, carrier, text[:MAX_EVIDENCE_CHARS], len(text)
                )
        return FieldEvidence(False)

    evidence = {
        "title": first(
            from_posting("title"),
            from_meta("og:title"),
            (collector.title, "html:title"),
        ),
        "organization": first(from_posting("hiringOrganization", "name")),
        "location": first(
            from_posting("jobLocation", "address", "addressLocality"),
            from_posting("jobLocation", "address", "addressRegion"),
        ),
        "specialty": first(from_posting("occupationalCategory"), from_posting("industry")),
        "internship_type": first(from_posting("employmentType")),
        "work_mode": first(from_posting("jobLocationType")),
        "duration": first(from_posting("estimatedSalary", "duration"),
                          from_posting("employmentUnit", "name")),
    }
    described, carrier = None, None
    for text, source in (
        from_posting("description"),
        from_meta("og:description", "description"),
    ):
        if text:
            described, carrier = text, source
            break
    evidence["description"] = (
        FieldEvidence(True, carrier, None, len(described))
        if described
        else FieldEvidence(False)
    )
    return {name: evidence[name] for name in DETAIL_FIELDS}


# ------------------------------------------------------- application action --

APPLICATION_INTENT_MARKERS = (
    "postuler",
    "postulez",
    "candidater",
    "candidature",
    "apply",
    "déposer",
    "deposer",
)

APPLY_SAME_HOST = "SAME_HOST"
APPLY_OFF_DOMAIN = "OFF_DOMAIN"
APPLY_NONE = "NONE"


@dataclass(frozen=True)
class ApplicationEvidence:
    """Whether the page offers an explicit application action, and where to."""

    found: bool
    label: str | None = None
    href_category: str = APPLY_NONE
    href: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "found": self.found,
            "label": self.label,
            "href_category": self.href_category,
            "href": self.href,
            "fetched": False,
        }


def application_evidence(html: str, base_url: str) -> ApplicationEvidence:
    """Find an explicit application control, if the page offers one.

    Deliberately not the page's canonical URL, `og:url` or `JobPosting.url`:
    those name the posting — the page we are already on — and every offer has
    one, so reading them as an application route would claim an application
    route for every offer ever published, including closed ones. What counts is
    a control that *says* it applies.

    An off-domain href is **recorded and categorized, never fetched**: an
    employer's own ATS is exactly what a reviewer needs to see, and recording a
    URL is not requesting it.
    """
    collector = _LinkCollector()
    collector.feed(html or "")
    for text, labelled, href in collector.controls:
        matched = next(
            (
                name
                for name in (text, labelled)
                if name
                and any(marker in name.lower() for marker in APPLICATION_INTENT_MARKERS)
            ),
            None,
        )
        if matched is None:
            continue
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            # A control with no usable target is still evidence that the site
            # offers applying; it just does not give us a URL.
            return ApplicationEvidence(
                True, matched[:MAX_EVIDENCE_CHARS], APPLY_NONE, None
            )
        resolved = urljoin(base_url, href)
        if urlsplit(resolved).scheme not in {"http", "https"}:
            return ApplicationEvidence(
                True, matched[:MAX_EVIDENCE_CHARS], APPLY_NONE, None
            )
        category = APPLY_SAME_HOST if is_stage_ma_host(resolved) else APPLY_OFF_DOMAIN
        return ApplicationEvidence(
            True, matched[:MAX_EVIDENCE_CHARS], category, resolved
        )
    return ApplicationEvidence(False)


# ------------------------------------------------------------- selection ----

#: How the bounded detail sample is chosen, stated in the report so a reviewer
#: never has to read the code to know what they are looking at.
DETAIL_SELECTION_STRATEGY = (
    "canonical URL ascending over the deduplicated live-discovered set; chosen "
    "for deterministic, reproducible selection only — it is NOT an ordering by "
    "recency, and the numeric URL ID is never read as a date or a position in "
    "time"
)


def select_detail_targets(urls: tuple[str, ...], limit: int) -> tuple[str, ...]:
    """Choose which discovered offer pages this bounded run will fetch.

    Deduplicated by canonical URL and ordered by that URL, which is a total
    order over data the site published and makes the sample reproducible. It is
    deliberately not "highest numeric ID first": that would smuggle in a recency
    claim this audit has no evidence for.

    The limit is validated here, so there is exactly one place the hard bound is
    enforced and no path to the network that skips it.
    """
    bounded = validate_detail_limit(limit)
    unique = sorted({url for url in urls if is_offer_detail_url(url)})
    return tuple(unique[:bounded])


# ----------------------------------------------------------- feasibility ----

FEASIBILITY_PUBLIC_HTML = "PUBLIC_HTML_CANDIDATE"
FEASIBILITY_SITEMAP = "SITEMAP_CANDIDATE"
FEASIBILITY_MULTI_SURFACE = "MULTI_SURFACE_CANDIDATE"
FEASIBILITY_INSUFFICIENT_DISCOVERY = "INSUFFICIENT_DISCOVERY"
FEASIBILITY_STRUCTURE_INSUFFICIENT = "STRUCTURE_INSUFFICIENT"
FEASIBILITY_ACCESS_BLOCKED = "ACCESS_BLOCKED"
FEASIBILITY_UNKNOWN = "UNKNOWN"


def derive_feasibility(
    *,
    access_blocked: bool,
    surfaces_with_offers: int,
    sitemap_offer_urls: int,
    live_detail_urls: int,
    sampled_live_pages: int,
    pages_with_title_and_organization: int,
) -> str:
    """Name what the evidence supports, including when it supports nothing.

    Evidence for the Architect, not a Phase 7C.5B decision. The order matters:
    being blocked outranks everything, because we learned nothing about
    structure; no discovery outranks structure, because a parser for pages we
    cannot find is not a collector; and structure is only judged on pages that
    were actually **live-discovered**, so a benchmark canary can never carry a
    feasibility verdict on its own.
    """
    if access_blocked:
        return FEASIBILITY_ACCESS_BLOCKED
    if live_detail_urls == 0 and sitemap_offer_urls == 0:
        return FEASIBILITY_INSUFFICIENT_DISCOVERY
    if sampled_live_pages and pages_with_title_and_organization == 0:
        return FEASIBILITY_STRUCTURE_INSUFFICIENT
    if surfaces_with_offers >= 2 or (surfaces_with_offers and sitemap_offer_urls):
        return FEASIBILITY_MULTI_SURFACE
    if surfaces_with_offers == 1:
        return FEASIBILITY_PUBLIC_HTML
    if sitemap_offer_urls:
        return FEASIBILITY_SITEMAP
    return FEASIBILITY_UNKNOWN


TERMS_NOT_ATTEMPTED = "NOT_ATTEMPTED_NO_EVIDENCED_TERMS_URL"

TERMS_REVIEW_NOTE = (
    "Structural check only: URL, status and reachability. This audit does not "
    "read, quote, classify or interpret the text, and makes no legal "
    "determination in either direction. Reachability is not permission and "
    "silence is not permission; a human must read the terms in full."
)
