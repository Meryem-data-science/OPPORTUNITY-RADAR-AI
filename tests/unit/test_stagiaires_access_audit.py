"""Offline tests for the Phase 7C.4A Stagiaires.ma access audit.

Every byte these tests parse is written here, by hand, and is deliberately tiny
and synthetic. Nothing opens a socket: a test that needs the real site is a
test that fails when the site is slow, when a marketing team edits a template,
or when CI has no egress — none of which says anything about this code. The
fixtures imitate the *shape* the audit must cope with (a namespaced sitemap, a
`<lastmod>`, an off-domain `<loc>`, a JSON-LD `JobPosting`), never real
Stagiaires.ma content.

The invariants worth breaking a build over are the ones a later phase could
quietly violate: that `<lastmod>` never becomes a publication date, that a
numeric ID under two URLs is surfaced rather than merged, that the detail
sample cannot exceed three pages, and that none of this reaches production.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from evaluation.morocco_pfe.cli import stagiaires_access_audit as cli
from evaluation.morocco_pfe.stagiaires_access import (
    AUDIT_USER_AGENT,
    DETAIL_SAMPLE_STRATEGY,
    DETAIL_SIGNALS,
    MAX_DETAIL_LIMIT,
    PFE_TARGET_URL,
    REJECT_MISSING_NUMERIC_ID,
    REJECT_NOT_A_URL,
    REJECT_OFF_DOMAIN,
    REJECT_WRONG_PATH_FAMILY,
    ROBOTS_OBEY,
    ROBOTS_URL,
    SitemapOfferEntry,
    StagiairesAccessError,
    canonical_url,
    classify_robots_response,
    combine_offer_sitemaps,
    describe_detail_page,
    detail_signals,
    extract_external_id,
    is_stagiaires_host,
    parse_offer_sitemap,
    parse_robots_txt,
    parse_sitemap_index,
    robots_target,
    robots_verdict,
    select_detail_sample,
    sitemap_declarations,
    summarize_jsonld,
    validate_detail_limit,
)

HOST = "https://www.stagiaires.ma"
OFFER = f"{HOST}/stage-emploi-maroc"

ROBOTS_BODY = """
# synthetic robots.txt, not a copy of the real file
User-agent: *
Disallow: /admin/
Allow: /

Sitemap: https://www.stagiaires.ma/sitemap_v9.xml
Sitemap: https://www.stagiaires.ma/sitemap_v9.xml
Sitemap: /relative-sitemap.xml
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.stagiaires.ma/offre-sitemap.xml</loc></sitemap>
  <sitemap><loc>https://www.stagiaires.ma/offre-sitemap2.xml</loc></sitemap>
  <sitemap><loc>https://www.stagiaires.ma/offre-sitemap.xml</loc></sitemap>
  <sitemap><loc>https://mirror.example.com/offre-sitemap3.xml</loc></sitemap>
  <sitemap><loc>https://www.stagiaires.ma/page-sitemap.xml</loc></sitemap>
</sitemapindex>
"""


def urlset(rows: list[tuple[str, str | None]]) -> str:
    """Build a `<urlset>` from (loc, lastmod) pairs, namespaced like the real one."""
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


#: Stands in for a job description. Its *length* is what the audit reports;
#: the test asserts against this constant so the fixture and the expectation
#: cannot drift apart.
DETAIL_DESCRIPTION = "Description synthetique dune offre de stage inventee."

DETAIL_HTML = """<html><head>
<title>Offre — site title</title>
<meta property="og:title" content="OG fallback title">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Stage PFE Data",
 "hiringOrganization":{"@type":"Organization","name":"Entreprise Test"},
 "jobLocation":{"@type":"Place","address":{"addressLocality":"Casablanca"}},
 "employmentType":"INTERN","datePosted":"2026-08-30","validThrough":"2026-10-30",
 "description":"__DESCRIPTION__"}
</script></head>
<body><h1>Titre visible</h1><script id="__NEXT_DATA__">{"props":{}}</script></body></html>
""".replace(
    "__DESCRIPTION__", DETAIL_DESCRIPTION
)


# ------------------------------------------------ A. robots declarations ----


def test_robots_groups_and_target_verdict_are_read_from_the_file() -> None:
    groups = parse_robots_txt(ROBOTS_BODY)

    assert groups
    verdict = robots_verdict(groups, "/stage-emploi-type-stage/stage-de-fin-d-etudes")
    assert verdict.allowed is True
    assert robots_verdict(groups, "/admin/secret").allowed is False


def test_sitemap_declarations_are_absolute_deduplicated_and_ordered() -> None:
    """A `Sitemap:` line is the audit's only sanctioned way to find a sitemap."""
    assert sitemap_declarations(ROBOTS_BODY) == (f"{HOST}/sitemap_v9.xml",)


def test_a_robots_we_were_refused_is_never_read_as_permission() -> None:
    refused = classify_robots_response(403, ROBOTS_URL, "")
    assert refused.may_proceed is False

    assert classify_robots_response(200, ROBOTS_URL, ROBOTS_BODY).disposition == (
        ROBOTS_OBEY
    )


# --------------------------------------------------- B. sitemapindex --------


def test_the_index_yields_same_host_offer_sitemaps_only_and_without_duplicates() -> None:
    index = parse_sitemap_index(SITEMAP_INDEX)

    assert index.offer_sitemaps == (
        f"{HOST}/offre-sitemap.xml",
        f"{HOST}/offre-sitemap2.xml",
    )
    assert len(set(index.offer_sitemaps)) == len(index.offer_sitemaps)
    assert index.other_sitemaps == (f"{HOST}/page-sitemap.xml",)
    assert [item.reason for item in index.rejected] == [REJECT_OFF_DOMAIN]


def test_a_further_numbered_offer_sitemap_is_discovered_not_hardcoded() -> None:
    """Two files today is an observation, not a rule the parser may encode."""
    document = SITEMAP_INDEX.replace(
        "<sitemap><loc>https://www.stagiaires.ma/page-sitemap.xml</loc></sitemap>",
        "<sitemap><loc>https://www.stagiaires.ma/offre-sitemap3.xml</loc></sitemap>",
    )
    assert parse_sitemap_index(document).offer_sitemaps[-1] == (
        f"{HOST}/offre-sitemap3.xml"
    )


def test_a_document_that_is_not_a_sitemapindex_is_refused() -> None:
    """An HTML error page parsed 'leniently' would report zero offers as success."""
    with pytest.raises(StagiairesAccessError, match="sitemapindex"):
        parse_sitemap_index(urlset([(f"{OFFER}/1-a", None)]))
    with pytest.raises(StagiairesAccessError, match="well-formed"):
        parse_sitemap_index("<html><body>oops")


# ------------------------------------------------------- C. offer urlset ----


def test_offer_urls_yield_a_canonical_url_and_a_numeric_external_id() -> None:
    parse = parse_offer_sitemap(
        urlset(
            [
                (f"{OFFER}/6159-ad-simulation-sw-developer-junior-h-f", "2026-09-01"),
                (f"{OFFER}/6152-auditeur-it-junior-h-f", None),
            ]
        )
    )

    assert [item.source_external_id for item in parse.entries] == ["6159", "6152"]
    assert parse.entries[0].canonical_url == (
        f"{OFFER}/6159-ad-simulation-sw-developer-junior-h-f"
    )
    assert parse.url_count == 2
    assert parse.rejected == ()


def test_lastmod_is_preserved_separately_and_only_where_the_site_gave_one() -> None:
    parse = parse_offer_sitemap(
        urlset([(f"{OFFER}/10-a", "2026-09-01T08:00:00+00:00"), (f"{OFFER}/11-b", None)])
    )

    assert parse.entries[0].sitemap_lastmod == "2026-09-01T08:00:00+00:00"
    assert parse.entries[1].sitemap_lastmod is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{OFFER}/6159-slug", "6159"),
        (f"{OFFER}/6159", "6159"),
        (f"{OFFER}/6159-slug/", "6159"),
        (f"{OFFER}/6159-slug?utm_source=x#top", "6159"),
        (f"{OFFER}/007-slug", "007"),
    ],
)
def test_the_numeric_id_is_read_from_the_path_as_published(
    url: str, expected: str
) -> None:
    """`007` stays `007`: normalizing it to `7` would invent an equality."""
    assert extract_external_id(url) == expected


# ---------------------------------------------------- D. lastmod invariant --


