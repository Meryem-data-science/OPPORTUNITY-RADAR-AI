"""Offline tests for the Phase 7C.5B Stage.ma production collector.

Nothing here opens a socket. The fixtures imitate the shapes the collector must
survive — a listing that carries cards, one that says it is empty, one that
silently carries nothing, an expired offer, an anonymous employer — never real
Stage.ma content.

The distinction most of these tests defend is **skip versus fail**. Stage.ma
publishes anonymous and expired offers alongside live ones, so leaving one out
is ordinary; but a listing that changed shape, or a page with no `JobPosting`,
must fail the whole source rather than quietly returning a smaller batch that
looks like a slow day.
"""

from __future__ import annotations

import ast
import json
from itertools import count
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from services.collector.agent import RadarAgent
from services.collector.collectors.factory import (
    COLLECTOR_REGISTRY,
    UnsupportedCollectorTypeError,
    collector_for,
)
from services.collector.collectors.stage_ma import (
    SKIP_ANONYMOUS,
    SKIP_EXPIRED,
    SKIP_GONE,
    SKIP_UNPUBLISHED,
    StageMaCollectionError,
    StageMaCollector,
    StageMaPayloadError,
)
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.opportunities import PersistenceSummary
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.models.source_run import SourceRunAttempt
from services.collector.parsers.stage_ma import (
    LISTING_URL,
    PARSER_VERSION,
    ROBOTS_URL,
)
from services.collector.qualification.persistence import (
    PersistenceSummary as QualificationPersistenceSummary,
)
from services.collector.sources import (
    MAX_DETAIL_PAGE_LIMIT,
    SourceConfig,
    SourceConfigurationError,
    get_enabled_source,
    load_source_registry,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HOST = "https://www.stage.ma"
OFFER = f"{HOST}/offres-stage"

ROBOTS_BODY = "User-agent: *\nDisallow: /admin/\nAllow: /\n"


def card(number: int, *, title="Stage synthetique", organisation="Entreprise Synthetique",
         location=None) -> str:
    parts = [f'<a href="/offres-stage/{number}-slug-{number}">{title}</a>']
    if organisation is not None:
        parts.append(f'<a href="/organismes/{500 + number}-x">{organisation}</a>')
    if location is not None:
        parts.append(f'<span itemprop="addressLocality">{location}</span>')
    return f'<div class="card">{"".join(parts)}</div>'


def listing(*cards: str) -> str:
    return "<html><body><main>" + "".join(cards) + "</main></body></html>"


def detail(*, title="Stage PFE IA", organisation="Entreprise Synthetique",
           locality="Casablanca", description="Description synthetique inventee.",
           date_posted="2026-03-23", state_text="", apply_markup="") -> str:
    posting = {"@context": "https://schema.org", "@type": "JobPosting"}
    if title is not None:
        posting["title"] = title
    if organisation is not None:
        posting["hiringOrganization"] = {"@type": "Organization", "name": organisation}
    if locality is not None:
        posting["jobLocation"] = {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": locality},
        }
    if description is not None:
        posting["description"] = description
    if date_posted is not None:
        posting["datePosted"] = date_posted
    return (
        "<html><head><title>Offre</title>"
        f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        f"</head><body><h1>Offre</h1><p>{state_text}</p>{apply_markup}</body></html>"
    )


def stage_ma_source(**overrides) -> SourceConfig:
    values = {
        "id": "stage_ma",
        "type": "stage_ma_html",
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

    def __init__(self, routes, redirects=None, failing_url=None, redirect_status=302,
                 timeout_url=None):
        self.routes = routes
        self.redirects = redirects or {}
        self.failing_url = failing_url
        self.timeout_url = timeout_url
        self.redirect_status = redirect_status
        self.requested: list[str] = []

    def get(self, url, timeout=None, follow_redirects=False):
        assert follow_redirects is False, (
            "the collector must resolve redirects itself, never hand them to httpx"
        )
        self.requested.append(url)
        if url == self.timeout_url:
            raise httpx.ConnectTimeout("synthetic timeout")
        if url == self.failing_url:
            raise httpx.ConnectError("synthetic network failure")
        if url in self.redirects:
            return _FakeResponse(
                url, self.redirect_status, "", "text/html",
                {"location": self.redirects[url]},
            )
        status, body, content_type = self.routes.get(
            url, (404, "page introuvable", "text/html")
        )
        return _FakeResponse(url, status, body, content_type)


def routes(count: int = 3, **overrides) -> dict:
    cards = [card(9300 + n) for n in range(count)]
    table = {
        ROBOTS_URL: (200, ROBOTS_BODY, "text/plain"),
        LISTING_URL: (200, listing(*cards), "text/html"),
    }
    for n in range(count):
        table[f"{OFFER}/{9300 + n}-slug-{9300 + n}"] = (200, detail(), "text/html")
    table.update(overrides)
    return table


def collect(client, source=None):
    collector = StageMaCollector(source or stage_ma_source(), client=client)
    collector.DELAY_SECONDS = 0.0
    return collector, collector.collect()


# ================================ SOURCE CONFIG =============================


def test_the_stage_ma_source_type_is_accepted() -> None:
    assert stage_ma_source().type == "stage_ma_html"
    assert stage_ma_source().detail_page_limit == 25


@pytest.mark.parametrize("limit", [1, 25, 50])
def test_limits_inside_the_bound_are_accepted(limit: int) -> None:
    assert stage_ma_source(detail_page_limit=limit).detail_page_limit == limit


@pytest.mark.parametrize("limit", [0, -1, 51, 1000, True, False, 2.5, "25", None])
def test_a_limit_outside_the_bound_is_refused(limit) -> None:
    """`True` is an `int` in Python; a flag becoming "fetch 1 page" is nonsense."""
    with pytest.raises(SourceConfigurationError, match="detail_page_limit"):
        stage_ma_source(detail_page_limit=limit)


def test_a_stage_ma_source_without_a_limit_is_refused() -> None:
    with pytest.raises(SourceConfigurationError, match="detail_page_limit"):
        SourceConfig.from_mapping(
            {"id": "stage_ma", "type": "stage_ma_html", "enabled": True}
        )


def test_the_hard_ceiling_is_shared_and_still_fifty() -> None:
    assert MAX_DETAIL_PAGE_LIMIT == 50


def test_the_committed_production_row_matches_the_approved_values() -> None:
    configured = {item.id: item for item in load_source_registry()}["stage_ma"]

    assert configured.type == "stage_ma_html"
    assert configured.enabled is True
    assert configured.category == "jobs"
    assert configured.country == "MA"
    assert configured.frequency_minutes == 360
    assert configured.status == "active"
    assert configured.detail_page_limit == 25


# ================================= ACTIVATION ===============================
#
# Stage.ma is enabled AND active, on the Architect's decision, after the real
# Phase 7C.5B validation run read ten live offers off the approved specialty
# listing and found every one of them expired. Zero admissible candidates is
# what the site had that day; it is not a defect in the collector, which read
# the site correctly and reported honestly, and holding the source back would
# only guarantee that a newly published offer is never seen.
#
# Nothing below is a Stage.ma special case in the runtime. `enabled` and
# `status` are the generic fields every source already has, and the agent's
# existing `enabled and status == "active"` filter does all of the work.


def test_stage_ma_is_enabled_and_active() -> None:
    """The whole activation mechanism, in two fields."""
    configured = {item.id: item for item in load_source_registry()}["stage_ma"]

    assert configured.enabled is True
    assert configured.status == "active"


def test_a_manual_dry_run_can_still_resolve_stage_ma() -> None:
    """`collect_source --source stage_ma` keeps working; it turns on `enabled`
    alone, which activation did not touch."""
    resolved = get_enabled_source("stage_ma", Path("config/sources.yaml"))

    assert resolved.id == "stage_ma"
    assert resolved.type == "stage_ma_html"
    assert resolved.detail_page_limit == 25


def test_the_agent_now_includes_stage_ma_in_an_ordinary_run() -> None:
    """No agent branch names Stage.ma; the generic status filter admits it."""
    eligible = [
        item.id
        for item in load_source_registry()
        if item.enabled and item.status == "active"
    ]

    assert "stage_ma" in eligible
    assert "stagiaires_ma" in eligible


def test_the_agent_actually_runs_the_committed_stage_ma_row() -> None:
    """The end-to-end shape of activation, against the real committed config.

    Naming Stage.ma used to raise "source is disabled or inactive"; now the run
    reaches the collector. The collector here is a stub returning nothing —
    persistence and the network stay out of a unit test — but the source that
    reaches it is the row this repository really ships, which is the part that
    activation changed.
    """
    counter = count(1)
    collector = Mock()
    collector.collect.return_value = []
    factory = Mock(return_value=collector)
    persister = Mock(return_value=PersistenceSummary(0, 0))

    summary = RadarAgent(
        source_loader=load_source_registry,
        collector_factory=factory,
        persister=persister,
        qualification_persister=Mock(
            return_value=QualificationPersistenceSummary(0, 0, 0, 0)
        ),
        settings_loader=lambda: Settings(
            ApplicationEnvironment.TEST, DatabaseBackend.SQLITE
        ),
        run_starter=lambda unused_settings, config: SourceRunAttempt(
            next(counter), config.id, "2026-01-01T00:00:00.000000+00:00"
        ),
        run_finalizer=Mock(return_value=None),
        source_ids=["stage_ma"],
    ).run_once()

    assert factory.call_args.args[0].id == "stage_ma"
    assert factory.call_args.args[0].type == "stage_ma_html"
    collector.collect.assert_called_once_with()
    assert (summary.sources_total, summary.sources_succeeded) == (1, 1)


def test_activating_stage_ma_did_not_disturb_the_other_sources() -> None:
    """Five sources, all enabled and active; none of the other four moved."""
    statuses = {item.id: (item.enabled, item.status) for item in load_source_registry()}

    assert statuses["stage_ma"] == (True, "active")
    for other in (
        "scale_ai_greenhouse",
        "artefact_greenhouse",
        "linkedin_job_alert_email",
        "stagiaires_ma",
    ):
        assert statuses[other] == (True, "active")


def test_the_collector_is_registered_for_the_activated_source() -> None:
    """Activation is an operational decision about existing code: the row, the
    type, the factory entry and the collector are the same ones."""
    configured = {item.id: item for item in load_source_registry()}["stage_ma"]

    assert isinstance(collector_for(configured), StageMaCollector)


def test_the_other_source_types_are_unaffected() -> None:
    greenhouse = SourceConfig.from_mapping(
        {"id": "g", "type": "greenhouse", "enabled": True,
         "organization": "O", "board_token": "t"}
    )
    gmail = SourceConfig.from_mapping(
        {"id": "l", "type": "gmail_linkedin_alert", "enabled": True,
         "gmail_query": "newer_than:7d", "gmail_message_limit": 50}
    )
    stagiaires = SourceConfig.from_mapping(
        {"id": "s", "type": "stagiaires_sitemap", "enabled": True,
         "detail_page_limit": 25}
    )

    assert greenhouse.detail_page_limit is None
    assert gmail.detail_page_limit is None
    assert stagiaires.detail_page_limit == 25


def test_a_stagiaires_source_still_requires_its_own_bound() -> None:
    with pytest.raises(SourceConfigurationError, match="detail_page_limit"):
        SourceConfig.from_mapping(
            {"id": "s", "type": "stagiaires_sitemap", "enabled": True}
        )


# ================================== FACTORY =================================


def test_the_factory_builds_the_stage_ma_collector() -> None:
    assert isinstance(collector_for(stage_ma_source()), StageMaCollector)


def test_the_registry_gained_stage_ma_without_displacing_anything() -> None:
    assert set(COLLECTOR_REGISTRY) == {
        "greenhouse",
        "gmail_linkedin_alert",
        "stagiaires_sitemap",
        "stage_ma_html",
    }
    assert COLLECTOR_REGISTRY["stage_ma_html"] is StageMaCollector


def test_an_unregistered_type_is_still_refused() -> None:
    with pytest.raises(UnsupportedCollectorTypeError):
        collector_for(SourceConfig(id="x", type="lever", enabled=True))


def test_the_collector_refuses_a_source_of_another_type() -> None:
    with pytest.raises(ValueError, match="stage_ma_html"):
        StageMaCollector(
            SourceConfig.from_mapping(
                {"id": "s", "type": "stagiaires_sitemap", "enabled": True,
                 "detail_page_limit": 5}
            )
        )


# ================================== ROBOTS ==================================


def test_a_permissive_robots_lets_collection_proceed() -> None:
    client = _FakeClient(routes())
    _, candidates = collect(client)

    assert client.requested[0] == ROBOTS_URL
    assert len(candidates) == 3


@pytest.mark.parametrize("status", [401, 403, 407, 429])
def test_a_refused_robots_fails_the_source(status: int) -> None:
    """An unknown rule is never read as a permissive one."""
    table = routes()
    table[ROBOTS_URL] = (status, "", "text/html")
    client = _FakeClient(table)

    with pytest.raises(StageMaCollectionError, match="could not be resolved"):
        collect(client)
    assert client.requested == [ROBOTS_URL]


@pytest.mark.parametrize("status", [500, 502, 503, 418])
def test_an_unresolved_robots_fails_the_source(status: int) -> None:
    table = routes()
    table[ROBOTS_URL] = (status, "", "text/html")

    with pytest.raises(StageMaCollectionError):
        collect(_FakeClient(table))


@pytest.mark.parametrize("status", [404, 410])
def test_an_absent_robots_lets_collection_continue(status: int) -> None:
    """404 means no explicit rule applies. It is not permission of any kind."""
    table = routes()
    table[ROBOTS_URL] = (status, "", "text/plain")

    _, candidates = collect(_FakeClient(table))

    assert len(candidates) == 3


def test_a_network_failure_on_robots_fails_the_source() -> None:
    with pytest.raises(StageMaCollectionError, match="network request failed"):
        collect(_FakeClient(routes(), failing_url=ROBOTS_URL))


def test_a_timeout_on_robots_fails_the_source() -> None:
    with pytest.raises(StageMaCollectionError, match="timed out"):
        collect(_FakeClient(routes(), timeout_url=ROBOTS_URL))


def test_a_disallowed_listing_is_never_requested() -> None:
    table = routes()
    table[ROBOTS_URL] = (200, "User-agent: *\nDisallow: /specialites/\nAllow: /\n", "text/plain")
    client = _FakeClient(table)

    with pytest.raises(StageMaCollectionError, match="robots.txt disallows"):
        collect(client)
    assert LISTING_URL not in client.requested


def test_a_query_rule_is_enforced_because_robots_matches_path_and_query() -> None:
    from services.collector.parsers.stage_ma import parse_robots_txt, robots_allows

    groups = parse_robots_txt("User-agent: *\nDisallow: /*?print=1\n")

    assert robots_allows(groups, f"{LISTING_URL}?print=1") is False
    assert robots_allows(groups, LISTING_URL) is True


# ================================= REDIRECTS ================================


def test_a_same_host_redirect_is_followed() -> None:
    moved = f"{HOST}/specialites/informatique"
    table = routes()
    table[moved] = (200, listing(card(9400)), "text/html")
    table[f"{OFFER}/9400-slug-9400"] = (200, detail(), "text/html")
    client = _FakeClient(table, redirects={LISTING_URL: moved})

    _, candidates = collect(client)

    assert moved in client.requested
    assert len(candidates) == 1


def test_an_off_domain_redirect_target_is_never_requested() -> None:
    elsewhere = "https://tracker.example.com/landing"
    client = _FakeClient(routes(), redirects={LISTING_URL: elsewhere})

    with pytest.raises(StageMaCollectionError, match="off-domain redirect"):
        collect(client)
    assert not any("tracker.example.com" in item for item in client.requested)


def test_a_robots_disallowed_redirect_target_is_never_requested() -> None:
    """Obeying robots on the URL we asked for but not the one we got is not
    obeying robots."""
    secret = f"{HOST}/admin/secret"
    table = routes()
    table[secret] = (200, listing(card(9400)), "text/html")
    client = _FakeClient(table, redirects={LISTING_URL: secret})

    with pytest.raises(StageMaCollectionError, match="robots.txt disallows"):
        collect(client)
    assert secret not in client.requested


def test_a_redirect_loop_is_bounded() -> None:
    a, b = f"{HOST}/loop-a", f"{HOST}/loop-b"
    client = _FakeClient(routes(), redirects={LISTING_URL: a, a: b, b: a})

    with pytest.raises(StageMaCollectionError, match="too many redirects"):
        collect(client)
    assert client.requested.count(a) <= StageMaCollector.MAX_REDIRECTS + 1


def test_a_redirect_without_a_location_fails() -> None:
    table = routes()
    table[LISTING_URL] = (302, "", "text/html")

    with pytest.raises(StageMaCollectionError, match="redirect without a location"):
        collect(_FakeClient(table))


def test_no_request_in_a_whole_run_leaves_the_stage_ma_hosts() -> None:
    client = _FakeClient(routes())
    collect(client)

    for url in client.requested:
        assert url.startswith(("https://www.stage.ma", "https://stage.ma")), url


# ================================= DISCOVERY ================================


def test_only_the_approved_specialty_surface_is_used_for_discovery() -> None:
    """The homepage and the generic listing exposed nothing in the 7C.5A audit."""
    client = _FakeClient(routes())

    collect(client)

    assert LISTING_URL in client.requested
    assert f"{HOST}/" not in client.requested
    assert f"{HOST}/offres-stage" not in client.requested
    assert not any("sitemap" in item for item in client.requested)
    assert not any(
        item.startswith(f"{HOST}/specialites/") and item != LISTING_URL
        for item in client.requested
    )


def test_the_listing_is_requested_exactly_once() -> None:
    client = _FakeClient(routes())

    collect(client)

    assert client.requested.count(LISTING_URL) == 1


def test_other_specialties_and_show_all_links_are_never_followed() -> None:
    table = routes()
    table[LISTING_URL] = (
        200,
        listing(card(9300))
        + '<a href="/specialites/marketing">Marketing</a>'
        + '<a href="/offres-stage">Afficher tout</a>',
        "text/html",
    )
    client = _FakeClient(table)

    collect(client)

    assert f"{HOST}/specialites/marketing" not in client.requested
    assert f"{HOST}/offres-stage" not in client.requested


# ================================== LISTING =================================


def test_a_normal_listing_yields_bounded_detail_targets() -> None:
    client = _FakeClient(routes(count=10))

    _, candidates = collect(client, source=stage_ma_source(detail_page_limit=4))

    details = [item for item in client.requested if "/offres-stage/" in item]
    assert len(details) == 4
    assert len(candidates) == 4


def test_an_explicit_empty_listing_is_a_valid_zero_result() -> None:
    """The site said it has nothing. An empty batch is the honest answer."""
    table = routes()
    table[LISTING_URL] = (
        200, "<html><body><p>Aucune Offre de Stage</p></body></html>", "text/html",
    )
    client = _FakeClient(table)

    _, candidates = collect(client)

    assert candidates == []
    assert not any("/offres-stage/" in item for item in client.requested)


def test_an_ambiguous_empty_listing_fails_the_source() -> None:
    """Zero links with no empty state is what a redesign looks like."""
    table = routes()
    table[LISTING_URL] = (
        200, "<html><body><main><h1>Informatique</h1></main></body></html>", "text/html",
    )

    with pytest.raises(StageMaPayloadError, match="no explicit empty state"):
        collect(_FakeClient(table))


@pytest.mark.parametrize("status", [404, 410])
def test_a_missing_listing_fails_the_source(status: int) -> None:
    """The one surface this collector depends on; its loss is structural."""
    table = routes()
    table[LISTING_URL] = (status, "page introuvable", "text/html")

    with pytest.raises(StageMaCollectionError, match="listing is unavailable"):
        collect(_FakeClient(table))


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_a_blocked_or_broken_listing_fails_the_source(status: int) -> None:
    table = routes()
    table[LISTING_URL] = (status, "", "text/html")

    with pytest.raises(StageMaCollectionError):
        collect(_FakeClient(table))


def test_a_network_failure_on_the_listing_fails_the_source() -> None:
    with pytest.raises(StageMaCollectionError):
        collect(_FakeClient(routes(), failing_url=LISTING_URL))


# =================================== DETAIL =================================


def test_a_valid_detail_page_becomes_a_candidate() -> None:
    _, candidates = collect(_FakeClient(routes(count=1)))

    candidate = candidates[0]
    assert candidate.source_id == "stage_ma"
    assert candidate.source_external_id == "9300"
    assert candidate.canonical_title == "Stage PFE IA"
    assert candidate.organization == "Entreprise Synthetique"
    assert candidate.location == "Casablanca"
    assert candidate.published_at == "2026-03-23"
    assert candidate.source_url == f"{OFFER}/9300-slug-9300"
    assert candidate.canonical_url == f"{OFFER}/9300-slug-9300"


@pytest.mark.parametrize("status", [404, 410])
def test_a_detail_gone_after_discovery_is_an_individual_skip(status: int) -> None:
    """Ordinary churn on a job board: only this offer is lost."""
    table = routes(count=3)
    table[f"{OFFER}/9301-slug-9301"] = (status, "page introuvable", "text/html")
    collector, candidates = collect(_FakeClient(table))

    assert len(candidates) == 2
    assert collector.skipped[f"{OFFER}/9301-slug-9301"] == SKIP_GONE


@pytest.mark.parametrize("status", [403, 429])
def test_a_blocked_detail_page_fails_the_source(status: int) -> None:
    table = routes(count=3)
    table[f"{OFFER}/9301-slug-9301"] = (status, "", "text/html")

    with pytest.raises(StageMaCollectionError):
        collect(_FakeClient(table))


@pytest.mark.parametrize("status", [500, 503])
def test_a_broken_detail_page_fails_the_source(status: int) -> None:
    table = routes(count=3)
    table[f"{OFFER}/9301-slug-9301"] = (status, "", "text/html")

    with pytest.raises(StageMaCollectionError):
        collect(_FakeClient(table))


def test_a_detail_network_failure_or_timeout_fails_the_source() -> None:
    with pytest.raises(StageMaCollectionError):
        collect(_FakeClient(routes(count=3), failing_url=f"{OFFER}/9301-slug-9301"))
    with pytest.raises(StageMaCollectionError):
        collect(_FakeClient(routes(count=3), timeout_url=f"{OFFER}/9301-slug-9301"))


def test_a_detail_page_without_a_job_posting_fails_the_source() -> None:
    """A structural contract failure is never a quietly smaller batch."""
    table = routes(count=3)
    table[f"{OFFER}/9301-slug-9301"] = (
        200, "<html><body><h1>Offre</h1></body></html>", "text/html",
    )

    with pytest.raises(StageMaPayloadError, match="publishes no JobPosting"):
        collect(_FakeClient(table))


# ================================= LIFECYCLE ================================


def test_an_expired_offer_is_skipped_individually() -> None:
    table = routes(count=3)
    table[f"{OFFER}/9301-slug-9301"] = (200, detail(state_text="Offre expirée"), "text/html")
    collector, candidates = collect(_FakeClient(table))

    assert len(candidates) == 2
    assert collector.skipped[f"{OFFER}/9301-slug-9301"] == SKIP_EXPIRED


def test_an_unpublished_offer_is_skipped_individually() -> None:
    table = routes(count=3)
    table[f"{OFFER}/9301-slug-9301"] = (200, detail(state_text="Offre non publiée"), "text/html")
    collector, candidates = collect(_FakeClient(table))

    assert len(candidates) == 2
    assert collector.skipped[f"{OFFER}/9301-slug-9301"] == SKIP_UNPUBLISHED


def test_a_published_offer_is_admitted() -> None:
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (
        200, detail(state_text="Publiée le 23/03/2026"), "text/html",
    )
    _, candidates = collect(_FakeClient(table))

    assert len(candidates) == 1


def test_an_unknown_state_is_admitted_because_unknown_is_not_expired() -> None:
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(state_text=""), "text/html")
    _, candidates = collect(_FakeClient(table))

    assert len(candidates) == 1


def test_a_whole_batch_of_expired_offers_is_zero_candidates_not_a_failure() -> None:
    """A technically correct run may legitimately find nothing admissible."""
    table = routes(count=3)
    for n in range(3):
        table[f"{OFFER}/{9300 + n}-slug-{9300 + n}"] = (
            200, detail(state_text="Offre expirée"), "text/html",
        )
    collector, candidates = collect(_FakeClient(table))

    assert candidates == []
    assert set(collector.skipped.values()) == {SKIP_EXPIRED}


# ===================== REAL PAGE SHAPES (review fix) ========================


#: The sentence the live Stage.ma detail page really renders on a hidden offer.
REAL_UNPUBLISHED_NOTICE = (
    "Cette offre de stage n\u2019est pas publi\u00e9e. "
    "Seul le recruteur et l\u2019administrateur peuvent la visualiser."
)


def unpublished_detail() -> str:
    """A hidden offer exactly as the site publishes one: the notice, and lower
    down "Publiee le 01/01/1970" - a never-set column printed as a publication
    label. Everything else on the page is valid, so only the notice can keep
    the offer out, and only reading the label instead could let it in.
    """
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Stagiaire Full Stack Developer",
        "hiringOrganization": {"@type": "Organization", "name": "ACME SARL"},
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": "Casablanca"},
        },
        "description": "Description synthetique inventee.",
        "datePosted": "01/01/1970",
    }
    return (
        "<html><head><title>Offre</title>"
        f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        "</head><body>"
        "<h1>Stagiaire Full Stack Developer</h1>"
        f'<div class="alert">{REAL_UNPUBLISHED_NOTICE}</div>'
        "<p>Publi\u00e9e le 01/01/1970</p>"
        "</body></html>"
    )


