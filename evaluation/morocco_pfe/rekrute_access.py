"""Network-free analysis for the ReKrute public-access audit (Phase 7C.3A).

7C.3A must answer "what does ReKrute publicly expose?" *before* anybody writes
a parser against it. This module is the pure half of that question: it turns
bytes somebody else fetched into evidence, and it opens no socket.

Its defining property is that it **discovers rather than assumes**. It does not
know a ReKrute offer URL pattern, a CSS class or a field name, and it never
guesses one. It reports what a page actually contains — which JSON-LD types are
present, which schema.org keys those carry, what shapes the same-host links
have — so that a human reading the report can state ReKrute's real contract.
That report is the evidence 7C.3B needs; this phase deliberately stops there.

`robots.txt` handling is the standard REP reading — longest match wins, `Allow`
breaks ties, `*` and `$` are the two wildcards — and it exists so the audit can
*obey* the file, not merely print it.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from services.collector.parsers.rekrute import REKRUTE_HOSTS, canonical_offer_url

#: Bounded by policy, not by taste: this audit reads a handful of public pages
#: to characterize them. It is not a crawler and must never become one.
DEFAULT_DETAIL_LIMIT = 5
MAX_DETAIL_LIMIT = 10

#: The audit identifies itself honestly. No browser is impersonated, because
#: impersonating one is exactly the evasion this phase forbids.
AUDIT_USER_AGENT = "OpportunityRadarAI-AccessAudit/1.0 (+public access audit; GET-only)"


class RekruteAccessError(RuntimeError):
    """Raised when the audit cannot proceed honestly."""


def validate_detail_limit(value: object) -> int:
    """Return a detail-page limit within the audit's hard bound.

    `bool` is rejected explicitly: `True` is an `int` in Python, and a flag
    silently becoming "fetch 1 page" is the kind of quiet nonsense this
    repository's other limit validators already refuse.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise RekruteAccessError("detail limit must be an integer")
    if not 1 <= value <= MAX_DETAIL_LIMIT:
        raise RekruteAccessError(
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
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not expecting_agents:
                flush()
                agents, rules = [], []
                expecting_agents = True
            agents.append(value.lower())
        elif field in {"allow", "disallow"}:
            expecting_agents = False
            if agents and value:
                rules.append(RobotsRule(allow=field == "allow", pattern=value))
            elif agents and field == "disallow":
                # "Disallow:" with an empty value allows everything; it is a
                # real REP idiom and dropping it would misread the file.
                rules.append(RobotsRule(allow=True, pattern="/"))
    flush()
    return tuple(groups)


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


# ------------------------------------------------------------- discovery ----

class _ScriptCollector(HTMLParser):
    """Collect `<script type="application/ld+json">` bodies and every `href`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.hrefs: list[str] = []
        self._in_jsonld = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if tag.lower() == "script":
            if "ld+json" in attributes.get("type", "").lower():
                self._in_jsonld = True
                self._buffer = []
        elif tag.lower() == "a" and attributes.get("href"):
            self.hrefs.append(attributes["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_jsonld:
            self._in_jsonld = False
            if (payload := "".join(self._buffer).strip()):
                self.jsonld.append(payload)

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._buffer.append(data)


@dataclass(frozen=True)
class JsonLdSummary:
    """What JSON-LD a page really carries — types and keys, never values."""

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


def summarize_jsonld(html: str) -> JsonLdSummary:
    """Report which schema.org types and JobPosting keys a page publishes.

    Reports *keys*, never values: the point is to learn whether ReKrute exposes
    structured `JobPosting` data and which fields it fills, without copying
    third-party job content into this repository.
    """
    collector = _ScriptCollector()
    collector.feed(html or "")
    types: Counter[str] = Counter()
    job_keys: set[str] = set()
    job_count = 0
    invalid = 0
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
                job_count += 1
                job_keys.update(str(key) for key in node)
    return JsonLdSummary(
        block_count=len(collector.jsonld),
        invalid_block_count=invalid,
        types=tuple(sorted(types)),
        job_posting_count=job_count,
        job_posting_keys=tuple(sorted(job_keys)),
    )


def extract_rekrute_links(html: str, base_url: str) -> tuple[str, ...]:
    """Return deduplicated, canonical, same-host ReKrute links from a page.

    Generic on purpose: every `<a href>` is resolved against ``base_url`` and
    kept only if it canonicalizes to a ReKrute http(s) URL. No offer-path
    pattern is assumed, because none has been verified. Order is preserved so
    the audit's "first N links" is reproducible.
    """
    collector = _ScriptCollector()
    collector.feed(html or "")
    seen: set[str] = set()
    links: list[str] = []
    for href in collector.hrefs:
        try:
            absolute = urljoin(base_url, href.strip())
            canonical = canonical_offer_url(absolute)
        except Exception:
            continue
        if canonical not in seen:
            seen.add(canonical)
            links.append(canonical)
    return tuple(links)


def path_shapes(urls: tuple[str, ...], top: int = 10) -> tuple[tuple[str, int], ...]:
    """Group URLs by path template so an offer-URL pattern becomes visible.

    Digit runs collapse to `#` and long slugs to `<slug>`, turning many concrete
    paths into a few shapes. This is the audit's main structural finding: it is
    how a reviewer learns ReKrute's real offer URL grammar without anyone
    having guessed it in advance.
    """
    counter: Counter[str] = Counter()
    for url in urls:
        path = urlsplit(url).path or "/"
        shape = re.sub(r"\d+", "#", path)
        shape = re.sub(r"[^/]{25,}", "<slug>", shape)
        counter[shape] += 1
    return tuple(counter.most_common(top))


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
      the audit stops before touching the requested page.
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
            ROBOTS_BARRIER, "ROBOTS_HTTP_429_RATE_LIMITED", "robots.txt was rate limited"
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
        for segment in ("/login", "/signin", "/connexion")
    ):
        return RobotsDisposition(
            ROBOTS_BARRIER, "ROBOTS_REDIRECTED_TO_LOGIN", "robots.txt redirected to login"
        )
    return RobotsDisposition(ROBOTS_OBEY, None, "robots.txt retrieved")


# ------------------------------------------------------------------ terms ----

#: The public terms page an architect observed independently. Recorded as a URL
#: to *check*, with no assumption whatsoever about what it says.
TERMS_URL = "https://www.rekrute.com/conditions-utilisation.html"

#: Case-insensitive markers worth a human's attention when reading the terms.
#: Their presence is a pointer for manual review; their **absence is not
#: permission**, and nothing in this module decides whether anything is allowed.
AUTOMATION_TERM_MARKERS = (
    "robot", "crawler", "scrap", "scraping", "aspir", "automatis", "bot",
)


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping script/style so markup cannot match."""

    _SKIP = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP and self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._depth:
            self.parts.append(data)


def visible_text(html: str) -> str:
    """Return a page's visible text.

    Tags are dropped on purpose: a `<meta name="robots">` element is markup,
    not a statement in the terms, and matching it would manufacture a finding.
    """
    extractor = _TextExtractor()
    try:
        extractor.feed(html or "")
    except Exception:  # malformed markup is evidence, not a crash
        return " ".join((html or "").split())
    return " ".join("".join(extractor.parts).split())


def automation_term_indicators(html: str) -> dict[str, bool]:
    """Report which automation-related words appear in a terms page's text.

    A structural pointer for a human reader and nothing more. This is not a
    legal classifier: an all-`False` result means these words were not found,
    never that automated access is permitted.
    """
    text = visible_text(html).lower()
    return {marker: marker in text for marker in AUTOMATION_TERM_MARKERS}


#: Markers that a response is a bot wall or a login redirect rather than the
#: public page. Matched case-insensitively against a bounded prefix of the body.
_CHALLENGE_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "cf-challenge", "cf_chl",
    "attention required", "checking your browser", "access denied",
    "just a moment", "bot detection", "datadome",
)
_LOGIN_MARKERS = ("se connecter", "connexion", "sign in", "log in", "identifiez-vous")


