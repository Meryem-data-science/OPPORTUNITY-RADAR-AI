"""Offline tests for the Phase 7C.4B Stagiaires.ma production collector.

Nothing here touches the network. Every byte parsed is a small synthetic fixture
written in this file: a test that needs the real site fails when the site is
slow, when a template is edited, or when CI has no egress, none of which says
anything about this code. The fixtures imitate the *shapes* the collector must
survive — a namespaced sitemap, a `<lastmod>`, an off-domain `<loc>`, a JSON-LD
`JobPosting`, an apply button — never real Stagiaires.ma content.

The invariants worth breaking a build over are the ones a later change could
quietly violate: that a sitemap `<lastmod>` never becomes `published_at`, that
the collector never GETs a host or path it may not, that a partial read fails
instead of looking complete, and that no field is ever invented to fill a gap.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from services.collector.collectors.factory import (
    COLLECTOR_REGISTRY,
    UnsupportedCollectorTypeError,
    collector_for,
)
from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.collectors.linkedin_job_alert import LinkedInJobAlertCollector
from services.collector.collectors.stagiaires import StagiairesCollector
from services.collector.parsers.stagiaires import (
    COLLECTOR_USER_AGENT,
    PARSER_VERSION,
    ROBOTS_ABSENT,
    ROBOTS_OBEY,
    ROBOTS_REFUSED,
    ROBOTS_UNRESOLVED,
    ROBOTS_URL,
    SitemapOfferEntry,
    StagiairesCollectionError,
    StagiairesPayloadError,
    application_action_url,
    classify_robots_response,
    extract_external_id,
    freshness_key,
    job_posting,
    parse_lastmod,
    parse_offer_sitemap,
    parse_robots_txt,
    parse_sitemap_index,
    posting_organization,
    robots_allows,
    select_detail_targets,
    sitemap_declarations,
)
from services.collector.sources import (
    MAX_DETAIL_PAGE_LIMIT,
    SourceConfig,
    SourceConfigurationError,
    load_source_registry,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HOST = "https://www.stagiaires.ma"
OFFER = f"{HOST}/stage-emploi-maroc"
SITEMAP = f"{HOST}/sitemap_v9.xml"

ROBOTS_BODY = (
    "# synthetic robots.txt, not a copy of the real file\n"
    "User-agent: *\n"
    "Disallow: /admin/\n"
    "Allow: /\n"
    "\n"
    f"Sitemap: {SITEMAP}\n"
)

SITEMAP_INDEX = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{HOST}/offre-sitemap.xml</loc></sitemap>
  <sitemap><loc>{HOST}/offre-sitemap2.xml</loc></sitemap>
  <sitemap><loc>https://mirror.example.com/offre-sitemap3.xml</loc></sitemap>
  <sitemap><loc>{HOST}/page-sitemap.xml</loc></sitemap>
</sitemapindex>
"""


def urlset(rows: list[tuple[str, str | None]]) -> str:
    body = "".join(
        f"<url><loc>{loc}</loc>"
        + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
        + "</url>"
        for loc, lastmod in rows
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )


def detail_html(
    *,
    title: str | None = "Stage PFE Data Engineer",
    organization: str | None = "Entreprise Synthetique",
    locality: str | None = "Casablanca",
    description: str | None = "Description synthetique inventee pour ce test.",
    date_posted: str | None = "2026-08-30",
    apply_markup: str = "",
    posting_url: str | None = None,
) -> str:
    """Build one offer page carrying a JSON-LD JobPosting."""
    posting: dict[str, object] = {"@context": "https://schema.org", "@type": "JobPosting"}
    if title is not None:
        posting["title"] = title
    if organization is not None:
        posting["hiringOrganization"] = {"@type": "Organization", "name": organization}
    if locality is not None:
        posting["jobLocation"] = {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": locality},
        }
    if description is not None:
        posting["description"] = description
    if date_posted is not None:
        posting["datePosted"] = date_posted
    if posting_url is not None:
        posting["url"] = posting_url
    import json

    return (
        "<html><head><title>Offre</title>"
        f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        "</head><body><h1>Offre</h1><p>"
        + "contenu " * 40
        + f"</p>{apply_markup}</body></html>"
    )


def stagiaires_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "stagiaires_ma",
        "type": "stagiaires_sitemap",
        "enabled": True,
        "category": "jobs",
        "country": "MA",
        "frequency_minutes": 360,
        "status": "active",
        "detail_page_limit": 25,
    }
    values.update(overrides)
    return SourceConfig.from_mapping(values)


class _FakeResponse:
    def __init__(self, url, status_code, text, content_type, extra_headers=None):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type, **(extra_headers or {})}


class _FakeClient:
    """Records every GET, and refuses to follow redirects on the caller's behalf."""

    def __init__(self, routes, redirects=None, failing_url=None, redirect_status=302):
        self.routes = routes
        self.redirects = redirects or {}
        self.failing_url = failing_url
        self.redirect_status = redirect_status
        self.requested: list[str] = []

    def get(self, url, timeout=None, follow_redirects=False):
        assert follow_redirects is False, (
            "the collector must resolve redirects itself, never hand them to httpx"
        )
        self.requested.append(url)
        if url == self.failing_url:
            raise httpx.ConnectTimeout("synthetic transport failure")
        if url in self.redirects:
            return _FakeResponse(
                url, self.redirect_status, "", "text/html",
                {"location": self.redirects[url]},
            )
        status, body, content_type = self.routes.get(url, (404, "not found", "text/html"))
        return _FakeResponse(url, status, body, content_type)


