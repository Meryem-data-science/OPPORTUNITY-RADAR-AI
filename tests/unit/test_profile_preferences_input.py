"""Unit coverage for the Phase 3.4C explicit input models and canonical codec.

Every value below is invented. No real availability, no real address, no real
constraint and no real career objective takes part, and nothing here opens a
database: these tests are about what may be *said* and how it is *written*.

Half of them are about what the package refuses. The other half are about the
one property everything downstream rests on: one statement has exactly one
encoding, whatever order it was typed in.
"""

import json

import pytest

from services.digital_twin.preferences.codec import (
    canonical_json,
    decode_availability,
    decode_career_objectives,
    decode_mobility,
    decode_preferences,
    encode_availability,
    encode_career_objectives,
    encode_mobility,
    encode_preferences,
)
from services.digital_twin.preferences.models import (
    EXPLICIT_PROFILE_INPUT_VERSION,
    AvailabilityPreference,
    AvailabilityStatus,
    CareerObjectives,
    ConventionStatus,
    ExplicitProfileInputError,
    MobilityPreference,
    MobilityScope,
    OpportunityPreferences,
    OpportunityType,
    VisaSponsorshipRequired,
    WorkMode,
)

# TEST ONLY wordings, invented for these tests.
TEST_ONLY_LOCATIONS = ("Ville Exemple", "Autre Ville")
TEST_ONLY_DOMAINS = ("Domaine fictif", "Autre domaine")
TEST_ONLY_CONSTRAINTS = ("Contrainte inventée",)
TEST_ONLY_OBJECTIVES = ("Objectif inventé", "Second objectif inventé")


def a_preference(**overrides) -> OpportunityPreferences:
    """A complete, valid preference, so each test overrides only its subject."""
    fields = {
        "opportunity_types": (OpportunityType.PFE,),
        "work_modes": (WorkMode.HYBRID,),
        "preferred_domains": TEST_ONLY_DOMAINS,
        "convention_status": ConventionStatus.UNKNOWN,
        "visa_sponsorship_required": VisaSponsorshipRequired.UNKNOWN,
        "constraints": TEST_ONLY_CONSTRAINTS,
    }
    fields.update(overrides)
    return OpportunityPreferences(**fields)


# --------------------------------------------------------------------------
# The contract version
# --------------------------------------------------------------------------


def test_the_input_version_is_the_documented_one() -> None:
    assert EXPLICIT_PROFILE_INPUT_VERSION == "explicit-profile-input-v1"


# --------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------


def test_canonical_json_sorts_keys_and_pads_nothing() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def test_canonical_json_keeps_accented_text_as_itself() -> None:
    # Escaped copies of somebody's words are harder to read back and no safer.
    assert canonical_json(["Données"]) == '["Données"]'


def test_canonical_json_is_stable_across_equal_payloads() -> None:
    first = canonical_json({"a": 1, "b": 2})
    second = canonical_json({"b": 2, "a": 1})
    assert first == second


def test_every_encoding_round_trips_through_its_decoder() -> None:
    availability = AvailabilityPreference(AvailabilityStatus.AVAILABLE_FROM, "2030-01-15")
    mobility = MobilityPreference(MobilityScope.RESTRICTED, TEST_ONLY_LOCATIONS)
    preferences = a_preference()
    objectives = CareerObjectives(TEST_ONLY_OBJECTIVES)

    assert decode_availability(encode_availability(availability)) == availability
    assert decode_mobility(encode_mobility(mobility)) == mobility
    assert decode_preferences(encode_preferences(preferences)) == preferences
    assert decode_career_objectives(encode_career_objectives(objectives)) == objectives


def test_encoding_is_byte_stable_across_calls() -> None:
    first = encode_preferences(a_preference())
    second = encode_preferences(a_preference())
    assert first == second


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


def test_available_now_encodes_a_null_date() -> None:
    encoded = encode_availability(AvailabilityPreference(AvailabilityStatus.AVAILABLE_NOW))
    assert encoded == '{"available_from":null,"status":"AVAILABLE_NOW"}'


def test_available_from_encodes_the_typed_date() -> None:
    encoded = encode_availability(
        AvailabilityPreference(AvailabilityStatus.AVAILABLE_FROM, "2030-01-15")
    )
    assert encoded == '{"available_from":"2030-01-15","status":"AVAILABLE_FROM"}'