def real_card(number: int, *, title: str, organisation: str | None,
              organisation_id: int = 500) -> str:
    """A card shaped like the live listing: the same offer linked twice, from
    the title and from the "+ Voir Offre de Stage" call to action."""
    inner = [f'<h3><a href="/offres-stage/{number}-slug-{number}">{title}</a></h3>']
    if organisation is not None:
        inner.append(
            f'<a class="org" href="/organismes/{organisation_id}-slug">{organisation}</a>'
        )
    inner.append(
        f'<a class="cta" href="/offres-stage/{number}-slug-{number}">+ Voir Offre de Stage</a>'
    )
    return f'<div class="offer-item"><div class="offer-body">{"".join(inner)}</div></div>'


def test_the_real_unpublished_page_is_skipped_and_yields_no_candidate() -> None:
    """The whole reason the notice matters: without it this page is complete,
    admissible, and would be stored as a live offer dated 1970."""
    table = routes(count=3)
    table[f"{OFFER}/9301-slug-9301"] = (200, unpublished_detail(), "text/html")
    collector, candidates = collect(_FakeClient(table))

    assert collector.skipped[f"{OFFER}/9301-slug-9301"] == SKIP_UNPUBLISHED
    assert len(candidates) == 2
    assert all(
        candidate.source_url != f"{OFFER}/9301-slug-9301" for candidate in candidates
    )