def routes(offer_count: int = 4, **overrides) -> dict:
    first = urlset(
        [(f"{OFFER}/{6100 + n}-slug-{n}", f"2026-09-0{n + 1}") for n in range(offer_count)]
    )
    second = urlset([(f"{OFFER}/5000-autre", "2026-01-01")])
    table = {
        ROBOTS_URL: (200, ROBOTS_BODY, "text/plain"),
        SITEMAP: (200, SITEMAP_INDEX, "application/xml"),
        f"{HOST}/offre-sitemap.xml": (200, first, "application/xml"),
        f"{HOST}/offre-sitemap2.xml": (200, second, "application/xml"),
        f"{OFFER}/5000-autre": (200, detail_html(), "text/html"),
    }
    for n in range(offer_count):
        table[f"{OFFER}/{6100 + n}-slug-{n}"] = (200, detail_html(), "text/html")
    table.update(overrides)
    return table


def collect(client, source=None):
    collector = StagiairesCollector(source or stagiaires_source(), client=client)
    collector.DELAY_SECONDS = 0.0
    return collector, collector.collect()


# ============================== SOURCE CONFIG ==============================


def test_the_stagiaires_source_type_is_accepted() -> None:
    assert stagiaires_source().type == "stagiaires_sitemap"


def test_the_approved_detail_page_limit_is_accepted() -> None:
    assert stagiaires_source().detail_page_limit == 25


@pytest.mark.parametrize("limit", [1, 25, 50])
def test_limits_inside_the_bound_are_accepted(limit: int) -> None:
    assert stagiaires_source(detail_page_limit=limit).detail_page_limit == limit


@pytest.mark.parametrize("limit", [0, -1, 51, 1000, True, False, 2.5, "25", None])
def test_a_limit_outside_the_bound_is_refused_rather_than_clamped(limit) -> None:
    """`True` is an `int` in Python; a flag silently becoming "fetch 1 page" is
    exactly the quiet nonsense this validator exists to refuse."""
    with pytest.raises(SourceConfigurationError, match="detail_page_limit"):
        stagiaires_source(detail_page_limit=limit)


def test_a_stagiaires_source_without_a_limit_is_refused() -> None:
    values = {
        "id": "stagiaires_ma",
        "type": "stagiaires_sitemap",
        "enabled": True,
    }
    with pytest.raises(SourceConfigurationError, match="detail_page_limit"):
        SourceConfig.from_mapping(values)


def test_the_hard_ceiling_is_fifty() -> None:
    assert MAX_DETAIL_PAGE_LIMIT == 50


def test_greenhouse_configuration_is_unchanged_by_the_new_field() -> None:
    source = SourceConfig.from_mapping(
        {
            "id": "scale_ai_greenhouse",
            "type": "greenhouse",
            "enabled": True,
            "organization": "Scale AI",
            "board_token": "scaleai",
        }
    )
    assert source.board_token == "scaleai"
    assert source.detail_page_limit is None


def test_gmail_linkedin_configuration_is_unchanged_by_the_new_field() -> None:
    source = SourceConfig.from_mapping(
        {
            "id": "linkedin_job_alert_email",
            "type": "gmail_linkedin_alert",
            "enabled": True,
            "gmail_query": "newer_than:7d",
            "gmail_message_limit": 50,
        }
    )
    assert source.gmail_message_limit == 50
    assert source.detail_page_limit is None


def test_the_committed_production_row_matches_the_approved_values() -> None:
    configured = {item.id: item for item in load_source_registry()}["stagiaires_ma"]

    assert configured.type == "stagiaires_sitemap"
    assert configured.enabled is True
    assert configured.category == "jobs"
    assert configured.country == "MA"
    assert configured.frequency_minutes == 360
    assert configured.status == "active"
    assert configured.detail_page_limit == 25


# ================================= FACTORY =================================


def test_the_factory_builds_the_stagiaires_collector() -> None:
    assert isinstance(collector_for(stagiaires_source()), StagiairesCollector)


def test_the_existing_collector_registrations_are_unchanged() -> None:
    assert set(COLLECTOR_REGISTRY) == {
        "greenhouse",
        "gmail_linkedin_alert",
        "stagiaires_sitemap",
    }
    assert COLLECTOR_REGISTRY["greenhouse"] is GreenhouseCollector
    assert COLLECTOR_REGISTRY["gmail_linkedin_alert"] is LinkedInJobAlertCollector
    assert COLLECTOR_REGISTRY["stagiaires_sitemap"] is StagiairesCollector


def test_an_unregistered_type_is_still_refused() -> None:
    with pytest.raises(UnsupportedCollectorTypeError):
        collector_for(
            SourceConfig(id="x", type="lever", enabled=True)
        )


def test_the_collector_refuses_a_source_of_another_type() -> None:
    greenhouse = SourceConfig.from_mapping(
        {
            "id": "g",
            "type": "greenhouse",
            "enabled": True,
            "organization": "O",
            "board_token": "t",
        }
    )
    with pytest.raises(ValueError, match="stagiaires_sitemap"):
        StagiairesCollector(greenhouse)


# ============================ ROBOTS / NETWORK =============================


def test_a_permissive_robots_lets_collection_proceed() -> None:
    client = _FakeClient(routes())
    _, candidates = collect(client)

    assert client.requested[0] == ROBOTS_URL
    assert len(candidates) == 5


def test_a_disallowed_path_is_never_requested() -> None:
    table = routes()
    table[ROBOTS_URL] = (
        200,
        f"User-agent: *\nDisallow: /stage-emploi-maroc/\nAllow: /\nSitemap: {SITEMAP}\n",
        "text/plain",
    )
    client = _FakeClient(table)

    with pytest.raises(StagiairesCollectionError, match="robots.txt disallows"):
        collect(client)
    assert not any("/stage-emploi-maroc/" in item for item in client.requested)


def test_a_query_rule_is_enforced_because_robots_matches_path_and_query() -> None:
    """Matching only the path would ignore every rule keyed on a parameter."""
    groups = parse_robots_txt("User-agent: *\nDisallow: /*?download=true\n")

    assert robots_allows(groups, f"{HOST}/allowed?download=true") is False
    assert robots_allows(groups, f"{HOST}/allowed") is True


