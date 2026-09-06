"""Offline tests for the Phase 7C.5B Stage.ma production parser.

Nothing here opens a socket. Every byte parsed is a compact synthetic fixture
written in this file: a test that needs the real site fails when the site is
slow, when a template is edited, or when CI has no egress, none of which says
anything about this code. The fixtures imitate the *shapes* the parser must cope
with — a card with a named employer, one deliberately anonymous, a footer that
must not leak, an epoch date, an expired banner — never real Stage.ma content.

The invariants worth breaking a build over: that one card's employer can never
become another's, that "Anonyme" never becomes a company, that `01/01/1970`
never becomes a publication date, and that the numeric ID never orders anything.
"""

from __future__ import annotations

import json

import pytest

from services.collector.parsers.stage_ma import (
    LISTING_URL,
    PARSER_VERSION,
    ROBOTS_ABSENT,
    ROBOTS_OBEY,
    ROBOTS_REFUSED,
    ROBOTS_UNRESOLVED,
    ROBOTS_URL,
    STATE_EXPIRED,
    STATE_PUBLISHED,
    STATE_UNKNOWN,
    STATE_UNPUBLISHED,
    StageMaPayloadError,
    access_barrier,
    application_url,
    canonical_url,
    classify_robots_response,
    extract_candidate_id,
    has_explicit_empty_state,
    is_offer_detail_url,
    is_organisation_url,
    is_stage_ma_host,
    is_usable_organization,
    job_posting,
    parse_listing,
    parse_robots_txt,
    posting_description,
    posting_location,
    posting_organization,
    posting_published_at,
    posting_title,
    publication_state,
    robots_allows,
    select_listing_targets,
)

HOST = "https://www.stage.ma"
OFFER = f"{HOST}/offres-stage"


def card(
    number: int,
    *,
    title: str = "Stage synthetique",
    organisation: str | None = "Entreprise Synthetique",
    organisation_id: int = 500,
    location: str | None = None,
    slug: str = "stage-synthetique",
) -> str:
    """One offer card: a detail anchor, optionally an employer anchor and a place."""
    parts = [f'<a href="/offres-stage/{number}-{slug}">{title}</a>']
    if organisation is not None:
        parts.append(f'<a href="/organismes/{organisation_id}-x">{organisation}</a>')
    if location is not None:
        parts.append(f'<span itemprop="addressLocality">{location}</span>')
    return f'<div class="card">{"".join(parts)}</div>'


def listing(*cards: str, footer: str = "") -> str:
    return (
        "<html><head><title>Informatique</title></head><body><main>"
        + "".join(cards)
        + "</main>"
        + (f'<footer><span itemprop="addressLocality">{footer}</span></footer>' if footer else "")
        + "</body></html>"
    )


def detail(
    *,
    title: str | None = "Stage PFE Intelligence Artificielle",
    organisation: str | None = "Entreprise Synthetique",
    locality: str | None = "Casablanca",
    description: str | None = "Description synthetique inventee pour ce test.",
    date_posted: str | None = "2026-03-23",
    valid_through: str | None = "2026-06-30",
    state_text: str = "",
    apply_markup: str = "",
    posting_url: str | None = None,
    posting: dict | None = None,
) -> str:
    if posting is None:
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
        if valid_through is not None:
            posting["validThrough"] = valid_through
        if posting_url is not None:
            posting["url"] = posting_url
    return (
        "<html><head><title>Offre</title>"
        f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        f"</head><body><h1>Offre</h1><p>{state_text}</p>{apply_markup}</body></html>"
    )


# ============================= DETAIL URL IDENTITY ==========================


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{OFFER}/9355-stagiaire-full-stack-developer", "9355"),
        (f"{OFFER}/9344", "9344"),
        (f"{OFFER}/9342-slug/", "9342"),
        (f"{OFFER}/007-slug", "007"),
    ],
)
def test_the_candidate_id_is_read_as_published(url: str, expected: str) -> None:
    """`007` stays `007`: normalizing it to `7` would invent an equality."""
    assert extract_candidate_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://autre.example.com/offres-stage/9355-a",
        f"{HOST}/blog/9355-a",
        f"{OFFER}/slug-sans-id",
        f"{OFFER}/93a55-mixte",
        f"{OFFER}/9355abc-colle",
        f"{OFFER}/",
        f"{HOST}/organismes/500-acme",
    ],
)
def test_an_invalid_or_foreign_shape_is_not_an_offer(url: str) -> None:
    assert extract_candidate_id(url) is None
    assert is_offer_detail_url(url) is False