def test_a_run_of_only_hidden_offers_produces_no_opportunity_candidate() -> None:
    """Nothing partial reaches persistence: no candidate at all, not a candidate
    with a 1970 date."""
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (200, unpublished_detail(), "text/html")
    collector, candidates = collect(_FakeClient(table))

    assert candidates == []
    assert not any(isinstance(item, OpportunityCandidate) for item in candidates)
    assert collector.skipped == {f"{OFFER}/9300-slug-9300": SKIP_UNPUBLISHED}


def test_the_real_listing_shape_collects_both_offers_with_their_own_employers() -> None:
    """Against the live card shape every organization used to come back empty,
    because the doubled offer link made one card look like two offers."""
    table = routes(count=2)
    table[LISTING_URL] = (
        200,
        listing(
            real_card(9300, title="Stage A", organisation="ACME", organisation_id=500),
            real_card(9301, title="Stage B", organisation="BETA", organisation_id=600),
        ),
        "text/html",
    )
    for number in (9300, 9301):
        table[f"{OFFER}/{number}-slug-{number}"] = (
            200, detail(organisation=None, state_text="Publi\u00e9e le 23/03/2026"), "text/html",
        )

    collector, candidates = collect(_FakeClient(table))

    assert [candidate.organization for candidate in candidates] == ["ACME", "BETA"]
    assert [candidate.canonical_title for candidate in candidates] == [
        "Stage PFE IA",
        "Stage PFE IA",
    ]
    assert collector.run_metrics()["pages_checked"] == 4  # robots, listing, two details