def test_an_empty_disallow_never_cancels_a_site_wide_disallow() -> None:
    groups = parse_robots_txt("User-agent: *\nDisallow: /\nDisallow:\n")

    assert robots_allows(groups, f"{HOST}/anything") is False


def test_a_later_applicable_group_is_not_silently_ignored() -> None:
    groups = parse_robots_txt(
        f"User-agent: *\nAllow: /\n\nUser-agent: *\nDisallow: /stage-emploi-maroc/\n"
    )

    assert robots_allows(groups, f"{OFFER}/1-a") is False


@pytest.mark.parametrize("status", [401, 403, 407, 429])
def test_a_refused_robots_fails_the_run_closed(status: int) -> None:
    """An unknown rule is never read as a permissive one."""
    table = routes()
    table[ROBOTS_URL] = (status, "", "text/html")
    client = _FakeClient(table)

    with pytest.raises(StagiairesCollectionError, match="robots.txt could not be resolved"):
        collect(client)
    assert client.requested == [ROBOTS_URL]


@pytest.mark.parametrize("status", [500, 502, 503, 418])
def test_an_unresolved_robots_fails_the_run_closed(status: int) -> None:
    table = routes()
    table[ROBOTS_URL] = (status, "", "text/html")

    with pytest.raises(StagiairesCollectionError):
        collect(_FakeClient(table))


def test_a_bot_challenge_served_as_robots_is_a_refusal() -> None:
    assert (
        classify_robots_response(200, ROBOTS_URL, "Just a moment... checking your browser")
        == ROBOTS_REFUSED
    )


def test_an_absent_robots_is_not_permission_and_declares_no_sitemap() -> None:
    """404 means no rule applies. It does not mean a sitemap we invented exists."""
    assert classify_robots_response(404, ROBOTS_URL, "") == ROBOTS_ABSENT
    assert classify_robots_response(200, ROBOTS_URL, ROBOTS_BODY) == ROBOTS_OBEY
    assert classify_robots_response(503, ROBOTS_URL, "") == ROBOTS_UNRESOLVED

    table = routes()
    table[ROBOTS_URL] = (404, "", "text/plain")
    with pytest.raises(StagiairesCollectionError, match="declares no same-host sitemap"):
        collect(_FakeClient(table))


def test_a_same_host_allowed_redirect_is_followed() -> None:
    moved = f"{OFFER}/6103-slug-3-v2"
    table = routes()
    table[moved] = (200, detail_html(), "text/html")
    client = _FakeClient(table, redirects={f"{OFFER}/6103-slug-3": moved})

    _, candidates = collect(client)

    assert moved in client.requested
    assert len(candidates) == 5


def test_a_robots_disallowed_redirect_target_is_never_requested() -> None:
    """Obeying robots on the URL we asked for but not the one we are handed is
    not obeying robots."""
    secret = f"{HOST}/admin/secret"
    table = routes()
    table[secret] = (200, detail_html(), "text/html")
    client = _FakeClient(table, redirects={f"{OFFER}/6103-slug-3": secret})

    with pytest.raises(StagiairesCollectionError, match="robots.txt disallows"):
        collect(client)
    assert secret not in client.requested


def test_an_off_domain_redirect_target_is_never_requested() -> None:
    elsewhere = "https://tracker.example.com/landing"
    client = _FakeClient(routes(), redirects={f"{OFFER}/6103-slug-3": elsewhere})

    with pytest.raises(StagiairesCollectionError, match="off-domain redirect"):
        collect(client)
    assert not any("tracker.example.com" in item for item in client.requested)


def test_a_redirect_loop_is_bounded() -> None:
    a, b = f"{HOST}/loop-a", f"{HOST}/loop-b"
    client = _FakeClient(routes(), redirects={f"{OFFER}/6103-slug-3": a, a: b, b: a})

    with pytest.raises(StagiairesCollectionError, match="too many redirects"):
        collect(client)
    assert client.requested.count(a) <= StagiairesCollector.MAX_REDIRECTS + 1


def test_no_request_in_a_whole_run_leaves_the_stagiaires_hosts() -> None:
    client = _FakeClient(routes())
    collect(client)

    for url in client.requested:
        assert url.startswith(("https://www.stagiaires.ma", "https://stagiaires.ma"))


def test_an_access_barrier_on_a_detail_page_fails_the_run() -> None:
    table = routes()
    table[f"{OFFER}/6103-slug-3"] = (403, "", "text/html")

    with pytest.raises(StagiairesCollectionError, match="HTTP_403_FORBIDDEN"):
        collect(_FakeClient(table))


def test_the_collector_identifies_itself_honestly() -> None:
    assert COLLECTOR_USER_AGENT.startswith("OpportunityRadarAI-Collector/")
    for impersonated in ("Mozilla", "Chrome", "Safari", "Googlebot", "Firefox"):
        assert impersonated.lower() not in COLLECTOR_USER_AGENT.lower()


# ================================ DISCOVERY ================================


def test_the_sitemap_comes_from_the_robots_declaration() -> None:
    assert sitemap_declarations(ROBOTS_BODY) == (SITEMAP,)

    client = _FakeClient(routes())
    collect(client)
    assert SITEMAP in client.requested


def test_a_sitemap_robots_does_not_declare_is_never_fetched() -> None:
    other = f"{HOST}/sitemap_secret.xml"
    table = routes()
    table[other] = (200, SITEMAP_INDEX, "application/xml")
    client = _FakeClient(table)

    collect(client)
    assert other not in client.requested


def test_the_offer_sitemap_family_is_discovered_from_the_index() -> None:
    assert parse_sitemap_index(SITEMAP_INDEX, SITEMAP) == (
        f"{HOST}/offre-sitemap.xml",
        f"{HOST}/offre-sitemap2.xml",
    )