def test_an_organisation_profile_is_never_an_offer_url() -> None:
    """Employer profiles are evidence about a card, never a collection target."""
    assert is_organisation_url(f"{HOST}/organismes/500-acme") is True
    assert is_offer_detail_url(f"{HOST}/organismes/500-acme") is False
    assert is_organisation_url("https://autre.example.com/organismes/1-a") is False


def test_canonicalization_drops_only_the_meaningless_parts() -> None:
    assert canonical_url(f"HTTPS://WWW.Stage.MA/Offres-Stage/9355-A/?x=1#f") == (
        f"{HOST}/Offres-Stage/9355-A"
    )
    assert is_stage_ma_host(f"{OFFER}/1-a") is True
    assert is_stage_ma_host("https://stage.ma.evil.example/1") is False
    with pytest.raises(StageMaPayloadError):
        canonical_url("   ")


# ================================ LISTING ORDER =============================


def test_the_listing_preserves_document_order() -> None:
    """Not the numeric ID: that would smuggle in a recency claim."""
    html = listing(card(1000), card(9999), card(5000))

    assert [entry.source_external_id for entry in parse_listing(html)] == [
        "1000",
        "9999",
        "5000",
    ]


def test_a_duplicate_offer_url_is_kept_once_and_the_first_wins() -> None:
    html = listing(
        card(9355, title="Premier", organisation="Vrai Employeur"),
        card(9355, title="Deuxieme", organisation="Autre Employeur"),
    )
    entries = parse_listing(html)

    assert len(entries) == 1
    assert entries[0].listing_title == "Premier"
    assert entries[0].listing_organization == "Vrai Employeur"


def test_the_bound_is_applied_after_deduplication() -> None:
    html = listing(card(1), card(1), card(2), card(3))
    entries = parse_listing(html)

    assert len(entries) == 3
    assert [item.source_external_id for item in select_listing_targets(entries, 2)] == [
        "1",
        "2",
    ]


def test_the_bound_never_reorders_by_numeric_id() -> None:
    html = listing(card(1000), card(9999))

    assert [
        item.source_external_id
        for item in select_listing_targets(parse_listing(html), 1)
    ] == ["1000"]


@pytest.mark.parametrize("limit", [0, -1, True, False, 2.5, "3", None])
def test_an_invalid_bound_is_refused(limit) -> None:
    with pytest.raises(StageMaPayloadError):
        select_listing_targets(parse_listing(listing(card(1))), limit)


# ============================ LISTING ORGANIZATION ==========================


def test_a_same_card_employer_anchor_supplies_the_organization() -> None:
    entries = parse_listing(listing(card(9355, organisation="ACME SARL")))

    assert entries[0].listing_organization == "ACME SARL"


def test_an_employer_from_another_card_cannot_leak_in() -> None:
    """The invariant card scoping exists for: employers stay in their own card."""
    html = listing(
        card(9355, organisation="Premier Employeur"),
        card(9344, organisation="Second Employeur"),
    )
    entries = parse_listing(html)

    assert entries[0].listing_organization == "Premier Employeur"
    assert entries[1].listing_organization == "Second Employeur"


def test_a_card_without_an_employer_anchor_gets_no_organization() -> None:
    html = listing(
        card(9355, organisation=None),
        card(9344, organisation="Voisin SARL"),
    )
    entries = parse_listing(html)

    assert entries[0].listing_organization is None
    assert entries[1].listing_organization == "Voisin SARL"


@pytest.mark.parametrize("name", ["Anonyme", "anonyme", "ANONYME", "Anonymous", "---", "-", " - . - "])
def test_an_anonymous_or_separator_value_is_not_an_organization(name: str) -> None:
    """The site publishes anonymous offers on purpose; that is not a name."""
    assert is_usable_organization(name) is False
    assert parse_listing(listing(card(9355, organisation=name)))[0].listing_organization is None