def test_available_now_refuses_a_date() -> None:
    with pytest.raises(ExplicitProfileInputError):
        AvailabilityPreference(AvailabilityStatus.AVAILABLE_NOW, "2030-01-15")


def test_available_from_requires_a_date() -> None:
    with pytest.raises(ExplicitProfileInputError):
        AvailabilityPreference(AvailabilityStatus.AVAILABLE_FROM)


@pytest.mark.parametrize(
    "written",
    ("2030-1-15", "20300115", "15/01/2030", "2030-01-15T00:00:00", "bientôt", ""),
)
def test_a_date_that_is_not_written_yyyy_mm_dd_is_refused(written: str) -> None:
    # Refused, never reshaped: a package that repairs a date decides what
    # somebody meant by it.
    with pytest.raises(ExplicitProfileInputError):
        AvailabilityPreference(AvailabilityStatus.AVAILABLE_FROM, written)


def test_a_date_the_calendar_does_not_have_is_refused() -> None:
    with pytest.raises(ExplicitProfileInputError):
        AvailabilityPreference(AvailabilityStatus.AVAILABLE_FROM, "2030-02-30")


def test_an_availability_status_outside_the_registry_is_refused() -> None:
    with pytest.raises(ExplicitProfileInputError):
        AvailabilityPreference("MAYBE_LATER")


def test_a_past_date_is_the_persons_statement_and_is_kept() -> None:
    # Nothing here reads the clock, so "in the past" is not a category this
    # package has, and a date is never compared to today.
    availability = AvailabilityPreference(AvailabilityStatus.AVAILABLE_FROM, "1999-01-01")
    assert availability.available_from == "1999-01-01"


# --------------------------------------------------------------------------
# Mobility
# --------------------------------------------------------------------------


def test_open_mobility_accepts_an_empty_list() -> None:
    mobility = MobilityPreference(MobilityScope.OPEN)
    assert mobility.locations == ()
    assert encode_mobility(mobility) == '{"locations":[],"scope":"OPEN"}'


def test_open_mobility_may_still_name_locations() -> None:
    mobility = MobilityPreference(MobilityScope.OPEN, TEST_ONLY_LOCATIONS)
    assert mobility.locations == TEST_ONLY_LOCATIONS


def test_restricted_mobility_keeps_the_named_locations_in_order() -> None:
    mobility = MobilityPreference(MobilityScope.RESTRICTED, TEST_ONLY_LOCATIONS)
    assert mobility.locations == TEST_ONLY_LOCATIONS


def test_restricted_mobility_without_a_location_is_refused() -> None:
    with pytest.raises(ExplicitProfileInputError):
        MobilityPreference(MobilityScope.RESTRICTED, ())


def test_locations_are_trimmed_and_exact_duplicates_dropped() -> None:
    mobility = MobilityPreference(
        MobilityScope.RESTRICTED,
        ("  Ville Exemple  ", "Autre Ville", "Ville Exemple"),
    )
    assert mobility.locations == TEST_ONLY_LOCATIONS


def test_deduplication_is_case_sensitive_and_never_fuzzy() -> None:
    # Deciding that two spellings are one place would be fuzzy matching, and
    # this project does none.
    mobility = MobilityPreference(MobilityScope.RESTRICTED, ("Ville", "ville", "VILLE"))
    assert mobility.locations == ("Ville", "ville", "VILLE")


def test_an_empty_location_is_refused_rather_than_dropped() -> None:
    with pytest.raises(ExplicitProfileInputError):
        MobilityPreference(MobilityScope.RESTRICTED, ("Ville Exemple", "   "))


def test_a_single_text_is_not_a_list_of_locations() -> None:
    with pytest.raises(ExplicitProfileInputError):
        MobilityPreference(MobilityScope.RESTRICTED, "Ville Exemple")


def test_no_location_is_ever_added_to_what_was_typed() -> None:
    # No country from a city, no city from a country, no region expanded.
    mobility = MobilityPreference(MobilityScope.RESTRICTED, ("Ville Exemple",))
    assert mobility.locations == ("Ville Exemple",)
    assert json.loads(encode_mobility(mobility))["locations"] == ["Ville Exemple"]


# --------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------


