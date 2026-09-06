"""Offline tests for the Phase 7C.5A Stage.ma public-access audit.

Nothing here opens a socket. Every byte parsed is a compact synthetic fixture
written in this file: a test that needs the real site fails when the site is
slow, when a template is edited, or when CI has no egress, none of which says
anything about this code. The fixtures imitate the *shapes* the audit must cope
with — a listing that carries offer links, one that does not, a sentinel date, an
expired banner, an off-domain apply button — never real Stage.ma content.

The invariants worth breaking a build over: that `01/01/1970` never becomes a
publication date, that a benchmark canary can never masquerade as live
discovery, that a clean audit finding nothing is a completed audit rather than a
failed one, and that no request ever leaves the Stage.ma hosts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from evaluation.morocco_pfe.cli import stage_ma_access_audit as cli
from evaluation.morocco_pfe.stage_ma_access import (
    AUDIT_USER_AGENT,
    AUDIT_VERSION,
    BENCHMARK_CANARY_URLS,
    DATE_SENTINEL_OR_INVALID,
    DATE_UNKNOWN,
    DATE_VALID_CANDIDATE,
    DETAIL_FIELDS,
    DISCOVERY_CANARY,
    DISCOVERY_LIVE,
    FEASIBILITY_ACCESS_BLOCKED,
    FEASIBILITY_INSUFFICIENT_DISCOVERY,
    FEASIBILITY_MULTI_SURFACE,
    FEASIBILITY_PUBLIC_HTML,
    FEASIBILITY_SITEMAP,
    FEASIBILITY_STRUCTURE_INSUFFICIENT,
    MAX_DETAIL_LIMIT,
    ROBOTS_ABSENT,
    ROBOTS_BARRIER,
    ROBOTS_OBEY,
    ROBOTS_UNRESOLVED,
    ROBOTS_URL,
    STATE_EXPIRED,
    STATE_PUBLISHED,
    STATE_UNKNOWN,
    STATE_UNPUBLISHED,
    SURFACE_HOME,
    SURFACE_LISTING,
    SURFACE_SPECIALTY,
    SURFACE_URLS,
    APPLY_NONE,
    APPLY_OFF_DOMAIN,
    APPLY_SAME_HOST,
    StageMaAccessError,
    application_evidence,
    canonical_url,
    classify_date_candidate,
    classify_robots_response,
    collect_surface_links,
    derive_feasibility,
    detail_field_evidence,
    detect_publication_state,
    extract_candidate_id,
    fetch_url,
    is_offer_detail_url,
    is_specialty_url,
    is_stage_ma_host,
    looks_browser_rendered,
    parse_robots_txt,
    parse_sitemap_document,
    publication_date_candidate,
    robots_verdict,
    select_detail_targets,
    sitemap_declarations,
    validate_detail_limit,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HOST = "https://www.stage.ma"
OFFER = f"{HOST}/offres-stage"
SITEMAP = f"{HOST}/sitemap.xml"

ROBOTS_BODY = (
    "# synthetic robots.txt, not a copy of the real file\n"
    "User-agent: *\n"
    "Disallow: /admin/\n"
    "Allow: /\n"
)
ROBOTS_WITH_SITEMAP = ROBOTS_BODY + f"\nSitemap: {SITEMAP}\n"


def listing_html(ids: list[int], *, specialties: list[str] | None = None) -> str:
    """A listing page whose ordinary server HTML carries the offer links."""
    offers = "".join(
        f'<a href="/offres-stage/{number}-stage-synthetique-{number}">Offre {number}</a>'
        for number in ids
    )
    categories = "".join(
        f'<a href="/specialites/{name}">{name}</a>' for name in (specialties or [])
    )
    return (
        "<html><head><title>Offres</title>"
        f'<link rel="canonical" href="{HOST}/offres-stage">'
        "</head><body><h1>Offres de stage</h1>"
        f"{offers}{categories}"
        '<a href="https://partenaire.example.com/ailleurs">Partenaire</a>'
        "<p>" + "texte de remplissage " * 40 + "</p>"
        "</body></html>"
    )


JS_ONLY_HTML = (
    "<html><head><title>Offres</title>"
    '<meta name="description" content="Offres de stage au Maroc">'
    "</head>"
    '<body><div id="root"></div>'
    '<script id="__NEXT_DATA__">{"props":{"pageProps":{"offers":[]}}}</script>'
    "<noscript>Activez JavaScript</noscript></body></html>"
)


def detail_html(
    *,
    title: str | None = "Stage PFE Intelligence Artificielle",
    organization: str | None = "Entreprise Synthetique",
    locality: str | None = "Casablanca",
    description: str | None = "Description synthetique inventee pour ce test unitaire.",
    date_posted: str | None = "2026-03-15",
    state_text: str = "",
    apply_markup: str = "",
    posting_url: str | None = None,
) -> str:
    import json as _json

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
    return (
        "<html><head><title>Offre</title>"
        f'<link rel="canonical" href="{OFFER}/9279-stage-pfe-intelligence-artificielle">'
        f'<script type="application/ld+json">{_json.dumps(posting)}</script>'
        "</head><body><h1>Offre</h1>"
        f"<p>{state_text}</p><p>" + "contenu " * 40 + "</p>"
        f"{apply_markup}</body></html>"
    )


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
            "the audit must resolve redirects itself, never hand them to httpx"
        )
        self.requested.append(url)
        if url == self.failing_url:
            raise httpx.ConnectTimeout("synthetic transport failure")
        if url in self.redirects:
            return _FakeResponse(
                url, self.redirect_status, "", "text/html",
                {"location": self.redirects[url]},
            )
        status, body, content_type = self.routes.get(
            url, (404, "page introuvable", "text/html")
        )
        return _FakeResponse(url, status, body, content_type)


def routes(**overrides) -> dict:
    table = {
        ROBOTS_URL: (200, ROBOTS_BODY, "text/plain"),
        SURFACE_HOME: (200, listing_html([100], specialties=["computer-science"]), "text/html"),
        SURFACE_LISTING: (200, listing_html([9279, 9233, 9100]), "text/html"),
        SURFACE_SPECIALTY: (200, listing_html([9279, 8800]), "text/html"),
    }
    for number in (100, 8800, 9100, 9233, 9279):
        table[f"{OFFER}/{number}-stage-synthetique-{number}"] = (
            200, detail_html(), "text/html",
        )
    table[f"{OFFER}/9279-stage-pfe-intelligence-artificielle"] = (
        200, detail_html(), "text/html",
    )
    table.update(overrides)
    return table


def routes_with_detail(html: str, **overrides) -> dict:
    """Every discovered detail page serves ``html``.

    Selection is by canonical URL, so which three pages a run samples is a
    property of the fixture; setting them all keeps these tests about the
    behaviour under test rather than about sort order.
    """
    table = routes(**overrides)
    for key in list(table):
        if "/offres-stage/" in key:
            table[key] = (200, html, "text/html")
    return table


def audit(client, **kwargs):
    return cli.run(client=client, delay=0.0, **kwargs)


# ================================== ROBOTS ==================================


def test_a_parseable_robots_is_obeyed() -> None:
    assert classify_robots_response(200, ROBOTS_URL, ROBOTS_BODY).disposition == ROBOTS_OBEY


@pytest.mark.parametrize("status", [404, 410])
def test_an_absent_robots_is_absent_and_not_permission(status: int) -> None:
    disposition = classify_robots_response(status, ROBOTS_URL, "")

    assert disposition.disposition == ROBOTS_ABSENT
    assert disposition.may_proceed is True


@pytest.mark.parametrize("status", [401, 403, 407, 429])
def test_a_refused_robots_is_a_barrier_and_stops_the_audit(status: int) -> None:
    disposition = classify_robots_response(status, ROBOTS_URL, "")

    assert disposition.disposition == ROBOTS_BARRIER
    assert disposition.may_proceed is False


@pytest.mark.parametrize("status", [500, 502, 503, 418])
def test_an_unresolved_robots_leaves_the_audit_incomplete(status: int) -> None:
    disposition = classify_robots_response(status, ROBOTS_URL, "")

    assert disposition.disposition == ROBOTS_UNRESOLVED
    assert disposition.may_proceed is False


def test_a_challenge_page_served_as_robots_is_a_refusal() -> None:
    assert classify_robots_response(
        200, ROBOTS_URL, "Just a moment... checking your browser"
    ).disposition == ROBOTS_BARRIER


def test_robots_matches_on_path_and_query() -> None:
    """Matching the path alone ignores every rule keyed on a parameter."""
    groups = parse_robots_txt("User-agent: *\nDisallow: /*?print=1\n")

    assert robots_verdict(groups, f"{OFFER}?print=1").allowed is False
    assert robots_verdict(groups, f"{OFFER}").allowed is True


def test_an_empty_disallow_never_cancels_a_site_wide_disallow() -> None:
    groups = parse_robots_txt("User-agent: *\nDisallow: /\nDisallow:\n")

    assert robots_verdict(groups, f"{HOST}/anything").allowed is False


def test_a_group_naming_the_audit_agent_wins_over_the_wildcard() -> None:
    groups = parse_robots_txt(
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: OpportunityRadarAI-AccessAudit\nAllow: /\n"
    )

    assert robots_verdict(groups, f"{OFFER}/1-a").allowed is True


def test_all_applicable_groups_are_merged() -> None:
    groups = parse_robots_txt(
        "User-agent: *\nAllow: /\n\nUser-agent: *\nDisallow: /offres-stage/\n"
    )

    assert robots_verdict(groups, f"{OFFER}/1-a").allowed is False
    assert robots_verdict(groups, f"{HOST}/autre").allowed is True


def test_an_end_anchored_rule_matches_only_the_exact_path() -> None:
    groups = parse_robots_txt("User-agent: *\nDisallow: /offres-stage$\n")

    assert robots_verdict(groups, SURFACE_LISTING).allowed is False
    assert robots_verdict(groups, f"{OFFER}/1-a").allowed is True


def test_a_disallowed_surface_is_never_requested() -> None:
    table = routes()
    table[ROBOTS_URL] = (200, "User-agent: *\nDisallow: /specialites/\nAllow: /\n", "text/plain")
    client = _FakeClient(table)

    report = audit(client)

    assert SURFACE_SPECIALTY not in client.requested
    specialty = next(
        item for item in report["surfaces"] if item["url"] == SURFACE_SPECIALTY
    )
    assert specialty["barrier"] == "ROBOTS_DISALLOWED"


def test_a_refused_robots_stops_before_any_surface_is_fetched() -> None:
    client = _FakeClient({ROBOTS_URL: (403, "", "text/html")})

    report = audit(client)

    assert client.requested == [ROBOTS_URL]
    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER
    assert report["feasibility"] == FEASIBILITY_ACCESS_BLOCKED


def test_an_unresolvable_robots_is_incomplete_not_completed() -> None:
    client = _FakeClient({ROBOTS_URL: (503, "", "text/html")})

    report = audit(client)

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE
    assert client.requested == [ROBOTS_URL]


# ================================= REDIRECTS ================================


def test_a_same_host_redirect_is_followed() -> None:
    moved = f"{HOST}/offres-stage-2026"
    table = routes()
    table[moved] = (200, listing_html([7001]), "text/html")
    client = _FakeClient(table, redirects={SURFACE_LISTING: moved})

    audit(client)

    assert moved in client.requested


def test_an_off_domain_redirect_target_is_never_requested() -> None:
    elsewhere = "https://tracker.example.com/landing"
    client = _FakeClient(routes(), redirects={SURFACE_LISTING: elsewhere})

    report = audit(client)

    assert elsewhere not in client.requested
    assert not any("tracker.example.com" in item for item in client.requested)
    listing = next(item for item in report["surfaces"] if item["url"] == SURFACE_LISTING)
    assert listing["barrier"] == cli.OFF_DOMAIN_REDIRECT
    assert listing["redirect_target_host"] == "tracker.example.com"


def test_a_robots_disallowed_redirect_target_is_never_requested() -> None:
    secret = f"{HOST}/admin/secret"
    table = routes()
    table[secret] = (200, listing_html([1]), "text/html")
    client = _FakeClient(table, redirects={SURFACE_LISTING: secret})

    report = audit(client)

    assert secret not in client.requested
    listing = next(item for item in report["surfaces"] if item["url"] == SURFACE_LISTING)
    assert listing["barrier"] == cli.ROBOTS_DISALLOWED_REDIRECT


def test_a_redirect_loop_is_bounded() -> None:
    a, b = f"{HOST}/loop-a", f"{HOST}/loop-b"
    client = _FakeClient(routes(), redirects={SURFACE_LISTING: a, a: b, b: a})

    audit(client)

    assert client.requested.count(a) <= cli.MAX_REDIRECTS + 1


def test_no_request_in_a_whole_run_leaves_the_stage_ma_hosts() -> None:
    client = _FakeClient(routes())

    audit(client)

    for url in client.requested:
        assert is_stage_ma_host(url), url


# ================================= SURFACES =================================


def test_a_listing_surface_exposes_its_offer_links() -> None:
    links = collect_surface_links(listing_html([9279, 9233]), SURFACE_LISTING)

    assert links.offer_urls == (
        f"{OFFER}/9279-stage-synthetique-9279",
        f"{OFFER}/9233-stage-synthetique-9233",
    )
    assert links.off_domain_links == 1


def test_a_specialty_surface_exposes_offer_and_category_links() -> None:
    links = collect_surface_links(
        listing_html([9279], specialties=["computer-science", "data"]),
        SURFACE_SPECIALTY,
    )

    assert len(links.offer_urls) == 1
    assert links.specialty_urls == (
        f"{HOST}/specialites/computer-science",
        f"{HOST}/specialites/data",
    )


def test_a_page_with_no_offer_links_is_a_valid_observation() -> None:
    """"Nothing here" is a finding about the source, not an error."""
    links = collect_surface_links("<html><body><h1>Bonjour</h1></body></html>", HOST)

    assert links.offer_urls == ()
    assert links.specialty_urls == ()


def test_duplicate_links_are_deduplicated() -> None:
    html = (
        f'<a href="{OFFER}/9279-a">un</a>'
        f'<a href="{OFFER}/9279-a">deux</a>'
        f'<a href="{OFFER}/9279-a/">trois</a>'
    )
    links = collect_surface_links(html, SURFACE_LISTING)

    assert len(links.offer_urls) == 1


def test_ordinary_html_is_distinguished_from_a_browser_rendered_page() -> None:
    """The fact a browser-free collector cares about: were the offers sent?"""
    served = listing_html([9279])
    ordinary = looks_browser_rendered(served, collect_surface_links(served, SURFACE_LISTING))
    empty = looks_browser_rendered(
        JS_ONLY_HTML, collect_surface_links(JS_ONLY_HTML, SURFACE_LISTING)
    )

    assert ordinary["offer_links_in_server_html"] is True
    assert ordinary["appears_to_require_browser"] is False
    assert empty["offer_links_in_server_html"] is False
    assert empty["appears_to_require_browser"] is True
    assert "__NEXT_DATA__" in empty["framework_markers"]


def test_the_audited_surfaces_are_a_fixed_bounded_set() -> None:
    """No flag can point this audit at an arbitrary discovery URL."""
    assert SURFACE_URLS == (SURFACE_HOME, SURFACE_LISTING, SURFACE_SPECIALTY)
    options = set(cli.build_parser()._option_string_actions)
    assert options == {"-h", "--help", "--limit", "--terms-url", "--timeout", "--delay"}


# ================================= SITEMAP ==================================


def test_a_declared_sitemap_is_read_and_classified() -> None:
    table = routes()
    table[ROBOTS_URL] = (200, ROBOTS_WITH_SITEMAP, "text/plain")
    table[SITEMAP] = (
        200,
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{OFFER}/9279-a</loc></url>"
        f"<url><loc>{HOST}/specialites/data</loc></url>"
        f"<url><loc>https://autre.example.com/offres-stage/1-a</loc></url>"
        "</urlset>",
        "application/xml",
    )
    report = audit(_FakeClient(table))

    document = report["sitemap"]["documents"][0]
    assert document["offer_detail_urls"] == 1
    assert document["specialty_urls"] == 1
    assert document["off_domain_urls_ignored"] == 1
    assert report["sitemap"]["offer_detail_urls_total"] == 1


def test_no_sitemap_declaration_is_recorded_honestly_and_nothing_is_guessed() -> None:
    """A lucky 200 on a filename we invented is not a discovery contract."""
    table = routes()
    table[SITEMAP] = (200, "<urlset/>", "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert report["sitemap"]["declared_by_robots"] == []
    assert report["sitemap"]["guessed_any_url"] is False
    assert SITEMAP not in client.requested
    assert "declares no same-host sitemap" in report["sitemap"]["note"]


def test_an_off_domain_declared_sitemap_is_not_fetched() -> None:
    table = routes()
    table[ROBOTS_URL] = (
        200,
        ROBOTS_BODY + "\nSitemap: https://cdn.example.com/sitemap.xml\n",
        "text/plain",
    )
    client = _FakeClient(table)

    report = audit(client)

    assert not any("cdn.example.com" in item for item in client.requested)
    assert report["sitemap"]["off_domain_declarations_ignored"] == 1
    assert report["sitemap"]["same_host_declarations"] == []


def test_a_malformed_declared_sitemap_leaves_the_audit_incomplete() -> None:
    """A required document the site pointed us at must not read as zero URLs."""
    table = routes()
    table[ROBOTS_URL] = (200, ROBOTS_WITH_SITEMAP, "text/plain")
    table[SITEMAP] = (200, "<html><body>pas du XML</body></html>", "text/html")

    report = audit(_FakeClient(table))

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE
    assert any("SITEMAP_UNPARSABLE" in item for item in report["incomplete_reasons"])


def test_an_unreachable_declared_sitemap_leaves_the_audit_incomplete() -> None:
    table = routes()
    table[ROBOTS_URL] = (200, ROBOTS_WITH_SITEMAP, "text/plain")
    client = _FakeClient(table, failing_url=SITEMAP)

    report = audit(client)

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert any("SITEMAP_UNREADABLE" in item for item in report["incomplete_reasons"])


def test_a_sitemapindex_root_is_accepted_as_well_as_a_urlset() -> None:
    parsed = parse_sitemap_document(
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{HOST}/sitemap-1.xml</loc></sitemap></sitemapindex>",
        SITEMAP,
    )

    assert parsed.root_tag == "sitemapindex"
    assert parsed.nested_sitemaps == (f"{HOST}/sitemap-1.xml",)


def test_a_document_that_is_not_a_sitemap_is_refused() -> None:
    with pytest.raises(StageMaAccessError):
        parse_sitemap_document("<html><body>oops</body></html>", SITEMAP)


# =============================== DETAIL URLS ================================


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{OFFER}/9279-stage-pfe-intelligence-artificielle", "9279"),
        (f"{OFFER}/9233", "9233"),
        (f"{OFFER}/9279-slug/", "9279"),
        (f"{OFFER}/007-slug", "007"),
    ],
)
def test_the_candidate_id_is_read_as_published(url: str, expected: str) -> None:
    """`007` stays `007`: normalizing it to `7` would invent an equality."""
    assert extract_candidate_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://autre.example.com/offres-stage/9279-a",
        f"{HOST}/blog/9279-a",
        f"{OFFER}/slug-sans-id",
        f"{OFFER}/92a79-mixte",
        f"{OFFER}/9279abc-colle",
        f"{OFFER}/",
    ],
)
def test_an_invalid_detail_shape_is_rejected(url: str) -> None:
    assert extract_candidate_id(url) is None
    assert is_offer_detail_url(url) is False


def test_specialty_urls_are_recognized_separately_from_offers() -> None:
    assert is_specialty_url(f"{HOST}/specialites/computer-science") is True
    assert is_specialty_url(f"{OFFER}/9279-a") is False


def test_canonicalization_drops_only_the_meaningless_parts() -> None:
    assert canonical_url(f"HTTPS://WWW.Stage.MA/Offres-Stage/9279-A/?x=1#f") == (
        f"{HOST}/Offres-Stage/9279-A"
    )
    with pytest.raises(StageMaAccessError):
        canonical_url("   ")


def test_the_numeric_id_never_orders_the_detail_sample() -> None:
    """Ordering by ID would smuggle in a recency claim we have no evidence for."""
    urls = (
        f"{OFFER}/9999-tres-recent-en-apparence",
        f"{OFFER}/1000-ancien-en-apparence",
    )
    chosen = select_detail_targets(urls, 2)

    assert chosen == (
        f"{OFFER}/1000-ancien-en-apparence",
        f"{OFFER}/9999-tres-recent-en-apparence",
    )
    assert select_detail_targets(tuple(reversed(urls)), 2) == chosen


def test_the_detail_sample_is_deduplicated_and_hard_bounded() -> None:
    urls = tuple(f"{OFFER}/{n}-slug" for n in range(200)) * 2

    assert len(select_detail_targets(urls, MAX_DETAIL_LIMIT)) == MAX_DETAIL_LIMIT
    assert MAX_DETAIL_LIMIT == 5


@pytest.mark.parametrize("limit", [0, -1, 6, 100, True, False, 2.5, "3", None])
def test_a_limit_outside_the_bound_is_refused_rather_than_clamped(limit) -> None:
    with pytest.raises(StageMaAccessError):
        validate_detail_limit(limit)


def test_the_configured_limit_bounds_real_detail_requests() -> None:
    client = _FakeClient(routes())

    audit(client, limit=1)

    details = [item for item in client.requested if "/offres-stage/" in item]
    assert len(details) == 1


# ============================= DETAIL STRUCTURE =============================


def test_a_detail_page_reports_the_fields_it_carries() -> None:
    fields = detail_field_evidence(detail_html())

    assert fields["title"].found is True
    assert fields["title"].value == "Stage PFE Intelligence Artificielle"
    assert fields["organization"].value == "Entreprise Synthetique"
    assert fields["location"].value == "Casablanca"
    assert tuple(fields) == DETAIL_FIELDS


def test_the_description_is_measured_and_never_quoted() -> None:
    """Third-party job text is counted, not copied into this repository."""
    description = detail_field_evidence(detail_html())["description"]

    assert description.found is True
    assert description.value is None
    assert description.chars == len(
        "Description synthetique inventee pour ce test unitaire."
    )


def test_an_absent_field_is_reported_absent_rather_than_guessed() -> None:
    fields = detail_field_evidence(
        detail_html(organization=None, locality=None, description=None)
    )

    for name in ("organization", "location", "description"):
        assert fields[name].found is False
        assert fields[name].carrier is None
        assert fields[name].value is None


def test_structured_data_is_reported_as_types_and_key_names_never_values() -> None:
    from evaluation.morocco_pfe.stage_ma_access import summarize_structured_data

    summary = summarize_structured_data(detail_html())

    assert summary["types"] == ["JobPosting"]
    assert "title" in summary["job_posting_keys"]
    assert "Stage PFE Intelligence Artificielle" not in summary["job_posting_keys"]


# ============================ PUBLICATION STATE =============================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Offre expirée", STATE_EXPIRED),
        ("Cette offre n'est plus disponible", STATE_EXPIRED),
        ("Offre non publiée", STATE_UNPUBLISHED),
        ("Publiée le 15/03/2026", STATE_PUBLISHED),
        ("Date de publication : 15/03/2026", STATE_PUBLISHED),
        ("", STATE_UNKNOWN),
        ("Un texte sans indication", STATE_UNKNOWN),
    ],
)
def test_the_publication_state_comes_from_the_pages_own_words(
    text: str, expected: str
) -> None:
    assert detect_publication_state(detail_html(state_text=text)).state == expected


def test_expiry_overrides_a_publication_label_on_the_same_page() -> None:
    """"Publiée le 3 mars — offre expirée" is expired, not published."""
    state = detect_publication_state(
        detail_html(state_text="Publiée le 03/03/2026 — Offre expirée")
    )

    assert state.state == STATE_EXPIRED
    assert state.evidence is not None


def test_an_unknown_state_carries_no_invented_evidence() -> None:
    state = detect_publication_state(detail_html(state_text=""))

    assert state.state == STATE_UNKNOWN
    assert state.evidence is None


def test_the_state_is_not_inferred_from_http_status_alone() -> None:
    """A 200 does not mean an offer is open, and the audit does not pretend."""
    assert detect_publication_state(detail_html(state_text="")).state == STATE_UNKNOWN


# ============================== DATE SEMANTICS ==============================


@pytest.mark.parametrize(
    ("value", "normalized"),
    [("2026-03-15", "2026-03-15"), ("15/03/2026", "2026-03-15"), ("15-03-2026", "2026-03-15")],
)
def test_a_real_displayed_date_is_a_valid_candidate(value: str, normalized: str) -> None:
    candidate = classify_date_candidate(value)

    assert candidate.status == DATE_VALID_CANDIDATE
    assert candidate.normalized == normalized
    # A candidate for a later phase to consider — never asserted as published_at.
    assert candidate.as_dict()["is_published_at"] is False


@pytest.mark.parametrize("value", ["01/01/1970", "1970-01-01", "1/1/1970", "01-01-1970"])
def test_epoch_zero_is_flagged_as_a_sentinel_not_a_publication_date(value: str) -> None:
    """The case this exists for: an unset date column rendered as 1970."""
    candidate = classify_date_candidate(value)

    assert candidate.status == DATE_SENTINEL_OR_INVALID
    assert candidate.as_dict()["is_published_at"] is False


@pytest.mark.parametrize("value", ["32/13/2026", "pas une date", "0000-00-00", "-"])
def test_a_malformed_date_is_flagged_and_never_replaced(value: str) -> None:
    candidate = classify_date_candidate(value)

    assert candidate.status == DATE_SENTINEL_OR_INVALID
    assert candidate.normalized is None


@pytest.mark.parametrize("value", ["", "   ", None, 17])
def test_an_absent_date_stays_unknown(value) -> None:
    candidate = classify_date_candidate(value)

    assert candidate.status == DATE_UNKNOWN
    assert candidate.raw is None
    assert candidate.normalized is None


def test_a_page_stating_no_date_yields_unknown_not_a_derived_one() -> None:
    """Nothing is derived from the numeric URL id or from when we fetched."""
    candidate = publication_date_candidate(detail_html(date_posted=None))

    assert candidate.status == DATE_UNKNOWN
    assert candidate.normalized is None


def test_a_sentinel_date_on_a_real_page_is_surfaced_in_the_report() -> None:
    report = audit(_FakeClient(routes_with_detail(detail_html(date_posted="01/01/1970"))))

    anomalies = report["date_anomalies"]
    assert anomalies["sentinel_or_invalid"] == 3
    assert anomalies["sentinel_examples"]
    assert report["displayed_date_mapped_to_published_at"] is False


def test_no_report_field_maps_a_displayed_date_to_published_at() -> None:
    """Checked against the report's keys, not its prose.

    The report says out loud that it maps nothing to `published_at`; a test
    forbidding the word would forbid saying so. What must not exist is a
    *field* by that name carrying a value.
    """
    def keys(node) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            for key, value in node.items():
                found.add(key)
                found |= keys(value)
        elif isinstance(node, list):
            for item in node:
                found |= keys(item)
        return found

    report = audit(_FakeClient(routes()))

    assert "published_at" not in keys(report)
    assert report["displayed_date_mapped_to_published_at"] is False
    for page in report["detail_sample"]["pages"]:
        assert page["publication_date_candidate"]["is_published_at"] is False


# ============================= APPLICATION URL ==============================


def test_an_explicit_same_host_application_action_is_recorded() -> None:
    evidence = application_evidence(
        '<a href="/postuler/9279">Postuler</a>', f"{OFFER}/9279-a"
    )

    assert evidence.found is True
    assert evidence.href_category == APPLY_SAME_HOST
    assert evidence.href == f"{HOST}/postuler/9279"


def test_an_off_domain_application_href_is_categorized_but_never_fetched() -> None:
    client = _FakeClient(
        routes_with_detail(
            detail_html(
                apply_markup='<a href="https://ats.example.com/apply/1">Postuler</a>'
            )
        )
    )

    report = audit(client)

    applications = {
        page["application"]["href_category"]
        for page in report["detail_sample"]["pages"]
        if page.get("application")
    }
    assert applications == {APPLY_OFF_DOMAIN}
    assert not any("ats.example.com" in item for item in client.requested)
    for page in report["detail_sample"]["pages"]:
        if page.get("application"):
            assert page["application"]["fetched"] is False


def test_a_posting_url_alone_is_not_an_application_url() -> None:
    """Every offer has a canonical URL; treating one as an apply link would
    claim an application route for offers that may have none."""
    evidence = application_evidence(
        detail_html(posting_url=f"{OFFER}/9279-a"), f"{OFFER}/9279-a"
    )

    assert evidence.found is False
    assert evidence.href_category == APPLY_NONE


def test_a_navigation_link_is_not_an_application_link() -> None:
    assert application_evidence(
        '<a href="/">Accueil</a><a href="/offres-stage">Toutes les offres</a>',
        f"{OFFER}/9279-a",
    ).found is False


def test_a_control_without_a_usable_href_reports_intent_but_no_url() -> None:
    evidence = application_evidence(
        "<a href='javascript:void(0)'>Postuler</a>", f"{OFFER}/9279-a"
    )

    assert evidence.found is True
    assert evidence.href_category == APPLY_NONE
    assert evidence.href is None


# ================================ DISCOVERY =================================


def test_live_discovered_details_are_preferred_over_the_benchmark_canaries() -> None:
    client = _FakeClient(routes())

    report = audit(client)

    assert report["discovery"]["used_benchmark_canaries"] is False
    assert report["detail_sample"]["live_discovered"] == 3
    assert report["detail_sample"]["benchmark_canaries"] == 0
    for page in report["detail_sample"]["pages"]:
        assert page["discovery"] == DISCOVERY_LIVE


def test_the_benchmark_canaries_are_used_only_when_nothing_was_discovered() -> None:
    """A canary proves a page parses. It never proves discovery works."""
    table = routes()
    for surface in SURFACE_URLS:
        table[surface] = (200, JS_ONLY_HTML, "text/html")
    for url in BENCHMARK_CANARY_URLS:
        table[url] = (200, detail_html(), "text/html")
    client = _FakeClient(table)

    report = audit(client)

    assert report["discovery"]["used_benchmark_canaries"] is True
    assert report["detail_sample"]["live_discovered"] == 0
    assert report["detail_sample"]["benchmark_canaries"] > 0
    for page in report["detail_sample"]["pages"]:
        assert page["discovery"] == DISCOVERY_CANARY
        assert page["url"] in BENCHMARK_CANARY_URLS


def test_a_canary_never_counts_as_live_discovery_for_feasibility() -> None:
    """Parsing a known URL cannot rescue a source whose discovery is broken."""
    table = routes()
    for surface in SURFACE_URLS:
        table[surface] = (200, JS_ONLY_HTML, "text/html")
    for url in BENCHMARK_CANARY_URLS:
        table[url] = (200, detail_html(), "text/html")

    report = audit(_FakeClient(table))

    assert report["discovery"]["live_offer_urls_found"] == 0
    assert report["feasibility"] == FEASIBILITY_INSUFFICIENT_DISCOVERY


# ============================== AUDIT STATUS ================================


def test_a_clean_run_with_discovery_completes_and_is_feasible() -> None:
    report = audit(_FakeClient(routes()))

    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["exit_code"] == cli.EXIT_OK
    assert report["feasibility"] in {
        FEASIBILITY_PUBLIC_HTML,
        FEASIBILITY_MULTI_SURFACE,
    }


def test_a_clean_run_finding_nothing_completes_with_insufficient_discovery() -> None:
    """The distinction that matters: a good audit may report a bad source."""
    table = routes()
    for surface in SURFACE_URLS:
        table[surface] = (200, JS_ONLY_HTML, "text/html")
    for url in BENCHMARK_CANARY_URLS:
        table[url] = (200, detail_html(), "text/html")

    report = audit(_FakeClient(table))

    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["exit_code"] == cli.EXIT_OK
    assert report["feasibility"] == FEASIBILITY_INSUFFICIENT_DISCOVERY


def test_a_surface_barrier_makes_the_run_a_barrier() -> None:
    table = routes()
    table[SURFACE_LISTING] = (403, "", "text/html")

    report = audit(_FakeClient(table))

    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER
    assert "HTTP_403_FORBIDDEN" in report["barriers"]


def test_an_unreadable_surface_makes_the_run_incomplete() -> None:
    client = _FakeClient(routes(), failing_url=SURFACE_LISTING)

    report = audit(client)

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE


def test_an_unavailable_offer_page_is_a_lifecycle_finding_not_a_barrier() -> None:
    """A deleted offer says something about the source, not about our access."""
    table = routes()
    table[f"{OFFER}/9100-stage-synthetique-9100"] = (404, "page introuvable", "text/html")

    report = audit(_FakeClient(table))

    assert report["outcome"] == cli.OUTCOME_COMPLETED
    unavailable = [
        page for page in report["detail_sample"]["pages"]
        if page.get("page_state") == "PAGE_UNAVAILABLE"
    ]
    assert len(unavailable) == 1
    # Recorded as a page state, never as an access barrier.
    assert unavailable[0].get("barrier") is None
    assert report["barriers"] == []


@pytest.mark.parametrize(
    "scenario", ["healthy", "robots_refused", "robots_unresolved", "surface_403", "no_discovery"]
)
def test_outcome_and_exit_code_never_contradict_each_other(scenario: str) -> None:
    table = routes()
    if scenario == "robots_refused":
        table[ROBOTS_URL] = (403, "", "text/html")
    elif scenario == "robots_unresolved":
        table[ROBOTS_URL] = (500, "", "text/html")
    elif scenario == "surface_403":
        table[SURFACE_LISTING] = (403, "", "text/html")
    elif scenario == "no_discovery":
        for surface in SURFACE_URLS:
            table[surface] = (200, JS_ONLY_HTML, "text/html")
        for url in BENCHMARK_CANARY_URLS:
            table[url] = (200, detail_html(), "text/html")

    report = audit(_FakeClient(table))
    outcome, exit_code = report["outcome"], report["exit_code"]

    assert (outcome == cli.OUTCOME_COMPLETED) == (exit_code == cli.EXIT_OK)
    if outcome == cli.OUTCOME_BARRIER:
        assert exit_code == cli.EXIT_BARRIER
    if outcome == cli.OUTCOME_INCOMPLETE:
        assert exit_code == cli.EXIT_FAILURE
    if exit_code == cli.EXIT_OK:
        assert not report.get("barriers")
        assert not report.get("incomplete_reasons")


def test_no_failure_can_silently_become_a_success() -> None:
    table = routes()
    table[SURFACE_LISTING] = (403, "", "text/html")
    table[SURFACE_SPECIALTY] = (429, "", "text/html")

    report = audit(_FakeClient(table))

    assert report["exit_code"] != cli.EXIT_OK
    assert set(report["barriers"]) == {"HTTP_403_FORBIDDEN", "HTTP_429_RATE_LIMITED"}


# ============================== FEASIBILITY =================================


def test_feasibility_reflects_only_what_the_evidence_supports() -> None:
    common = dict(sampled_live_pages=1, pages_with_title_and_organization=1)

    assert derive_feasibility(
        access_blocked=True, surfaces_with_offers=2, sitemap_offer_urls=5,
        live_detail_urls=5, **common,
    ) == FEASIBILITY_ACCESS_BLOCKED
    assert derive_feasibility(
        access_blocked=False, surfaces_with_offers=0, sitemap_offer_urls=0,
        live_detail_urls=0, sampled_live_pages=0, pages_with_title_and_organization=0,
    ) == FEASIBILITY_INSUFFICIENT_DISCOVERY
    assert derive_feasibility(
        access_blocked=False, surfaces_with_offers=1, sitemap_offer_urls=0,
        live_detail_urls=3, **common,
    ) == FEASIBILITY_PUBLIC_HTML
    assert derive_feasibility(
        access_blocked=False, surfaces_with_offers=2, sitemap_offer_urls=0,
        live_detail_urls=3, **common,
    ) == FEASIBILITY_MULTI_SURFACE
    assert derive_feasibility(
        access_blocked=False, surfaces_with_offers=0, sitemap_offer_urls=4,
        live_detail_urls=4, **common,
    ) == FEASIBILITY_SITEMAP


def test_discovery_that_works_but_yields_unusable_pages_is_structure_insufficient() -> None:
    assert derive_feasibility(
        access_blocked=False,
        surfaces_with_offers=2,
        sitemap_offer_urls=0,
        live_detail_urls=5,
        sampled_live_pages=3,
        pages_with_title_and_organization=0,
    ) == FEASIBILITY_STRUCTURE_INSUFFICIENT


def test_a_source_whose_pages_lack_required_fields_is_reported_as_such() -> None:
    report = audit(
        _FakeClient(routes_with_detail(detail_html(title=None, organization=None)))
    )

    assert report["feasibility"] == FEASIBILITY_STRUCTURE_INSUFFICIENT
    assert report["outcome"] == cli.OUTCOME_COMPLETED


# =================================== TERMS ==================================


def test_the_terms_check_is_not_attempted_without_an_operator_url() -> None:
    report = audit(_FakeClient(routes()))

    assert report["terms_review"]["barrier"] == "NOT_ATTEMPTED_NO_EVIDENCED_TERMS_URL"
    assert report["terms_review"]["attempted"] is False
    assert report["terms_review"]["manual_review_required"] is True
    assert report["outcome"] == cli.OUTCOME_COMPLETED


def test_an_off_domain_terms_url_is_never_fetched() -> None:
    client = _FakeClient(routes())

    report = audit(client, terms_url="https://example.com/cgu")

    assert report["terms_review"]["barrier"] == "NOT_ATTEMPTED_OFF_DOMAIN"
    assert not any("example.com" in item for item in client.requested)


def test_a_reachable_terms_page_still_requires_human_review() -> None:
    table = routes()
    table[f"{HOST}/cgu"] = (
        200, "<html><body><h1>CGU</h1><p>" + "texte " * 100 + "</p></body></html>", "text/html",
    )

    report = audit(_FakeClient(table), terms_url=f"{HOST}/cgu")

    assert report["terms_review"]["available"] is True
    assert report["terms_review"]["manual_review_required"] is True
    assert "not permission" in report["terms_review"]["note"].lower()
    assert report["manual_review_required"] is True


def test_a_terms_url_never_becomes_a_discovery_source() -> None:
    table = routes()
    terms = f"{HOST}/cgu"
    table[terms] = (200, listing_html([4242]), "text/html")

    report = audit(_FakeClient(table), terms_url=terms)

    assert f"{OFFER}/4242-stage-synthetique-4242" not in str(
        report["detail_sample"]["pages"]
    )


# =================================== OUTPUT =================================


def test_the_report_is_one_json_document_with_the_expected_sections() -> None:
    import json

    report = audit(_FakeClient(routes()))
    payload = json.loads(json.dumps(report))

    for section in (
        "audit", "audit_version", "target_host", "outcome", "exit_code",
        "feasibility", "manual_review_required", "robots", "sitemap", "surfaces",
        "discovery", "detail_sample", "date_anomalies", "barriers",
        "incomplete_reasons", "terms_review",
    ):
        assert section in payload, section
    assert payload["audit"] == "7C.5A"
    assert payload["audit_version"] == AUDIT_VERSION
    assert payload["user_agent"] == AUDIT_USER_AGENT


def test_the_report_carries_no_page_body_or_description() -> None:
    import json

    payload = json.dumps(audit(_FakeClient(routes())))

    assert "<html" not in payload
    assert "__NEXT_DATA__" not in payload
    assert "Description synthetique inventee" not in payload
    assert "texte de remplissage" not in payload


def test_the_report_declares_the_guarantees_this_slice_makes() -> None:
    report = audit(_FakeClient(routes()))

    for guarantee in (
        "authenticated",
        "browser_automation",
        "javascript_executed",
        "wrote_database",
        "wrote_raw_html",
        "activated_production_source",
        "displayed_date_mapped_to_published_at",
    ):
        assert report[guarantee] is False, guarantee


# ============================ PRODUCTION ISOLATION ==========================


def test_the_audit_itself_remains_evaluation_only() -> None:
    """The audit stays an audit, even now that a collector exists.

    7C.5A asserted that no Stage.ma production code existed anywhere; Phase
    7C.5B added a collector and a parser, deliberately and under review, so that
    exact assertion belonged to that phase. What must still hold is narrower and
    permanent: *this* module is evidence infrastructure. It builds no collector,
    reads no production registry, and cannot put a source into service.
    """
    import sys

    for name in (
        "evaluation.morocco_pfe.stage_ma_access",
        "evaluation.morocco_pfe.cli.stage_ma_access_audit",
    ):
        __import__(name)
        module = sys.modules[name]
        for attribute in vars(module).values():
            origin = getattr(attribute, "__module__", "") or ""
            assert not origin.startswith("services."), f"{name} -> {origin}"

    report = audit(_FakeClient(routes()))
    assert report["activated_production_source"] is False
    assert report["wrote_database"] is False


def test_implementing_a_collector_did_not_activate_the_source_map() -> None:
    """Phase 7C.5B built the collector; activation is still a separate decision.

    7C.5A asserted no production row existed at all, which was true of that
    phase. The invariant that replaces it is the one that still matters: code
    existing is not evidence that it works, so the source map stays a CANDIDATE
    until a real dry-run and SQLite double-run say otherwise.
    """
    from evaluation.morocco_pfe.validator import (
        DEFAULT_SOURCE_MAP_PATH,
        load_source_map,
    )

    entry = {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["stage_ma"]

    assert entry.integration_status == "CANDIDATE"
    assert entry.collection_strategy == "FUTURE_COLLECTOR"
    assert entry.production_source_id is None
    assert entry.live_canary is False


def test_this_phase_added_no_database_migration_for_stage_ma() -> None:
    for path in (REPOSITORY_ROOT / "migrations").glob("*.sql"):
        assert "stage.ma" not in path.read_text(encoding="utf-8").lower()
        assert "stage_ma" not in path.name.lower()


def test_production_services_never_import_this_audit_module() -> None:
    """`evaluation/` is an audit trail, not a runtime dependency.

    Asserted from the import graph rather than from the text: the production
    parser's docstring names this module precisely to explain that it re-states
    its semantics instead of importing them, and that sentence is worth keeping.
    """
    offenders = []
    for path in (REPOSITORY_ROOT / "services").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            for module in modules:
                if module.split(".")[0] == "evaluation":
                    offenders.append(f"{path.relative_to(REPOSITORY_ROOT)}: {module}")
    assert offenders == []


def test_the_audit_imports_nothing_that_could_write_or_collect() -> None:
    """Checked against imports, not prose: the docstrings say "no Selenium"."""
    for module in (
        "evaluation/morocco_pfe/stage_ma_access.py",
        "evaluation/morocco_pfe/cli/stage_ma_access_audit.py",
    ):
        tree = ast.parse((REPOSITORY_ROOT / module).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for name in imported:
            lowered = name.lower()
            for forbidden in (
                "services.",
                "sqlite",
                "libsql",
                "selenium",
                "playwright",
                "webdriver",
            ):
                assert forbidden not in lowered, f"{module}: imports {name}"


def test_the_audit_never_issues_a_non_get_request() -> None:
    """The fake client only implements `get`; a POST would raise here."""
    client = _FakeClient(routes())

    audit(client)

    assert not hasattr(client, "post")
    assert client.requested


# ================================ SOURCE MAP ================================


def test_stage_ma_remains_a_non_production_candidate() -> None:
    """7C.5A audits a source. It does not promote one."""
    from evaluation.morocco_pfe.validator import (
        DEFAULT_SOURCE_MAP_PATH,
        load_source_map,
    )

    entry = {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["stage_ma"]

    assert entry.integration_status == "CANDIDATE"
    assert entry.collection_strategy == "FUTURE_COLLECTOR"
    assert entry.production_source_id is None
    assert entry.live_canary is False
    assert entry.priority == "P1"
    assert entry.coverage_role == "PRIMARY"
    assert entry.country == "MA"


def test_the_stage_ma_note_claims_no_collector_and_no_activation() -> None:
    from evaluation.morocco_pfe.validator import (
        DEFAULT_SOURCE_MAP_PATH,
        load_source_map,
    )

    note = " ".join(
        {
            item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
        }["stage_ma"].notes.split()
    )

    assert "ACTIVE" not in note
    assert "no collector" in note.lower()
    assert "production_source_id: stage_ma" not in note


def test_stagiaires_activation_is_untouched_by_this_slice() -> None:
    """Do not weaken the neighbouring source to make this one pass."""
    from evaluation.morocco_pfe.validator import (
        DEFAULT_SOURCE_MAP_PATH,
        load_source_map,
    )

    entry = {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["stagiaires_ma"]

    assert entry.integration_status == "ACTIVE"
    assert entry.production_source_id == "stagiaires_ma"


# ====================================================================
# Architect review — completion semantics
# ====================================================================


def sitemap_urlset(offer_ids: list[int]) -> str:
    body = "".join(
        f"<url><loc>{OFFER}/{number}-stage-synthetique-{number}</loc></url>"
        for number in offer_ids
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )


def sitemap_index(children: list[str]) -> str:
    body = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in children)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</sitemapindex>"
    )


def robots_declaring(*sitemaps: str) -> str:
    return ROBOTS_BODY + "\n" + "".join(f"Sitemap: {url}\n" for url in sitemaps)


# ------------------------- 1. the sitemap bound refuses ---------------------


def test_exactly_the_document_budget_may_be_audited() -> None:
    urls = [f"{HOST}/sitemap-{n}.xml" for n in range(cli.MAX_SITEMAPS)]
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(*urls), "text/plain")
    for index, url in enumerate(urls):
        table[url] = (200, sitemap_urlset([7000 + index]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert report["sitemap"]["documents_read"] == cli.MAX_SITEMAPS
    assert report["sitemap"]["complete"] is True
    assert report["outcome"] == cli.OUTCOME_COMPLETED


def test_more_root_declarations_than_the_budget_fails_closed() -> None:
    """The finding: `same_host[:MAX_SITEMAPS]` made a partial read look whole."""
    urls = [f"{HOST}/sitemap-{n}.xml" for n in range(cli.MAX_SITEMAPS + 1)]
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(*urls), "text/plain")
    for url in urls:
        table[url] = (200, sitemap_urlset([7001]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE
    assert "SITEMAP_DECLARATION_COUNT_EXCEEDS_BOUND" in report["incomplete_reasons"]
    # Nothing truncated: not one of the declared documents was fetched.
    assert not any(url in client.requested for url in urls)
    assert report["sitemap"]["documents_read"] == 0
    assert report["sitemap"]["complete"] is False
    assert report["sitemap"]["offer_detail_urls_total"] == 0


def test_the_document_budget_is_not_raised() -> None:
    assert cli.MAX_SITEMAPS == 3


# ---------------------- 2. bounded sitemap-index traversal ------------------


def test_a_declared_index_leads_to_its_child_urlset() -> None:
    child = f"{HOST}/sitemap-offres-1.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index([child]), "application/xml")
    table[child] = (200, sitemap_urlset([7100, 7101]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert child in client.requested
    assert report["sitemap"]["documents_read"] == 2
    assert report["sitemap"]["offer_detail_urls_total"] == 2
    assert report["sitemap"]["complete"] is True


def test_child_offer_urls_count_only_when_the_child_was_actually_read() -> None:
    """An index naming a child is not the same as having read the child."""
    child = f"{HOST}/sitemap-offres-1.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index([child]), "application/xml")
    client = _FakeClient(table, failing_url=child)

    report = audit(client)

    assert report["sitemap"]["offer_detail_urls_total"] == 0
    assert report["sitemap"]["complete"] is False
    assert report["outcome"] == cli.OUTCOME_INCOMPLETE


def test_root_plus_nested_reads_never_exceed_the_global_budget() -> None:
    children = [f"{HOST}/sitemap-offres-{n}.xml" for n in range(2)]
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index(children), "application/xml")
    for index, url in enumerate(children):
        table[url] = (200, sitemap_urlset([7200 + index]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    sitemap_gets = [item for item in client.requested if "sitemap" in item]
    assert len(sitemap_gets) <= cli.MAX_SITEMAPS
    assert report["sitemap"]["documents_read"] == 3


def test_a_nested_sitemap_named_twice_is_read_once() -> None:
    child = f"{HOST}/sitemap-offres-1.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index([child, child, f"{child}/"]), "application/xml")
    table[child] = (200, sitemap_urlset([7300]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert client.requested.count(child) == 1
    assert report["sitemap"]["complete"] is True


def test_an_off_domain_nested_sitemap_is_never_fetched() -> None:
    elsewhere = "https://cdn.example.com/sitemap-offres.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index([elsewhere]), "application/xml")
    client = _FakeClient(table)

    audit(client)

    assert not any("cdn.example.com" in item for item in client.requested)


def test_a_robots_disallowed_nested_sitemap_is_never_fetched() -> None:
    child = f"{HOST}/admin/sitemap-offres.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index([child]), "application/xml")
    table[child] = (200, sitemap_urlset([7400]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert child not in client.requested
    assert report["outcome"] == cli.OUTCOME_ROBOTS_DISALLOWED
    assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED
    assert f"sitemap:{child}" in report["robots_disallowed_targets"]


def test_more_children_than_the_remaining_budget_is_incomplete_not_partial() -> None:
    """Reading the first few children and reporting their URLs would present a
    fraction of the site's sitemap coverage as all of it."""
    children = [f"{HOST}/sitemap-offres-{n}.xml" for n in range(4)]
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index(children), "application/xml")
    for index, url in enumerate(children):
        table[url] = (200, sitemap_urlset([7500 + index]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE
    assert any(
        item.startswith("SITEMAP_INDEX_CHILDREN_EXCEED_BOUND")
        for item in report["incomplete_reasons"]
    )
    assert not any(url in client.requested for url in children)
    assert report["sitemap"]["complete"] is False
    assert report["sitemap"]["coverage_note"].startswith("PARTIAL")


def test_a_partial_sitemap_read_never_claims_complete_coverage() -> None:
    child = f"{HOST}/sitemap-offres-1.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index([child]), "application/xml")
    table[child] = (200, "<html>pas du XML</html>", "text/html")

    report = audit(_FakeClient(table))

    assert report["sitemap"]["complete"] is False
    assert report["outcome"] == cli.OUTCOME_INCOMPLETE


# --------------------- 3. HTTP / page-state classification ------------------


@pytest.mark.parametrize("status", [404, 410])
def test_a_missing_discovery_surface_is_a_page_state_not_a_barrier(status: int) -> None:
    table = routes()
    table[SURFACE_SPECIALTY] = (status, "page introuvable", "text/html")

    report = audit(_FakeClient(table))

    surface = next(
        item for item in report["surfaces"] if item["url"] == SURFACE_SPECIALTY
    )
    assert surface["page_state"] == "PAGE_UNAVAILABLE"
    assert surface.get("barrier") is None
    assert report["barriers"] == []
    # The remaining surfaces still carried the audit to completion.
    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["exit_code"] == cli.EXIT_OK


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_server_error_on_a_surface_is_incomplete_not_a_barrier(status: int) -> None:
    table = routes()
    table[SURFACE_LISTING] = (status, "", "text/html")

    report = audit(_FakeClient(table))

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE
    assert report["barriers"] == []


def test_a_transport_failure_on_a_surface_is_incomplete() -> None:
    report = audit(_FakeClient(routes(), failing_url=SURFACE_LISTING))

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE


@pytest.mark.parametrize("status", [401, 403, 407, 429])
def test_an_explicit_refusal_on_a_surface_is_a_barrier(status: int) -> None:
    table = routes()
    table[SURFACE_LISTING] = (status, "", "text/html")

    report = audit(_FakeClient(table))

    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER


@pytest.mark.parametrize("status", [404, 410])
def test_a_declared_sitemap_that_is_gone_is_incomplete(status: int) -> None:
    """A required document we could not read, unlike an expired offer page."""
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (status, "page introuvable", "text/html")

    report = audit(_FakeClient(table))

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE
    assert any("SITEMAP_UNREADABLE" in item for item in report["incomplete_reasons"])
    assert report["barriers"] == []


@pytest.mark.parametrize("status", [404, 410])
def test_a_missing_detail_page_is_a_lifecycle_observation(status: int) -> None:
    table = routes_with_detail(detail_html())
    table[f"{OFFER}/100-stage-synthetique-100"] = (status, "page introuvable", "text/html")

    report = audit(_FakeClient(table))

    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["barriers"] == []
    gone = [
        page for page in report["detail_sample"]["pages"]
        if page.get("page_state") == "PAGE_UNAVAILABLE"
    ]
    assert len(gone) == 1


# ----------------------- 4. ROBOTS_DISALLOWED is an outcome -----------------


def test_a_robots_disallowed_surface_produces_the_robots_outcome() -> None:
    """The finding: the outcome and exit code existed but were never reached."""
    table = routes()
    table[ROBOTS_URL] = (
        200, "User-agent: *\nDisallow: /specialites/\nAllow: /\n", "text/plain",
    )
    client = _FakeClient(table)

    report = audit(client)

    assert SURFACE_SPECIALTY not in client.requested
    assert report["outcome"] == cli.OUTCOME_ROBOTS_DISALLOWED
    assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED
    assert f"surface:{SURFACE_SPECIALTY}" in report["robots_disallowed_targets"]


def test_a_robots_disallowed_required_sitemap_produces_the_robots_outcome() -> None:
    forbidden = f"{HOST}/admin/sitemap.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(forbidden), "text/plain")
    table[forbidden] = (200, sitemap_urlset([7600]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert forbidden not in client.requested
    assert report["outcome"] == cli.OUTCOME_ROBOTS_DISALLOWED
    assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED


def test_a_robots_disallowed_live_detail_page_produces_the_robots_outcome() -> None:
    table = routes()
    table[ROBOTS_URL] = (
        200,
        "User-agent: *\nDisallow: /offres-stage/100-stage-synthetique-100\nAllow: /\n",
        "text/plain",
    )
    client = _FakeClient(table)

    report = audit(client)

    assert f"{OFFER}/100-stage-synthetique-100" not in client.requested
    assert report["outcome"] == cli.OUTCOME_ROBOTS_DISALLOWED
    assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED
    assert any(
        item.startswith("detail:") for item in report["robots_disallowed_targets"]
    )


def test_a_barrier_outranks_a_robots_disallowed_target() -> None:
    """Fixed precedence: BARRIER > ROBOTS_DISALLOWED > INCOMPLETE > COMPLETED."""
    table = routes()
    table[ROBOTS_URL] = (
        200, "User-agent: *\nDisallow: /specialites/\nAllow: /\n", "text/plain",
    )
    table[SURFACE_LISTING] = (403, "", "text/html")

    report = audit(_FakeClient(table))

    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER
    # Still recorded, just outranked.
    assert report["robots_disallowed_targets"]


def test_robots_disallowed_outranks_an_incomplete_reason() -> None:
    table = routes()
    table[ROBOTS_URL] = (
        200, "User-agent: *\nDisallow: /specialites/\nAllow: /\n", "text/plain",
    )
    client = _FakeClient(table, failing_url=SURFACE_HOME)

    report = audit(client)

    assert report["outcome"] == cli.OUTCOME_ROBOTS_DISALLOWED
    assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED
    assert report["incomplete_reasons"]


def test_a_disallowed_benchmark_canary_alone_does_not_fail_the_run() -> None:
    """A canary is optional fallback evidence, never a required target."""
    table = routes()
    for surface in SURFACE_URLS:
        table[surface] = (200, JS_ONLY_HTML, "text/html")
    table[ROBOTS_URL] = (
        200, "User-agent: *\nDisallow: /offres-stage/\nAllow: /\n", "text/plain",
    )
    client = _FakeClient(table)

    report = audit(client)

    for url in BENCHMARK_CANARY_URLS:
        assert url not in client.requested
    # No live detail was selected, so nothing required was forbidden.
    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["robots_disallowed_targets"] == []
    canaries = [
        page for page in report["detail_sample"]["pages"]
        if page["discovery"] == DISCOVERY_CANARY
    ]
    assert canaries and all(page["barrier"] == "ROBOTS_DISALLOWED" for page in canaries)


def test_every_outcome_and_exit_code_pair_stays_consistent() -> None:
    pairs = {
        cli.OUTCOME_COMPLETED: cli.EXIT_OK,
        cli.OUTCOME_ROBOTS_DISALLOWED: cli.EXIT_ROBOTS_DISALLOWED,
        cli.OUTCOME_INCOMPLETE: cli.EXIT_FAILURE,
        cli.OUTCOME_BARRIER: cli.EXIT_BARRIER,
    }
    scenarios = [routes()]
    disallowed = routes()
    disallowed[ROBOTS_URL] = (
        200, "User-agent: *\nDisallow: /specialites/\nAllow: /\n", "text/plain",
    )
    scenarios.append(disallowed)
    refused = routes()
    refused[SURFACE_LISTING] = (403, "", "text/html")
    scenarios.append(refused)
    broken = routes()
    broken[SURFACE_LISTING] = (500, "", "text/html")
    scenarios.append(broken)

    for table in scenarios:
        report = audit(_FakeClient(table))
        assert pairs[report["outcome"]] == report["exit_code"], report["outcome"]


# ------------------- 5. publication dates need publication evidence ---------


def test_a_job_posting_date_posted_is_a_valid_candidate() -> None:
    candidate = publication_date_candidate(detail_html(date_posted="2026-03-15"))

    assert candidate.status == DATE_VALID_CANDIDATE
    assert candidate.normalized == "2026-03-15"


def test_an_explicitly_labelled_date_is_a_valid_candidate() -> None:
    candidate = publication_date_candidate(
        "<html><body><p>Publiée le 15/03/2026</p></body></html>"
    )

    assert candidate.status == DATE_VALID_CANDIDATE
    assert candidate.normalized == "2026-03-15"


def test_a_bare_time_element_is_not_a_publication_date() -> None:
    """The finding: the first `<time>` on the page was taken as publication.

    An offer page routinely carries several dates, and taking whichever came
    first would attach an arbitrary one to the offer while looking principled.
    """
    candidate = publication_date_candidate(
        '<html><body><time datetime="2026-03-15">15 mars</time></body></html>'
    )

    assert candidate.status == DATE_UNKNOWN
    assert candidate.normalized is None


@pytest.mark.parametrize(
    "label",
    [
        "Date limite de candidature",
        "Début du stage",
        "Dernière mise à jour",
        "Date de l'événement",
    ],
)
def test_a_time_element_for_another_kind_of_date_is_never_publication(label: str) -> None:
    candidate = publication_date_candidate(
        f'<html><body><p>{label} :</p>'
        '<time datetime="2026-04-30">30 avril</time></body></html>'
    )

    assert candidate.status == DATE_UNKNOWN


def test_a_time_element_the_page_ties_to_publication_is_accepted() -> None:
    labelled = publication_date_candidate(
        '<html><body><p>Publiée le</p>'
        '<time datetime="2026-03-15">15 mars</time></body></html>'
    )
    by_itemprop = publication_date_candidate(
        '<html><body><time itemprop="datePosted" datetime="2026-03-15">x</time></body></html>'
    )

    assert labelled.status == DATE_VALID_CANDIDATE
    assert labelled.normalized == "2026-03-15"
    assert by_itemprop.status == DATE_VALID_CANDIDATE


def test_a_deadline_time_never_wins_over_an_absent_publication_date() -> None:
    """Both dates present, only one labelled: the unlabelled one is ignored."""
    candidate = publication_date_candidate(
        '<html><body>'
        '<p>Date limite :</p><time datetime="2026-04-30">30 avril</time>'
        '<p>Publiée le</p><time datetime="2026-03-15">15 mars</time>'
        "</body></html>"
    )

    assert candidate.normalized == "2026-03-15"


@pytest.mark.parametrize("value", ["01/01/1970", "1970-01-01"])
def test_a_labelled_epoch_date_remains_a_sentinel(value: str) -> None:
    candidate = publication_date_candidate(
        f"<html><body><p>Publiée le {value}</p></body></html>"
    )

    assert candidate.status == DATE_SENTINEL_OR_INVALID
    assert candidate.as_dict()["is_published_at"] is False


def test_a_malformed_explicit_publication_date_stays_invalid() -> None:
    candidate = publication_date_candidate(
        "<html><body><p>Date de publication : 32/13/2026</p></body></html>"
    )

    assert candidate.status == DATE_SENTINEL_OR_INVALID
    assert candidate.normalized is None


def test_no_date_is_ever_fabricated_when_the_page_states_none() -> None:
    candidate = publication_date_candidate(
        detail_html(date_posted=None, state_text="Une offre sans date")
    )

    assert candidate.status == DATE_UNKNOWN
    assert candidate.raw is None
    assert candidate.normalized is None


# ====================================================================
# Architect review — remaining evidence gaps
# ====================================================================

# ---------- 1. partial sitemap evidence never reaches discovery -------------


def partially_read_sitemap_routes(*, surfaces_expose_offers: bool) -> dict:
    """Two declared roots: the first parses with offers, the second is gone.

    The chain is therefore incomplete *and* has genuinely observed offer URLs —
    the case where letting observation pass for coverage would do real damage.
    """
    first, second = f"{HOST}/sitemap-1.xml", f"{HOST}/sitemap-2.xml"
    table = routes()
    if not surfaces_expose_offers:
        for surface in SURFACE_URLS:
            table[surface] = (200, JS_ONLY_HTML, "text/html")
        for url in BENCHMARK_CANARY_URLS:
            table[url] = (200, detail_html(), "text/html")
    else:
        table[SURFACE_HOME] = (200, JS_ONLY_HTML, "text/html")
        table[SURFACE_SPECIALTY] = (200, JS_ONLY_HTML, "text/html")
    table[ROBOTS_URL] = (200, robots_declaring(first, second), "text/plain")
    table[first] = (200, sitemap_urlset([7101, 7102]), "application/xml")
    table[second] = (404, "page introuvable", "text/html")
    for number in (7101, 7102):
        table[f"{OFFER}/{number}-stage-synthetique-{number}"] = (
            200, detail_html(), "text/html",
        )
    return table


def test_partial_sitemap_urls_are_retained_but_not_trusted() -> None:
    """Observed is not trusted. The report keeps both facts, distinctly."""
    report = audit(_FakeClient(partially_read_sitemap_routes(surfaces_expose_offers=False)))
    sitemap = report["sitemap"]

    assert sitemap["complete"] is False
    assert sitemap["trusted_for_discovery"] is False
    # The evidence survives...
    assert sitemap["offer_urls_observed"] == 2
    assert len(sitemap["offer_url_pool"]) == 2
    # ...and is kept out of everything that would treat it as coverage.
    assert sitemap["offer_urls_contributed_to_discovery"] == 0
    assert sitemap["evidence_note"].startswith("OBSERVED BUT NOT TRUSTED")
    assert report["discovery"]["sitemap_offer_urls"] == 0


def test_an_incomplete_sitemap_can_never_yield_sitemap_candidate() -> None:
    report = audit(_FakeClient(partially_read_sitemap_routes(surfaces_expose_offers=False)))

    assert report["feasibility"] != FEASIBILITY_SITEMAP
    assert report["feasibility"] == FEASIBILITY_INSUFFICIENT_DISCOVERY


def test_an_incomplete_sitemap_with_one_html_surface_is_not_multi_surface() -> None:
    """One real surface is one surface, whatever a partial sitemap also saw."""
    report = audit(_FakeClient(partially_read_sitemap_routes(surfaces_expose_offers=True)))

    assert report["feasibility"] == FEASIBILITY_PUBLIC_HTML
    assert report["feasibility"] != FEASIBILITY_MULTI_SURFACE
    assert report["sitemap"]["offer_urls_observed"] == 2
    assert report["discovery"]["sitemap_offer_urls"] == 0


def test_no_detail_page_is_fetched_from_untrusted_sitemap_coverage() -> None:
    client = _FakeClient(partially_read_sitemap_routes(surfaces_expose_offers=False))

    audit(client)

    for number in (7101, 7102):
        assert f"{OFFER}/{number}-stage-synthetique-{number}" not in client.requested


def test_a_complete_sitemap_still_contributes_its_urls() -> None:
    """The isolation must not disable the healthy path."""
    table = routes()
    for surface in SURFACE_URLS:
        table[surface] = (200, JS_ONLY_HTML, "text/html")
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_urlset([7201, 7202]), "application/xml")
    for number in (7201, 7202):
        table[f"{OFFER}/{number}-stage-synthetique-{number}"] = (
            200, detail_html(), "text/html",
        )
    client = _FakeClient(table)

    report = audit(client)

    assert report["sitemap"]["trusted_for_discovery"] is True
    assert report["sitemap"]["offer_urls_contributed_to_discovery"] == 2
    assert report["discovery"]["sitemap_offer_urls"] == 2
    assert report["feasibility"] == FEASIBILITY_SITEMAP
    assert f"{OFFER}/7201-stage-synthetique-7201" in client.requested


# --------------------- 2. redirect policy outcomes --------------------------


def test_an_off_domain_surface_redirect_is_a_barrier() -> None:
    elsewhere = "https://tracker.example.com/landing"
    client = _FakeClient(routes(), redirects={SURFACE_LISTING: elsewhere})

    report = audit(client)

    assert elsewhere not in client.requested
    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER
    assert cli.OFF_DOMAIN_REDIRECT in report["barriers"]


def test_a_robots_disallowed_surface_redirect_is_a_robots_outcome() -> None:
    """A rule we chose to obey is not the site refusing us."""
    secret = f"{HOST}/admin/secret"
    client = _FakeClient(routes(), redirects={SURFACE_LISTING: secret})

    report = audit(client)

    assert secret not in client.requested
    assert report["outcome"] == cli.OUTCOME_ROBOTS_DISALLOWED
    assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED
    assert f"surface-redirect:{SURFACE_LISTING}" in report["robots_disallowed_targets"]


def test_a_surface_redirect_loop_is_incomplete_not_a_barrier() -> None:
    """Nobody turned us away; the redirects never settled."""
    a, b = f"{HOST}/loop-a", f"{HOST}/loop-b"
    client = _FakeClient(routes(), redirects={SURFACE_LISTING: a, a: b, b: a})

    report = audit(client)

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE
    assert report["barriers"] == []


def test_an_off_domain_sitemap_redirect_is_a_barrier() -> None:
    elsewhere = "https://cdn.example.com/sitemap.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    client = _FakeClient(table, redirects={SITEMAP: elsewhere})

    report = audit(client)

    assert elsewhere not in client.requested
    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER


def test_a_robots_disallowed_sitemap_redirect_is_a_robots_outcome() -> None:
    forbidden = f"{HOST}/admin/sitemap.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    client = _FakeClient(table, redirects={SITEMAP: forbidden})

    report = audit(client)

    assert forbidden not in client.requested
    assert report["outcome"] == cli.OUTCOME_ROBOTS_DISALLOWED
    assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED
    assert f"sitemap-redirect:{SITEMAP}" in report["robots_disallowed_targets"]


def test_a_sitemap_redirect_loop_is_incomplete() -> None:
    a, b = f"{HOST}/loop-a.xml", f"{HOST}/loop-b.xml"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    client = _FakeClient(table, redirects={SITEMAP: a, a: b, b: a})

    report = audit(client)

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["barriers"] == []


def test_an_off_domain_detail_redirect_is_a_barrier() -> None:
    elsewhere = "https://ats.example.com/apply/1"
    client = _FakeClient(
        routes(), redirects={f"{OFFER}/100-stage-synthetique-100": elsewhere}
    )

    report = audit(client)

    assert elsewhere not in client.requested
    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER


def test_a_robots_disallowed_live_detail_redirect_is_a_robots_outcome() -> None:
    secret = f"{HOST}/admin/offre"
    client = _FakeClient(
        routes(), redirects={f"{OFFER}/100-stage-synthetique-100": secret}
    )

    report = audit(client)

    assert secret not in client.requested
    assert report["outcome"] == cli.OUTCOME_ROBOTS_DISALLOWED
    assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED
    assert any(
        item.startswith("detail-redirect:")
        for item in report["robots_disallowed_targets"]
    )


def test_a_detail_redirect_loop_is_not_a_barrier() -> None:
    a, b = f"{HOST}/loop-a", f"{HOST}/loop-b"
    client = _FakeClient(
        routes(), redirects={f"{OFFER}/100-stage-synthetique-100": a, a: b, b: a}
    )

    report = audit(client)

    assert report["barriers"] == []
    assert report["outcome"] == cli.OUTCOME_INCOMPLETE


def test_a_canary_redirect_refusal_does_not_decide_an_otherwise_clean_run() -> None:
    """A canary is optional fallback evidence and never drives the outcome."""
    table = routes()
    for surface in SURFACE_URLS:
        table[surface] = (200, JS_ONLY_HTML, "text/html")
    secret = f"{HOST}/admin/offre"
    client = _FakeClient(table, redirects={BENCHMARK_CANARY_URLS[0]: secret})

    report = audit(client)

    assert secret not in client.requested
    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["robots_disallowed_targets"] == []


# ------------------- 3. sitemap fetch URLs preserve the query ---------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"{HOST}/sitemap.xml?part=1", f"{HOST}/sitemap.xml?part=1"),
        (f"{HOST}/sitemap.xml?part=1#frag", f"{HOST}/sitemap.xml?part=1"),
        ("HTTPS://WWW.Stage.MA/sitemap.xml?part=2", f"{HOST}/sitemap.xml?part=2"),
        (f"{HOST}/sitemap.xml/", f"{HOST}/sitemap.xml"),
        (f"{HOST}/sitemap.xml", f"{HOST}/sitemap.xml"),
    ],
)
def test_a_fetchable_url_keeps_its_query_and_drops_its_fragment(
    raw: str, expected: str
) -> None:
    assert fetch_url(raw) == expected


def test_offer_identity_still_drops_the_query() -> None:
    """The two normalizations answer different questions and must stay apart."""
    assert canonical_url(f"{OFFER}/9279-a?utm_source=x") == f"{OFFER}/9279-a"
    assert fetch_url(f"{HOST}/sitemap.xml?part=1") != f"{HOST}/sitemap.xml"


@pytest.mark.parametrize("raw", ["   ", "ftp://www.stage.ma/sitemap.xml", "not a url"])
def test_a_fetchable_url_must_be_a_real_http_url(raw: str) -> None:
    with pytest.raises(StageMaAccessError):
        fetch_url(raw)


def test_a_declared_sitemap_with_a_query_is_fetched_exactly() -> None:
    """The finding: the query was stripped, requesting a URL never declared."""
    declared = f"{HOST}/sitemap.xml?part=1"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(declared), "text/plain")
    table[declared] = (200, sitemap_urlset([7301]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert declared in client.requested
    assert f"{HOST}/sitemap.xml" not in client.requested
    assert report["sitemap"]["offer_urls_observed"] == 1


def test_two_query_variants_are_two_distinct_documents() -> None:
    first, second = f"{HOST}/sitemap.xml?part=1", f"{HOST}/sitemap.xml?part=2"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(first, second), "text/plain")
    table[first] = (200, sitemap_urlset([7401]), "application/xml")
    table[second] = (200, sitemap_urlset([7402]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert first in client.requested
    assert second in client.requested
    assert report["sitemap"]["documents_read"] == 2
    assert report["sitemap"]["offer_urls_observed"] == 2


def test_a_nested_sitemap_keeps_its_query_when_fetched() -> None:
    child = f"{HOST}/sitemap-offres.xml?page=2"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index([child]), "application/xml")
    table[child] = (200, sitemap_urlset([7501]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert child in client.requested
    assert f"{HOST}/sitemap-offres.xml" not in client.requested
    assert report["sitemap"]["offer_urls_observed"] == 1


def test_a_robots_rule_on_a_sitemap_query_is_honoured() -> None:
    """robots is matched against the URL we would really request."""
    declared = f"{HOST}/sitemap.xml?part=2"
    table = routes()
    table[ROBOTS_URL] = (
        200,
        "User-agent: *\nDisallow: /*?part=2\nAllow: /\n" f"\nSitemap: {declared}\n",
        "text/plain",
    )
    table[declared] = (200, sitemap_urlset([7601]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert declared not in client.requested
    assert report["outcome"] == cli.OUTCOME_ROBOTS_DISALLOWED
    assert f"sitemap:{declared}" in report["robots_disallowed_targets"]


def test_query_variants_of_one_sitemap_are_not_deduplicated_together() -> None:
    first, second = f"{HOST}/sitemap.xml?part=1", f"{HOST}/sitemap.xml?part=2"
    table = routes()
    table[ROBOTS_URL] = (200, robots_declaring(SITEMAP), "text/plain")
    table[SITEMAP] = (200, sitemap_index([first, second]), "application/xml")
    table[first] = (200, sitemap_urlset([7701]), "application/xml")
    table[second] = (200, sitemap_urlset([7702]), "application/xml")
    client = _FakeClient(table)

    report = audit(client)

    assert report["sitemap"]["documents_read"] == 3
    assert report["sitemap"]["offer_urls_observed"] == 2