@pytest.mark.parametrize(
    "name",
    ["ACME", "Groupe Anonyme-Tech", "A-B", "Société Générale", "3D Systems", "X.Y Consulting"],
)
def test_a_real_company_name_is_never_discarded(name: str) -> None:
    """The placeholder check is deliberately narrow: a wrong reject is a lost offer."""
    assert is_usable_organization(name) is True


def test_arbitrary_nearby_text_does_not_become_an_organization() -> None:
    html = listing(
        '<div class="card">'
        '<a href="/offres-stage/9355-a">Stage</a>'
        "<span>Publié il y a 2 jours</span><span>Casablanca</span>"
        "</div>"
    )

    assert parse_listing(html)[0].listing_organization is None


# ============================== LISTING TITLE ===============================


def test_the_offer_anchor_text_is_the_listing_title() -> None:
    entries = parse_listing(listing(card(9355, title="Stagiaire Full Stack")))

    assert entries[0].listing_title == "Stagiaire Full Stack"


def test_an_anchor_without_text_falls_back_to_its_title_attribute() -> None:
    html = listing(
        '<div><a href="/offres-stage/9355-a" title="Stage IA"><img src="/x.png"></a></div>'
    )

    assert parse_listing(html)[0].listing_title == "Stage IA"


# ============================= LISTING LOCATION =============================


def test_an_explicit_same_card_location_is_captured() -> None:
    entries = parse_listing(listing(card(9355, location="Casablanca")))

    assert entries[0].listing_location == "Casablanca"


def test_a_location_from_another_card_cannot_leak_in() -> None:
    html = listing(
        card(9355, location=None),
        card(9344, location="Rabat"),
    )
    entries = parse_listing(html)

    assert entries[0].listing_location is None
    assert entries[1].listing_location == "Rabat"


def test_the_site_footer_never_becomes_every_offers_location() -> None:
    """A page carries one address of its own; it is not where the jobs are."""
    html = listing(card(9355), card(9344), footer="Casablanca")

    for entry in parse_listing(html):
        assert entry.listing_location is None


def test_a_missing_location_stays_none() -> None:
    assert parse_listing(listing(card(9355)))[0].listing_location is None


# ===================== REAL LISTING SHAPE (review fix) ======================


def real_card(
    number: int,
    *,
    title: str,
    slug: str,
    organisation: str | None,
    organisation_id: int = 500,
    organisation_as_anchor: bool = True,
    location: str | None = None,
) -> str:
    """A card shaped the way the live Informatique listing really renders one.

    The detail is the whole point: Stage.ma links each offer **twice** from its
    own card, once from the title and once from a "+ Voir Offre de Stage" call
    to action, both at the same `/offres-stage/<id>-<slug>`. Counting anchors
    made that card look like two offers, so the boundary collapsed onto the
    anchor itself and the employer anchor sitting next to it was never inside
    the card. Every organization came back `None` against the real page while
    every synthetic single-anchor fixture passed.

    The nesting is deliberately deeper than the fixtures above, because a card
    boundary read from the document's structure has to survive real markup, and
    the class names here are decoration - nothing in the parser reads them.
    """
    inner = [f'<h3 class="offer-title"><a href="/offres-stage/{number}-{slug}">{title}</a></h3>']
    if organisation is not None:
        if organisation_as_anchor:
            inner.append(
                f'<a class="org" href="/organismes/{organisation_id}-slug">{organisation}</a>'
            )
        else:
            inner.append(f'<span class="org">{organisation}</span>')
    if location is not None:
        inner.append(f'<span itemprop="addressLocality">{location}</span>')
    inner.append(
        f'<a class="cta" href="/offres-stage/{number}-{slug}">+ Voir Offre de Stage</a>'
    )
    return (
        '<div class="offer-item"><div class="offer-inner"><div class="offer-body">'
        + "".join(inner)
        + "</div></div></div>"
    )


CARD_A = real_card(
    9355,
    title="Stagiaire Full Stack Developer",
    slug="stage-a",
    organisation="ACME",
    organisation_id=500,
)
CARD_B = real_card(
    9344,
    title="Stage B",
    slug="stage-b",
    organisation="BETA",
    organisation_id=600,
)


def test_the_real_listing_shape_yields_one_entry_per_offer() -> None:
    """Two cards, four offer anchors, two offers."""
    entries = parse_listing(listing(CARD_A, CARD_B))

    assert len(entries) == 2
    assert [entry.source_external_id for entry in entries] == ["9355", "9344"]