def test_lastmod_is_never_exposed_as_a_publication_date() -> None:
    """The invariant the whole slice exists to protect.

    A `<lastmod>` says a page changed. It does not say an offer was posted, and
    a later phase that quietly maps one to the other would corrupt every date in
    the product. There is no `published_at` to map it to, by construction.
    """
    entry = parse_offer_sitemap(urlset([(f"{OFFER}/6159-a", "2026-09-01")])).entries[0]

    assert entry.sitemap_lastmod == "2026-09-01"
    assert not hasattr(entry, "published_at")
    assert "published_at" not in entry.as_dict()
    assert set(entry.as_dict()) == {
        "source_external_id",
        "canonical_url",
        "sitemap_lastmod",
    }


def test_the_combined_audit_states_that_lastmod_is_not_a_publication_date() -> None:
    audit = combine_offer_sitemaps(
        [parse_offer_sitemap(urlset([(f"{OFFER}/6159-a", "2026-09-01")]))]
    )
    payload = audit.as_dict()

    assert payload["lastmod_is_publication_date"] is False
    assert "published_at" not in payload


def test_no_detail_signal_is_ever_fed_by_a_sitemap_lastmod() -> None:
    """A page with no date reports none — it does not borrow the sitemap's."""
    signals = detail_signals("<html><head><title>t</title></head><body></body></html>")

    assert signals["publication_date"].found is False
    assert signals["publication_date"].value is None


# ------------------------------------------------------ E. malformed URLs ---


@pytest.mark.parametrize(
    ("loc", "reason"),
    [
        (f"https://jobs.example.com{'/stage-emploi-maroc'}/6159-a", REJECT_OFF_DOMAIN),
        (f"{HOST}/blog/6159-a", REJECT_WRONG_PATH_FAMILY),
        (f"{OFFER}/", REJECT_WRONG_PATH_FAMILY),
        (f"{OFFER}/slug-without-id", REJECT_MISSING_NUMERIC_ID),
        (f"{OFFER}/61a59-mixed", REJECT_MISSING_NUMERIC_ID),
        (f"{OFFER}/6159abc-glued", REJECT_MISSING_NUMERIC_ID),
        (f"ftp://www.stagiaires.ma{'/stage-emploi-maroc'}/6159-a", REJECT_NOT_A_URL),
    ],
)
def test_a_url_outside_the_offer_family_is_surfaced_with_its_reason(
    loc: str, reason: str
) -> None:
    parse = parse_offer_sitemap(urlset([(loc, None)]))

    assert parse.entries == ()
    assert [item.reason for item in parse.rejected] == [reason]
    assert parse.as_dict()["rejected_reasons"] == {reason: 1}


def test_rejections_do_not_silently_shrink_the_url_count() -> None:
    """`url_count` is what the site published; `accepted` is what we could use."""
    parse = parse_offer_sitemap(
        urlset([(f"{OFFER}/6159-a", None), (f"{HOST}/blog/x", None)])
    )

    assert parse.url_count == 2
    assert len(parse.entries) == 1


def test_an_off_domain_url_is_not_a_stagiaires_host() -> None:
    assert is_stagiaires_host(f"{OFFER}/1-a") is True
    assert is_stagiaires_host("https://stagiaires.ma.evil.example/1") is False
    assert is_stagiaires_host("javascript:alert(1)") is False


def test_canonicalization_drops_only_the_meaningless_parts() -> None:
    assert canonical_url("HTTPS://WWW.Stagiaires.MA/Stage-Emploi-Maroc/6159-A/?x=1#f") == (
        "https://www.stagiaires.ma/Stage-Emploi-Maroc/6159-A"
    )
    with pytest.raises(StagiairesAccessError):
        canonical_url("   ")


# ------------------------------------------------------ F. deduplication ----


def test_the_same_canonical_url_in_two_sitemaps_is_one_logical_record() -> None:
    duplicated = urlset([(f"{OFFER}/6159-a", "2026-09-01")])
    audit = combine_offer_sitemaps(
        [parse_offer_sitemap(duplicated), parse_offer_sitemap(duplicated)]
    )

    assert len(audit.entries) == 1
    assert audit.duplicate_canonical_urls == 1
    assert audit.total_urls == 2
    assert audit.as_dict()["unique_canonical_urls"] == 1


def test_a_trailing_slash_or_query_variant_is_the_same_record() -> None:
    audit = combine_offer_sitemaps(
        [
            parse_offer_sitemap(
                urlset(
                    [
                        (f"{OFFER}/6159-a", None),
                        (f"{OFFER}/6159-a/", None),
                        (f"{OFFER}/6159-a?utm_source=newsletter", None),
                    ]
                )
            )
        ]
    )

    assert len(audit.entries) == 1
    assert audit.duplicate_canonical_urls == 2


# --------------------------------------------------- G. ID collisions -------


def test_one_numeric_id_under_two_urls_is_surfaced_as_an_audit_problem() -> None:
    """The finding that decides whether the ID may ever be a production key."""
    audit = combine_offer_sitemaps(
        [parse_offer_sitemap(urlset([(f"{OFFER}/6159-a", None), (f"{OFFER}/6159-b", None)]))]
    )

    assert len(audit.entries) == 2
    assert audit.unique_ids == 1
    assert [item.source_external_id for item in audit.id_collisions] == ["6159"]
    assert audit.id_collisions[0].canonical_urls == (
        f"{OFFER}/6159-a",
        f"{OFFER}/6159-b",
    )
    assert audit.as_dict()["id_collision_count"] == 1


def test_a_collision_is_reported_rather_than_resolved_by_dropping_one_url() -> None:
    audit = combine_offer_sitemaps(
        [parse_offer_sitemap(urlset([(f"{OFFER}/6159-a", None), (f"{OFFER}/6159-b", None)]))]
    )

    assert {item.canonical_url for item in audit.entries} == {
        f"{OFFER}/6159-a",
        f"{OFFER}/6159-b",
    }


def test_distinct_ids_produce_no_collision() -> None:
    audit = combine_offer_sitemaps(
        [parse_offer_sitemap(urlset([(f"{OFFER}/1-a", None), (f"{OFFER}/2-b", None)]))]
    )

    assert audit.id_collisions == ()


# ------------------------------------------------- H. detail sample bound ---


def test_the_detail_sample_is_hard_bounded_at_three_pages() -> None:
    entries = tuple(
        SitemapOfferEntry(str(number), f"{OFFER}/{number}-slug") for number in range(500)
    )

    assert len(select_detail_sample(entries, MAX_DETAIL_LIMIT)) == MAX_DETAIL_LIMIT
    assert MAX_DETAIL_LIMIT == 3


@pytest.mark.parametrize("limit", [0, -1, 4, 10, 1000, True, 2.0, "3", None])
def test_a_limit_outside_the_bound_is_refused_rather_than_clamped(
    limit: object,
) -> None:
    with pytest.raises(StagiairesAccessError):
        validate_detail_limit(limit)
    with pytest.raises(StagiairesAccessError):
        select_detail_sample((), limit)  # type: ignore[arg-type]


def test_the_sample_is_deterministic_and_independent_of_input_order() -> None:
    """Highest numeric ID first, ties by URL — a total order, not a coincidence."""
    entries = tuple(
        SitemapOfferEntry(identifier, f"{OFFER}/{identifier}-{suffix}")
        for identifier, suffix in [("10", "a"), ("6159", "z"), ("6159", "b"), ("99", "c")]
    )

    chosen = select_detail_sample(entries, 3)
    assert [item.canonical_url for item in chosen] == [
        f"{OFFER}/6159-b",
        f"{OFFER}/6159-z",
        f"{OFFER}/99-c",
    ]
    assert select_detail_sample(tuple(reversed(entries)), 3) == chosen


# ------------------------------------------------- detail page structure ----


def test_a_job_posting_page_reports_which_carrier_supplied_each_signal() -> None:
    signals = detail_signals(DETAIL_HTML)

    assert signals["title"].carrier == "jsonld:JobPosting.title"
    assert signals["title"].value == "Stage PFE Data"
    assert signals["organization"].value == "Entreprise Test"
    assert signals["location"].value == "Casablanca"
    assert signals["publication_date"].value == "2026-08-30"
    assert signals["deadline"].value == "2026-10-30"


def test_a_description_is_measured_and_never_quoted() -> None:
    """Third-party job text is counted, not copied into this repository."""
    description = detail_signals(DETAIL_HTML)["description"]

    assert description.found is True
    assert description.value is None
    assert description.chars == len(DETAIL_DESCRIPTION)


def test_an_absent_signal_is_reported_absent_rather_than_guessed() -> None:
    signals = detail_signals(
        "<html><head><title>Une offre</title></head><body></body></html>"
    )

    assert signals["title"].found is True
    assert signals["title"].carrier == "html:title"
    for name in ("organization", "location", "deadline", "work_mode"):
        assert signals[name].found is False, name
        assert signals[name].carrier is None