def test_the_opportunity_type_registry_is_the_documented_v1_one() -> None:
    assert tuple(member.value for member in OpportunityType) == (
        "PFA",
        "PFE",
        "SUMMER_INTERNSHIP",
        "PRE_HIRE_INTERNSHIP",
        "ALTERNANCE",
        "INTERNSHIP",
        "FIRST_JOB",
        "JUNIOR_ROLE",
    )


def test_the_work_mode_registry_is_the_documented_one() -> None:
    assert tuple(member.value for member in WorkMode) == ("ON_SITE", "HYBRID", "REMOTE")


def test_the_convention_registry_is_the_documented_one() -> None:
    assert tuple(member.value for member in ConventionStatus) == (
        "UNKNOWN",
        "AVAILABLE",
        "NOT_AVAILABLE",
    )


def test_the_visa_registry_is_the_documented_one() -> None:
    assert tuple(member.value for member in VisaSponsorshipRequired) == (
        "UNKNOWN",
        "YES",
        "NO",
    )


def test_registry_lists_are_deduplicated_into_declaration_order() -> None:
    # A closed registry has a canonical order of its own, so the same set typed
    # two ways is one statement rather than a correction.
    preferences = a_preference(opportunity_types=("PFE", "PFA", "PFE"))
    assert preferences.opportunity_types == (OpportunityType.PFA, OpportunityType.PFE)


def test_the_same_registry_set_typed_in_two_orders_encodes_identically() -> None:
    first = encode_preferences(a_preference(work_modes=("REMOTE", "ON_SITE")))
    second = encode_preferences(a_preference(work_modes=("ON_SITE", "REMOTE")))
    assert first == second


def test_an_opportunity_type_outside_the_registry_is_refused() -> None:
    with pytest.raises(ExplicitProfileInputError):
        a_preference(opportunity_types=("CDI",))


def test_a_work_mode_outside_the_registry_is_refused() -> None:
    with pytest.raises(ExplicitProfileInputError):
        a_preference(work_modes=("FULL_REMOTE",))


def test_a_value_outside_the_registry_is_never_mapped_to_the_nearest_member() -> None:
    with pytest.raises(ExplicitProfileInputError):
        a_preference(work_modes=("remote",))


def test_opportunity_types_must_name_at_least_one_value() -> None:
    with pytest.raises(ExplicitProfileInputError):
        a_preference(opportunity_types=())


def test_work_modes_must_name_at_least_one_value() -> None:
    with pytest.raises(ExplicitProfileInputError):
        a_preference(work_modes=())


def test_preferred_domains_and_constraints_may_be_empty() -> None:
    preferences = a_preference(preferred_domains=(), constraints=())
    assert preferences.preferred_domains == ()
    assert preferences.constraints == ()


def test_unknown_convention_and_visa_are_the_defaults_and_are_valid() -> None:
    preferences = OpportunityPreferences(
        opportunity_types=(OpportunityType.PFE,), work_modes=(WorkMode.REMOTE,)
    )
    assert preferences.convention_status is ConventionStatus.UNKNOWN
    assert preferences.visa_sponsorship_required is VisaSponsorshipRequired.UNKNOWN


def test_unknown_is_encoded_as_unknown_and_never_as_no() -> None:
    payload = json.loads(encode_preferences(a_preference()))
    assert payload["convention_status"] == "UNKNOWN"
    assert payload["visa_sponsorship_required"] == "UNKNOWN"
    assert payload["visa_sponsorship_required"] != "NO"


def test_domains_and_constraints_keep_the_persons_case_and_wording() -> None:
    preferences = a_preference(
        preferred_domains=("  Data Engineering  ",), constraints=("  Pas le week-end  ",)
    )
    assert preferences.preferred_domains == ("Data Engineering",)
    assert preferences.constraints == ("Pas le week-end",)


def test_domains_are_deduplicated_keeping_the_first_occurrence() -> None:
    preferences = a_preference(
        preferred_domains=("Domaine fictif", "Autre domaine", "Domaine fictif")
    )
    assert preferences.preferred_domains == TEST_ONLY_DOMAINS


def test_a_preference_holds_only_what_was_typed() -> None:
    # No domain arrives from a skill, a project or a CV section, because the
    # constructor has no other input than its arguments.
    preferences = a_preference(preferred_domains=("Domaine fictif",))
    assert preferences.preferred_domains == ("Domaine fictif",)