def test_the_real_listing_shape_keeps_each_card_its_own_employer() -> None:
    entries = parse_listing(listing(CARD_A, CARD_B))

    assert entries[0].listing_organization == "ACME"
    assert entries[1].listing_organization == "BETA"


def test_the_repeated_call_to_action_never_replaces_the_title() -> None:
    """The first meaningful anchor text is the title; "+ Voir Offre de Stage"
    is navigation furniture and says nothing about the offer."""
    entries = parse_listing(listing(CARD_A, CARD_B))

    assert entries[0].listing_title == "Stagiaire Full Stack Developer"
    assert entries[1].listing_title == "Stage B"
    for entry in entries:
        assert "Voir Offre" not in (entry.listing_title or "")


def test_the_duplicate_link_inside_a_card_does_not_duplicate_the_entry() -> None:
    urls = [entry.canonical_url for entry in parse_listing(listing(CARD_A, CARD_B))]

    assert urls == [f"{OFFER}/9355-stage-a", f"{OFFER}/9344-stage-b"]
    assert len(set(urls)) == len(urls)


def test_the_real_listing_shape_still_preserves_document_order() -> None:
    """A is 9355 and B is 9344, so a numeric sort would swap them. Nothing here
    sorts: the higher id stays first because the page put it first."""
    forward = parse_listing(listing(CARD_A, CARD_B))
    reversed_page = parse_listing(listing(CARD_B, CARD_A))

    assert [entry.source_external_id for entry in forward] == ["9355", "9344"]
    assert [entry.source_external_id for entry in reversed_page] == ["9344", "9355"]


def test_no_employer_leaks_between_two_real_shaped_cards() -> None:
    """Card A carries no employer at all; B's must not fill the gap."""
    html = listing(
        real_card(9355, title="Stage A", slug="stage-a", organisation=None),
        CARD_B,
    )
    entries = parse_listing(html)

    assert entries[0].listing_organization is None
    assert entries[1].listing_organization == "BETA"


def test_no_location_leaks_between_two_real_shaped_cards() -> None:
    html = listing(
        real_card(9355, title="Stage A", slug="stage-a", organisation="ACME"),
        real_card(
            9344, title="Stage B", slug="stage-b", organisation="BETA", location="Rabat"
        ),
    )
    entries = parse_listing(html)

    assert entries[0].listing_location is None
    assert entries[1].listing_location == "Rabat"


@pytest.mark.parametrize("as_anchor", [True, False])
def test_a_real_shaped_anonymous_card_invents_no_employer(as_anchor: bool) -> None:
    """The site publishes anonymous offers on purpose. Now that the card
    boundary is right, "Anonyme" is actually *seen* - and must still be
    rejected, whether the template links it or merely prints it."""
    html = listing(
        real_card(
            9355,
            title="Stage anonyme",
            slug="stage-a",
            organisation="Anonyme",
            organisation_as_anchor=as_anchor,
        ),
        CARD_B,
    )
    entries = parse_listing(html)

    assert len(entries) == 2
    assert entries[0].listing_title == "Stage anonyme"
    assert entries[0].listing_organization is None
    assert entries[1].listing_organization == "BETA"


def test_a_card_holding_two_distinct_offers_attributes_its_employer_to_neither() -> None:
    """The boundary is one distinct offer identity, not one anchor. A wrapper
    around two different offers is not a card, so the employer inside it
    belongs to no offer rather than to both."""
    html = listing(
        '<div class="offer-item">'
        '<a class="org" href="/organismes/500-shared">Ambigu SARL</a>'
        '<a href="/offres-stage/9355-stage-a">Stage A</a>'
        '<a href="/offres-stage/9344-stage-b">Stage B</a>'
        "</div>"
    )
    entries = parse_listing(html)

    assert len(entries) == 2
    assert [entry.listing_organization for entry in entries] == [None, None]


# =============================== EMPTY STATE ================================


@pytest.mark.parametrize(
    "text",
    [
        "Aucune Offre de Stage",
        "aucune offre disponible",
        "Aucun résultat",
        "No offers found here",
    ],
)
def test_an_explicit_empty_state_is_recognized(text: str) -> None:
    assert has_explicit_empty_state(f"<html><body><p>{text}</p></body></html>") is True