def test_the_doubled_offer_link_does_not_double_the_fetches() -> None:
    """Four offer anchors, two offers: the collector must GET two detail pages."""
    table = routes(count=2)
    table[LISTING_URL] = (
        200,
        listing(
            real_card(9300, title="Stage A", organisation="ACME"),
            real_card(9301, title="Stage B", organisation="BETA", organisation_id=600),
        ),
        "text/html",
    )
    client = _FakeClient(table)
    _, candidates = collect(client)

    detail_requests = [url for url in client.requested if url.startswith(f"{OFFER}/")]
    assert detail_requests == [f"{OFFER}/9300-slug-9300", f"{OFFER}/9301-slug-9301"]
    assert len(candidates) == 2


# ================================ ORGANIZATION ==============================


def test_the_posting_organization_is_preferred() -> None:
    table = routes(count=1)
    table[LISTING_URL] = (200, listing(card(9300, organisation="Listing SARL")), "text/html")
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(organisation="Detail SARL"), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].organization == "Detail SARL"


def test_an_anonymous_detail_falls_back_to_an_explicit_listing_employer() -> None:
    """Only 1 of 3 sampled pages carried an organization; the card is real evidence."""
    table = routes(count=1)
    table[LISTING_URL] = (200, listing(card(9300, organisation="Listing SARL")), "text/html")
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(organisation="Anonyme"), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].organization == "Listing SARL"