# --------------------------------------------------------------------------
# Career objectives
# --------------------------------------------------------------------------


def test_objectives_keep_their_order_and_are_trimmed() -> None:
    objectives = CareerObjectives(("  Objectif inventé  ", "Second objectif inventé"))
    assert objectives.objectives == TEST_ONLY_OBJECTIVES


def test_objectives_are_deduplicated_deterministically() -> None:
    objectives = CareerObjectives(
        ("Objectif inventé", "Second objectif inventé", "Objectif inventé")
    )
    assert objectives.objectives == TEST_ONLY_OBJECTIVES


def test_an_empty_objective_list_is_refused() -> None:
    # An empty list is not "no objectives": that is UNKNOWN, and UNKNOWN is an
    # absent fact rather than an empty array somebody could read as an answer.
    with pytest.raises(ExplicitProfileInputError):
        CareerObjectives(())


def test_a_blank_objective_is_refused() -> None:
    with pytest.raises(ExplicitProfileInputError):
        CareerObjectives(("   ",))


# --------------------------------------------------------------------------
# What the decoders refuse
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    (
        "not json",
        "[]",
        '"AVAILABLE_NOW"',
        "null",
        '{"status":"AVAILABLE_NOW"}',
        '{"available_from":null,"status":"AVAILABLE_NOW","note":"x"}',
        '{"available_from":null,"status":"WHENEVER"}',
        '{"available_from":"2030-01-15","status":"AVAILABLE_NOW"}',
        '{"status": "AVAILABLE_NOW", "available_from": null}',
        '{"status":"AVAILABLE_NOW","available_from":null}',
    ),
)
def test_a_non_canonical_or_unknown_availability_value_is_refused(value: str) -> None:
    with pytest.raises(ExplicitProfileInputError):
        decode_availability(value)


@pytest.mark.parametrize(
    "value",
    (
        '{"locations":[],"scope":"RESTRICTED"}',
        '{"locations":null,"scope":"OPEN"}',
        '{"locations":[1],"scope":"OPEN"}',
        '{"locations":["Ville","Ville"],"scope":"OPEN"}',
        '{"locations":["  Ville  "],"scope":"OPEN"}',
        '{"locations":[],"scope":"ANYWHERE"}',
        '{"scope":"OPEN","locations":[]}',
    ),
)
def test_a_non_canonical_or_unknown_mobility_value_is_refused(value: str) -> None:
    with pytest.raises(ExplicitProfileInputError):
        decode_mobility(value)


def test_a_preference_value_naming_an_unknown_type_is_refused() -> None:
    broken = encode_preferences(a_preference()).replace('"PFE"', '"CDI"')
    with pytest.raises(ExplicitProfileInputError):
        decode_preferences(broken)


def test_a_preference_value_missing_a_key_is_refused() -> None:
    payload = json.loads(encode_preferences(a_preference()))
    del payload["constraints"]
    with pytest.raises(ExplicitProfileInputError):
        decode_preferences(canonical_json(payload))


def test_a_preference_value_whose_registry_order_is_wrong_is_refused() -> None:
    # It parses and every member is known, but this package would never have
    # written it, so reading it would quietly re-canonicalize somebody's words.
    payload = json.loads(encode_preferences(a_preference(work_modes=("ON_SITE", "REMOTE"))))
    payload["work_modes"] = ["REMOTE", "ON_SITE"]
    with pytest.raises(ExplicitProfileInputError):
        decode_preferences(canonical_json(payload))


def test_an_empty_objectives_value_is_refused() -> None:
    with pytest.raises(ExplicitProfileInputError):
        decode_career_objectives('{"objectives":[]}')


def test_the_encoders_refuse_anything_that_is_not_their_own_model() -> None:
    with pytest.raises(ExplicitProfileInputError):
        encode_availability({"status": "AVAILABLE_NOW"})
    with pytest.raises(ExplicitProfileInputError):
        encode_mobility({"scope": "OPEN"})
    with pytest.raises(ExplicitProfileInputError):
        encode_preferences({})
    with pytest.raises(ExplicitProfileInputError):
        encode_career_objectives(["Objectif inventé"])