def test_a_page_with_no_links_and_no_empty_state_is_not_an_empty_listing() -> None:
    """Silence is what a redesign looks like too, so silence is not zero offers."""
    html = "<html><body><main><h1>Informatique</h1></main></body></html>"

    assert has_explicit_empty_state(html) is False
    assert parse_listing(html) == ()


def test_a_listing_with_offers_is_not_an_empty_state() -> None:
    assert has_explicit_empty_state(listing(card(9355))) is False


# ================================ JOBPOSTING ================================


def test_a_parseable_job_posting_is_found() -> None:
    posting = job_posting(detail())

    assert posting is not None
    assert posting_title(posting) == "Stage PFE Intelligence Artificielle"
    assert posting_organization(posting) == "Entreprise Synthetique"
    assert posting_location(posting) == "Casablanca"
    assert posting_description(posting).startswith("Description synthetique")


def test_a_malformed_json_ld_block_does_not_hide_a_later_valid_one() -> None:
    html = (
        '<html><head><script type="application/ld+json">{not json}</script>'
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","title":"Stage"}</script></head><body></body></html>'
    )

    assert posting_title(job_posting(html)) == "Stage"


def test_a_page_without_a_job_posting_returns_none() -> None:
    assert job_posting("<html><body><h1>Offre</h1></body></html>") is None
    assert job_posting('<script type="application/ld+json">{"@type":"Article"}</script>') is None


def test_an_anonymous_posting_organization_is_not_usable() -> None:
    """Filtered here so the listing card gets its turn as a fallback."""
    assert posting_organization(job_posting(detail(organisation="Anonyme"))) is None
    assert posting_organization(job_posting(detail(organisation="---"))) is None


def test_a_posting_without_a_location_yields_none() -> None:
    assert posting_location(job_posting(detail(locality=None))) is None


# ================================== STATE ===================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Offre expirée", STATE_EXPIRED),
        ("Cette offre n'est plus disponible", STATE_EXPIRED),
        ("Offre non publiée", STATE_UNPUBLISHED),
        ("En attente de validation", STATE_UNPUBLISHED),
        ("Publiée le 23/03/2026", STATE_PUBLISHED),
        ("Date de publication : 23/03/2026", STATE_PUBLISHED),
        ("", STATE_UNKNOWN),
        ("Un texte sans indication", STATE_UNKNOWN),
    ],
)
def test_the_state_comes_from_the_pages_own_words(text: str, expected: str) -> None:
    assert publication_state(detail(state_text=text)) == expected


def test_expiry_wins_over_a_publication_label_on_the_same_page() -> None:
    """"Publiée le 3 mars — offre expirée" describes a closed offer."""
    assert publication_state(
        detail(state_text="Publiée le 03/03/2026 — Offre expirée")
    ) == STATE_EXPIRED


def test_unpublished_wins_over_a_publication_label() -> None:
    assert publication_state(
        detail(state_text="Publiée le 03/03/2026 — Non publiée")
    ) == STATE_UNPUBLISHED


def test_an_unknown_state_is_not_expired() -> None:
    assert publication_state(detail(state_text="")) == STATE_UNKNOWN


# ==================== REAL UNPUBLISHED PAGE (review fix) ====================


#: The sentence the live Stage.ma detail page really renders on a hidden offer,
#: typographic apostrophes and all.
REAL_UNPUBLISHED_NOTICE = (
    "Cette offre de stage n\u2019est pas publi\u00e9e. "
    "Seul le recruteur et l\u2019administrateur peuvent la visualiser."
)


