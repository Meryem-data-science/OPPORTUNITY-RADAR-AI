"""The Phase 7A.1 geographic resolver, on strings alone.

No database is opened here and no posting is read: every string below is
invented or is the *shape* of a string the corpus holds, and the resolver is a
pure function over text. The operational `data/` database takes no part in any
test in this repository, and nothing here would be able to reach it.

Most of these tests are about what the resolver may **not** do: recognise a
country it was never given, read a city as a country outside its catalogue,
treat `MA` as Morocco when the corpus writes it for Massachusetts, or answer
anything but UNKNOWN for text that names no place.
"""

import pytest

from services.geography.models import GeographicError, ResolutionStatus
from services.geography.registry import (
    AMBIGUOUS_REGIONS,
    CITY_COUNTRIES,
    COUNTRY_ALIASES,
    normalize_geographic_text,
)
from services.geography.resolver import (
    AMBIGUOUS_REGION_RULE,
    CITY_CATALOGUE_RULE,
    COUNTRY_ALIAS_RULE,
    MULTIPLE_COUNTRIES_RULE,
    NO_SIGNAL_RULE,
    location_fingerprint,
    resolve_location_text,
    resolve_segment,
    split_segments,
)


def codes(text: str) -> list[str | None]:
    return [segment.country_code for segment in resolve_location_text(text)]


def statuses(text: str) -> list[ResolutionStatus]:
    return [segment.status for segment in resolve_location_text(text)]


# --------------------------------------------------------------------------
# The countries the phase must resolve
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Maroc", "MA"),
        ("Morocco", "MA"),
        ("Casablanca, Maroc", "MA"),
        ("Rabat, Morocco", "MA"),
        ("Paris, France", "FR"),
        ("London, UK", "GB"),
        ("London, United Kingdom", "GB"),
        ("Doha, Qatar", "QA"),
        ("Brussels, Belgium", "BE"),
        ("Dubai, UAE", "AE"),
        ("Austin, United States", "US"),
    ],
)
def test_a_named_country_resolves_to_its_iso_code(text, expected) -> None:
    resolution = resolve_segment(text)
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.country_code == expected
    assert resolution.rule_id == COUNTRY_ALIAS_RULE


def test_a_catalogued_moroccan_city_alone_names_its_country() -> None:
    """`Casablanca` with no country is an ordinary way to write a Moroccan post."""
    resolution = resolve_segment("Casablanca")
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.country_code == "MA"
    assert resolution.city_key == "casablanca"
    assert resolution.rule_id == CITY_CATALOGUE_RULE


def test_a_street_address_still_resolves_through_the_country_it_ends_with() -> None:
    resolution = resolve_segment(
        "Capital Tower, Boulevard Mly Youssef, Casablanca, Maroc"
    )
    assert resolution.country_code == "MA"
    assert resolution.city_key == "casablanca"
    assert resolution.rule_id == COUNTRY_ALIAS_RULE


def test_accents_and_periods_do_not_change_a_country() -> None:
    assert resolve_segment("Paris, U.K.").country_code == "GB"
    assert resolve_segment("Tétouan").country_code == "MA"
    assert resolve_segment("Tetouan").country_code == "MA"


# --------------------------------------------------------------------------
# AMBIGUOUS and UNKNOWN, which are two different answers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["APAC", "EMEA", "South East Asia", "Europe", "Worldwide"]
)
def test_an_area_that_is_not_one_country_is_ambiguous(text) -> None:
    resolution = resolve_segment(text)
    assert resolution.status is ResolutionStatus.AMBIGUOUS
    assert resolution.country_code is None
    assert resolution.rule_id == AMBIGUOUS_REGION_RULE


@pytest.mark.parametrize("text", ["Any Office", "Remote", "TBD", "Headquarters"])
def test_text_that_names_no_place_is_unknown(text) -> None:
    resolution = resolve_segment(text)
    assert resolution.status is ResolutionStatus.UNKNOWN
    assert resolution.country_code is None
    assert resolution.rule_id == NO_SIGNAL_RULE


def test_two_countries_in_one_segment_are_ambiguous_rather_than_a_pick() -> None:
    resolution = resolve_segment("France / Morocco")
    assert resolution.status is ResolutionStatus.AMBIGUOUS
    assert resolution.rule_id == MULTIPLE_COUNTRIES_RULE
    assert resolution.country_code is None


def test_remote_is_not_read_as_a_work_mode_or_as_a_place() -> None:
    """`work_mode` is Phase 3.5A's answer; this package never states one."""
    resolution = resolve_segment("Remote")
    assert resolution.status is ResolutionStatus.UNKNOWN
    assert "REMOTE" not in resolution.rule_id.upper()