def test_a_further_numbered_offer_sitemap_is_discovered_not_hardcoded() -> None:
    document = SITEMAP_INDEX.replace(
        f"<sitemap><loc>{HOST}/page-sitemap.xml</loc></sitemap>",
        f"<sitemap><loc>{HOST}/offre-sitemap3.xml</loc></sitemap>",
    )
    assert parse_sitemap_index(document, SITEMAP)[-1] == f"{HOST}/offre-sitemap3.xml"


def test_an_off_domain_sitemap_is_not_fetched() -> None:
    client = _FakeClient(routes())
    collect(client)

    assert not any("mirror.example.com" in item for item in client.requested)


def test_a_malformed_sitemap_index_fails_rather_than_returning_nothing() -> None:
    table = routes()
    table[SITEMAP] = (200, "<html><body>not xml at all</body></html>", "text/html")

    with pytest.raises(StagiairesPayloadError):
        collect(_FakeClient(table))


def test_a_sitemap_index_declaring_no_offer_sitemap_fails() -> None:
    table = routes()
    table[SITEMAP] = (
        200,
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{HOST}/page-sitemap.xml</loc></sitemap></sitemapindex>",
        "application/xml",
    )

    with pytest.raises(StagiairesPayloadError, match="no offer sitemap"):
        collect(_FakeClient(table))


def test_an_unreadable_required_offer_sitemap_fails_the_run() -> None:
    """A partial read must never look like a complete one.

    The offer set is the union of the declared sitemaps, so one file we could
    not read makes the result a silent subset — the shape of the bug where a
    site's 1710 offers quietly become 1000.
    """
    client = _FakeClient(routes(), failing_url=f"{HOST}/offre-sitemap2.xml")

    with pytest.raises(StagiairesCollectionError, match="offre-sitemap2.xml"):
        collect(client)


def test_a_malformed_required_offer_sitemap_fails_the_run() -> None:
    table = routes()
    table[f"{HOST}/offre-sitemap2.xml"] = (200, "<html>nope</html>", "text/html")

    with pytest.raises(StagiairesPayloadError):
        collect(_FakeClient(table))


@pytest.mark.parametrize(
    "loc",
    [
        "https://jobs.example.com/stage-emploi-maroc/6159-a",
        f"{HOST}/blog/6159-a",
        f"{OFFER}/slug-without-id",
        f"{OFFER}/61a59-mixed",
        f"{OFFER}/6159abc-glued",
    ],
)
def test_a_malformed_or_foreign_offer_url_is_rejected(loc: str) -> None:
    assert parse_offer_sitemap(urlset([(loc, None)]), SITEMAP) == ()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{OFFER}/6159-slug", "6159"),
        (f"{OFFER}/6159", "6159"),
        (f"{OFFER}/6159-slug/", "6159"),
        (f"{OFFER}/007-slug", "007"),
    ],
)
def test_the_numeric_id_is_read_as_published(url: str, expected: str) -> None:
    """`007` stays `007`: normalizing it to `7` would invent an equality."""
    assert extract_external_id(url) == expected


# ============================= FRESHNESS BOUND =============================


def test_a_run_fetches_at_most_the_configured_detail_limit() -> None:
    entries = tuple(
        SitemapOfferEntry(str(n), f"{OFFER}/{n}-slug", "2026-09-01") for n in range(500)
    )
    assert len(select_detail_targets(entries, 25)) == 25


def test_the_configured_limit_actually_bounds_real_detail_requests() -> None:
    client = _FakeClient(routes(offer_count=40))
    collect(client, source=stagiaires_source(detail_page_limit=3))

    detail_gets = [item for item in client.requested if "/stage-emploi-maroc/" in item]
    assert len(detail_gets) == 3


def test_the_newest_valid_lastmod_is_fetched_first() -> None:
    entries = (
        SitemapOfferEntry("1", f"{OFFER}/1-a", "2026-01-01"),
        SitemapOfferEntry("2", f"{OFFER}/2-b", "2026-09-05"),
        SitemapOfferEntry("3", f"{OFFER}/3-c", "2026-05-01"),
    )
    assert [item.source_external_id for item in select_detail_targets(entries, 3)] == [
        "2",
        "3",
        "1",
    ]


def test_an_equal_lastmod_breaks_the_tie_on_canonical_url() -> None:
    entries = (
        SitemapOfferEntry("2", f"{OFFER}/2-b", "2026-09-05"),
        SitemapOfferEntry("1", f"{OFFER}/1-a", "2026-09-05"),
    )
    chosen = select_detail_targets(entries, 2)

    assert [item.canonical_url for item in chosen] == [f"{OFFER}/1-a", f"{OFFER}/2-b"]
    assert select_detail_targets(tuple(reversed(entries)), 2) == chosen


@pytest.mark.parametrize("lastmod", [None, "", "not-a-date", "2026-13-45", "soon"])
def test_an_absent_or_invalid_lastmod_loses_priority_without_being_invented(
    lastmod,
) -> None:
    """An unreadable timestamp is not repaired and not replaced with "now"."""
    assert parse_lastmod(lastmod) is None

    entries = (
        SitemapOfferEntry("1", f"{OFFER}/1-a", lastmod),
        SitemapOfferEntry("2", f"{OFFER}/2-b", "2020-01-01"),
    )
    assert [item.source_external_id for item in select_detail_targets(entries, 2)] == [
        "2",
        "1",
    ]


def test_undated_entries_are_ordered_deterministically_among_themselves() -> None:
    entries = (
        SitemapOfferEntry("9", f"{OFFER}/9-z", None),
        SitemapOfferEntry("1", f"{OFFER}/1-a", None),
    )
    assert [item.canonical_url for item in select_detail_targets(entries, 2)] == [
        f"{OFFER}/1-a",
        f"{OFFER}/9-z",
    ]