def unpublished_detail(notice: str = REAL_UNPUBLISHED_NOTICE) -> str:
    """The real combination a hidden Stage.ma offer shows, in one page.

    Both halves are on purpose. The notice is the only truthful signal; further
    down, the same page prints "Publiee le 01/01/1970" - a database column that
    was never set, rendered as an ordinary publication label. Read the label
    alone and a hidden offer becomes a live one dated 1970, which is how this
    fixture earned its place: the parser scored exactly that before the fix.

    Everything else about the page is deliberately valid - a complete
    `JobPosting`, a real title, a named employer - so that nothing but the
    notice can be what keeps the offer out.
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
        "description": "Description synthetique inventee pour ce test.",
        "datePosted": "01/01/1970",
    }
    return (
        "<html><head><title>Offre</title>"
        f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        "</head><body>"
        "<h1>Stagiaire Full Stack Developer</h1>"
        f'<div class="alert">{notice}</div>'
        "<p>Publi\u00e9e le 01/01/1970</p>"
        "<p>Ville : Casablanca</p>"
        "</body></html>"
    )


def test_the_real_unpublished_notice_beats_the_epoch_publication_label() -> None:
    """The page carries both; only one of them is true."""
    assert publication_state(unpublished_detail()) == STATE_UNPUBLISHED


@pytest.mark.parametrize(
    "notice",
    [
        REAL_UNPUBLISHED_NOTICE,
        "Cette offre de stage n'est pas publi\u00e9e. "
        "Seul le recruteur et l'administrateur peuvent la visualiser.",
        "Cette offre de stage n'est pas publiee. "
        "Seul le recruteur et l'administrateur peuvent la visualiser.",
        "Cette offre de stage n\u2018est pas publi\u00e9e.",
        "Cette offre de stage n\u00b4est pas publi\u00e9e.",
        "CETTE OFFRE DE STAGE N\u2019EST PAS PUBLI\u00c9E.",
    ],
)
def test_the_notice_is_read_through_its_typographic_variants(notice: str) -> None:
    """One page may spell the apostrophe four ways and drop the accents; the
    offer is hidden in every one of them."""
    assert publication_state(unpublished_detail(notice)) == STATE_UNPUBLISHED


def test_the_epoch_label_on_that_page_never_becomes_a_publication_date() -> None:
    """Independently of the state: `01/01/1970` is not a date this parser keeps."""
    assert posting_published_at({"datePosted": "01/01/1970"}) is None
    assert posting_published_at(job_posting(unpublished_detail())) is None


def test_the_rest_of_the_hidden_page_is_otherwise_perfectly_valid() -> None:
    """So the test above can only be passing because of the notice."""
    posting = job_posting(unpublished_detail())

    assert posting_title(posting) == "Stagiaire Full Stack Developer"
    assert posting_organization(posting) == "ACME SARL"
    assert posting_location(posting) == "Casablanca"


# =================================== DATE ===================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-03-23", "2026-03-23"), ("23/03/2026", "2026-03-23"), ("23-03-2026", "2026-03-23")],
)
def test_a_valid_date_posted_becomes_the_published_at(value: str, expected: str) -> None:
    assert posting_published_at({"datePosted": value}) == expected


@pytest.mark.parametrize("value", ["01/01/1970", "1970-01-01", "1/1/1970", "01-01-1970"])
def test_an_epoch_sentinel_never_becomes_a_publication_date(value: str) -> None:
    """An unset date column renders as 1970 far more often than sites admit."""
    assert posting_published_at({"datePosted": value}) is None


@pytest.mark.parametrize("value", ["", "   ", "pas une date", "32/13/2026", "0000-00-00", None, 17])
def test_a_missing_or_malformed_date_yields_none(value) -> None:
    assert posting_published_at({"datePosted": value}) is None


def test_no_other_field_can_stand_in_for_a_missing_date_posted() -> None:
    """Not validThrough, not a start date, not the id, not the crawl time."""
    posting = {
        "@type": "JobPosting",
        "title": "Stage",
        "validThrough": "2026-06-30",
        "jobStartDate": "2026-04-01",
        "identifier": "9355",
        "dateModified": "2026-03-30",
    }

    assert posting_published_at(posting) is None


def test_the_numeric_id_is_never_read_as_a_date() -> None:
    assert posting_published_at({"datePosted": "9355"}) is None


# =============================== APPLICATION ================================


@pytest.mark.parametrize(
    "markup",
    [
        '<a href="/postuler/9355">Postuler</a>',
        '<a href="/postuler/9355">Candidater maintenant</a>',
        '<a href="/postuler/9355">Apply now</a>',
    ],
)
def test_an_explicit_same_host_application_href_is_kept(markup: str) -> None:
    assert application_url(markup, f"{OFFER}/9355-a") == f"{HOST}/postuler/9355"


def test_an_off_domain_application_href_is_recorded_as_a_string() -> None:
    """Recording a URL is not requesting it: an employer's ATS is a real target."""
    assert application_url(
        '<a href="https://ats.example.com/apply/1">Postuler</a>', f"{OFFER}/9355-a"
    ) == "https://ats.example.com/apply/1"