# --------------------------------------------------------------------------
# The registry stays closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["MA", "CA", "NY", "IN", "DE", "San Francisco, CA"])
def test_a_bare_two_letter_state_code_is_never_read_as_a_country(text) -> None:
    """The corpus writes `San Francisco, CA` and `New York, NY` for US states.

    Reading `CA` as Canada or `MA` as Morocco would place postings in countries
    nobody named, and `MA` is the country this whole product targets — the one
    place a false positive would do the most damage.
    """
    assert resolve_segment(text).country_code is None


def test_a_city_never_resolves_a_country_outside_the_catalogue() -> None:
    """There is a Paris in Texas; the catalogue may not contain a guess."""
    assert resolve_segment("Paris").country_code is None
    assert resolve_segment("Springfield").country_code is None


def test_a_company_name_containing_a_country_does_not_move_the_posting() -> None:
    """Matching is by equality on whole parts, never by substring."""
    resolution = resolve_segment("France Telecom, Casablanca")
    assert resolution.country_code == "MA"
    assert resolution.rule_id == CITY_CATALOGUE_RULE


def test_every_registered_alias_is_normalised_and_maps_to_an_alpha_2_code() -> None:
    for catalogue in (COUNTRY_ALIASES, CITY_COUNTRIES):
        for alias, code in catalogue.items():
            assert alias == normalize_geographic_text(alias)
            assert len(code) == 2 and code.isupper()
    for region in AMBIGUOUS_REGIONS:
        assert region == normalize_geographic_text(region)
        assert region not in COUNTRY_ALIASES


def test_no_alias_is_a_bare_two_letter_token_except_the_deliberate_ones() -> None:
    two_letter = {alias for alias in COUNTRY_ALIASES if len(alias) == 2}
    assert two_letter == {"uk", "us"}


# --------------------------------------------------------------------------
# Multi-location strings
# --------------------------------------------------------------------------


def test_a_semicolon_separates_places_and_a_comma_does_not() -> None:
    assert split_segments("Doha, Qatar; London, UK") == ("Doha, Qatar", "London, UK")
    assert split_segments("Casablanca, Maroc") == ("Casablanca, Maroc",)


def test_multi_location_strings_resolve_to_one_row_per_place() -> None:
    assert codes("Casablanca, Maroc; Paris, France") == ["MA", "FR"]
    assert codes("Paris, France; London, UK") == ["FR", "GB"]
    assert codes("Doha, Qatar; London, UK; Dubai, UAE") == ["QA", "GB", "AE"]


def test_an_unresolvable_place_beside_a_resolved_one_keeps_both_answers() -> None:
    resolutions = resolve_location_text("Paris, France; APAC")
    assert [item.status for item in resolutions] == [
        ResolutionStatus.RESOLVED,
        ResolutionStatus.AMBIGUOUS,
    ]
    assert [item.country_code for item in resolutions] == ["FR", None]


def test_the_order_the_employer_wrote_is_preserved() -> None:
    assert [item.raw_segment for item in resolve_location_text(
        "Doha, Qatar; London, UK"
    )] == ["Doha, Qatar", "London, UK"]


def test_us_state_segments_are_unknown_rather_than_out_of_target() -> None:
    assert statuses("San Francisco, CA; New York, NY") == [
        ResolutionStatus.UNKNOWN,
        ResolutionStatus.UNKNOWN,
    ]


def test_a_string_of_separators_still_reads_as_one_segment() -> None:
    """Every source row produces at least one reading, or `sync` never settles."""
    assert split_segments(";") == (";",)
    assert resolve_segment(";").status is ResolutionStatus.UNKNOWN


def test_empty_text_is_refused_rather_than_resolved() -> None:
    with pytest.raises(GeographicError):
        split_segments("   ")
    with pytest.raises(GeographicError):
        resolve_segment("")


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_the_resolver_is_deterministic() -> None:
    text = "Casablanca, Maroc; Paris, France; APAC"
    assert resolve_location_text(text) == resolve_location_text(text)


def test_the_fingerprint_depends_on_the_text_and_only_on_the_text() -> None:
    assert location_fingerprint("Casablanca, Maroc") == location_fingerprint(
        "Casablanca, Maroc"
    )
    assert location_fingerprint("Casablanca, Maroc") != location_fingerprint(
        "Casablanca, Morocco"
    )
    assert len(location_fingerprint("Rabat")) == 64