def detect_access_barrier(status_code: int, final_url: str, body: str) -> str | None:
    """Name the barrier a response represents, or ``None`` if it looks public.

    A barrier is reported, never worked around. 7C.3A stops at the wall.
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
    if any(segment in path for segment in ("/login", "/signin", "/connexion")):
        return "REDIRECTED_TO_LOGIN"
    if status_code == 200 and len(sample.strip()) < 200:
        return "EMPTY_OR_JAVASCRIPT_ONLY_BODY"
    if status_code != 200:
        return f"HTTP_{status_code}"
    if any(marker in sample for marker in _LOGIN_MARKERS) and "<form" in sample:
        # A site-wide header login form is normal; only flag it when the page
        # carries nothing else worth reading.
        if len(sample) < 4000:
            return "POSSIBLE_FORCED_LOGIN"
    return None


__all__ = [
    "AUDIT_USER_AGENT",
    "AUTOMATION_TERM_MARKERS",
    "ROBOTS_ABSENT",
    "ROBOTS_BARRIER",
    "ROBOTS_OBEY",
    "ROBOTS_UNRESOLVED",
    "RobotsDisposition",
    "TERMS_URL",
    "automation_term_indicators",
    "classify_robots_response",
    "visible_text",
    "DEFAULT_DETAIL_LIMIT",
    "MAX_DETAIL_LIMIT",
    "REKRUTE_HOSTS",
    "JsonLdSummary",
    "RekruteAccessError",
    "RobotsGroup",
    "RobotsRule",
    "RobotsVerdict",
    "detect_access_barrier",
    "extract_rekrute_links",
    "parse_robots_txt",
    "path_shapes",
    "robots_verdict",
    "summarize_jsonld",
    "validate_detail_limit",
]