def test_microdata_supplies_signals_when_there_is_no_json_ld() -> None:
    """The fallback path, including scope across void elements.

    A `<meta itemprop>` never closes, so a scope stack that pushed for it would
    misattribute every later string on the page. This asserts the values land on
    the elements that actually carry them.
    """
    signals = detail_signals(
        "<html><head><meta name='description' content='Une description.'></head>"
        "<body itemscope itemtype='https://schema.org/JobPosting'>"
        "<meta itemprop='datePosted' content='2026-07-15'>"
        "<h1 itemprop='title'>Stage Data Engineer</h1>"
        "<span itemprop='hiringOrganization'>Societe Test</span>"
        "<span itemprop='addressLocality'>Rabat</span>"
        "</body></html>"
    )

    assert signals["title"].carrier == "microdata:itemprop=title"
    assert signals["title"].value == "Stage Data Engineer"
    assert signals["organization"].value == "Societe Test"
    assert signals["location"].value == "Rabat"
    assert signals["publication_date"].value == "2026-07-15"
    assert signals["description"].carrier == "meta:description"


def test_every_declared_signal_is_answered_for_any_page() -> None:
    for html in (DETAIL_HTML, "", "<html>", "<html><body>plain</body></html>"):
        assert tuple(detail_signals(html)) == DETAIL_SIGNALS


def test_the_page_summary_reports_types_and_key_names_never_values() -> None:
    summary = summarize_jsonld(DETAIL_HTML)

    assert summary.types == ("JobPosting",)
    assert "title" in summary.job_posting_keys
    assert "Stage PFE Data" not in summary.job_posting_keys
    assert describe_detail_page(DETAIL_HTML)["has_next_data_script"] is True


def test_malformed_json_ld_is_counted_not_crashed_on() -> None:
    summary = summarize_jsonld(
        '<script type="application/ld+json">{not json}</script>'
    )

    assert summary.block_count == 1
    assert summary.invalid_block_count == 1
    assert summary.job_posting_count == 0


# -------------------------------------------------------- the audit run -----


class _FakeResponse:
    def __init__(
        self,
        url: str,
        status_code: int,
        text: str,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type, **(extra_headers or {})}


class _FakeClient:
    """A recording stand-in for `httpx.Client`. Serves fixtures, counts GETs.

    `redirects` maps a URL to the `Location` it answers with, so a test can
    prove where the audit does — and does not — go next. The client refuses to
    be asked to follow redirects itself: that is the audit's decision to make,
    and a regression that handed it back to httpx would silently reopen the
    off-domain hole.
    """

    def __init__(
        self,
        routes: dict[str, tuple[int, str, str]],
        redirects: dict[str, str] | None = None,
        redirect_status: int = 302,
    ) -> None:
        self.routes = routes
        self.redirects = redirects or {}
        self.redirect_status = redirect_status
        self.requested: list[str] = []

    def get(self, url: str, timeout: float | None = None, follow_redirects: bool = False):
        assert follow_redirects is False, (
            "the audit must resolve redirects itself, never hand them to httpx"
        )
        self.requested.append(url)
        if url in self.redirects:
            return _FakeResponse(
                url, self.redirect_status, "", "text/html", {"location": self.redirects[url]}
            )
        status, body, content_type = self.routes.get(
            url, (404, "not found", "text/html")
        )
        return _FakeResponse(url, status, body, content_type)

    def close(self) -> None:  # pragma: no cover - parity with httpx.Client
        pass


def _routes(offer_count: int = 8) -> dict[str, tuple[int, str, str]]:
    first = urlset([(f"{OFFER}/{6100 + n}-slug-{n}", "2026-09-01") for n in range(offer_count)])
    second = urlset([(f"{OFFER}/{5000 + n}-other-{n}", None) for n in range(3)])
    routes: dict[str, tuple[int, str, str]] = {
        ROBOTS_URL: (200, ROBOTS_BODY, "text/plain"),
        PFE_TARGET_URL: (200, "<html><body>" + "x" * 500 + "</body></html>", "text/html"),
        f"{HOST}/sitemap_v9.xml": (200, SITEMAP_INDEX, "application/xml"),
        f"{HOST}/offre-sitemap.xml": (200, first, "application/xml"),
        f"{HOST}/offre-sitemap2.xml": (200, second, "application/xml"),
    }
    for number in range(offer_count):
        routes[f"{OFFER}/{6100 + number}-slug-{number}"] = (
            200,
            DETAIL_HTML,
            "text/html",
        )
    return routes


def test_the_audit_walks_robots_then_the_sitemap_chain_and_stops_at_three_pages() -> None:
    client = _FakeClient(_routes())

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] == "COMPLETED"
    assert report["exit_code"] == cli.EXIT_OK
    assert report["user_agent"] == AUDIT_USER_AGENT
    # robots, the listing, the index, two offer sitemaps, three detail pages.
    assert len(client.requested) == 8
    assert client.requested[0] == ROBOTS_URL
    assert report["detail_audit"]["sampled"] == 3
    assert report["offers"]["accepted_offer_urls"] == 11
    assert report["offers"]["unique_source_external_ids"] == 11


def test_no_page_beyond_the_bound_is_ever_requested() -> None:
    """A thousand offers must not become a thousand GETs."""
    client = _FakeClient(_routes(offer_count=400))

    cli.run(client=client, delay=0.0)

    detail_gets = [item for item in client.requested if "/stage-emploi-maroc/" in item]
    assert len(detail_gets) == MAX_DETAIL_LIMIT


def test_the_listing_is_recorded_but_never_used_as_the_discovery_source() -> None:
    client = _FakeClient(_routes())

    report = cli.run(client=client, delay=0.0)

    assert report["pfe_listing"]["status_code"] == 200
    assert report["pfe_listing"]["used_as_discovery_source"] is False
    assert report["sitemap_index"]["selected"] == f"{HOST}/sitemap_v9.xml"
    assert report["sitemap_index"]["selected_from"] == "robots.txt"


def test_the_report_declares_the_guarantees_this_slice_makes() -> None:
    report = cli.run(client=_FakeClient(_routes()), delay=0.0)

    assert report["audit_phase"] == "7C.4A"
    for guarantee in (
        "authenticated",
        "browser_automation",
        "wrote_database",
        "wrote_raw_html",
        "activated_production_source",
        "lastmod_mapped_to_published_at",
    ):
        assert report[guarantee] is False, guarantee


def test_a_refused_robots_stops_the_audit_before_anything_else_is_fetched() -> None:
    client = _FakeClient({ROBOTS_URL: (403, "", "text/html")})

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] == "ROBOTS_BARRIER"
    assert report["exit_code"] == cli.EXIT_BARRIER
    assert client.requested == [ROBOTS_URL]


def test_a_robots_that_disallows_the_listing_is_obeyed() -> None:
    routes = _routes()
    routes[ROBOTS_URL] = (
        200,
        "User-agent: *\nDisallow: /stage-emploi-type-stage/\n",
        "text/plain",
    )
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] == "ROBOTS_DISALLOWED"
    assert report["exit_code"] == cli.EXIT_ROBOTS_DISALLOWED
    assert PFE_TARGET_URL not in client.requested


def test_a_sitemap_the_site_does_not_declare_is_never_guessed() -> None:
    routes = _routes()
    routes[ROBOTS_URL] = (200, "User-agent: *\nAllow: /\n", "text/plain")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] == "NO_SITEMAP_DECLARED"
    assert f"{HOST}/sitemap_v9.xml" not in client.requested


def test_the_terms_check_is_not_attempted_without_an_operator_supplied_url() -> None:
    report = cli.run(client=_FakeClient(_routes()), delay=0.0)

    assert report["terms"]["url"] is None
    assert report["terms"]["barrier"] == "NOT_ATTEMPTED_NO_EVIDENCED_TERMS_URL"
    assert report["terms"]["manual_review_required"] is True


def test_a_reachable_terms_page_still_requires_human_review() -> None:
    """A 200 is reachability. It is not permission, and never becomes one."""
    routes = _routes()
    routes[f"{HOST}/conditions"] = (
        200,
        "<html><body><h1>Conditions</h1><p>" + "texte " * 100 + "</p></body></html>",
        "text/html",
    )
    report = cli.run(
        client=_FakeClient(routes), delay=0.0, terms_url=f"{HOST}/conditions"
    )

    assert report["terms"]["available"] is True
    assert report["terms"]["manual_review_required"] is True
    assert "permission" in report["terms"]["note"].lower()


def test_an_off_domain_terms_url_is_not_fetched() -> None:
    client = _FakeClient(_routes())

    report = cli.run(
        client=client, delay=0.0, terms_url="https://example.com/conditions"
    )

    assert report["terms"]["barrier"] == "NOT_ATTEMPTED_OFF_DOMAIN"
    assert "https://example.com/conditions" not in client.requested