def test_anonymous_in_both_places_is_an_individual_skip() -> None:
    """The source publishes anonymous offers on purpose; that is not a fault."""
    table = routes(count=3)
    table[LISTING_URL] = (
        200,
        listing(card(9300, organisation="Anonyme"), card(9301), card(9302)),
        "text/html",
    )
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(organisation="Anonyme"), "text/html")
    collector, candidates = collect(_FakeClient(table))

    assert len(candidates) == 2
    assert collector.skipped[f"{OFFER}/9300-slug-9300"] == SKIP_ANONYMOUS


def test_a_missing_organization_in_both_places_is_an_individual_skip() -> None:
    table = routes(count=3)
    table[LISTING_URL] = (
        200,
        listing(card(9300, organisation=None), card(9301), card(9302)),
        "text/html",
    )
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(organisation=None), "text/html")
    collector, candidates = collect(_FakeClient(table))

    assert len(candidates) == 2
    assert collector.skipped[f"{OFFER}/9300-slug-9300"] == SKIP_ANONYMOUS


@pytest.mark.parametrize(
    "placeholder", ["Stage.ma", "Unknown", "Anonymous company", "N/A", "Employer", "Company"]
)
def test_no_placeholder_organization_is_ever_invented(placeholder: str) -> None:
    table = routes(count=1)
    table[LISTING_URL] = (200, listing(card(9300, organisation=None)), "text/html")
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(organisation=None), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert candidates == []
    assert placeholder not in str(candidates)