def test_a_higher_numeric_id_alone_does_not_win_freshness_priority() -> None:
    """The audit refused to claim that a larger ID means a newer offer, and this
    keeps that refusal from being smuggled back in through the fetch order."""
    entries = (
        SitemapOfferEntry("9999", f"{OFFER}/9999-new-looking", "2020-01-01"),
        SitemapOfferEntry("1", f"{OFFER}/1-old-looking", "2026-09-05"),
    )
    assert select_detail_targets(entries, 1)[0].source_external_id == "1"


def test_the_freshness_key_never_reads_the_numeric_id() -> None:
    same_dates = (
        SitemapOfferEntry("1", f"{OFFER}/aaa", "2026-09-05"),
        SitemapOfferEntry("9999", f"{OFFER}/aaa", "2026-09-05"),
    )
    assert freshness_key(same_dates[0]) == freshness_key(same_dates[1])


def test_the_sitemap_lastmod_never_becomes_the_candidate_published_at() -> None:
    """The invariant the whole integration protects.

    A `<lastmod>` says a page changed, not that an offer was posted. If the page
    states no `datePosted`, `published_at` is None — never the sitemap's date.
    """
    table = routes(offer_count=1)
    table[f"{OFFER}/6100-slug-0"] = (200, detail_html(date_posted=None), "text/html")
    table[f"{HOST}/offre-sitemap.xml"] = (
        200,
        urlset([(f"{OFFER}/6100-slug-0", "2026-09-01T10:00:00+00:00")]),
        "application/xml",
    )
    table[f"{HOST}/offre-sitemap2.xml"] = (200, urlset([]), "application/xml")

    _, candidates = collect(_FakeClient(table))

    assert len(candidates) == 1
    assert candidates[0].published_at is None
    assert not hasattr(SitemapOfferEntry("1", "u"), "published_at")


def test_a_real_date_posted_is_used_and_is_not_the_lastmod() -> None:
    table = routes(offer_count=1)
    table[f"{OFFER}/6100-slug-0"] = (
        200,
        detail_html(date_posted="2026-08-30"),
        "text/html",
    )
    table[f"{HOST}/offre-sitemap.xml"] = (
        200,
        urlset([(f"{OFFER}/6100-slug-0", "2026-09-01")]),
        "application/xml",
    )
    table[f"{HOST}/offre-sitemap2.xml"] = (200, urlset([]), "application/xml")

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].published_at == "2026-08-30"


# ============================== DETAIL PARSER ==============================


def one_candidate(**page):
    table = routes(offer_count=1)
    table[f"{OFFER}/6100-slug-0"] = (200, detail_html(**page), "text/html")
    table[f"{HOST}/offre-sitemap.xml"] = (
        200,
        urlset([(f"{OFFER}/6100-slug-0", "2026-09-01")]),
        "application/xml",
    )
    table[f"{HOST}/offre-sitemap2.xml"] = (200, urlset([]), "application/xml")
    _, candidates = collect(_FakeClient(table))
    return candidates[0]


def test_a_job_posting_maps_every_explicit_field() -> None:
    candidate = one_candidate()

    assert candidate.source_id == "stagiaires_ma"
    assert candidate.source_external_id == "6100"
    assert candidate.canonical_title == "Stage PFE Data Engineer"
    assert candidate.organization == "Entreprise Synthetique"
    assert candidate.location == "Casablanca"
    assert candidate.description == "Description synthetique inventee pour ce test."
    assert candidate.published_at == "2026-08-30"
    assert candidate.source_url == f"{OFFER}/6100-slug-0"
    assert candidate.canonical_url == f"{OFFER}/6100-slug-0"


def test_the_source_url_is_the_stable_canonical_url_not_a_redirect_target() -> None:
    """Opportunities are keyed on `(source_id, source_url)`. An identity that
    drifted between runs would re-create every offer as new on the next run."""
    moved = f"{OFFER}/6100-slug-0-v2"
    table = routes(offer_count=1)
    table[moved] = (200, detail_html(), "text/html")
    table[f"{HOST}/offre-sitemap.xml"] = (
        200, urlset([(f"{OFFER}/6100-slug-0", "2026-09-01")]), "application/xml",
    )
    table[f"{HOST}/offre-sitemap2.xml"] = (200, urlset([]), "application/xml")
    client = _FakeClient(table, redirects={f"{OFFER}/6100-slug-0": moved})

    _, candidates = collect(client)

    assert moved in client.requested
    assert candidates[0].source_url == f"{OFFER}/6100-slug-0"


@pytest.mark.parametrize("field", ["locality", "description", "date_posted"])
def test_a_missing_optional_field_becomes_none_not_a_placeholder(field: str) -> None:
    candidate = one_candidate(**{field: None})
    mapped = {"locality": "location", "description": "description", "date_posted": "published_at"}

    assert getattr(candidate, mapped[field]) is None


def test_a_missing_application_action_becomes_none() -> None:
    assert one_candidate().application_url is None


def test_a_missing_title_fails_the_run() -> None:
    table = routes(offer_count=1)
    table[f"{OFFER}/6100-slug-0"] = (200, detail_html(title=None), "text/html")
    table[f"{HOST}/offre-sitemap.xml"] = (
        200, urlset([(f"{OFFER}/6100-slug-0", None)]), "application/xml",
    )
    table[f"{HOST}/offre-sitemap2.xml"] = (200, urlset([]), "application/xml")

    with pytest.raises(StagiairesPayloadError, match="missing a title"):
        collect(_FakeClient(table))


def test_a_missing_organization_fails_the_run() -> None:
    table = routes(offer_count=1)
    table[f"{OFFER}/6100-slug-0"] = (200, detail_html(organization=None), "text/html")
    table[f"{HOST}/offre-sitemap.xml"] = (
        200, urlset([(f"{OFFER}/6100-slug-0", None)]), "application/xml",
    )
    table[f"{HOST}/offre-sitemap2.xml"] = (200, urlset([]), "application/xml")

    with pytest.raises(StagiairesPayloadError, match="missing a hiring organization"):
        collect(_FakeClient(table))