def test_an_unparsable_offer_sitemap_is_a_finding_not_a_zero() -> None:
    """Evidence from the readable sibling survives — and the run does not pass.

    Both halves matter. Discarding the 8 offers we did read would throw away
    good evidence; calling the run COMPLETED would present those 8 as the site's
    offer count when a second file we could not read holds more.
    """
    routes = _routes()
    routes[f"{HOST}/offre-sitemap2.xml"] = (
        200,
        "<html><body><h1>Erreur</h1><p>" + "pas un sitemap " * 40 + "</p></body></html>",
        "text/html",
    )
    report = cli.run(client=_FakeClient(routes), delay=0.0)

    files = report["sitemap_index"]["offer_sitemaps_read"]
    assert any("URLSET_UNPARSABLE" in str(item.get("error")) for item in files)
    assert report["offers"]["accepted_offer_urls"] == 8
    assert report["outcome"] != cli.OUTCOME_COMPLETED
    assert report["exit_code"] != cli.EXIT_OK


def test_the_report_carries_no_page_body() -> None:
    """Structure is published; third-party page source never is."""
    import json

    payload = json.dumps(cli.run(client=_FakeClient(_routes()), delay=0.0))

    assert "__NEXT_DATA__" not in payload
    assert DETAIL_DESCRIPTION not in payload
    assert "<html" not in payload


# ------------------------------------------------ J. production isolation ---

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_the_audit_itself_still_activates_nothing() -> None:
    """The audit remains an audit, even now that a collector exists.

    7C.4A asserted that no Stagiaires production source or collector existed
    anywhere; Phase 7C.4B added both, deliberately and under review, so that
    exact assertion belonged to that phase. What must still hold is narrower and
    permanent: *this* module is evidence infrastructure. It builds no collector,
    reads no production registry, and cannot put a source into service — the
    import-direction test below is what keeps that true.
    """
    import sys

    for name in (
        "evaluation.morocco_pfe.stagiaires_access",
        "evaluation.morocco_pfe.cli.stagiaires_access_audit",
    ):
        __import__(name)
        module = sys.modules[name]
        # Nothing production-side is reachable from the audit's namespace, so it
        # has nothing to build, register or persist with.
        for attribute in vars(module).values():
            origin = getattr(attribute, "__module__", "") or ""
            assert not origin.startswith("services."), f"{name} -> {origin}"

    report = cli.run(client=_FakeClient(_routes()), delay=0.0)
    assert report["activated_production_source"] is False
    assert report["wrote_database"] is False


def test_a_stagiaires_production_source_would_still_be_rejected(tmp_path: Path) -> None:
    """The production loader's closed type set is unchanged by this slice."""
    from services.collector.sources import (
        SourceConfigurationError,
        load_source_registry,
    )

    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "sources:\n"
        "  - id: stagiaires_ma\n"
        "    type: stagiaires_ma\n"
        "    enabled: true\n"
        "    category: jobs\n"
        "    country: MA\n"
        "    frequency_minutes: 120\n"
        "    status: active\n",
        encoding="utf-8",
    )
    with pytest.raises(SourceConfigurationError, match="unsupported type"):
        load_source_registry(registry)


def test_production_never_depends_on_the_evaluation_layer() -> None:
    """The layering direction, which outlives any single phase.

    Phase 7C.4B legitimately added production Stagiaires code, so scanning the
    production tree for the word no longer proves anything. The invariant that
    still matters is the arrow between the layers: `evaluation/` is an audit
    trail, free to change as evidence changes, and production importing it would
    make the runtime depend on a record of what we once checked.
    """
    offenders = []
    for path in (REPOSITORY_ROOT / "services").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "evaluation." in text or "from evaluation" in text:
            offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert offenders == []


def test_the_audit_imports_nothing_that_could_write_or_collect() -> None:
    """The evaluation layer cannot activate a source it cannot even import.

    Checked against the modules' *import* lines rather than their prose: the
    docstrings say "no SQLite" out loud, and a test that forbade the word would
    forbid saying so.
    """
    for module in (
        "evaluation/morocco_pfe/stagiaires_access.py",
        "evaluation/morocco_pfe/cli/stagiaires_access_audit.py",
    ):
        imports = [
            line
            for line in (REPOSITORY_ROOT / module).read_text(encoding="utf-8").splitlines()
            if line.startswith(("import ", "from "))
        ]
        for line in imports:
            lowered = line.lower()
            assert "services." not in lowered, f"{module}: {line}"
            assert "sqlite" not in lowered, f"{module}: {line}"
            assert "libsql" not in lowered, f"{module}: {line}"
            assert "selenium" not in lowered, f"{module}: {line}"
            assert "playwright" not in lowered, f"{module}: {line}"


# ====================================================================
# Architect review fixes — regression tests
# ====================================================================

# ------------------------------- 1. robots.txt semantics --------------------


def test_an_empty_disallow_does_not_cancel_a_site_wide_disallow() -> None:
    """The finding: `Disallow:` was being turned into `Allow: /`.

        User-agent: *
        Disallow: /
        Disallow:

    An empty `Disallow` imposes no rule. Synthesizing `Allow: /` from it made a
    rule that tied with the real `Disallow: /` on length and then won the tie,
    silently converting a site-wide refusal into blanket permission.
    """
    groups = parse_robots_txt("User-agent: *\nDisallow: /\nDisallow:\n")

    for path in ("/anything", "/", "/stage-emploi-maroc/6159-a"):
        verdict = robots_verdict(groups, path)
        assert verdict.allowed is False, path
        assert verdict.matched_rule == "Disallow: /"


def test_an_empty_disallow_alone_imposes_no_rule_at_all() -> None:
    """No rule matched is the REP default of allowed — not a synthesized rule."""
    verdict = robots_verdict(parse_robots_txt("User-agent: *\nDisallow:\n"), "/x")

    assert verdict.allowed is True
    assert verdict.matched_rule is None


def test_a_later_applicable_group_is_not_silently_ignored() -> None:
    """The finding: only the first matching record was consulted."""
    groups = parse_robots_txt(
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        "User-agent: *\n"
        "Disallow: /stage-emploi-maroc/\n"
    )

    assert robots_verdict(groups, "/stage-emploi-maroc/6159-a").allowed is False
    assert robots_verdict(groups, "/autre-page").allowed is True


def test_a_later_group_naming_the_audit_agent_is_merged_too() -> None:
    groups = parse_robots_txt(
        "User-agent: OpportunityRadarAI-AccessAudit\n"
        "Allow: /\n"
        "\n"
        "User-agent: OpportunityRadarAI-AccessAudit\n"
        "Disallow: /prive/\n"
    )

    assert robots_verdict(groups, "/prive/x").allowed is False
    assert robots_verdict(groups, "/public/x").allowed is True


def test_every_wildcard_group_is_merged_when_no_group_names_us() -> None:
    groups = parse_robots_txt(
        "User-agent: *\nDisallow: /a/\n\nUser-agent: *\nDisallow: /b/\n"
    )

    assert robots_verdict(groups, "/a/x").allowed is False
    assert robots_verdict(groups, "/b/x").allowed is False


def test_a_group_naming_us_wins_over_the_wildcard_group() -> None:
    """REP precedence: the specific record replaces `*`, it does not add to it."""
    groups = parse_robots_txt(
        "User-agent: *\nDisallow: /\n"
        "\n"
        "User-agent: OpportunityRadarAI-AccessAudit\nAllow: /\n"
    )

    assert robots_verdict(groups, "/stage-emploi-maroc/1-a").allowed is True


def test_an_unrelated_agent_group_does_not_capture_this_audit() -> None:
    """Matching is exact or a product-token prefix, never a loose substring."""
    groups = parse_robots_txt(
        "User-agent: ai\nAllow: /\n\nUser-agent: *\nDisallow: /secret/\n"
    )

    # `ai` occurs inside our token but does not address us; the wildcard rules do.
    assert robots_verdict(groups, "/secret/x").allowed is False


def test_a_shorter_product_token_prefix_still_addresses_us() -> None:
    groups = parse_robots_txt("User-agent: OpportunityRadarAI\nDisallow: /prive/\n")

    assert robots_verdict(groups, "/prive/x").allowed is False


def test_the_real_audit_style_wildcard_rules_still_behave() -> None:
    """Finding C: the ordinary Allow/Disallow file is unaffected by the fix."""
    groups = parse_robots_txt(ROBOTS_BODY)

    assert robots_verdict(groups, "/stage-emploi-type-stage/stage-de-fin-d-etudes").allowed
    assert robots_verdict(groups, "/stage-emploi-maroc/6159-a").allowed
    assert robots_verdict(groups, "/admin/secret").allowed is False
    assert robots_verdict(groups, "/admin/secret").matched_rule == "Disallow: /admin/"


