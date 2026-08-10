"""Offline contract tests for conservative public ReKrute collection."""

import httpx
import pytest

from services.collector.collectors.rekrute import (
    HARD_LIMIT,
    ReKruteCollector,
    ReKruteParseError,
    RobotsDeniedError,
    canonicalize_offer_url,
    discover_offer_urls,
    offer_id,
    parse_offer,
)
from services.collector.models.opportunity import OpportunityCandidate

OFFER_1 = "https://www.rekrute.com/offre-emploi-data-recrutement-acme-casa-184203.html"
OFFER_2 = "https://www.rekrute.com/fr/offre-emploi-analyst-recrutement-beta-rabat-184204.html"

SEARCH_HTML = f"""
<html><body>
 <a href="{OFFER_1}?utm_source=test">Data</a>
 <a href="/company/acme">Entreprise</a><a href="/login">Login</a>
 <a href="/signup">Signup</a><a href="https://example.org/offre-emploi-x-999.html">Ad</a>
 <a href="{OFFER_1}">Duplicate</a>
 <a href="/offre-emploi-without-id.html">Broken</a>
 <a href="{OFFER_2}">Analyst</a>
</body></html>
"""

DETAIL_HTML = """
<html><body>
 <h1 class="job-title"> Senior&nbsp;Data   Engineer </h1>
 <div class="company-name"> Acme &amp; Fils </div>
 <span class="location">Casablanca et région - Maroc</span>
 <div>Publication : du 03/07/2026 au 03/08/2026</div>
 <section><h2>Poste</h2><p>Construire &amp; maintenir les pipelines.</p></section>
 <section><h2>Profil recherché</h2><p>Python et SQL requis.</p></section>
 <div class="login-modal">Connectez-vous</div>
 <footer>Texte générique ReKrute et tests 4K</footer>
</body></html>
"""


class FakeClient:
    def __init__(self, pages: dict[str, str | Exception]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def fetch_html(self, url: str) -> str:
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


def robots(*rules: str) -> str:
    return "User-agent: *\n" + "\n".join(rules) + "\n"


def test_search_discovers_only_real_deduplicated_offer_urls() -> None:
    assert discover_offer_urls(SEARCH_HTML) == [OFFER_1, OFFER_2]


def test_single_and_multiple_offer_discovery() -> None:
    assert discover_offer_urls(f'<a href="{OFFER_1}">one</a>') == [OFFER_1]
    assert len(discover_offer_urls(SEARCH_HTML)) == 2


def test_numeric_identity_and_fr_variant() -> None:
    assert offer_id(OFFER_1) == "184203"
    assert offer_id(OFFER_2) == "184204"
    assert canonicalize_offer_url(OFFER_2) == OFFER_2
    assert canonicalize_offer_url("https://www.rekrute.com/offre-emploi-no-id.html") is None


def test_complete_active_offer_maps_real_fields_and_excludes_chrome() -> None:
    candidate = parse_offer(DETAIL_HTML, OFFER_1)
    assert isinstance(candidate, OpportunityCandidate)
    assert candidate.source_id == "rekrute_public"
    assert candidate.source_external_id == "184203"
    assert candidate.canonical_title == "Senior Data Engineer"
    assert candidate.organization == "Acme & Fils"
    assert candidate.location == "Casablanca et région - Maroc"
    assert candidate.published_at == "2026-07-03"
    assert candidate.canonical_url == OFFER_1
    assert candidate.application_url == OFFER_1
    assert "Construire & maintenir" in candidate.description
    assert "Python et SQL" in candidate.description
    assert "Connectez-vous" not in candidate.description
    assert "tests 4K" not in candidate.description


def test_expired_offer_is_excluded() -> None:
    assert parse_offer(DETAIL_HTML + "Cette offre d'emploi n'est plus d'actualité.", OFFER_1) is None


@pytest.mark.parametrize(
    ("html", "message"),
    [
        (DETAIL_HTML.replace('<h1 class="job-title"> Senior&nbsp;Data   Engineer </h1>', ""), "title"),
        (DETAIL_HTML.replace('<div class="company-name"> Acme &amp; Fils </div>', ""), "organization"),
        ("<html><h1 class=\"job-title\">Broken", "organization"),
    ],
)
def test_missing_or_malformed_required_content_is_controlled(html: str, message: str) -> None:
    with pytest.raises(ReKruteParseError, match=message):
        parse_offer(html, OFFER_1)


def test_robots_allows_search_and_offer_and_collection_is_bounded() -> None:
    search_url = "https://www.rekrute.com/offres.html?keyword=data-engineer"
    client = FakeClient(
        {
            "https://www.rekrute.com/robots.txt": robots("Allow: /offres.html", "Allow: /offre-emploi-"),
            search_url: SEARCH_HTML,
            OFFER_1: DETAIL_HTML,
        }
    )
    result = ReKruteCollector(client, pause_seconds=0).collect("data-engineer", 1)
    assert result.status == "success"
    assert len(result.candidates) == 1
    assert OFFER_2 not in client.calls


@pytest.mark.parametrize(
    "robots_body",
    [
        robots("Disallow: /offres.html"),
        "",
        "this is not a robots policy",
        "User-agent: *\n",
    ],
)
def test_robots_denied_or_unusable_fails_closed(robots_body: str) -> None:
    client = FakeClient({"https://www.rekrute.com/robots.txt": robots_body})
    with pytest.raises(RobotsDeniedError):
        ReKruteCollector(client).collect("data-engineer", 1)


def test_robots_disallowed_offer_fails_before_detail_fetch() -> None:
    search_url = "https://www.rekrute.com/offres.html?keyword=data-engineer"
    client = FakeClient({
        "https://www.rekrute.com/robots.txt": robots("Allow: /offres.html", "Disallow: /offre-emploi-"),
        search_url: f'<a href="{OFFER_1}">offer</a>',
    })
    with pytest.raises(RobotsDeniedError):
        ReKruteCollector(client).collect("data-engineer", 1)
    assert OFFER_1 not in client.calls


def test_unreachable_robots_fails_closed() -> None:
    request = httpx.Request("GET", "https://www.rekrute.com/robots.txt")
    client = FakeClient({"https://www.rekrute.com/robots.txt": httpx.ConnectError("offline", request=request)})
    with pytest.raises(RobotsDeniedError):
        ReKruteCollector(client).collect("data-engineer", 1)


def test_detail_failures_are_isolated_and_classified() -> None:
    search_url = "https://www.rekrute.com/offres.html?keyword=data-engineer"
    request = httpx.Request("GET", OFFER_1)
    client = FakeClient({
        "https://www.rekrute.com/robots.txt": robots("Allow: /"),
        search_url: SEARCH_HTML,
        OFFER_1: httpx.ConnectError("offline", request=request),
        OFFER_2: DETAIL_HTML,
    })
    result = ReKruteCollector(client, pause_seconds=0).collect("data-engineer", 2)
    assert result.status == "success"
    assert result.detail_fetch_failed == 1
    assert len(result.candidates) == 1


@pytest.mark.parametrize("limit", [0, HARD_LIMIT + 1, True])
def test_limit_is_hard_bounded(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        ReKruteCollector(FakeClient({})).collect("data-engineer", limit)
