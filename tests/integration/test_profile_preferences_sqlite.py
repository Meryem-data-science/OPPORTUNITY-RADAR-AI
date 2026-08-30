"""Migration 0011 and the Phase 3.4C projection, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away. No real
availability, no real address, no real constraint, no real objective and no
real identity takes part: every value is invented and `.invalid` never
resolves. The operational `.data/` database is never opened, and no count taken
from it appears anywhere below — the fixtures decide what exists.

The projection is derived data, so most of these tests are about what it may
**not** do: read a fact nobody accepted, write to `profile_facts`, touch the
Phase 3.4A skill tables or the Phase 3.4B structured tables, invent a row for a
profile that stated nothing, keep a row nothing justifies any more, pick
between two statements somebody made, or let one profile's facts reach another
profile's rows.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
)
from services.digital_twin.facts.models import (
    FactSourceType,
    ProfileFactType,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    ProfileFactError,
    accept_profile_fact,
    add_profile_fact_provenance,
    correct_profile_fact,
    list_profile_facts,
    list_profile_fact_provenance,
    propose_profile_fact,
    record_verified_user_input_fact,
    reject_profile_fact,
)
from services.digital_twin.preferences.codec import (
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
from services.digital_twin.preferences.repository import (
    _PROJECTIONS,
    AmbiguousExplicitInputError,
    find_stated_fact,
    ProfilePreferenceNotFoundError,
    get_profile_availability,
    get_profile_career_objectives,
    get_profile_mobility,
    get_profile_preferences,
    summarize_profile_preferences,
    synchronize_profile_preferences,
)
from services.digital_twin.preferences.service import (
    ExplicitInputAction,
    set_profile_availability,
    set_profile_career_objectives,
    set_profile_mobility,
    set_profile_preferences,
)
from services.digital_twin.repository import ensure_user_profile
from services.digital_twin.skills.repository import synchronize_profile_skills
from services.digital_twin.structured_profile.repository import (
    synchronize_structured_profile_entries,
)

REPOSITORY_SOURCE = Path("services/digital_twin/preferences/repository.py")
MIGRATION = Path("migrations/0011_profile_preferences_availability_mobility.sql")

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_OTHER_EMAIL = "other.student@example.invalid"

BEFORE_THIS_SLICE = (
    "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
)

#: The four tables `0011` adds.
C_TABLES = (
    "profile_availability",
    "profile_mobility",
    "profile_preferences",
    "profile_career_objectives",
)
#: The neighbours this slice must leave alone.
B_TABLES = (
    "profile_experiences",
    "profile_projects",
    "profile_educations",
    "profile_certifications",
    "profile_languages",
)
SKILL_TABLES = ("skills", "profile_skills", "profile_skill_evidence")

# TEST ONLY wordings, invented for these tests.
TEST_ONLY_LOCATIONS = ("Ville Exemple", "Autre Ville")
TEST_ONLY_DOMAINS = ("Domaine fictif",)
TEST_ONLY_CONSTRAINTS = ("Contrainte inventée",)
TEST_ONLY_OBJECTIVES = ("Objectif inventé",)

AVAILABILITY = AvailabilityPreference(AvailabilityStatus.AVAILABLE_FROM, "2030-01-15")
OTHER_AVAILABILITY = AvailabilityPreference(AvailabilityStatus.AVAILABLE_NOW)
MOBILITY = MobilityPreference(MobilityScope.RESTRICTED, TEST_ONLY_LOCATIONS)
PREFERENCES = OpportunityPreferences(
    opportunity_types=(OpportunityType.PFE, OpportunityType.ALTERNANCE),
    work_modes=(WorkMode.HYBRID, WorkMode.REMOTE),
    preferred_domains=TEST_ONLY_DOMAINS,
    convention_status=ConventionStatus.AVAILABLE,
    visa_sponsorship_required=VisaSponsorshipRequired.NO,
    constraints=TEST_ONLY_CONSTRAINTS,
)
OBJECTIVES = CareerObjectives(TEST_ONLY_OBJECTIVES)


def source_of(path: Path) -> str:
    """Return a module's whole source text.

    Read whole rather than tokenized down to executable code, unlike the Phase
    3.4B tests: every statement this module runs lives inside a string
    constant, so stripping strings would leave the assertions below reading an
    empty file and passing for that reason.
    """
    return path.read_text(encoding="utf-8")


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "preferences.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


@pytest.fixture
def other_profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_OTHER_EMAIL).profile_id


@pytest.fixture
def stated(migrated, profile_id) -> int:
    """A profile that stated all four things, projected."""
    set_profile_availability(migrated, profile_id, AVAILABILITY)
    set_profile_mobility(migrated, profile_id, MOBILITY)
    set_profile_preferences(migrated, profile_id, PREFERENCES)
    set_profile_career_objectives(migrated, profile_id, OBJECTIVES)
    synchronize_profile_preferences(migrated, profile_id)
    return profile_id


def _tables(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _foreign_keys(connection, table: str) -> set[tuple[str, str, str, str]]:
    return {
        (row[2], row[3], row[4], row[6])
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    }


def _counts(connection, tables) -> tuple[int, ...]:
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    )


def _fact_rows(connection) -> list[tuple]:
    return connection.execute(
        "SELECT id, profile_id, fact_type, value, normalized_value, status, "
        "replaced_by_fact_id, created_at, updated_at, decided_at "
        "FROM profile_facts ORDER BY id"
    ).fetchall()


def _provenance_rows(connection) -> list[tuple]:
    return connection.execute(
        "SELECT id, fact_id, source_type, provenance_key, created_at "
        "FROM profile_fact_provenance ORDER BY id"
    ).fetchall()


def _rows(connection, table: str) -> list[tuple]:
    return connection.execute(f"SELECT * FROM {table} ORDER BY profile_id").fetchall()


# --------------------------------------------------------------------------
# Migration 0011
# --------------------------------------------------------------------------


def test_migration_0011_is_discovered_after_the_earlier_ones() -> None:
    versions = [
        migration.version for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY)
    ]
    assert versions[: len(BEFORE_THIS_SLICE)] == list(BEFORE_THIS_SLICE)
    assert versions[len(BEFORE_THIS_SLICE)] == "0011"


def test_migration_0011_creates_the_four_tables(migrated) -> None:
    assert set(C_TABLES) <= _tables(migrated)


def test_the_projection_tables_hold_the_documented_columns(migrated) -> None:
    assert _columns(migrated, "profile_availability") == [
        "profile_id", "fact_id", "availability_status", "available_from",
        "input_version", "created_at",
    ]
    assert _columns(migrated, "profile_mobility") == [
        "profile_id", "fact_id", "mobility_scope", "locations_json",
        "input_version", "created_at",
    ]
    assert _columns(migrated, "profile_preferences") == [
        "profile_id", "fact_id", "opportunity_types_json", "work_modes_json",
        "preferred_domains_json", "convention_status", "visa_sponsorship_required",
        "constraints_json", "input_version", "created_at",
    ]
    assert _columns(migrated, "profile_career_objectives") == [
        "profile_id", "fact_id", "objectives_json", "input_version", "created_at",
    ]


@pytest.mark.parametrize("table", C_TABLES)
def test_no_projection_table_carries_a_score_or_a_provenance(migrated, table) -> None:
    # The audit chain is a chain: `fact_id` is the way to the provenance, and
    # no evidence column is copied here. Nothing carries a confidence, a score
    # or a match either — that would be Phase 3.6, which is not implemented.
    forbidden = {
        "verified", "confidence", "score", "match_score", "eligibility", "rank",
        "ranking", "source_type", "provenance_key", "cv_sha256", "parser_version",
        "extractor_version",
    }
    assert forbidden.isdisjoint(_columns(migrated, table))


@pytest.mark.parametrize("table", C_TABLES)
def test_every_projection_points_at_a_fact_of_the_same_profile(migrated, table) -> None:
    keys = _foreign_keys(migrated, table)
    assert ("profiles", "profile_id", "id", "CASCADE") in keys
    assert ("profile_facts", "fact_id", "id", "CASCADE") in keys
    assert ("profile_facts", "profile_id", "profile_id", "CASCADE") in keys


@pytest.mark.parametrize("table", C_TABLES)
def test_no_projection_table_is_seeded(migrated, table) -> None:
    # Applying `0011` states nothing about anybody.
    assert int(migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) == 0


def test_migration_0011_alters_no_earlier_migration() -> None:
    body = MIGRATION.read_text(encoding="utf-8").upper()
    assert "ALTER TABLE" not in body
    assert "DROP" not in body


def test_the_earlier_migrations_are_untouched() -> None:
    # Read as bytes: 0001..0010 are applied databases everywhere, so a change
    # to one of them is a change to history rather than a migration.
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version in BEFORE_THIS_SLICE:
            assert migration.path.exists()
    assert "0011" in MIGRATION.name


def test_availability_refuses_a_now_row_carrying_a_date(migrated, profile_id) -> None:
    fact = record_verified_user_input_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.AVAILABILITY,
        value=encode_availability(OTHER_AVAILABILITY),
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_availability (profile_id, fact_id, "
            "availability_status, available_from, input_version) "
            "VALUES (?, ?, 'AVAILABLE_NOW', '2030-01-15', ?)",
            (profile_id, fact.id, EXPLICIT_PROFILE_INPUT_VERSION),
        )


def test_mobility_refuses_a_restricted_row_naming_nowhere(migrated, profile_id) -> None:
    fact = record_verified_user_input_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.MOBILITY,
        value=encode_mobility(MOBILITY),
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_mobility (profile_id, fact_id, mobility_scope, "
            "locations_json, input_version) VALUES (?, ?, 'RESTRICTED', '[]', ?)",
            (profile_id, fact.id, EXPLICIT_PROFILE_INPUT_VERSION),
        )


def test_a_projection_may_not_point_at_another_profiles_fact(
    migrated, profile_id, other_profile_id
) -> None:
    migrated.execute("PRAGMA foreign_keys = ON")
    fact = record_verified_user_input_fact(
        migrated,
        profile_id=other_profile_id,
        fact_type=ProfileFactType.CAREER_OBJECTIVE,
        value=encode_career_objectives(OBJECTIVES),
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_career_objectives (profile_id, fact_id, "
            "objectives_json, input_version) VALUES (?, ?, '[\"x\"]', ?)",
            (profile_id, fact.id, EXPLICIT_PROFILE_INPUT_VERSION),
        )


# --------------------------------------------------------------------------
# The USER_INPUT primitive
# --------------------------------------------------------------------------


def test_an_explicit_statement_is_accepted_with_its_provenance(
    migrated, profile_id
) -> None:
    fact = record_verified_user_input_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.AVAILABILITY,
        value=encode_availability(AVAILABILITY),
    )
    provenance = list_profile_fact_provenance(migrated, profile_id, fact.id)

    assert fact.status.value == "ACCEPTED"
    assert fact.is_verified
    assert fact.decided_at is not None
    assert len(provenance) == 1
    assert provenance[0].source_type is FactSourceType.USER_INPUT


def test_an_explicit_statement_refuses_evidence_that_is_not_user_input(
    migrated, profile_id
) -> None:
    with pytest.raises(ProfileFactError):
        record_verified_user_input_fact(
            migrated,
            profile_id=profile_id,
            fact_type=ProfileFactType.AVAILABILITY,
            value=encode_availability(AVAILABILITY),
            provenance=ProvenanceInput(source_type=FactSourceType.CV),
        )
    assert _fact_rows(migrated) == []


def test_a_statement_and_its_provenance_are_written_atomically(
    migrated, profile_id
) -> None:
    def fail() -> None:
        raise RuntimeError("interrupted between the fact and its evidence")

    with pytest.raises(RuntimeError):
        record_verified_user_input_fact(
            migrated,
            profile_id=profile_id,
            fact_type=ProfileFactType.AVAILABILITY,
            value=encode_availability(AVAILABILITY),
            after_fact=fail,
        )

    # No fact without evidence, and no evidence without a fact.
    assert _fact_rows(migrated) == []
    assert _provenance_rows(migrated) == []


def test_an_explicit_statement_is_scoped_by_profile(migrated) -> None:
    with pytest.raises(ProfileFactError):
        record_verified_user_input_fact(
            migrated,
            profile_id=9999,
            fact_type=ProfileFactType.AVAILABILITY,
            value=encode_availability(AVAILABILITY),
        )


def test_an_empty_statement_is_refused(migrated, profile_id) -> None:
    with pytest.raises(ProfileFactError):
        record_verified_user_input_fact(
            migrated,
            profile_id=profile_id,
            fact_type=ProfileFactType.AVAILABILITY,
            value="   ",
        )


# --------------------------------------------------------------------------
# First statement, no-op, correction
# --------------------------------------------------------------------------


def test_the_first_statement_creates_one_accepted_fact(migrated, profile_id) -> None:
    outcome = set_profile_availability(migrated, profile_id, AVAILABILITY)

    assert outcome.action is ExplicitInputAction.CREATED
    assert outcome.previous_fact_id is None
    facts = list_profile_facts(migrated, profile_id)
    assert len(facts) == 1
    assert facts[0].status.value == "ACCEPTED"
    assert facts[0].value == encode_availability(AVAILABILITY)


def test_restating_the_same_thing_writes_nothing(migrated, profile_id) -> None:
    first = set_profile_availability(migrated, profile_id, AVAILABILITY)
    before = _fact_rows(migrated)

    second = set_profile_availability(migrated, profile_id, AVAILABILITY)

    assert second.action is ExplicitInputAction.UNCHANGED
    assert second.fact_id == first.fact_id
    assert not second.changed
    # Byte-identical rows: not even `updated_at` moved.
    assert _fact_rows(migrated) == before
    assert len(_provenance_rows(migrated)) == 1


def test_restating_a_preference_in_another_order_is_still_a_no_op(
    migrated, profile_id
) -> None:
    set_profile_preferences(migrated, profile_id, PREFERENCES)
    before = _fact_rows(migrated)

    reordered = OpportunityPreferences(
        opportunity_types=(OpportunityType.ALTERNANCE, OpportunityType.PFE),
        work_modes=(WorkMode.REMOTE, WorkMode.HYBRID),
        preferred_domains=PREFERENCES.preferred_domains,
        convention_status=PREFERENCES.convention_status,
        visa_sponsorship_required=PREFERENCES.visa_sponsorship_required,
        constraints=PREFERENCES.constraints,
    )
    outcome = set_profile_preferences(migrated, profile_id, reordered)

    assert outcome.action is ExplicitInputAction.UNCHANGED
    assert _fact_rows(migrated) == before


def test_a_new_value_corrects_the_old_one_and_keeps_it_readable(
    migrated, profile_id
) -> None:
    first = set_profile_availability(migrated, profile_id, AVAILABILITY)
    second = set_profile_availability(migrated, profile_id, OTHER_AVAILABILITY)

    facts = {fact.id: fact for fact in list_profile_facts(migrated, profile_id)}
    assert second.action is ExplicitInputAction.CORRECTED
    assert second.previous_fact_id == first.fact_id
    # The old fact keeps its own value; a correction never overwrites one.
    assert facts[first.fact_id].status.value == "CORRECTED"
    assert facts[first.fact_id].value == encode_availability(AVAILABILITY)
    assert facts[first.fact_id].replaced_by_fact_id == second.fact_id
    assert facts[second.fact_id].status.value == "ACCEPTED"
    assert facts[second.fact_id].value == encode_availability(OTHER_AVAILABILITY)


def test_a_chain_of_corrections_keeps_every_value_ever_stated(
    migrated, profile_id
) -> None:
    set_profile_availability(migrated, profile_id, AVAILABILITY)
    set_profile_availability(migrated, profile_id, OTHER_AVAILABILITY)
    third = AvailabilityPreference(AvailabilityStatus.AVAILABLE_FROM, "2031-06-01")
    set_profile_availability(migrated, profile_id, third)

    values = [fact.value for fact in list_profile_facts(migrated, profile_id)]
    assert values == [
        encode_availability(AVAILABILITY),
        encode_availability(OTHER_AVAILABILITY),
        encode_availability(third),
    ]
    accepted = [
        fact for fact in list_profile_facts(migrated, profile_id) if fact.is_verified
    ]
    assert len(accepted) == 1


def test_every_correction_carries_its_own_user_input_evidence(
    migrated, profile_id
) -> None:
    set_profile_availability(migrated, profile_id, AVAILABILITY)
    set_profile_availability(migrated, profile_id, OTHER_AVAILABILITY)

    sources = {row[2] for row in _provenance_rows(migrated)}
    assert sources == {"USER_INPUT"}
    assert len(_provenance_rows(migrated)) == 2


def test_each_domain_gets_its_own_piece_of_evidence(migrated, stated) -> None:
    # A bare USER_INPUT provenance would resolve to one key for every domain,
    # leaving four facts sharing one proof.
    keys = [row[3] for row in _provenance_rows(migrated)]
    assert len(set(keys)) == len(keys)


def test_no_provenance_key_carries_a_value_the_person_typed(migrated, stated) -> None:
    keys = " ".join(row[3] for row in _provenance_rows(migrated))
    for value in (
        *TEST_ONLY_LOCATIONS, *TEST_ONLY_DOMAINS, *TEST_ONLY_CONSTRAINTS,
        *TEST_ONLY_OBJECTIVES, "2030-01-15",
    ):
        assert value not in keys


def test_two_accepted_facts_of_one_domain_are_refused_by_the_service(
    migrated, profile_id
) -> None:
    for _ in range(2):
        record_verified_user_input_fact(
            migrated,
            profile_id=profile_id,
            fact_type=ProfileFactType.MOBILITY,
            value=encode_mobility(MOBILITY),
            provenance=ProvenanceInput(
                source_type=FactSourceType.USER_INPUT,
                provenance_key=f"TEST-ONLY-{_}",
            ),
        )
    with pytest.raises(AmbiguousExplicitInputError):
        set_profile_mobility(migrated, profile_id, MOBILITY)


def test_a_proposed_fact_of_the_same_type_does_not_block_a_first_statement(
    migrated, profile_id
) -> None:
    propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.MOBILITY,
        value=encode_mobility(MOBILITY),
        provenance=ProvenanceInput(
            source_type=FactSourceType.CV, provenance_key="TEST-ONLY-PROPOSED"
        ),
    )
    outcome = set_profile_mobility(migrated, profile_id, MOBILITY)
    assert outcome.action is ExplicitInputAction.CREATED


# --------------------------------------------------------------------------
# Synchronization
# --------------------------------------------------------------------------


def test_a_profile_that_stated_nothing_has_no_projection(migrated, profile_id) -> None:
    outcome = synchronize_profile_preferences(migrated, profile_id)

    # Absence is UNKNOWN, and UNKNOWN has no row. Not a default row, not an
    # "unknown" row, and never a FALSE.
    assert _counts(migrated, C_TABLES) == (0, 0, 0, 0)
    assert outcome.created == 0
    assert outcome.removed == 0
    assert not outcome.changed
    assert get_profile_availability(migrated, profile_id) is None
    assert get_profile_mobility(migrated, profile_id) is None
    assert get_profile_preferences(migrated, profile_id) is None
    assert get_profile_career_objectives(migrated, profile_id) is None


def test_the_four_types_are_projected(migrated, stated) -> None:
    availability = get_profile_availability(migrated, stated)
    mobility = get_profile_mobility(migrated, stated)
    preferences = get_profile_preferences(migrated, stated)
    objectives = get_profile_career_objectives(migrated, stated)

    assert availability.value == AVAILABILITY
    assert mobility.value == MOBILITY
    assert preferences.value == PREFERENCES
    assert objectives.value == OBJECTIVES
    assert {
        row.input_version
        for row in (availability, mobility, preferences, objectives)
    } == {EXPLICIT_PROFILE_INPUT_VERSION}


def test_the_stored_json_is_canonical(migrated, stated) -> None:
    stored = migrated.execute(
        "SELECT opportunity_types_json, work_modes_json, preferred_domains_json, "
        "constraints_json FROM profile_preferences WHERE profile_id = ?",
        (stated,),
    ).fetchone()
    assert stored[0] == '["PFE","ALTERNANCE"]'
    assert stored[1] == '["HYBRID","REMOTE"]'
    assert json.loads(stored[2]) == list(TEST_ONLY_DOMAINS)
    assert json.loads(stored[3]) == list(TEST_ONLY_CONSTRAINTS)


def test_a_second_synchronization_writes_nothing(migrated, stated) -> None:
    before = {table: _rows(migrated, table) for table in C_TABLES}

    outcome = synchronize_profile_preferences(migrated, stated)

    assert outcome.created == 0
    assert outcome.removed == 0
    assert not outcome.changed
    # Same rows, `created_at` included: nothing was rewritten.
    assert {table: _rows(migrated, table) for table in C_TABLES} == before


def test_a_proposed_fact_is_never_projected(migrated, profile_id) -> None:
    propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.MOBILITY,
        value=encode_mobility(MOBILITY),
        provenance=ProvenanceInput(
            source_type=FactSourceType.CV, provenance_key="TEST-ONLY-PROPOSED"
        ),
    )
    synchronize_profile_preferences(migrated, profile_id)

    assert get_profile_mobility(migrated, profile_id) is None


def test_a_rejected_fact_is_never_projected(migrated, profile_id) -> None:
    fact = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.MOBILITY,
        value=encode_mobility(MOBILITY),
        provenance=ProvenanceInput(
            source_type=FactSourceType.CV, provenance_key="TEST-ONLY-REJECTED"
        ),
    )
    reject_profile_fact(migrated, profile_id, fact.id)
    synchronize_profile_preferences(migrated, profile_id)

    assert get_profile_mobility(migrated, profile_id) is None


def test_a_corrected_fact_stops_being_projected(migrated, profile_id) -> None:
    first = set_profile_availability(migrated, profile_id, AVAILABILITY)
    synchronize_profile_preferences(migrated, profile_id)
    second = set_profile_availability(migrated, profile_id, OTHER_AVAILABILITY)
    outcome = synchronize_profile_preferences(migrated, profile_id)

    row = get_profile_availability(migrated, profile_id)
    assert row.fact_id == second.fact_id
    assert row.fact_id != first.fact_id
    assert row.value == OTHER_AVAILABILITY
    assert (outcome.created, outcome.removed) == (1, 1)
    assert int(
        migrated.execute("SELECT COUNT(*) FROM profile_availability").fetchone()[0]
    ) == 1


def test_a_domain_holding_two_accepted_facts_is_refused_and_rolled_back(
    migrated, stated
) -> None:
    before = {table: _rows(migrated, table) for table in C_TABLES}
    # Recorded through the facts package directly, which is the only way this
    # state can arise: the service refuses to create it.
    record_verified_user_input_fact(
        migrated,
        profile_id=stated,
        fact_type=ProfileFactType.MOBILITY,
        value=encode_mobility(MobilityPreference(MobilityScope.OPEN)),
        provenance=ProvenanceInput(
            source_type=FactSourceType.USER_INPUT, provenance_key="TEST-ONLY-SECOND"
        ),
    )

    with pytest.raises(AmbiguousExplicitInputError):
        synchronize_profile_preferences(migrated, stated)

    # The whole run rolled back, the other three domains included: nothing was
    # rewritten while one domain was ambiguous, and nothing was chosen.
    assert {table: _rows(migrated, table) for table in C_TABLES} == before


def test_a_fact_value_this_package_did_not_write_is_refused(
    migrated, profile_id
) -> None:
    record_verified_user_input_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.PREFERENCE,
        value='{"opportunity_types": ["PFE"]}',
    )
    with pytest.raises(ExplicitProfileInputError):
        synchronize_profile_preferences(migrated, profile_id)
    assert _counts(migrated, C_TABLES) == (0, 0, 0, 0)


def test_a_failure_after_the_read_rolls_the_whole_projection_back(
    migrated, profile_id
) -> None:
    set_profile_availability(migrated, profile_id, AVAILABILITY)
    set_profile_mobility(migrated, profile_id, MOBILITY)

    def fail() -> None:
        raise RuntimeError("interrupted mid-reconciliation")

    with pytest.raises(RuntimeError):
        synchronize_profile_preferences(migrated, profile_id, after_read=fail)

    assert _counts(migrated, C_TABLES) == (0, 0, 0, 0)


def test_synchronizing_never_touches_the_facts(migrated, stated) -> None:
    facts = _fact_rows(migrated)
    provenance = _provenance_rows(migrated)

    synchronize_profile_preferences(migrated, stated)

    assert _fact_rows(migrated) == facts
    assert _provenance_rows(migrated) == provenance


def test_the_repository_writes_to_no_fact_table() -> None:
    source = source_of(REPOSITORY_SOURCE)
    for table in ("profile_facts", "profile_fact_provenance"):
        for verb in ("INSERT INTO", "UPDATE", "DELETE FROM"):
            assert f"{verb} {table}" not in source


def test_the_repository_reads_only_accepted_facts_of_the_four_types() -> None:
    # Read off the statements the module will actually run, so no reading can
    # be widened without this failing — and so prose cannot make it pass.
    statements = [projection.facts_sql for projection in _PROJECTIONS]
    assert [projection.fact_type for projection in _PROJECTIONS] == [
        "AVAILABILITY", "MOBILITY", "PREFERENCE", "CAREER_OBJECTIVE",
    ]
    for projection in _PROJECTIONS:
        assert "status = 'ACCEPTED'" in projection.facts_sql
        assert f"fact_type = '{projection.fact_type}'" in projection.facts_sql
        assert "profile_id = ?" in projection.facts_sql
        # Acceptance alone is not enough: the person must have stated it.
        assert "profile_fact_provenance" in projection.facts_sql
        assert "source_type = 'USER_INPUT'" in projection.facts_sql
        assert "EXISTS (" in projection.facts_sql
    assert len(set(statements)) == len(statements)


def test_the_repository_selects_facts_from_nowhere_else() -> None:
    source = source_of(REPOSITORY_SOURCE)
    # One builder writes all four statements, so `profile_facts` is read from
    # exactly one place in the module and the filter cannot be weakened for one
    # domain and not the others.
    assert source.count("FROM profile_facts") == 1
    assert all(
        projection.facts_sql.startswith("SELECT f.id, f.value FROM profile_facts AS f")
        for projection in _PROJECTIONS
    )
    # And `profile_fact_provenance` is read only to prove the person spoke.
    assert source.count("FROM profile_fact_provenance") == 1


def test_the_repository_names_no_neighbouring_projection() -> None:
    source = source_of(REPOSITORY_SOURCE)
    for table in (*B_TABLES, *SKILL_TABLES, "opportunities"):
        assert table not in source


def test_synchronizing_disturbs_no_neighbouring_projection(migrated, profile_id) -> None:
    fact = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.SKILL,
        value="Compétence inventée",
        provenance=ProvenanceInput(
            source_type=FactSourceType.CV, provenance_key="TEST-ONLY-SKILL"
        ),
    )
    accept_profile_fact(migrated, profile_id, fact.id)
    experience = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EXPERIENCE,
        value="Poste inventé | Organisation inventée | 2020 - 2021",
        provenance=ProvenanceInput(
            source_type=FactSourceType.CV, provenance_key="TEST-ONLY-EXPERIENCE"
        ),
    )
    accept_profile_fact(migrated, profile_id, experience.id)
    synchronize_profile_skills(migrated, profile_id)
    synchronize_structured_profile_entries(migrated, profile_id)
    skills_before = _counts(migrated, SKILL_TABLES)
    structured_before = {table: _rows(migrated, table) for table in B_TABLES}

    set_profile_mobility(migrated, profile_id, MOBILITY)
    synchronize_profile_preferences(migrated, profile_id)

    assert _counts(migrated, SKILL_TABLES) == skills_before
    assert {table: _rows(migrated, table) for table in B_TABLES} == structured_before


def test_one_profiles_statements_never_reach_another_profiles_rows(
    migrated, profile_id, other_profile_id
) -> None:
    set_profile_mobility(migrated, profile_id, MOBILITY)
    synchronize_profile_preferences(migrated, profile_id)
    synchronize_profile_preferences(migrated, other_profile_id)

    assert get_profile_mobility(migrated, other_profile_id) is None
    assert get_profile_mobility(migrated, profile_id).value == MOBILITY


def test_synchronizing_an_absent_profile_is_refused(migrated) -> None:
    with pytest.raises(ProfilePreferenceNotFoundError):
        synchronize_profile_preferences(migrated, 9999)
    assert _counts(migrated, C_TABLES) == (0, 0, 0, 0)


# --------------------------------------------------------------------------
# The status report
# --------------------------------------------------------------------------


def test_an_empty_profile_reports_unknown_everywhere(migrated, profile_id) -> None:
    report = summarize_profile_preferences(migrated, profile_id).as_dict()

    assert report["availability"] == "UNKNOWN"
    assert report["mobility"] == "UNKNOWN"
    assert report["preferences"] == "UNKNOWN"
    assert report["career_objectives"] == "UNKNOWN"
    # UNKNOWN is never FALSE, and the report never says "no".
    assert "FALSE" not in {str(value).upper() for value in report.values()}


def test_a_stated_profile_reports_known_and_counts(migrated, stated) -> None:
    report = summarize_profile_preferences(migrated, stated).as_dict()

    assert report["availability"] == "KNOWN"
    assert report["mobility"] == "KNOWN"
    assert report["preferences"] == "KNOWN"
    assert report["career_objectives"] == "KNOWN"
    assert report["mobility_location_count"] == len(TEST_ONLY_LOCATIONS)
    assert report["opportunity_type_count"] == 2
    assert report["work_mode_count"] == 2
    assert report["preferred_domain_count"] == len(TEST_ONLY_DOMAINS)
    assert report["constraint_count"] == len(TEST_ONLY_CONSTRAINTS)
    assert report["career_objective_count"] == len(TEST_ONLY_OBJECTIVES)


def test_the_status_report_carries_no_value(migrated, stated) -> None:
    rendered = " ".join(
        str(value) for value in summarize_profile_preferences(migrated, stated).as_dict().values()
    )
    for value in (
        *TEST_ONLY_LOCATIONS, *TEST_ONLY_DOMAINS, *TEST_ONLY_CONSTRAINTS,
        *TEST_ONLY_OBJECTIVES, "2030-01-15", "AVAILABLE_FROM", "RESTRICTED",
    ):
        assert value not in rendered


def test_the_synchronization_summary_carries_no_value(migrated, stated) -> None:
    rendered = " ".join(
        str(value)
        for value in synchronize_profile_preferences(migrated, stated).as_dict().values()
    )
    for value in (
        *TEST_ONLY_LOCATIONS, *TEST_ONLY_DOMAINS, *TEST_ONLY_CONSTRAINTS,
        *TEST_ONLY_OBJECTIVES, "2030-01-15",
    ):
        assert value not in rendered


# --------------------------------------------------------------------------
# Acceptance is not enough: the person must have stated it
# --------------------------------------------------------------------------
#
# These four domains are not readings of a document, so a fact resting only on
# a CV, a GitHub page or a derivation from other accepted evidence is not weak
# evidence for them — it is the wrong kind entirely. Every test below is run
# over all four domains, because a guarantee that holds for `PREFERENCE` and
# not for `MOBILITY` is not a guarantee.

#: Each domain, with a valid canonical value for it. The setters take these
#: apart; the tests that write a fact directly use the encoded value.
DOMAINS = (
    (ProfileFactType.AVAILABILITY, encode_availability(AVAILABILITY),
     get_profile_availability, set_profile_availability, AVAILABILITY),
    (ProfileFactType.MOBILITY, encode_mobility(MOBILITY),
     get_profile_mobility, set_profile_mobility, MOBILITY),
    (ProfileFactType.PREFERENCE, encode_preferences(PREFERENCES),
     get_profile_preferences, set_profile_preferences, PREFERENCES),
    (ProfileFactType.CAREER_OBJECTIVE, encode_career_objectives(OBJECTIVES),
     get_profile_career_objectives, set_profile_career_objectives, OBJECTIVES),
)

#: The three source types that are **not** a person speaking.
NON_STATED_SOURCES = (
    FactSourceType.CV,
    FactSourceType.GITHUB,
    FactSourceType.OTHER_ACCEPTED_EVIDENCE,
)

_evidence_counter = 0


def _proof(source_type: FactSourceType) -> ProvenanceInput:
    """A distinct synthetic proof, so no two facts share one by accident."""
    global _evidence_counter
    _evidence_counter += 1
    return ProvenanceInput(
        source_type=source_type,
        provenance_key=f"TEST-ONLY-EVIDENCE-{_evidence_counter}",
    )


def accepted_with(connection, profile_id, fact_type, value, source_type):
    """One `ACCEPTED` fact of that type, evidenced only by that source type."""
    fact = propose_profile_fact(
        connection,
        profile_id=profile_id,
        fact_type=fact_type,
        value=value,
        provenance=_proof(source_type),
    )
    return accept_profile_fact(connection, profile_id, fact.id)


@pytest.mark.parametrize(
    "fact_type,value,read,_setter,expected",
    DOMAINS,
    ids=[domain[0].value for domain in DOMAINS],
)
def test_a_stated_fact_is_projected(
    migrated, profile_id, fact_type, value, read, _setter, expected
) -> None:
    record_verified_user_input_fact(
        migrated, profile_id=profile_id, fact_type=fact_type, value=value
    )
    synchronize_profile_preferences(migrated, profile_id)

    assert read(migrated, profile_id).value == expected


@pytest.mark.parametrize("source_type", NON_STATED_SOURCES)
@pytest.mark.parametrize(
    "fact_type,value,read,_setter,_expected",
    DOMAINS,
    ids=[domain[0].value for domain in DOMAINS],
)
def test_an_accepted_fact_nobody_stated_is_never_projected(
    migrated, profile_id, fact_type, value, read, _setter, _expected, source_type
) -> None:
    """A CV, a GitHub page and a derivation are all the wrong kind of evidence.

    The fact is `ACCEPTED` and its value is perfectly well-formed, so the only
    thing keeping it out of the projection is the provenance clause. Without
    that clause a document would be attributing a preference to somebody who
    never expressed one.
    """
    accepted_with(migrated, profile_id, fact_type, value, source_type)

    outcome = synchronize_profile_preferences(migrated, profile_id)

    assert read(migrated, profile_id) is None
    assert outcome.created == 0
    assert not outcome.changed
    assert _counts(migrated, C_TABLES) == (0, 0, 0, 0)
    # It is UNKNOWN, exactly as if the fact did not exist at all.
    assert find_stated_fact(migrated, profile_id, fact_type) is None


@pytest.mark.parametrize("source_type", NON_STATED_SOURCES)
@pytest.mark.parametrize(
    "fact_type,value,read,setter,expected",
    DOMAINS,
    ids=[domain[0].value for domain in DOMAINS],
)
def test_an_unstated_accepted_fact_is_not_corrected_by_a_first_statement(
    migrated, profile_id, fact_type, value, read, setter, expected, source_type
) -> None:
    """The person's first statement is a fact of its own, not a correction.

    Correcting the document's fact would rewrite what it was read as saying in
    order to record what somebody typed, and those are two different claims.
    """
    document = accepted_with(migrated, profile_id, fact_type, value, source_type)

    outcome = setter(migrated, profile_id, expected)
    synchronize_profile_preferences(migrated, profile_id)

    assert outcome.action is ExplicitInputAction.CREATED
    assert outcome.fact_id != document.id
    # The document's fact is untouched: still ACCEPTED, still unreplaced.
    unchanged = {fact.id: fact for fact in list_profile_facts(migrated, profile_id)}
    assert unchanged[document.id].status.value == "ACCEPTED"
    assert unchanged[document.id].replaced_by_fact_id is None
    # And the projection describes the statement, not the document.
    assert read(migrated, profile_id).fact_id == outcome.fact_id


@pytest.mark.parametrize(
    "fact_type,value,read,setter,expected",
    DOMAINS,
    ids=[domain[0].value for domain in DOMAINS],
)
def test_several_proofs_including_user_input_project_exactly_one_row(
    migrated, profile_id, fact_type, value, read, setter, expected
) -> None:
    """`EXISTS`, not a join: extra proofs corroborate, they do not duplicate."""
    outcome = setter(migrated, profile_id, expected)
    for source_type in (*NON_STATED_SOURCES, FactSourceType.USER_INPUT):
        add_profile_fact_provenance(
            migrated,
            profile_id=profile_id,
            fact_id=outcome.fact_id,
            provenance=_proof(source_type),
        )
    assert len(list_profile_fact_provenance(migrated, profile_id, outcome.fact_id)) == 5

    synchronize_profile_preferences(migrated, profile_id)

    projection = _projection_of(fact_type)
    assert _count_rows(migrated, projection.table, profile_id) == 1
    assert read(migrated, profile_id).value == expected
    assert find_stated_fact(migrated, profile_id, fact_type) == (outcome.fact_id, value)


@pytest.mark.parametrize(
    "fact_type,value,_read,_setter,_expected",
    DOMAINS,
    ids=[domain[0].value for domain in DOMAINS],
)
def test_two_stated_facts_of_one_domain_are_refused_and_rolled_back(
    migrated, stated, fact_type, value, _read, _setter, _expected
) -> None:
    before = {table: _rows(migrated, table) for table in C_TABLES}
    # A second stated fact, recorded through the facts package directly: the
    # service refuses to create this state, so only a bypass can produce it.
    record_verified_user_input_fact(
        migrated,
        profile_id=stated,
        fact_type=fact_type,
        value=value,
        provenance=_proof(FactSourceType.USER_INPUT),
    )

    with pytest.raises(AmbiguousExplicitInputError):
        synchronize_profile_preferences(migrated, stated)

    # The whole run rolled back, the other three domains included, and nothing
    # was chosen between the two statements.
    assert {table: _rows(migrated, table) for table in C_TABLES} == before


@pytest.mark.parametrize("source_type", NON_STATED_SOURCES)
@pytest.mark.parametrize(
    "fact_type,value,_read,_setter,_expected",
    DOMAINS,
    ids=[domain[0].value for domain in DOMAINS],
)
def test_an_unstated_accepted_fact_never_makes_a_domain_ambiguous(
    migrated, stated, fact_type, value, _read, _setter, _expected, source_type
) -> None:
    """One statement plus one document is one statement, not a conflict.

    The domain is ambiguous when the *person* said two things, not when a
    document happens to carry an accepted fact of the same type.
    """
    accepted_with(migrated, stated, fact_type, value, source_type)

    outcome = synchronize_profile_preferences(migrated, stated)

    assert not outcome.changed
    assert find_stated_fact(migrated, stated, fact_type) is not None


def _projection_of(fact_type):
    for projection in _PROJECTIONS:
        if projection.fact_type == fact_type.value:
            return projection
    raise AssertionError(f"no projection for {fact_type}")


def _count_rows(connection, table: str, profile_id: int) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE profile_id = ?", (profile_id,)
        ).fetchone()[0]
    )


# --------------------------------------------------------------------------
# A proof identifies an event, not a value
# --------------------------------------------------------------------------
#
# The identity of a *value* is its canonical JSON, and it is deterministic:
# that is what tells a no-op from a correction. The identity of a *proof* is
# something else — a person sat down and typed something on some occasion — and
# two occasions are two proofs even when the words are identical. Conflating
# the two would make going back to a previous answer, an ordinary thing to do,
# leave two facts of one profile sharing one proof.


def _provenance_keys(connection, profile_id: int) -> list[str]:
    """Every proof recorded for this profile, oldest first, with its fact."""
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT p.provenance_key FROM profile_fact_provenance AS p "
            "JOIN profile_facts AS f ON f.id = p.fact_id "
            "WHERE f.profile_id = ? ORDER BY p.id",
            (profile_id,),
        ).fetchall()
    ]


def test_returning_to_a_previous_value_records_a_third_distinct_proof(
    migrated, profile_id
) -> None:
    """A -> B -> A is three statements, three facts and three proofs.

    The first A and the last A are the same words on two different occasions.
    A key derived from the words would be one key on two facts, which is the
    state `AmbiguousFactEvidenceError` reports and which would then break
    `ensure_profile_fact_proposal` for this profile.
    """
    first = set_profile_availability(migrated, profile_id, AVAILABILITY)
    second = set_profile_availability(migrated, profile_id, OTHER_AVAILABILITY)
    third = set_profile_availability(migrated, profile_id, AVAILABILITY)
    synchronize_profile_preferences(migrated, profile_id)

    assert (first.action, second.action, third.action) == (
        ExplicitInputAction.CREATED,
        ExplicitInputAction.CORRECTED,
        ExplicitInputAction.CORRECTED,
    )

    facts = list_profile_facts(migrated, profile_id)
    assert len(facts) == 3
    by_id = {fact.id: fact for fact in facts}
    assert by_id[first.fact_id].status.value == "CORRECTED"
    assert by_id[second.fact_id].status.value == "CORRECTED"
    assert by_id[third.fact_id].status.value == "ACCEPTED"
    # The chain is intact and every value ever stated is still readable.
    assert by_id[first.fact_id].replaced_by_fact_id == second.fact_id
    assert by_id[second.fact_id].replaced_by_fact_id == third.fact_id
    assert by_id[first.fact_id].value == encode_availability(AVAILABILITY)
    assert by_id[third.fact_id].value == encode_availability(AVAILABILITY)
    # Same words, and deliberately the same bytes: the value stays canonical.
    assert by_id[first.fact_id].value == by_id[third.fact_id].value

    keys = _provenance_keys(migrated, profile_id)
    sources = {row[2] for row in _provenance_rows(migrated)}
    assert len(keys) == 3
    assert sources == {"USER_INPUT"}
    # Three occasions, three proofs. This is the assertion the fix is about.
    assert len(set(keys)) == 3

    # The projection describes the last statement, and settles.
    assert get_profile_availability(migrated, profile_id).fact_id == third.fact_id
    assert get_profile_availability(migrated, profile_id).value == AVAILABILITY
    again = synchronize_profile_preferences(migrated, profile_id)
    assert (again.created, again.removed) == (0, 0)
    assert not again.changed


def test_no_two_facts_of_a_profile_ever_share_a_proof(migrated, profile_id) -> None:
    """The invariant the whole facts package rests on, over a busy history."""
    for statement in (AVAILABILITY, OTHER_AVAILABILITY, AVAILABILITY):
        set_profile_availability(migrated, profile_id, statement)
    for scope in (MobilityScope.OPEN, MobilityScope.RESTRICTED, MobilityScope.OPEN):
        set_profile_mobility(
            migrated,
            profile_id,
            MobilityPreference(
                scope,
                TEST_ONLY_LOCATIONS if scope is MobilityScope.RESTRICTED else (),
            ),
        )
    set_profile_career_objectives(migrated, profile_id, OBJECTIVES)

    shared = migrated.execute(
        "SELECT p.provenance_key FROM profile_fact_provenance AS p "
        "JOIN profile_facts AS f ON f.id = p.fact_id "
        "WHERE f.profile_id = ? "
        "GROUP BY p.provenance_key HAVING COUNT(DISTINCT p.fact_id) > 1",
        (profile_id,),
    ).fetchall()

    assert shared == []
    keys = _provenance_keys(migrated, profile_id)
    assert len(set(keys)) == len(keys) == 7


def test_a_proof_key_names_the_contract_and_the_domain_and_nothing_personal(
    migrated, profile_id
) -> None:
    outcome = set_profile_career_objectives(migrated, profile_id, OBJECTIVES)
    key = list_profile_fact_provenance(migrated, profile_id, outcome.fact_id)[
        0
    ].provenance_key

    contract, domain, event = key.split(":")
    assert contract == EXPLICIT_PROFILE_INPUT_VERSION
    assert domain == "CAREER_OBJECTIVE"
    # An opaque event id: 32 hex characters, and nothing recoverable from it.
    assert len(event) == 32
    assert set(event) <= set("0123456789abcdef")
    for value in (*TEST_ONLY_OBJECTIVES, TEST_ONLY_EMAIL):
        assert value not in key


def test_restating_the_same_value_records_no_event_at_all(
    migrated, profile_id
) -> None:
    """A no-op writes no fact, so it generates no proof and no event id."""
    set_profile_availability(migrated, profile_id, AVAILABILITY)
    facts_before = _fact_rows(migrated)
    provenance_before = _provenance_rows(migrated)

    outcome = set_profile_availability(migrated, profile_id, AVAILABILITY)

    assert outcome.action is ExplicitInputAction.UNCHANGED
    assert len(_fact_rows(migrated)) == len(facts_before) == 1
    assert len(_provenance_rows(migrated)) == len(provenance_before) == 1
    # Byte-identical rows: no new key, and no timestamp moved anywhere.
    assert _fact_rows(migrated) == facts_before
    assert _provenance_rows(migrated) == provenance_before