# =================================== TITLE ==================================


def test_the_posting_title_is_preferred() -> None:
    table = routes(count=1)
    table[LISTING_URL] = (200, listing(card(9300, title="Titre du listing")), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].canonical_title == "Stage PFE IA"


def test_a_missing_posting_title_falls_back_to_the_listing_title() -> None:
    table = routes(count=1)
    table[LISTING_URL] = (200, listing(card(9300, title="Titre du listing")), "text/html")
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(title=None), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].canonical_title == "Titre du listing"


def test_a_title_missing_in_both_places_fails_the_source() -> None:
    """A structurally valid offer with no title means the contract changed."""
    table = routes(count=1)
    table[LISTING_URL] = (
        200,
        '<html><body><div><a href="/offres-stage/9300-slug-9300"></a>'
        '<a href="/organismes/1-x">ACME</a></div></body></html>',
        "text/html",
    )
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(title=None), "text/html")

    with pytest.raises(StageMaPayloadError, match="no usable title"):
        collect(_FakeClient(table))


# ================================== LOCATION ================================


def test_the_posting_location_is_preferred() -> None:
    table = routes(count=1)
    table[LISTING_URL] = (200, listing(card(9300, location="Rabat")), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].location == "Casablanca"


def test_a_missing_posting_location_falls_back_to_the_listing_card() -> None:
    table = routes(count=1)
    table[LISTING_URL] = (200, listing(card(9300, location="Rabat")), "text/html")
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(locality=None), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].location == "Rabat"