def test_a_page_without_a_job_posting_fails_rather_than_being_guessed_at() -> None:
    table = routes(offer_count=1)
    table[f"{OFFER}/6100-slug-0"] = (
        200, "<html><body>" + "x" * 500 + "</body></html>", "text/html",
    )
    table[f"{HOST}/offre-sitemap.xml"] = (
        200, urlset([(f"{OFFER}/6100-slug-0", None)]), "application/xml",
    )
    table[f"{HOST}/offre-sitemap2.xml"] = (200, urlset([]), "application/xml")

    with pytest.raises(StagiairesPayloadError, match="publishes no JobPosting"):
        collect(_FakeClient(table))


def test_no_placeholder_value_is_ever_substituted_for_a_missing_fact() -> None:
    """UNKNOWN stays UNKNOWN. A wrong value is worse than a missing one."""
    candidate = one_candidate(locality=None, description=None, date_posted=None)

    for value in (candidate.location, candidate.description, candidate.published_at):
        assert value is None
    for forbidden in ("UNKNOWN", "N/A", "Morocco", "Maroc", "Stagiaires.ma", ""):
        assert candidate.organization != forbidden
        assert candidate.canonical_title != forbidden


def test_the_organization_is_never_inferred_from_the_board_or_the_source() -> None:
    """Stagiaires.ma publishes other people's vacancies; naming it as the
    employer of every offer is not a missing value but a wrong one."""
    posting = job_posting(detail_html(organization="Vraie Entreprise"))

    assert posting_organization(posting) == "Vraie Entreprise"
    assert posting_organization({"@type": "JobPosting", "title": "t"}) is None


def test_the_location_is_never_inferred_from_the_source_country() -> None:
    candidate = one_candidate(locality=None)

    assert candidate.location is None
    assert stagiaires_source().country == "MA"


# ============================= APPLICATION URL =============================


@pytest.mark.parametrize(
    "markup",
    [
        '<a href="/postuler/1">Postuler</a>',
        '<a href="/postuler/1">Candidater maintenant</a>',
        '<a href="/postuler/1">Apply now</a>',
        '<button aria-label="Postulez a cette offre">envoyer</button>',
    ],
)
def test_an_explicit_application_control_is_recognized(markup: str) -> None:
    if "button" in markup:
        assert application_action_url(markup, f"{OFFER}/1-a") is None
    else:
        assert application_action_url(markup, f"{OFFER}/1-a") is not None


def test_a_relative_application_href_resolves_against_the_detail_page() -> None:
    candidate = one_candidate(apply_markup='<a href="/postuler/6100">Postuler</a>')

    assert candidate.application_url == f"{HOST}/postuler/6100"


def test_an_off_domain_application_href_is_recorded_but_never_requested() -> None:
    """Recording a URL is not requesting it: an employer's ATS is a real finding."""
    table = routes(offer_count=1)
    table[f"{OFFER}/6100-slug-0"] = (
        200,
        detail_html(apply_markup='<a href="https://ats.example.com/apply/1">Postuler</a>'),
        "text/html",
    )
    table[f"{HOST}/offre-sitemap.xml"] = (
        200, urlset([(f"{OFFER}/6100-slug-0", None)]), "application/xml",
    )
    table[f"{HOST}/offre-sitemap2.xml"] = (200, urlset([]), "application/xml")
    client = _FakeClient(table)

    _, candidates = collect(client)

    assert candidates[0].application_url == "https://ats.example.com/apply/1"
    assert not any("ats.example.com" in item for item in client.requested)


def test_the_canonical_posting_url_alone_is_not_an_application_url() -> None:
    """Every offer has a canonical URL; treating one as an apply link would claim
    an application route for offers that may have none."""
    candidate = one_candidate(posting_url=f"{OFFER}/6100-slug-0")

    assert candidate.canonical_url == f"{OFFER}/6100-slug-0"
    assert candidate.application_url is None


def test_a_job_posting_url_field_alone_is_not_an_application_url() -> None:
    html = detail_html(posting_url="https://www.stagiaires.ma/stage-emploi-maroc/1-a")

    assert application_action_url(html, f"{OFFER}/1-a") is None


def test_a_navigation_link_is_not_an_application_link() -> None:
    assert application_action_url(
        "<a href='/'>Accueil</a><a href='/offres'>Toutes les offres</a>",
        f"{OFFER}/1-a",
    ) is None


@pytest.mark.parametrize("href", ["javascript:void(0)", "#apply", "mailto:rh@x.test"])
def test_a_control_without_a_usable_href_yields_no_application_url(href: str) -> None:
    assert application_action_url(f"<a href='{href}'>Postuler</a>", f"{OFFER}/1-a") is None


# ================================= METRICS =================================


def test_run_metrics_report_only_what_the_run_measured() -> None:
    collector, _ = collect(_FakeClient(routes()))
    metrics = collector.run_metrics()

    assert metrics["parser_version"] == PARSER_VERSION
    assert metrics["pages_checked"] > 0
    # Never invented: the agent counts items, relevance is qualification's
    # judgement, and a multi-page run has no single HTTP status.
    for absent in ("items_found", "new_items", "relevant_items", "http_status"):
        assert absent not in metrics


# =========================== PRODUCTION ISOLATION ==========================


def test_the_collector_has_no_browser_or_credential_dependency() -> None:
    """Checked against imports and executable code, never prose.

    Both modules say "no Selenium, no cookies, no proxy" out loud in their
    docstrings, and a test that forbade those words would forbid saying so.
    """
    import ast

    for module in (
        "services/collector/collectors/stagiaires.py",
        "services/collector/parsers/stagiaires.py",
    ):
        text = (REPOSITORY_ROOT / module).read_text(encoding="utf-8")
        tree = ast.parse(text)

        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for name in imported:
            lowered = name.lower()
            for forbidden in ("selenium", "playwright", "webdriver", "puppeteer"):
                assert forbidden not in lowered, f"{module}: imports {name}"
            assert not lowered.startswith("evaluation"), f"{module}: imports {name}"

        # Strip docstrings, then look for credential/proxy machinery in code.
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant
                ) and isinstance(body[0].value.value, str):
                    body[0].value.value = ""
        code = ast.unparse(tree).lower()
        for forbidden in (
            "cookies",
            "proxies",
            "auth=",
            "authorization",
            "basicauth",
            "selenium",
            "playwright",
        ):
            assert forbidden not in code, f"{module}: {forbidden}"