def test_longest_match_and_allow_tie_break_are_preserved() -> None:
    groups = parse_robots_txt(
        "User-agent: *\nDisallow: /a/\nAllow: /a/public/\nDisallow: /a/public/deep/\n"
    )

    assert robots_verdict(groups, "/a/x").allowed is False
    assert robots_verdict(groups, "/a/public/x").allowed is True
    assert robots_verdict(groups, "/a/public/deep/x").allowed is False


# ---------------------- 2. the same-host network boundary -------------------


def test_an_off_domain_redirect_is_reported_and_never_followed() -> None:
    """The finding: httpx followed redirects, so a site could hand us any host.

    A same-host URL answering `302 Location: https://tracker.example.com/...`
    would previously have been followed automatically, and the audit would have
    issued a GET to a host it never chose to talk to.
    """
    routes = _routes()
    elsewhere = "https://tracker.example.com/landing"
    client = _FakeClient(routes, redirects={PFE_TARGET_URL: elsewhere})

    report = cli.run(client=client, delay=0.0)

    assert elsewhere not in client.requested
    assert not any("tracker.example.com" in item for item in client.requested)
    listing = report["pfe_listing"]
    assert listing["barrier"] == cli.OFF_DOMAIN_REDIRECT
    assert listing["redirect_target"] == elsewhere
    assert listing["redirect_target_host"] == "tracker.example.com"
    assert cli.OFF_DOMAIN_REDIRECT in report["barriers"]


def test_no_get_in_a_whole_run_ever_leaves_the_stagiaires_hosts() -> None:
    """The hard invariant, asserted over every request the audit makes."""
    routes = _routes()
    client = _FakeClient(
        routes,
        redirects={
            f"{OFFER}/6107-slug-7": "https://ats.example.net/apply/7",
            f"{HOST}/offre-sitemap2.xml": "https://cdn.example.org/sitemap2.xml",
        },
    )

    cli.run(client=client, delay=0.0)

    for requested in client.requested:
        assert is_stagiaires_host(requested), requested


def test_a_same_host_redirect_is_followed_and_bounded() -> None:
    routes = _routes()
    moved = f"{HOST}/stage-emploi-type-stage/pfe-2026"
    routes[moved] = (200, "<html><body>" + "x" * 500 + "</body></html>", "text/html")
    client = _FakeClient(routes, redirects={PFE_TARGET_URL: moved})

    report = cli.run(client=client, delay=0.0)

    assert moved in client.requested
    assert report["pfe_listing"]["barrier"] is None
    assert report["pfe_listing"]["redirect_chain"] == [moved]
    assert report["pfe_listing"]["status_code"] == 200


def test_a_redirect_loop_is_bounded_rather_than_followed_forever() -> None:
    routes = _routes()
    first, second = f"{HOST}/loop-a", f"{HOST}/loop-b"
    client = _FakeClient(
        routes, redirects={PFE_TARGET_URL: first, first: second, second: first}
    )

    report = cli.run(client=client, delay=0.0)

    assert report["pfe_listing"]["barrier"] == "TOO_MANY_REDIRECTS"
    assert client.requested.count(first) <= cli.MAX_REDIRECTS + 1


def test_the_audit_never_asks_httpx_to_follow_redirects() -> None:
    """`follow_redirects=True` is the regression this asserts against.

    The fake client raises if it is ever passed, so a completed run is itself
    the proof; this states it explicitly so the reason is not lost.
    """
    client = _FakeClient(_routes())

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] == "COMPLETED"
    assert client.requested


def test_an_ordinary_200_run_is_unchanged_by_the_redirect_handling() -> None:
    client = _FakeClient(_routes())

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] == "COMPLETED"
    assert len(client.requested) == 8
    assert report["detail_audit"]["sampled"] == 3
    assert "redirect_chain" not in report["pfe_listing"]


# ------------------- 3. no arbitrary sitemap override -----------------------


def test_the_cli_offers_no_way_to_name_a_sitemap_url() -> None:
    """The finding: `--sitemap-url` let an operator make the audit GET anything."""
    options = set(cli.build_parser()._option_string_actions)

    assert "--sitemap-url" not in options
    assert options == {"-h", "--help", "--limit", "--terms-url", "--timeout", "--delay"}


def test_run_takes_no_sitemap_override_parameter_either() -> None:
    """Removing the flag alone would leave a programmatic bypass behind."""
    import inspect

    parameters = set(inspect.signature(cli.run).parameters)

    assert "sitemap_url" not in parameters
    assert parameters == {"limit", "terms_url", "timeout", "delay", "client"}


def test_only_the_sitemap_robots_declares_is_ever_fetched() -> None:
    """A same-host sitemap robots does not declare stays unfetched."""
    routes = _routes()
    undeclared = f"{HOST}/sitemap_secret.xml"
    routes[undeclared] = (200, SITEMAP_INDEX, "application/xml")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0)

    assert undeclared not in client.requested
    assert report["sitemap_index"]["selected"] == f"{HOST}/sitemap_v9.xml"
    assert report["sitemap_index"]["selected_from"] == "robots.txt"


def test_an_off_domain_sitemap_declaration_is_not_selected() -> None:
    routes = _routes()
    routes[ROBOTS_URL] = (
        200,
        "User-agent: *\nAllow: /\nSitemap: https://cdn.example.com/sitemap.xml\n",
        "text/plain",
    )
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] == "NO_SITEMAP_DECLARED"
    assert not any("cdn.example.com" in item for item in client.requested)


# ------------------------ 4. application_url semantics ----------------------


def test_a_posting_url_is_not_an_application_url() -> None:
    """Finding A: `og:url` / `JobPosting.url` name the page, not a way to apply.

    Reading them as an application link reports "you can apply here" for every
    offer ever published, including ones whose only route is an email address.
    """
    html = (
        '<html><head>'
        '<meta property="og:url" content="https://www.stagiaires.ma/stage-emploi-maroc/1-a">'
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","title":"T",'
        '"url":"https://www.stagiaires.ma/stage-emploi-maroc/1-a"}'
        "</script></head><body><a href='/'>Accueil</a></body></html>"
    )

    signal = detail_signals(html)["application_url"]

    assert signal.found is False
    assert signal.carrier is None
    assert signal.href is None


def test_an_explicit_application_link_is_recognized_by_its_own_words() -> None:
    """Finding B: an anchor that says it applies is the evidence that counts."""
    signal = detail_signals(
        '<html><body><a href="/postuler/6159">Postuler</a></body></html>'
    )["application_url"]

    assert signal.found is True
    assert signal.carrier == "html:a[explicit-application-text]"
    assert "explicit-application" in signal.carrier
    assert signal.value == "Postuler"
    assert signal.href == "/postuler/6159"


def test_a_relative_application_link_resolves_against_the_detail_page() -> None:
    """Finding C: the caller supplies the page's own URL, and only for this."""
    signal = detail_signals(
        '<html><body><a href="/postuler/6159">Postuler</a></body></html>',
        f"{OFFER}/6159-ad-simulation-sw-developer-junior-h-f",
    )["application_url"]

    assert signal.href == f"{HOST}/postuler/6159"


@pytest.mark.parametrize(
    "markup",
    [
        '<button aria-label="Candidater a cette offre">envoyer</button>',
        '<input type="submit" value="Apply now">',
        '<a href="/x" title="Postulez maintenant"></a>',
        "<a href='/x'>Candidature spontanee</a>",
    ],
)
def test_application_intent_is_read_from_any_ordinary_control(markup: str) -> None:
    """No site-specific selector: the control's accessible name is the signal."""
    assert detail_signals(f"<html><body>{markup}</body></html>")[
        "application_url"
    ].found is True


def test_a_navigation_link_is_not_an_application_link() -> None:
    signals = detail_signals(
        "<html><body><a href='/'>Accueil</a>"
        "<a href='/offres'>Toutes les offres</a>"
        "<button>Partager</button></body></html>"
    )

    assert signals["application_url"].found is False


def test_an_off_domain_application_href_is_reported_but_never_fetched() -> None:
    """Reporting a URL is not requesting it — an ATS link is a finding.

    The same-host rule binds what the audit *fetches*; an employer's ATS is
    exactly the kind of destination a reviewer needs to see recorded.
    """
    routes = _routes()
    apply_html = (
        "<html><body><h1>Offre</h1><p>" + "contenu " * 40 + "</p>"
        "<a href='https://ats.example.com/apply/1'>Postuler</a>"
        "</body></html>"
    )
    for number in range(8):
        routes[f"{OFFER}/{6100 + number}-slug-{number}"] = (200, apply_html, "text/html")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0)

    hrefs = {
        page["signals"]["application_url"]["href"]
        for page in report["detail_audit"]["pages"]
    }
    assert hrefs == {"https://ats.example.com/apply/1"}
    assert not any("ats.example.com" in item for item in client.requested)