def test_a_location_missing_in_both_places_stays_none() -> None:
    """UNKNOWN stays UNKNOWN; nothing is inferred from country: MA."""
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(locality=None), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].location is None
    assert stage_ma_source().country == "MA"


def test_a_missing_description_or_date_stays_none() -> None:
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (
        200, detail(description=None, date_posted=None), "text/html",
    )

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].description is None
    assert candidates[0].published_at is None


def test_an_epoch_date_never_reaches_the_candidate() -> None:
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(date_posted="01/01/1970"), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].published_at is None


# ================================ APPLICATION ===============================


def test_an_explicit_application_link_is_recorded() -> None:
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (
        200, detail(apply_markup='<a href="/postuler/9300">Postuler</a>'), "text/html",
    )

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].application_url == f"{HOST}/postuler/9300"


def test_an_off_domain_application_link_is_recorded_but_never_fetched() -> None:
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (
        200,
        detail(apply_markup='<a href="https://ats.example.com/apply/1">Postuler</a>'),
        "text/html",
    )
    client = _FakeClient(table)

    _, candidates = collect(client)

    assert candidates[0].application_url == "https://ats.example.com/apply/1"
    assert not any("ats.example.com" in item for item in client.requested)


def test_a_javascript_only_application_control_yields_none() -> None:
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (
        200, detail(apply_markup='<a href="javascript:void(0)">Postuler</a>'), "text/html",
    )

    _, candidates = collect(_FakeClient(table))

    assert candidates[0].application_url is None


def test_no_application_control_yields_none() -> None:
    _, candidates = collect(_FakeClient(routes(count=1)))

    assert candidates[0].application_url is None


# ==================================== BOUND =================================


def test_the_configured_bound_limits_real_detail_requests() -> None:
    client = _FakeClient(routes(count=40))

    collect(client, source=stage_ma_source(detail_page_limit=3))

    details = [item for item in client.requested if "/offres-stage/" in item]
    assert len(details) == 3


def test_the_bound_takes_the_listings_own_order_not_the_highest_id() -> None:
    table = routes()
    table[LISTING_URL] = (200, listing(card(1000), card(9999)), "text/html")
    table[f"{OFFER}/1000-slug-1000"] = (200, detail(), "text/html")
    table[f"{OFFER}/9999-slug-9999"] = (200, detail(), "text/html")
    client = _FakeClient(table)

    collect(client, source=stage_ma_source(detail_page_limit=1))

    assert f"{OFFER}/1000-slug-1000" in client.requested
    assert f"{OFFER}/9999-slug-9999" not in client.requested


# =================================== METRICS ================================


def test_run_metrics_report_only_what_the_run_measured() -> None:
    collector, _ = collect(_FakeClient(routes(count=3)))
    metrics = collector.run_metrics()

    # robots + listing + three details.
    assert metrics["pages_checked"] == 5
    assert metrics["parser_version"] == PARSER_VERSION
    for absent in ("items_found", "new_items", "relevant_items", "http_status", "skipped"):
        assert absent not in metrics


# ================================== NO FILTER ===============================


@pytest.mark.parametrize(
    "title",
    [
        "Stage comptabilité générale",
        "Stagiaire assistant administratif",
        "Stage community management",
    ],
)
def test_offers_unrelated_to_data_ai_or_pfe_are_still_collected(title: str) -> None:
    """The collector observes; qualification classifies; ranking prioritizes."""
    table = routes(count=1)
    table[f"{OFFER}/9300-slug-9300"] = (200, detail(title=title), "text/html")

    _, candidates = collect(_FakeClient(table))

    assert len(candidates) == 1
    assert candidates[0].canonical_title == title


def test_the_collector_source_contains_no_relevance_filter() -> None:
    """Checked against executable code, not prose.

    The module docstring says out loud that there is no Data & AI or PFE keyword
    filter here; a test that forbade the words would forbid saying so.
    """
    tree = ast.parse(
        (REPOSITORY_ROOT / "services" / "collector" / "collectors" / "stage_ma.py")
        .read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant
            ) and isinstance(body[0].value.value, str):
                body[0].value.value = ""
    code = ast.unparse(tree).lower()

    for forbidden in ("data_ai", "is_pfe", "keyword", "relevan"):
        assert forbidden not in code, forbidden


# ============================ PRODUCTION ISOLATION ==========================


def test_the_collector_has_no_browser_or_credential_dependency() -> None:
    """Checked against imports and code, never prose: the docstrings say "no
    Selenium" out loud, and a test forbidding the word would forbid saying so."""
    for module in (
        "services/collector/collectors/stage_ma.py",
        "services/collector/parsers/stage_ma.py",
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
            for forbidden in ("selenium", "playwright", "webdriver", "puppeteer"):
                assert forbidden not in lowered, f"{module}: imports {name}"
            assert not lowered.startswith("evaluation"), f"{module}: imports {name}"

        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant
                ) and isinstance(body[0].value.value, str):
                    body[0].value.value = ""
        code = ast.unparse(tree).lower()
        for forbidden in ("cookies", "proxies", "auth=", "authorization", "\"post\"", "'post'"):
            assert forbidden not in code, f"{module}: {forbidden}"