def test_the_collector_never_imports_the_evaluation_layer() -> None:
    """`evaluation/` is an audit trail, not a runtime dependency."""
    for module in (
        "services/collector/collectors/stagiaires.py",
        "services/collector/parsers/stagiaires.py",
    ):
        text = (REPOSITORY_ROOT / module).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith(("import ", "from ")):
                assert "evaluation" not in line, f"{module}: {line}"


def test_this_phase_added_no_database_migration() -> None:
    """Identity is unchanged: no new column, no new uniqueness constraint."""
    migrations = sorted(
        path.name for path in (REPOSITORY_ROOT / "migrations").glob("*.sql")
    )
    assert not any("stagiaires" in name.lower() for name in migrations)
    for path in (REPOSITORY_ROOT / "migrations").glob("*.sql"):
        assert "stagiaires" not in path.read_text(encoding="utf-8").lower()


def test_the_radar_agent_has_no_stagiaires_specific_branch() -> None:
    """The agent runs this source through the same factory as every other."""
    agent = (REPOSITORY_ROOT / "services" / "collector" / "agent.py").read_text(
        encoding="utf-8"
    )

    assert "stagiaires" not in agent.lower()
    assert "collector_for" in agent


def test_the_collector_applies_no_data_ai_or_pfe_filter() -> None:
    """The collector observes; qualification classifies; ranking prioritizes."""
    table = routes(offer_count=1)
    table[f"{OFFER}/6100-slug-0"] = (
        200,
        detail_html(title="Stage comptabilite generale"),
        "text/html",
    )
    table[f"{HOST}/offre-sitemap.xml"] = (
        200, urlset([(f"{OFFER}/6100-slug-0", None)]), "application/xml",
    )
    table[f"{HOST}/offre-sitemap2.xml"] = (200, urlset([]), "application/xml")

    _, candidates = collect(_FakeClient(table))

    # An offer with nothing to do with Data/AI or PFE is still collected.
    assert len(candidates) == 1
    assert candidates[0].canonical_title == "Stage comptabilite generale"

    collector_source = (
        REPOSITORY_ROOT / "services" / "collector" / "collectors" / "stagiaires.py"
    ).read_text(encoding="utf-8")
    assert "data_ai" not in collector_source.lower()


# ================================ INTEGRATION ==============================


def test_the_dry_run_cli_resolves_the_collector_through_the_factory(monkeypatch) -> None:
    """It was hardcoded to Greenhouse, so every other source was dry-run through
    the wrong collector. The factory is what makes the command generic."""
    from services.collector.cli import collect_source

    built: list[SourceConfig] = []

    class _Recording:
        def __init__(self, source: SourceConfig) -> None:
            built.append(source)

        def collect(self):
            return [
                type(
                    "C",
                    (),
                    {
                        "source_id": "stagiaires_ma",
                        "source_external_id": "6100",
                        "canonical_title": "Stage",
                        "organization": "Entreprise",
                        "location": None,
                        "source_url": f"{OFFER}/6100-slug-0",
                    },
                )()
            ]

    monkeypatch.setattr(
        collect_source, "get_enabled_source", lambda source_id: stagiaires_source()
    )
    monkeypatch.setattr(collect_source, "collector_for", lambda source: _Recording(source))

    summaries = collect_source.run("stagiaires_ma", 5)

    assert [item.type for item in built] == ["stagiaires_sitemap"]
    assert summaries[0]["source_id"] == "stagiaires_ma"


def test_the_dry_run_cli_imports_no_persistence_at_all() -> None:
    """`--dry-run` is a safeguard only if nothing on the path can write."""
    text = (
        REPOSITORY_ROOT / "services" / "collector" / "cli" / "collect_source.py"
    ).read_text(encoding="utf-8")

    assert "persist" not in text.replace("without persistence", "").replace(
        "nothing here persists", ""
    ).lower()
    assert "GreenhouseCollector" not in text
    assert "collector_for" in text


def test_the_radar_agent_runs_a_stagiaires_source_through_the_normal_factory() -> None:
    """No agent change was needed, which is the integration this asserts."""
    from services.collector.agent import RadarAgent

    source = stagiaires_source()
    client = _FakeClient(routes())

    def factory(configured: SourceConfig):
        collector = StagiairesCollector(configured, client=client)
        collector.DELAY_SECONDS = 0.0
        return collector

    persisted: list = []

    class _Persisted:
        created = 5
        updated = 0

    class _Qualified:
        created = 0
        updated = 0
        unchanged = 0
        total = 0

    agent = RadarAgent(
        source_loader=lambda *args, **kwargs: [source],
        collector_factory=factory,
        settings_loader=lambda: object(),
        persister=lambda settings, configured, candidates: (
            persisted.extend(candidates) or _Persisted()
        ),
        qualification_persister=lambda *args, **kwargs: _Qualified(),
        run_starter=lambda *args, **kwargs: object(),
        run_finalizer=lambda *args, **kwargs: None,
    )

    summary = agent.run_once()

    assert summary.sources_total == 1
    assert summary.sources_succeeded == 1
    assert summary.sources_failed == 0
    assert summary.items_collected == 5
    assert [item.source_id for item in summary.source_results] == ["stagiaires_ma"]
    assert summary.source_results[0].success is True
    assert summary.source_results[0].collected == 5
    assert summary.source_results[0].error_type is None
    assert len(persisted) == 5