def test_a_javascript_only_control_reports_intent_without_a_url() -> None:
    signal = detail_signals(
        "<html><body><a href='javascript:void(0)'>Postuler</a></body></html>"
    )["application_url"]

    assert signal.found is True
    assert signal.href is None


# ============ Architect review: robots must gate redirect targets ===========

#: A robots.txt that allows the site but forbids one subtree, plus the sitemap
#: declaration the audit needs to get past robots at all.
ROBOTS_WITH_PRIVATE = (
    "User-agent: *\n"
    "Disallow: /private/\n"
    "Allow: /\n"
    "\n"
    "Sitemap: https://www.stagiaires.ma/sitemap_v9.xml\n"
)


def _routes_with_private() -> dict[str, tuple[int, str, str]]:
    routes = _routes()
    routes[ROBOTS_URL] = (200, ROBOTS_WITH_PRIVATE, "text/plain")
    routes[f"{HOST}/private/secret"] = (
        200,
        "<html><body>" + "x" * 500 + "</body></html>",
        "text/html",
    )
    return routes


def test_a_same_host_redirect_into_a_disallowed_path_is_not_followed() -> None:
    """The finding: robots was checked on the URL we asked for, not the one we got.

    An allowed path answering `302 Location: /private/secret` would previously
    have been followed, and the audit would have fetched a path robots
    explicitly disallows. Obeying robots only on the request we chose is not
    obeying robots.
    """
    secret = f"{HOST}/private/secret"
    client = _FakeClient(_routes_with_private(), redirects={PFE_TARGET_URL: secret})

    report = cli.run(client=client, delay=0.0)

    assert secret not in client.requested
    assert report["pfe_listing"]["barrier"] == cli.ROBOTS_DISALLOWED_REDIRECT
    assert report["pfe_listing"]["redirect_target"] == secret
    assert cli.ROBOTS_DISALLOWED_REDIRECT in report["barriers"]


def test_the_refused_redirect_target_is_never_requested_anywhere_in_the_run() -> None:
    """Asserted over every GET, not just the one that triggered the redirect."""
    secret = f"{HOST}/private/secret"
    routes = _routes_with_private()
    client = _FakeClient(
        routes,
        redirects={
            PFE_TARGET_URL: secret,
            f"{OFFER}/6107-slug-7": f"{HOST}/private/offer-7",
        },
    )

    cli.run(client=client, delay=0.0)

    for requested in client.requested:
        assert "/private/" not in requested, requested
        assert is_stagiaires_host(requested), requested


def test_every_hop_of_a_chain_is_checked_not_only_the_first() -> None:
    """A redirect chain cannot launder its way into a disallowed path."""
    hop = f"{HOST}/etape-intermediaire"
    secret = f"{HOST}/private/secret"
    routes = _routes_with_private()
    routes[hop] = (200, "<html><body>" + "x" * 500 + "</body></html>", "text/html")
    client = _FakeClient(routes, redirects={PFE_TARGET_URL: hop, hop: secret})

    report = cli.run(client=client, delay=0.0)

    assert hop in client.requested
    assert secret not in client.requested
    assert report["pfe_listing"]["barrier"] == cli.ROBOTS_DISALLOWED_REDIRECT
    assert report["pfe_listing"]["redirect_chain"] == [hop, secret]


def test_an_allowed_same_host_redirect_is_still_followed() -> None:
    """The retained behaviour: a permitted move is resolved, not refused."""
    moved = f"{HOST}/stage-emploi-type-stage/pfe-2026"
    routes = _routes_with_private()
    routes[moved] = (200, "<html><body>" + "x" * 500 + "</body></html>", "text/html")
    client = _FakeClient(routes, redirects={PFE_TARGET_URL: moved})

    report = cli.run(client=client, delay=0.0)

    assert moved in client.requested
    assert report["pfe_listing"]["barrier"] is None
    assert report["pfe_listing"]["status_code"] == 200


def test_an_off_domain_redirect_is_still_refused_under_the_robots_gate() -> None:
    """Adding the robots check must not have displaced the host check."""
    elsewhere = "https://tracker.example.com/landing"
    client = _FakeClient(_routes_with_private(), redirects={PFE_TARGET_URL: elsewhere})

    report = cli.run(client=client, delay=0.0)

    assert elsewhere not in client.requested
    assert report["pfe_listing"]["barrier"] == cli.OFF_DOMAIN_REDIRECT
    assert report["pfe_listing"]["redirect_target_host"] == "tracker.example.com"


def test_an_ordinary_run_is_unaffected_by_the_redirect_robots_gate() -> None:
    client = _FakeClient(_routes())

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] == "COMPLETED"
    assert len(client.requested) == 8
    assert report["detail_audit"]["sampled"] == 3
    assert report["pfe_listing"]["barrier"] is None


def test_robots_itself_is_fetched_before_any_rules_exist_to_gate_it() -> None:
    """robots.txt has no rules to check against yet; a same-host move still works."""
    moved = f"{HOST}/robots-v2.txt"
    routes = _routes_with_private()
    routes[moved] = (200, ROBOTS_WITH_PRIVATE, "text/plain")
    client = _FakeClient(routes, redirects={ROBOTS_URL: moved})

    report = cli.run(client=client, delay=0.0)

    assert moved in client.requested
    assert report["robots"]["available"] is True


# --------- Architect review: no unsupported recency claim in the sample -----


def test_the_sample_strategy_is_described_as_reproducible_not_as_newest() -> None:
    """A higher ID is not evidence of a later publication, and must not read so."""
    strategy = DETAIL_SAMPLE_STRATEGY.lower()

    assert "deterministic" in strategy or "reproducible" in strategy
    assert "newest" not in strategy
    assert "recent" not in strategy or "not evidence of a more recent" in strategy

    report = cli.run(client=_FakeClient(_routes()), delay=0.0)
    assert report["detail_sample_strategy"] == DETAIL_SAMPLE_STRATEGY
    assert "newest" not in report["detail_audit"]["strategy"].lower()


def test_no_module_docstring_claims_id_order_proves_recency() -> None:
    from evaluation.morocco_pfe import stagiaires_access

    for text in (
        stagiaires_access.__doc__ or "",
        stagiaires_access.select_detail_sample.__doc__ or "",
        cli.__doc__ or "",
    ):
        assert "newest" not in text.lower()


# --------- Architect review: --terms-url is the one supplied URL ------------


def test_terms_url_is_the_only_operator_supplied_url_and_is_same_host_only() -> None:
    """It exists, it is not a discovery input, and it cannot leave the host."""
    options = set(cli.build_parser()._option_string_actions)
    assert "--terms-url" in options
    assert "--sitemap-url" not in options

    client = _FakeClient(_routes())
    report = cli.run(
        client=client, delay=0.0, terms_url="https://example.com/conditions"
    )

    assert report["terms"]["barrier"] == "NOT_ATTEMPTED_OFF_DOMAIN"
    assert not any("example.com" in item for item in client.requested)


def test_a_terms_url_never_becomes_a_discovery_source() -> None:
    """Whatever it returns, it contributes no sitemap and no offer."""
    routes = _routes()
    terms = f"{HOST}/conditions"
    # Even if the terms URL served a sitemap index, it must not be read as one.
    routes[terms] = (200, SITEMAP_INDEX, "application/xml")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0, terms_url=terms)

    assert report["sitemap_index"]["selected"] == f"{HOST}/sitemap_v9.xml"
    assert report["sitemap_index"]["selected_from"] == "robots.txt"
    assert report["offers"]["accepted_offer_urls"] == 11
    assert report["terms"]["manual_review_required"] is True


def test_a_terms_url_robots_disallows_is_not_fetched() -> None:
    routes = _routes_with_private()
    forbidden = f"{HOST}/private/conditions"
    routes[forbidden] = (200, "<html><body>" + "t" * 500 + "</body></html>", "text/html")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0, terms_url=forbidden)

    assert forbidden not in client.requested
    assert report["terms"]["barrier"] == "ROBOTS_DISALLOWED"
    assert report["terms"]["manual_review_required"] is True


# ====================================================================
# Architect review: the audit must fail closed
# ====================================================================

#: The complete request history of a healthy run, in order. Tests below assert
#: against the *whole* list rather than "X not in requested", so a regression
#: that keeps knocking on other doors after a wall shows up as an extra entry.
HEALTHY_REQUESTS = [
    ROBOTS_URL,
    PFE_TARGET_URL,
    f"{HOST}/sitemap_v9.xml",
    f"{HOST}/offre-sitemap.xml",
    f"{HOST}/offre-sitemap2.xml",
    f"{OFFER}/6107-slug-7",
    f"{OFFER}/6106-slug-6",
    f"{OFFER}/6105-slug-5",
]