def test_production_never_imports_the_evaluation_audit_module() -> None:
    """`evaluation/` is an audit trail, not a runtime dependency.

    Asserted from the import graph rather than from the text: the parser's
    docstring names the audit module precisely to explain that it re-states its
    semantics instead of importing them, and that sentence is worth keeping.
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


def test_this_phase_added_no_database_migration() -> None:
    for path in (REPOSITORY_ROOT / "migrations").glob("*.sql"):
        assert "stage_ma" not in path.name.lower()
        assert "stage.ma" not in path.read_text(encoding="utf-8").lower()


def test_the_radar_agent_has_no_stage_ma_branch() -> None:
    agent = (REPOSITORY_ROOT / "services" / "collector" / "agent.py").read_text(
        encoding="utf-8"
    )

    assert "stage_ma" not in agent.lower()
    assert "collector_for" in agent


def test_the_shared_collector_contract_is_unchanged() -> None:
    """No collector was altered to give Stage.ma its per-item skips."""
    base = (
        REPOSITORY_ROOT / "services" / "collector" / "collectors" / "base.py"
    ).read_text(encoding="utf-8")

    assert "stage" not in base.lower()
    assert "-> OpportunityCandidate" in base
    assert "OpportunityCandidate | None" not in base


def test_persistence_receives_standard_opportunity_candidates() -> None:
    _, candidates = collect(_FakeClient(routes(count=3)))

    assert candidates
    for candidate in candidates:
        assert isinstance(candidate, OpportunityCandidate)


def test_the_same_offer_yields_a_stable_identity_across_two_runs() -> None:
    """Opportunities are keyed on `(source_id, source_url)`; drift would
    re-create the whole batch as new rows on the second run."""
    first = collect(_FakeClient(routes(count=3)))[1]
    second = collect(_FakeClient(routes(count=3)))[1]

    identity = lambda batch: sorted((c.source_id, c.source_url) for c in batch)
    assert identity(first) == identity(second)
    assert len(set(identity(first))) == len(first)


def test_the_identity_is_the_listing_url_not_a_redirect_destination() -> None:
    moved = f"{OFFER}/9300-slug-9300-v2"
    table = routes(count=1)
    table[moved] = (200, detail(), "text/html")
    client = _FakeClient(table, redirects={f"{OFFER}/9300-slug-9300": moved})

    _, candidates = collect(client)

    assert moved in client.requested
    assert candidates[0].source_url == f"{OFFER}/9300-slug-9300"


def test_the_radar_agent_runs_stage_ma_through_the_normal_factory() -> None:
    from services.collector.agent import RadarAgent

    source = stage_ma_source()
    client = _FakeClient(routes(count=3))

    def factory(configured: SourceConfig):
        collector = StageMaCollector(configured, client=client)
        collector.DELAY_SECONDS = 0.0
        return collector

    persisted: list = []

    class _Persisted:
        created = 3
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
    assert summary.items_collected == 3
    assert len(persisted) == 3


def test_a_source_failure_reaches_the_agent_as_a_failed_run() -> None:
    """A structural failure must never look like a quiet, empty success."""
    from services.collector.agent import RadarAgent

    table = routes()
    table[LISTING_URL] = (403, "", "text/html")
    client = _FakeClient(table)

    def factory(configured: SourceConfig):
        collector = StageMaCollector(configured, client=client)
        collector.DELAY_SECONDS = 0.0
        return collector

    class _Qualified:
        created = 0
        updated = 0
        unchanged = 0
        total = 0

    agent = RadarAgent(
        source_loader=lambda *args, **kwargs: [stage_ma_source()],
        collector_factory=factory,
        settings_loader=lambda: object(),
        persister=lambda *args, **kwargs: pytest.fail("must not persist"),
        qualification_persister=lambda *args, **kwargs: _Qualified(),
        run_starter=lambda *args, **kwargs: object(),
        run_finalizer=lambda *args, **kwargs: None,
    )

    summary = agent.run_once()

    assert summary.sources_failed == 1
    assert summary.source_results[0].success is False
    assert summary.source_results[0].error_type == "StageMaCollectionError"


# ================================= SOURCE MAP ===============================


def test_the_source_map_and_the_config_row_agree_about_stage_ma() -> None:
    """The map's ACTIVE and the config's `status: active` are one claim.

    They are stored in two files, so they can disagree; the validator's job is
    to make that disagreement impossible, and this test is the reason to trust
    it. `live_canary` stays false — activation schedules nothing continuous.
    """
    from evaluation.morocco_pfe.validator import (
        DEFAULT_SOURCE_MAP_PATH,
        DEFAULT_SOURCE_REGISTRY,
        check_source_map_against_production_registry,
        load_source_map,
    )

    active = check_source_map_against_production_registry(
        load_source_map(DEFAULT_SOURCE_MAP_PATH), DEFAULT_SOURCE_REGISTRY
    )
    entry = {item.id: item for item in active}["stage_ma"]
    configured = {item.id: item for item in load_source_registry()}["stage_ma"]

    assert entry.integration_status == "ACTIVE"
    assert entry.collection_strategy == "EXISTING_COLLECTOR"
    assert entry.production_source_id == configured.id
    assert entry.live_canary is False