def test_persistence_receives_standard_opportunity_candidates() -> None:
    """Nothing Stagiaires-shaped reaches the store: it is the same dataclass
    every other collector produces, so no persistence change was needed."""
    from services.collector.models.opportunity import OpportunityCandidate

    _, candidates = collect(_FakeClient(routes()))

    assert candidates
    for candidate in candidates:
        assert isinstance(candidate, OpportunityCandidate)


def test_the_same_offer_yields_a_stable_identity_across_two_runs() -> None:
    """The double-run invariant, in the one place a unit test can check it.

    Opportunities are keyed on `(source_id, source_url)`. If a second run
    produced different identities for the same offers, it would re-create the
    whole batch as new rows instead of updating them.
    """
    first = collect(_FakeClient(routes()))[1]
    second = collect(_FakeClient(routes()))[1]

    identity = lambda batch: sorted((c.source_id, c.source_url) for c in batch)
    assert identity(first) == identity(second)
    assert len(set(identity(first))) == len(first)


# ==================== the sitemap safety bound is a refusal ================


def index_declaring(count: int) -> str:
    """A sitemap index declaring ``count`` offer sitemaps in the known family."""
    entries = "".join(
        f"<sitemap><loc>{HOST}/offre-sitemap{'' if n == 0 else n + 1}.xml</loc></sitemap>"
        for n in range(count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</sitemapindex>"
    )


def routes_for_index(count: int) -> dict:
    """An index declaring ``count`` offer sitemaps, each of them readable."""
    table = {
        ROBOTS_URL: (200, ROBOTS_BODY, "text/plain"),
        SITEMAP: (200, index_declaring(count), "application/xml"),
        f"{OFFER}/6100-slug-0": (200, detail_html(), "text/html"),
    }
    for n in range(count):
        name = f"offre-sitemap{'' if n == 0 else n + 1}.xml"
        rows = [(f"{OFFER}/6100-slug-0", "2026-09-01")] if n == 0 else []
        table[f"{HOST}/{name}"] = (200, urlset(rows), "application/xml")
    return table


def test_exactly_the_bound_is_allowed() -> None:
    """Twenty declared sitemaps is the largest set this collector will read."""
    client = _FakeClient(routes_for_index(StagiairesCollector.MAX_OFFER_SITEMAPS))

    _, candidates = collect(client)

    fetched = [item for item in client.requested if "offre-sitemap" in item]
    assert len(fetched) == StagiairesCollector.MAX_OFFER_SITEMAPS
    assert len(candidates) == 1


def test_one_sitemap_above_the_bound_fails_before_anything_is_fetched() -> None:
    """The finding: the bound truncated silently instead of refusing.

    Reading the first twenty and returning a candidate batch would present a
    materially partial read of the source as a complete one — the same failure
    as an unreadable sitemap, only quieter, because nothing would look wrong.
    """
    over = StagiairesCollector.MAX_OFFER_SITEMAPS + 1
    client = _FakeClient(routes_for_index(over))

    with pytest.raises((StagiairesCollectionError, StagiairesPayloadError)):
        collect(client)

    # robots and the index only: the refusal happens before the bounded subset
    # is touched, so no offer sitemap and no detail page is ever requested.
    assert client.requested == [ROBOTS_URL, SITEMAP]
    assert not any("offre-sitemap" in item for item in client.requested)
    assert not any("/stage-emploi-maroc/" in item for item in client.requested)


def test_no_candidate_is_returned_when_the_bound_is_exceeded() -> None:
    """A partial-success mode is exactly what must not exist here."""
    collector = StagiairesCollector(
        stagiaires_source(),
        client=_FakeClient(routes_for_index(StagiairesCollector.MAX_OFFER_SITEMAPS + 5)),
    )
    collector.DELAY_SECONDS = 0.0

    with pytest.raises((StagiairesCollectionError, StagiairesPayloadError)):
        collector.collect()


def test_the_error_names_the_declared_count_and_the_production_bound() -> None:
    """A reader of the failed run must see why, not just that."""
    over = StagiairesCollector.MAX_OFFER_SITEMAPS + 1

    with pytest.raises(StagiairesPayloadError) as failure:
        collect(_FakeClient(routes_for_index(over)))

    message = str(failure.value)
    assert str(over) in message
    assert str(StagiairesCollector.MAX_OFFER_SITEMAPS) in message
    assert "bound" in message.lower()
    assert "partial" in message.lower()


def test_the_bound_is_not_raised_to_accommodate_a_larger_index() -> None:
    """The safety bound stays put; what changed is how exceeding it is handled."""
    assert StagiairesCollector.MAX_OFFER_SITEMAPS == 20


def test_the_ordinary_two_sitemap_run_is_unchanged() -> None:
    """The real site declares two today, and that path is untouched."""
    client = _FakeClient(routes())

    _, candidates = collect(client)

    fetched = [item for item in client.requested if "offre-sitemap" in item]
    assert fetched == [f"{HOST}/offre-sitemap.xml", f"{HOST}/offre-sitemap2.xml"]
    assert len(candidates) == 5


def test_an_index_declaring_no_offer_sitemap_still_fails_the_same_way() -> None:
    """The zero case is unchanged by the new upper guard."""
    with pytest.raises(StagiairesPayloadError, match="no offer sitemap"):
        collect(_FakeClient(routes_for_index(0)))


def test_an_unreadable_required_sitemap_within_the_bound_still_fails() -> None:
    """Truncation and unreadability are different faults; both still fail."""
    client = _FakeClient(routes(), failing_url=f"{HOST}/offre-sitemap2.xml")

    with pytest.raises(StagiairesCollectionError, match="offre-sitemap2.xml"):
        collect(client)


def test_a_malformed_required_sitemap_within_the_bound_still_fails() -> None:
    table = routes()
    table[f"{HOST}/offre-sitemap2.xml"] = (200, "<html>nope</html>", "text/html")

    with pytest.raises(StagiairesPayloadError):
        collect(_FakeClient(table))