class _FailingClient(_FakeClient):
    """A fake client that raises a transport error for one nominated URL."""

    def __init__(self, routes, failing_url: str, **kwargs) -> None:
        super().__init__(routes, **kwargs)
        self.failing_url = failing_url

    def get(self, url: str, timeout: float | None = None, follow_redirects: bool = False):
        if url == self.failing_url:
            self.requested.append(url)
            raise httpx.ConnectTimeout("synthetic transport failure")
        return super().get(url, timeout, follow_redirects)


# ------------ P1: stop at the PFE listing wall, before discovery ------------


def test_a_listing_403_stops_the_audit_before_any_discovery_request() -> None:
    """The finding: a refused listing did not stop the sitemap chain.

    Recording the barrier and then requesting the index, both offer sitemaps and
    three detail pages is not "stopping at the wall" — it is knocking on every
    other door in the building.
    """
    routes = _routes()
    routes[PFE_TARGET_URL] = (403, "", "text/html")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0)

    # The complete history: robots, the listing, and nothing else.
    assert client.requested == [ROBOTS_URL, PFE_TARGET_URL]
    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER
    assert report["discovery_attempted"] is False
    assert "sitemap_index" not in report
    assert "offers" not in report
    assert "detail_audit" not in report


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, ""),
        (429, ""),
        (401, ""),
        (200, "<html><body>Just a moment... checking your browser</body></html>"),
    ],
)
def test_every_kind_of_listing_access_barrier_stops_discovery(
    status: int, body: str
) -> None:
    routes = _routes()
    routes[PFE_TARGET_URL] = (status, body, "text/html")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0)

    assert client.requested == [ROBOTS_URL, PFE_TARGET_URL]
    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER


def test_a_refused_listing_redirect_stops_discovery_too() -> None:
    """The redirect destination is unfetched *and* nothing is tried after it."""
    elsewhere = "https://tracker.example.com/landing"
    client = _FakeClient(_routes(), redirects={PFE_TARGET_URL: elsewhere})

    report = cli.run(client=client, delay=0.0)

    assert client.requested == [ROBOTS_URL, PFE_TARGET_URL]
    assert elsewhere not in client.requested
    assert report["pfe_listing"]["barrier"] == cli.OFF_DOMAIN_REDIRECT
    assert report["discovery_attempted"] is False
    assert report["exit_code"] == cli.EXIT_BARRIER


def test_a_robots_disallowed_listing_redirect_stops_discovery_too() -> None:
    secret = f"{HOST}/private/secret"
    client = _FakeClient(_routes_with_private(), redirects={PFE_TARGET_URL: secret})

    report = cli.run(client=client, delay=0.0)

    assert client.requested == [ROBOTS_URL, PFE_TARGET_URL]
    assert report["pfe_listing"]["barrier"] == cli.ROBOTS_DISALLOWED_REDIRECT
    assert report["exit_code"] == cli.EXIT_BARRIER


def test_an_unreachable_listing_is_an_incomplete_audit_not_a_completed_one() -> None:
    """A transport failure is not an access refusal, and neither is success."""
    client = _FailingClient(_routes(), failing_url=PFE_TARGET_URL)

    report = cli.run(client=client, delay=0.0)

    assert client.requested == [ROBOTS_URL, PFE_TARGET_URL]
    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE
    assert report["discovery_attempted"] is False


def test_a_healthy_listing_leaves_the_discovery_chain_exactly_as_it_was() -> None:
    """Requirement 3: HTTP 200 behaviour is untouched by the stop rule."""
    client = _FakeClient(_routes())

    report = cli.run(client=client, delay=0.0)

    assert client.requested == HEALTHY_REQUESTS
    assert report["discovery_attempted"] is True
    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["exit_code"] == cli.EXIT_OK
    assert report["detail_audit"]["sampled"] == 3


# ---------- P1: an unreadable required offer sitemap is not COMPLETED -------


def test_an_offer_sitemap_transport_failure_is_never_a_completed_audit() -> None:
    """Partial totals must not be presented as the whole."""
    client = _FailingClient(_routes(), failing_url=f"{HOST}/offre-sitemap2.xml")

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] != cli.OUTCOME_COMPLETED
    assert report["exit_code"] != cli.EXIT_OK
    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE
    assert report["incomplete_reasons"] == [
        f"OFFER_SITEMAP_UNREADABLE:{HOST}/offre-sitemap2.xml"
    ]


def test_an_unparsable_offer_sitemap_is_never_a_completed_audit() -> None:
    routes = _routes()
    routes[f"{HOST}/offre-sitemap2.xml"] = (
        200,
        "<html><body><h1>Erreur</h1><p>" + "pas un sitemap " * 40 + "</p></body></html>",
        "text/html",
    )

    report = cli.run(client=_FakeClient(routes), delay=0.0)

    assert report["outcome"] == cli.OUTCOME_INCOMPLETE
    assert report["exit_code"] == cli.EXIT_FAILURE


def test_a_refused_offer_sitemap_is_a_barrier_not_merely_incomplete() -> None:
    """Being refused and being unable to read are different facts."""
    routes = _routes()
    routes[f"{HOST}/offre-sitemap2.xml"] = (403, "", "text/html")

    report = cli.run(client=_FakeClient(routes), delay=0.0)

    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER
    assert "HTTP_403_FORBIDDEN" in report["barriers"]


def test_evidence_from_the_readable_sitemap_survives_the_failure() -> None:
    """Requirement 3: a failed sibling must not discard what we did read."""
    client = _FailingClient(_routes(), failing_url=f"{HOST}/offre-sitemap2.xml")

    report = cli.run(client=client, delay=0.0)

    assert report["offers"]["accepted_offer_urls"] == 8
    assert report["offers"]["unique_source_external_ids"] == 8
    assert report["detail_audit"]["sampled"] == 3


def test_the_report_says_plainly_that_partial_totals_are_partial() -> None:
    """Requirement 4: a reader of the numbers must not mistake them for the whole."""
    client = _FailingClient(_routes(), failing_url=f"{HOST}/offre-sitemap2.xml")

    offers = cli.run(client=client, delay=0.0)["offers"]

    assert offers["required_sitemaps_all_read"] is False
    assert offers["totals_are_partial"] is True
    assert offers["totals_note"].startswith("PARTIAL")
    assert offers["offer_sitemaps_declared"] == 2
    assert offers["offer_sitemaps_parsed"] == 1
    assert offers["offer_sitemaps_unreadable"] == 1
    assert offers["offer_sitemaps_refused"] == 0


def test_all_sitemaps_readable_reports_complete_totals_and_exit_zero() -> None:
    """Requirement 5: the healthy path is unchanged."""
    offers = cli.run(client=_FakeClient(_routes()), delay=0.0)["offers"]

    assert offers["required_sitemaps_all_read"] is True
    assert offers["totals_are_partial"] is False
    assert offers["totals_note"].startswith("COMPLETE")
    assert offers["offer_sitemaps_declared"] == 2
    assert offers["offer_sitemaps_parsed"] == 2


# ------------- P2: a checked terms URL's barrier reaches the top ------------


def test_a_supplied_terms_url_answering_403_fails_the_audit() -> None:
    """The finding: a refused terms page left the audit reporting success."""
    routes = _routes()
    routes[f"{HOST}/conditions"] = (403, "", "text/html")

    report = cli.run(
        client=_FakeClient(routes), delay=0.0, terms_url=f"{HOST}/conditions"
    )

    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER
    assert "HTTP_403_FORBIDDEN" in report["barriers"]
    assert report["terms"]["attempted"] is True
    assert report["terms"]["manual_review_required"] is True


def test_a_supplied_terms_url_hitting_a_refused_redirect_fails_the_audit() -> None:
    elsewhere = "https://tracker.example.com/cgu"
    terms = f"{HOST}/conditions"
    client = _FakeClient(_routes(), redirects={terms: elsewhere})

    report = cli.run(client=client, delay=0.0, terms_url=terms)

    assert elsewhere not in client.requested
    assert report["terms"]["barrier"] == cli.OFF_DOMAIN_REDIRECT
    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER


def test_no_terms_url_supplied_is_normal_and_never_fails_the_audit() -> None:
    """Distinction A: not supplying one is the ordinary case, not a finding."""
    report = cli.run(client=_FakeClient(_routes()), delay=0.0)

    assert report["terms"]["barrier"] == "NOT_ATTEMPTED_NO_EVIDENCED_TERMS_URL"
    assert report["terms"]["attempted"] is False
    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["exit_code"] == cli.EXIT_OK