@pytest.mark.parametrize(
    "markup",
    [
        '<a href="javascript:void(0)">Postuler</a>',
        '<a href="#">Postuler</a>',
        "<a>Postuler</a>",
        '<a href="mailto:rh@x.test">Postuler</a>',
        "<button>Postuler</button>",
    ],
)
def test_a_control_without_a_usable_href_yields_no_application_url(markup: str) -> None:
    assert application_url(markup, f"{OFFER}/9355-a") is None


def test_the_posting_url_and_canonical_are_never_an_application_url() -> None:
    """Every offer has a canonical URL; treating one as an apply link would
    claim a way to apply for offers that may have none."""
    html = detail(posting_url=f"{OFFER}/9355-a")

    assert application_url(html, f"{OFFER}/9355-a") is None


def test_a_link_inside_the_description_is_not_an_application_url() -> None:
    html = detail(
        description="Voir https://exemple.test/offre pour plus d'informations",
        apply_markup='<a href="/organismes/500-acme">Entreprise Synthetique</a>',
    )

    assert application_url(html, f"{OFFER}/9355-a") is None


def test_a_navigation_link_is_not_an_application_link() -> None:
    assert application_url(
        '<a href="/">Accueil</a><a href="/offres-stage">Toutes les offres</a>',
        f"{OFFER}/9355-a",
    ) is None


# ================================== ROBOTS ==================================


def test_a_parseable_robots_is_obeyed() -> None:
    assert classify_robots_response(200, ROBOTS_URL, "User-agent: *\nAllow: /\n") == (
        ROBOTS_OBEY
    )


@pytest.mark.parametrize(("status", "expected"), [
    (404, ROBOTS_ABSENT), (410, ROBOTS_ABSENT),
    (401, ROBOTS_REFUSED), (403, ROBOTS_REFUSED),
    (407, ROBOTS_REFUSED), (429, ROBOTS_REFUSED),
    (500, ROBOTS_UNRESOLVED), (503, ROBOTS_UNRESOLVED), (418, ROBOTS_UNRESOLVED),
])
def test_each_robots_status_maps_to_its_disposition(status: int, expected: str) -> None:
    assert classify_robots_response(status, ROBOTS_URL, "") == expected


def test_a_challenge_page_served_as_robots_is_a_refusal() -> None:
    assert classify_robots_response(
        200, ROBOTS_URL, "Just a moment... checking your browser"
    ) == ROBOTS_REFUSED


def test_robots_matches_on_path_and_query() -> None:
    groups = parse_robots_txt("User-agent: *\nDisallow: /*?print=1\n")

    assert robots_allows(groups, f"{OFFER}/9355-a?print=1") is False
    assert robots_allows(groups, f"{OFFER}/9355-a") is True


def test_an_empty_disallow_never_cancels_a_site_wide_disallow() -> None:
    groups = parse_robots_txt("User-agent: *\nDisallow: /\nDisallow:\n")

    assert robots_allows(groups, f"{HOST}/anything") is False


def test_a_group_naming_the_collector_wins_over_the_wildcard() -> None:
    groups = parse_robots_txt(
        "User-agent: *\nDisallow: /\n\nUser-agent: OpportunityRadarAI-Collector\nAllow: /\n"
    )

    assert robots_allows(groups, LISTING_URL) is True


def test_all_applicable_groups_are_merged() -> None:
    groups = parse_robots_txt(
        "User-agent: *\nAllow: /\n\nUser-agent: *\nDisallow: /offres-stage/\n"
    )

    assert robots_allows(groups, f"{OFFER}/9355-a") is False
    assert robots_allows(groups, LISTING_URL) is True


@pytest.mark.parametrize("status", [401, 403, 407, 429, 500, 503])
def test_a_refusal_or_server_error_is_named_a_barrier(status: int) -> None:
    assert access_barrier(status, LISTING_URL, "") is not None


def test_a_missing_page_is_not_named_a_barrier() -> None:
    """404 is a lifecycle fact; what it means depends on which page it was."""
    assert access_barrier(404, LISTING_URL, "page introuvable") is None
    assert access_barrier(200, LISTING_URL, "<html></html>") is None


def test_the_parser_version_is_pinned() -> None:
    assert PARSER_VERSION == "stage-ma-html-v1"