@pytest.mark.parametrize(
    ("terms_url", "expected_barrier"),
    [
        ("https://example.com/conditions", "NOT_ATTEMPTED_OFF_DOMAIN"),
        (f"{HOST}/private/conditions", "ROBOTS_DISALLOWED"),
    ],
)
def test_a_terms_url_we_declined_to_request_does_not_fail_the_audit(
    terms_url: str, expected_barrier: str
) -> None:
    """Distinctions B and C: declining to ask is us obeying a rule, not a refusal.

    Both leave the URL unfetched, and neither is a fact about the site, so
    neither may turn a good audit red.
    """
    routes = _routes_with_private()
    routes[f"{HOST}/private/conditions"] = (200, "t" * 500, "text/html")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0, terms_url=terms_url)

    assert terms_url not in client.requested
    assert report["terms"]["barrier"] == expected_barrier
    assert report["terms"]["attempted"] is False
    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["exit_code"] == cli.EXIT_OK


def test_a_healthy_terms_page_keeps_the_audit_successful() -> None:
    """Distinction D, and E: 200 is fine, and review is still required."""
    routes = _routes()
    routes[f"{HOST}/conditions"] = (
        200,
        "<html><body><h1>Conditions</h1><p>" + "texte " * 100 + "</p></body></html>",
        "text/html",
    )

    report = cli.run(
        client=_FakeClient(routes), delay=0.0, terms_url=f"{HOST}/conditions"
    )

    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["exit_code"] == cli.EXIT_OK
    assert report["terms"]["available"] is True
    assert report["terms"]["manual_review_required"] is True


def test_a_terms_url_still_never_becomes_a_discovery_input() -> None:
    """Distinction F, re-asserted now that terms can affect the outcome."""
    routes = _routes()
    terms = f"{HOST}/conditions"
    routes[terms] = (200, SITEMAP_INDEX, "application/xml")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0, terms_url=terms)

    assert report["sitemap_index"]["selected"] == f"{HOST}/sitemap_v9.xml"
    assert report["sitemap_index"]["selected_from"] == "robots.txt"
    assert report["offers"]["accepted_offer_urls"] == 11
    assert client.requested == HEALTHY_REQUESTS + [terms]


# --------------- P2: robots matches on path AND query -----------------------

ROBOTS_WITH_QUERY_RULE = (
    "User-agent: *\n"
    "Disallow: /*?download=true\n"
    "Allow: /\n"
    "\n"
    "Sitemap: https://www.stagiaires.ma/sitemap_v9.xml\n"
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.stagiaires.ma/foo", "/foo"),
        ("https://www.stagiaires.ma/foo?a=1", "/foo?a=1"),
        ("https://www.stagiaires.ma/?download=true", "/?download=true"),
        ("https://www.stagiaires.ma?download=true", "/?download=true"),
        ("https://www.stagiaires.ma", "/"),
        ("https://www.stagiaires.ma/foo#frag", "/foo"),
    ],
)
def test_the_robots_match_target_is_path_plus_query(url: str, expected: str) -> None:
    assert robots_target(url) == expected


def test_a_query_only_disallow_rule_is_actually_enforced() -> None:
    """The finding: matching only the path ignored every query-based rule."""
    groups = parse_robots_txt(ROBOTS_WITH_QUERY_RULE)

    blocked = robots_target("https://www.stagiaires.ma/allowed?download=true")
    assert robots_verdict(groups, blocked).allowed is False
    assert robots_verdict(groups, robots_target(f"{HOST}/allowed")).allowed is True
    assert robots_verdict(groups, robots_target(f"{HOST}/allowed?a=1")).allowed is True


def test_a_redirect_into_a_query_disallowed_url_is_not_followed() -> None:
    """Requirement 2: the rule holds on the URL a redirect hands us."""
    blocked = f"{HOST}/allowed?download=true"
    routes = _routes()
    routes[ROBOTS_URL] = (200, ROBOTS_WITH_QUERY_RULE, "text/plain")
    routes[blocked] = (200, "<html><body>" + "x" * 500 + "</body></html>", "text/html")
    client = _FakeClient(routes, redirects={PFE_TARGET_URL: blocked})

    report = cli.run(client=client, delay=0.0)

    assert blocked not in client.requested
    assert client.requested == [ROBOTS_URL, PFE_TARGET_URL]
    assert report["pfe_listing"]["barrier"] == cli.ROBOTS_DISALLOWED_REDIRECT
    assert report["pfe_listing"]["redirect_target"] == blocked


def test_a_terms_url_with_a_disallowed_query_is_not_fetched() -> None:
    """Requirement 3: the same helper governs the operator-supplied URL."""
    blocked = f"{HOST}/conditions?download=true"
    routes = _routes()
    routes[ROBOTS_URL] = (200, ROBOTS_WITH_QUERY_RULE, "text/plain")
    routes[blocked] = (200, "t" * 500, "text/html")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0, terms_url=blocked)

    assert blocked not in client.requested
    assert report["terms"]["barrier"] == "ROBOTS_DISALLOWED"
    assert report["terms"]["attempted"] is False


def test_urls_with_a_permitted_query_are_unaffected() -> None:
    """Requirement 4: adding query matching must not block ordinary URLs."""
    routes = _routes()
    routes[ROBOTS_URL] = (200, ROBOTS_WITH_QUERY_RULE, "text/plain")
    client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0)

    assert client.requested == HEALTHY_REQUESTS
    assert report["outcome"] == cli.OUTCOME_COMPLETED
    assert report["exit_code"] == cli.EXIT_OK


# ------------------ failure semantics are internally consistent -------------


@pytest.mark.parametrize(
    "scenario",
    [
        "healthy",
        "listing_403",
        "listing_unreachable",
        "sitemap_unreadable",
        "sitemap_refused",
        "terms_403",
        "robots_refused",
        "no_sitemap_declared",
    ],
)
def test_outcome_and_exit_code_never_contradict_each_other(scenario: str) -> None:
    """One precedence, applied everywhere: no COMPLETED/exit-1 or BARRIER/exit-0."""
    routes = _routes()
    kwargs: dict[str, object] = {}
    client: _FakeClient
    if scenario == "listing_403":
        routes[PFE_TARGET_URL] = (403, "", "text/html")
        client = _FakeClient(routes)
    elif scenario == "listing_unreachable":
        client = _FailingClient(routes, failing_url=PFE_TARGET_URL)
    elif scenario == "sitemap_unreadable":
        client = _FailingClient(routes, failing_url=f"{HOST}/offre-sitemap2.xml")
    elif scenario == "sitemap_refused":
        routes[f"{HOST}/offre-sitemap2.xml"] = (403, "", "text/html")
        client = _FakeClient(routes)
    elif scenario == "terms_403":
        routes[f"{HOST}/conditions"] = (403, "", "text/html")
        kwargs["terms_url"] = f"{HOST}/conditions"
        client = _FakeClient(routes)
    elif scenario == "robots_refused":
        routes[ROBOTS_URL] = (403, "", "text/html")
        client = _FakeClient(routes)
    elif scenario == "no_sitemap_declared":
        routes[ROBOTS_URL] = (200, "User-agent: *\nAllow: /\n", "text/plain")
        client = _FakeClient(routes)
    else:
        client = _FakeClient(routes)

    report = cli.run(client=client, delay=0.0, **kwargs)  # type: ignore[arg-type]
    outcome, exit_code = report["outcome"], report["exit_code"]

    assert (outcome == cli.OUTCOME_COMPLETED) == (exit_code == cli.EXIT_OK)
    if outcome == cli.OUTCOME_BARRIER:
        assert exit_code == cli.EXIT_BARRIER
    if outcome == cli.OUTCOME_INCOMPLETE:
        assert exit_code == cli.EXIT_FAILURE
    if exit_code == cli.EXIT_OK:
        assert not report.get("barriers")
        assert not report.get("incomplete_reasons")


def test_a_barrier_outranks_an_unreadable_sitemap() -> None:
    """Fixed precedence: refusal is the more important fact, and wins."""
    routes = _routes()
    routes[f"{HOST}/offre-sitemap.xml"] = (403, "", "text/html")
    client = _FailingClient(routes, failing_url=f"{HOST}/offre-sitemap2.xml")

    report = cli.run(client=client, delay=0.0)

    assert report["outcome"] == cli.OUTCOME_BARRIER
    assert report["exit_code"] == cli.EXIT_BARRIER
    assert report["barriers"] == ["HTTP_403_FORBIDDEN"]
    # The incomplete reason is still recorded, just outranked.
    assert report["incomplete_reasons"] == [
        f"OFFER_SITEMAP_UNREADABLE:{HOST}/offre-sitemap2.xml"
    ]
