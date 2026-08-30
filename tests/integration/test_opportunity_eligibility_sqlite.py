"""Migration 0014 and the Phase 3.6 engine, end to end, on disposable SQLite.

Every database here is created under `tmp_path` and thrown away. Every posting
and every profile is invented; no real listing, company, description or person
takes part, and every string a test writes carries a `TEST ONLY` marker or an
`example.invalid` host so nothing here can be mistaken for collected data. **The
operational database is never opened**, no count taken from it appears anywhere
below, and nothing in this file writes to any database a person uses.

The path each test exercises is the real one: postings inserted, Phase 3.5A
synchronized, Phase 3.5B synchronized, a Digital Twin built from proposed and
accepted facts and its own projections synchronized, and only then the
eligibility engine run over both. Nothing is mocked. The verdicts below are
therefore statements about the actual rules over the actual schema, not about a
stub agreeing with itself.
"""

import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    apply_migrations,
    discover_migrations,
    split_sql_statements,
)
from services.collector.extractors.opportunity_constraints.requirements.service import (
    synchronize_opportunity_requirements,
)
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)
from services.digital_twin.facts.models import (
    FactSourceType,
    ProfileFactType,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    accept_profile_fact,
    propose_profile_fact,
)
from services.digital_twin.preferences.models import (
    ConventionStatus,
    OpportunityPreferences,
    OpportunityType,
    VisaSponsorshipRequired,
    WorkMode,
)
from services.digital_twin.preferences.repository import (
    synchronize_profile_preferences,
)
from services.digital_twin.preferences.service import set_profile_preferences
from services.digital_twin.repository import ensure_user_profile
from services.digital_twin.skills.repository import synchronize_profile_skills
from services.digital_twin.structured_profile.repository import (
    synchronize_structured_profile_entries,
)
from services.eligibility.audit import audit_eligibility
from services.eligibility.inputs import load_opportunity_input, load_profile_input
from services.eligibility.models import (
    ELIGIBILITY_ENGINE_VERSION,
    ConventionCapability,
    Dimension,
    GlobalStatus,
    RuleStatus,
    SponsorshipNeed,
)
from services.eligibility.repository import (
    EligibilityRepositoryError,
    read_eligibility,
    read_rule_results,
    store_eligibility,
    stored_eligibility_signature,
)
from services.eligibility.service import (
    EligibilityServiceError,
    evaluate_one_eligibility,
    synchronize_eligibility,
)

MIGRATION = Path("migrations/0014_opportunity_eligibility.sql")
REPOSITORY_SOURCE = Path("services/eligibility/repository.py")

#: The two tables `0014` adds, and nothing else.
ELIGIBILITY_TABLES = ("opportunity_eligibilities", "eligibility_rule_results")

#: Everything Phases 3.4 and 3.5 own. Phase 3.6 reads these and writes none.
UPSTREAM_TABLES = (
    "opportunities",
    "users",
    "profiles",
    "profile_facts",
    "profile_fact_provenance",
    "profile_skills",
    "profile_languages",
    "profile_educations",
    "profile_preferences",
    "opportunity_constraints",
    "opportunity_education_requirements",
    "opportunity_experience_requirements",
    "opportunity_skill_requirements",
    "opportunity_language_requirements",
    "opportunity_requirement_ambiguities",
    "opportunity_requirement_extraction_state",
)

# --------------------------------------------------------------------------
# TEST ONLY postings. Every one is invented for this file.
# --------------------------------------------------------------------------

SILENT_DESCRIPTION = (
    "TEST ONLY. We are a team that builds things and enjoys building them."
)

BLOCKING_DESCRIPTION = (
    "TEST ONLY posting.<h3>Requirements</h3><ul>"
    "<li>English B2 required</li>"
    "<li>Convention de stage obligatoire</li>"
    "<li>Proficiency in Python is required</li>"
    "<li>SQL is required</li>"
    "</ul>"
)

LANGUAGE_ONLY_DESCRIPTION = (
    "TEST ONLY posting.<h3>Requirements</h3><ul>"
    "<li>English C1 required</li>"
    "</ul>"
)

_provenance_counter = 0


def _provenance(source_type=FactSourceType.CV) -> ProvenanceInput:
    global _provenance_counter
    _provenance_counter += 1
    return ProvenanceInput(
        source_type=source_type, provenance_key=f"TEST-ONLY-{_provenance_counter}"
    )


def accept(connection, profile_id: int, value: str, *, fact_type):
    fact = propose_profile_fact(
        connection,
        profile_id=profile_id,
        fact_type=fact_type,
        value=value,
        provenance=_provenance(),
    )
    return accept_profile_fact(connection, profile_id, fact.id)


def insert_opportunity(connection, *, description: str, title="TEST ONLY Role") -> int:
    row = connection.execute(
        """INSERT INTO opportunities (
               canonical_title, organization, description, discovered_at,
               first_seen_at, last_seen_at, source_url, status, is_active
           ) VALUES (?, 'TEST ONLY Org', ?, 't', 't', 't',
                     'https://example.invalid/test-only', 'new', 1)
           RETURNING id""",
        (title, description),
    ).fetchone()
    connection.commit()
    return int(row[0])


def read_phases_3_5(connection) -> None:
    """Run the two real Phase 3.5 synchronizations over every posting in scope."""
    synchronize_opportunity_constraints(connection)
    synchronize_opportunity_requirements(connection)


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "eligibility.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def twin(migrated):
    """A TEST ONLY user and profile, with nothing stated about them yet."""
    return ensure_user_profile(migrated, "test-only@example.invalid")


def state_preferences(
    connection,
    profile_id: int,
    *,
    convention=ConventionStatus.UNKNOWN,
    sponsorship=VisaSponsorshipRequired.UNKNOWN,
):
    """What this invented person says they want, through the real 3.4C path."""
    outcome = set_profile_preferences(
        connection,
        profile_id,
        OpportunityPreferences(
            opportunity_types=(OpportunityType.PFE,),
            work_modes=(WorkMode.HYBRID,),
            convention_status=convention,
            visa_sponsorship_required=sponsorship,
        ),
    )
    # Stating something and projecting it are two steps in Phase 3.4C, and
    # Phase 3.6 reads the projection. Running both here keeps the fixture on
    # the real path rather than on a shortcut this test invented.
    synchronize_profile_preferences(connection, profile_id)
    return outcome


def project_profile(connection, profile_id: int) -> None:
    """Run the real Phase 3.4 projections that Phase 3.6 reads."""
    synchronize_profile_skills(connection, profile_id)
    synchronize_structured_profile_entries(connection, profile_id)


def _tables(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _row_counts(connection, tables) -> dict[str, int]:
    return {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in tables
    }


# --------------------------------------------------------------------------
# The migration
# --------------------------------------------------------------------------


def test_migration_0014_is_the_next_version_and_nothing_earlier_moved():
    versions = [migration.version for migration in discover_migrations()]
    assert versions[-1] == "0014"
    assert versions == sorted(versions, key=int)
    assert len(set(versions)) == len(versions)


def test_the_migration_adds_exactly_two_tables(tmp_path):
    connection = connect_database(tmp_path / "before.db")
    try:
        for migration in discover_migrations():
            if migration.version == "0014":
                break
            for statement in split_sql_statements(
                migration.path.read_text(encoding="utf-8")
            ):
                connection.execute(statement)
        connection.commit()
        before = _tables(connection)
        for statement in split_sql_statements(MIGRATION.read_text(encoding="utf-8")):
            connection.execute(statement)
        connection.commit()
        assert _tables(connection) - before == set(ELIGIBILITY_TABLES)
    finally:
        connection.close()


def test_the_migration_preserves_everything_already_stored(tmp_path):
    """Applied over a populated database, `0014` adds and changes nothing else."""
    connection = connect_database(tmp_path / "populated.db")
    try:
        for migration in discover_migrations():
            if migration.version == "0014":
                break
            for statement in split_sql_statements(
                migration.path.read_text(encoding="utf-8")
            ):
                connection.execute(statement)
        connection.commit()
        insert_opportunity(connection, description=BLOCKING_DESCRIPTION)
        insert_opportunity(connection, description=SILENT_DESCRIPTION)
        read_phases_3_5(connection)
        found = ensure_user_profile(connection, "test-only@example.invalid")
        accept(
            connection,
            found.profile.id,
            "Python",
            fact_type=ProfileFactType.SKILL,
        )
        project_profile(connection, found.profile.id)
        before = _row_counts(connection, UPSTREAM_TABLES)

        for statement in split_sql_statements(MIGRATION.read_text(encoding="utf-8")):
            connection.execute(statement)
        connection.commit()

        assert _row_counts(connection, UPSTREAM_TABLES) == before
    finally:
        connection.close()


def test_the_decision_is_unique_per_person_and_posting(migrated, twin):
    opportunity_id = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    with pytest.raises(sqlite3.IntegrityError):
        migrated.executemany(
            """INSERT INTO opportunity_eligibilities (
                   user_id, opportunity_id, status, engine_version,
                   input_fingerprint, satisfied_count, violated_count,
                   unknown_count, not_applicable_count, not_evaluated_count,
                   blocking_unknown_count, evaluated_at
               ) VALUES (?, ?, 'ELIGIBLE', 'v', ?, 0, 0, 0, 0, 0, 0, 't')""",
            [
                (twin.user.id, opportunity_id, "a" * 64),
                (twin.user.id, opportunity_id, "b" * 64),
            ],
        )
    migrated.rollback()


def test_the_schema_refuses_a_verdict_its_counters_contradict(migrated, twin):
    """An INELIGIBLE with nothing contradicted cannot reach disk."""
    opportunity_id = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO opportunity_eligibilities (
                   user_id, opportunity_id, status, engine_version,
                   input_fingerprint, satisfied_count, violated_count,
                   unknown_count, not_applicable_count, not_evaluated_count,
                   blocking_unknown_count, evaluated_at
               ) VALUES (?, ?, 'INELIGIBLE', 'v', ?, 0, 0, 3, 0, 0, 3, 't')""",
            (twin.user.id, opportunity_id, "c" * 64),
        )
    migrated.rollback()


def test_the_schema_refuses_an_eligible_verdict_with_an_open_hard_rule(migrated, twin):
    opportunity_id = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO opportunity_eligibilities (
                   user_id, opportunity_id, status, engine_version,
                   input_fingerprint, satisfied_count, violated_count,
                   unknown_count, not_applicable_count, not_evaluated_count,
                   blocking_unknown_count, evaluated_at
               ) VALUES (?, ?, 'ELIGIBLE', 'v', ?, 0, 0, 1, 0, 0, 1, 't')""",
            (twin.user.id, opportunity_id, "d" * 64),
        )
    migrated.rollback()


def _insert_decision(connection, user_id: int, opportunity_id: int) -> int:
    row = connection.execute(
        """INSERT INTO opportunity_eligibilities (
               user_id, opportunity_id, status, engine_version, input_fingerprint,
               satisfied_count, violated_count, unknown_count,
               not_applicable_count, not_evaluated_count, blocking_unknown_count,
               evaluated_at
           ) VALUES (?, ?, 'ELIGIBLE', 'v', ?, 0, 0, 0, 0, 0, 0, 't')
           RETURNING id""",
        (user_id, opportunity_id, "e" * 64),
    ).fetchone()
    return int(row[0])


@pytest.mark.parametrize(
    "dimension", ["SKILL", "AMBIGUITY", "MOBILITY", "LOCATION", "AVAILABILITY",
                  "DURATION", "START_DATE", "WORK_MODE"]
)
def test_the_schema_refuses_a_blocking_row_on_a_non_hard_dimension(
    migrated, twin, dimension
):
    opportunity_id = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    eligibility_id = _insert_decision(migrated, twin.user.id, opportunity_id)
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO eligibility_rule_results (
                   eligibility_id, position, dimension, rule_code, status,
                   is_blocking, requirement_kind, reason_code, explanation
               ) VALUES (?, 0, ?, 'X', 'SATISFIED', 1, 'REQUIRED', 'R', 'E')""",
            (eligibility_id, dimension),
        )
    migrated.rollback()


def test_the_schema_refuses_a_preferred_requirement_that_blocks(migrated, twin):
    opportunity_id = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    eligibility_id = _insert_decision(migrated, twin.user.id, opportunity_id)
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO eligibility_rule_results (
                   eligibility_id, position, dimension, rule_code, status,
                   is_blocking, requirement_kind, reason_code, explanation
               ) VALUES (?, 0, 'LANGUAGE', 'X', 'SATISFIED', 1, 'PREFERRED', 'R', 'E')""",
            (eligibility_id,),
        )
    migrated.rollback()


def test_the_schema_refuses_a_violation_from_a_rule_that_cannot_block(migrated, twin):
    opportunity_id = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    eligibility_id = _insert_decision(migrated, twin.user.id, opportunity_id)
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO eligibility_rule_results (
                   eligibility_id, position, dimension, rule_code, status,
                   is_blocking, requirement_kind, reason_code, explanation
               ) VALUES (?, 0, 'LANGUAGE', 'X', 'VIOLATED', 0, 'REQUIRED', 'R', 'E')""",
            (eligibility_id,),
        )
    migrated.rollback()


def test_deleting_a_decision_takes_its_reasons_with_it(migrated, twin):
    opportunity_id = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    eligibility_id = _insert_decision(migrated, twin.user.id, opportunity_id)
    migrated.execute(
        """INSERT INTO eligibility_rule_results (
               eligibility_id, position, dimension, rule_code, status,
               is_blocking, reason_code, explanation
           ) VALUES (?, 0, 'SKILL', 'X', 'NOT_EVALUATED', 0, 'R', 'E')""",
        (eligibility_id,),
    )
    migrated.commit()
    migrated.execute(
        "DELETE FROM opportunity_eligibilities WHERE id = ?", (eligibility_id,)
    )
    migrated.commit()
    assert (
        migrated.execute(
            "SELECT COUNT(*) FROM eligibility_rule_results"
        ).fetchone()[0]
        == 0
    )


# --------------------------------------------------------------------------
# The whole path: 3.4 + 3.5 -> 3.6 -> SQLite
# --------------------------------------------------------------------------


def test_a_verdict_and_its_reasons_are_persisted(migrated, twin):
    """The full traversal, with nothing mocked anywhere along it."""
    opportunity_id = insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    read_phases_3_5(migrated)
    accept(
        migrated,
        twin.profile.id,
        "English : B2",
        fact_type=ProfileFactType.LANGUAGE,
    )
    project_profile(migrated, twin.profile.id)
    state_preferences(
        migrated, twin.profile.id, convention=ConventionStatus.NOT_AVAILABLE
    )

    summary = synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    assert summary.processed == 1
    assert summary.created == 1
    assert summary.changed is True

    stored = read_eligibility(migrated, twin.user.id, opportunity_id)
    assert stored is not None
    assert stored.status is GlobalStatus.INELIGIBLE
    assert stored.engine_version == ELIGIBILITY_ENGINE_VERSION
    assert len(stored.input_fingerprint) == 64

    results = read_rule_results(migrated, stored.id)
    assert results
    violated = [item for item in results if item.status is RuleStatus.VIOLATED]
    assert [item.dimension for item in violated] == [Dimension.CONVENTION]
    assert stored.violated_count == len(violated)


def test_the_reasons_read_back_exactly_as_they_were_decided(migrated, twin):
    opportunity_id = insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    decision, written = evaluate_one_eligibility(
        migrated, twin.user.id, twin.profile.id, opportunity_id
    )
    assert written is True
    stored = read_eligibility(migrated, twin.user.id, opportunity_id)
    assert read_rule_results(migrated, stored.id) == decision.results


def test_a_posting_demanding_nothing_this_engine_checks_is_eligible(migrated, twin):
    opportunity_id = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    stored = read_eligibility(migrated, twin.user.id, opportunity_id)
    assert stored.status is GlobalStatus.ELIGIBLE


def test_a_required_language_the_twin_never_named_asks_rather_than_refuses(
    migrated, twin
):
    """The invariant this whole phase exists for, over real stored rows."""
    opportunity_id = insert_opportunity(
        migrated, description=LANGUAGE_ONLY_DESCRIPTION
    )
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)

    stored = read_eligibility(migrated, twin.user.id, opportunity_id)
    assert stored.status is GlobalStatus.UNKNOWN
    assert stored.violated_count == 0
    assert stored.blocking_unknown_count >= 1


def test_a_verified_lower_language_level_is_the_one_thing_that_refuses(
    migrated, twin
):
    opportunity_id = insert_opportunity(
        migrated, description=LANGUAGE_ONLY_DESCRIPTION
    )
    read_phases_3_5(migrated)
    accept(
        migrated,
        twin.profile.id,
        "English : A2",
        fact_type=ProfileFactType.LANGUAGE,
    )
    project_profile(migrated, twin.profile.id)
    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)

    stored = read_eligibility(migrated, twin.user.id, opportunity_id)
    assert stored.status is GlobalStatus.INELIGIBLE
    reasons = [
        item.reason_code.value
        for item in read_rule_results(migrated, stored.id)
        if item.status is RuleStatus.VIOLATED
    ]
    assert reasons == ["LANGUAGE_REQUIRED_LEVEL_VIOLATED"]


def test_a_missing_required_skill_never_refuses_over_real_extracted_rows(
    migrated, twin
):
    """The posting really does carry REQUIRED skill rows; none of them blocks."""
    opportunity_id = insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    read_phases_3_5(migrated)
    accept(
        migrated,
        twin.profile.id,
        "English : C1",
        fact_type=ProfileFactType.LANGUAGE,
    )
    project_profile(migrated, twin.profile.id)
    state_preferences(
        migrated, twin.profile.id, convention=ConventionStatus.AVAILABLE
    )

    required = int(
        migrated.execute(
            "SELECT COUNT(*) FROM opportunity_skill_requirements "
            "WHERE opportunity_id = ? AND requirement = 'REQUIRED'",
            (opportunity_id,),
        ).fetchone()[0]
    )
    assert required > 0

    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    stored = read_eligibility(migrated, twin.user.id, opportunity_id)
    assert stored.status is GlobalStatus.ELIGIBLE

    skill_rows = [
        item
        for item in read_rule_results(migrated, stored.id)
        if item.dimension is Dimension.SKILL
    ]
    assert len(skill_rows) >= required
    assert all(item.is_blocking is False for item in skill_rows)


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


def test_a_second_identical_synchronization_writes_nothing(migrated, twin):
    for description in (BLOCKING_DESCRIPTION, SILENT_DESCRIPTION,
                        LANGUAGE_ONLY_DESCRIPTION):
        insert_opportunity(migrated, description=description)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)

    first = synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    assert first.created == 3
    assert first.replaced == 0
    assert first.unchanged == 0
    assert first.changed is True

    second = synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    assert second.created == 0
    assert second.replaced == 0
    assert second.unchanged == 3
    assert second.changed is False


def test_an_unchanged_second_run_does_not_move_a_single_timestamp(migrated, twin):
    insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    synchronize_eligibility(
        migrated, twin.user.id, twin.profile.id, evaluated_at="2024-01-01T00:00:00"
    )
    before = migrated.execute(
        "SELECT id, evaluated_at, created_at, updated_at "
        "FROM opportunity_eligibilities"
    ).fetchall()

    synchronize_eligibility(
        migrated, twin.user.id, twin.profile.id, evaluated_at="2099-12-31T23:59:59"
    )
    after = migrated.execute(
        "SELECT id, evaluated_at, created_at, updated_at "
        "FROM opportunity_eligibilities"
    ).fetchall()
    assert after == before


def test_a_changed_profile_fact_recomputes_the_decisions_it_affects(migrated, twin):
    insert_opportunity(migrated, description=LANGUAGE_ONLY_DESCRIPTION)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)

    accept(
        migrated,
        twin.profile.id,
        "English : C2",
        fact_type=ProfileFactType.LANGUAGE,
    )
    project_profile(migrated, twin.profile.id)

    second = synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    assert second.replaced == 1
    assert second.created == 0
    assert second.changed is True


def test_an_irrelevant_profile_fact_recomputes_nothing(migrated, twin):
    """A telephone number and a GitHub URL are not read by any rule."""
    insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)

    accept(
        migrated, twin.profile.id, "+33 1 23 45 67 89", fact_type=ProfileFactType.PHONE
    )
    accept(
        migrated,
        twin.profile.id,
        "https://github.invalid/test-only",
        fact_type=ProfileFactType.GITHUB_URL,
    )
    project_profile(migrated, twin.profile.id)

    second = synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    assert second.created == 0
    assert second.replaced == 0
    assert second.unchanged == 1
    assert second.changed is False


def test_a_changed_posting_recomputes_only_that_posting(migrated, twin):
    kept = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    edited = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    kept_before = stored_eligibility_signature(migrated, twin.user.id, kept)

    migrated.execute(
        "UPDATE opportunities SET description = ? WHERE id = ?",
        (LANGUAGE_ONLY_DESCRIPTION, edited),
    )
    migrated.commit()
    read_phases_3_5(migrated)

    second = synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    assert second.replaced == 1
    assert second.unchanged == 1
    assert stored_eligibility_signature(migrated, twin.user.id, kept) == kept_before


def test_two_people_get_two_decisions_for_one_posting(migrated, twin):
    """Eligibility belongs to a person, not to a posting."""
    opportunity_id = insert_opportunity(
        migrated, description=LANGUAGE_ONLY_DESCRIPTION
    )
    read_phases_3_5(migrated)
    other = ensure_user_profile(migrated, "test-only-second@example.invalid")

    accept(
        migrated,
        twin.profile.id,
        "English : A1",
        fact_type=ProfileFactType.LANGUAGE,
    )
    project_profile(migrated, twin.profile.id)
    project_profile(migrated, other.profile.id)

    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    synchronize_eligibility(migrated, other.user.id, other.profile.id)

    assert (
        read_eligibility(migrated, twin.user.id, opportunity_id).status
        is GlobalStatus.INELIGIBLE
    )
    assert (
        read_eligibility(migrated, other.user.id, opportunity_id).status
        is GlobalStatus.UNKNOWN
    )


# --------------------------------------------------------------------------
# Transactions, refusals and what this phase may not touch
# --------------------------------------------------------------------------


def test_a_failed_write_leaves_the_previous_decision_intact(migrated, twin):
    opportunity_id = insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    decision, _ = evaluate_one_eligibility(
        migrated, twin.user.id, twin.profile.id, opportunity_id
    )
    before = read_eligibility(migrated, twin.user.id, opportunity_id)
    before_results = read_rule_results(migrated, before.id)

    def explode() -> None:
        raise RuntimeError("TEST ONLY interruption")

    with pytest.raises(RuntimeError):
        store_eligibility(migrated, twin.user.id, decision, after_delete=explode)

    after = read_eligibility(migrated, twin.user.id, opportunity_id)
    assert after.id == before.id
    assert after.input_fingerprint == before.input_fingerprint
    assert read_rule_results(migrated, after.id) == before_results


def test_the_run_refuses_a_corpus_phase_3_5_has_not_read(migrated, twin):
    insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    with pytest.raises(EligibilityServiceError):
        synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    assert (
        migrated.execute(
            "SELECT COUNT(*) FROM opportunity_eligibilities"
        ).fetchone()[0]
        == 0
    )


def test_a_person_is_never_created_by_deciding_about_them(migrated):
    insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    read_phases_3_5(migrated)
    with pytest.raises(EligibilityServiceError):
        synchronize_eligibility(migrated, 999, 999)
    assert migrated.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_an_unknown_opportunity_is_refused_rather_than_invented(migrated, twin):
    with pytest.raises(EligibilityRepositoryError):
        store_eligibility(
            migrated,
            twin.user.id,
            _decision_for_missing_opportunity(),
        )


def _decision_for_missing_opportunity():
    from services.eligibility.models import EligibilityDecision

    return EligibilityDecision(
        opportunity_id=999_999,
        profile_id=1,
        status=GlobalStatus.ELIGIBLE,
        input_fingerprint="f" * 64,
    )


def test_the_synchronization_writes_to_nothing_upstream(migrated, twin):
    insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    read_phases_3_5(migrated)
    accept(
        migrated,
        twin.profile.id,
        "Python",
        fact_type=ProfileFactType.SKILL,
    )
    project_profile(migrated, twin.profile.id)
    state_preferences(migrated, twin.profile.id)
    before = _row_counts(migrated, UPSTREAM_TABLES)
    before_opportunities = migrated.execute(
        "SELECT id, canonical_title, description, status, is_active, "
        "eligibility_score FROM opportunities ORDER BY id"
    ).fetchall()

    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)

    assert _row_counts(migrated, UPSTREAM_TABLES) == before
    assert (
        migrated.execute(
            "SELECT id, canonical_title, description, status, is_active, "
            "eligibility_score FROM opportunities ORDER BY id"
        ).fetchall()
        == before_opportunities
    )


def test_the_repository_never_writes_to_a_table_it_does_not_own():
    """Read from the source, so no future edit can quietly widen the reach."""
    source = inspect.getsource(
        __import__("services.eligibility.repository", fromlist=["x"])
    )
    statements = re.sub(r"\s+", " ", re.sub(r'""".*?"""', "", source, flags=re.S)).casefold()
    owned = set(ELIGIBILITY_TABLES)
    for table in UPSTREAM_TABLES:
        assert table not in owned
        for verb in ("insert into", "update", "delete from"):
            assert f"{verb} {table}" not in statements, (verb, table)


def test_the_historical_eligibility_score_column_is_left_alone(migrated, twin):
    """It is a column from an earlier design, and a verdict is not a number."""
    insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)
    scores = [
        row[0]
        for row in migrated.execute("SELECT eligibility_score FROM opportunities")
    ]
    assert all(score is None for score in scores)
    # And no module of this phase writes it, in any form.
    for path in sorted(Path("services/eligibility").glob("*.py")):
        source = re.sub(r'"""..*?"""', "", path.read_text(encoding="utf-8"), flags=re.S)
        assert "eligibility_score" not in source, path


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------


def test_the_stored_rows_satisfy_every_invariant_of_this_phase(migrated, twin):
    for description in (
        BLOCKING_DESCRIPTION,
        SILENT_DESCRIPTION,
        LANGUAGE_ONLY_DESCRIPTION,
    ):
        insert_opportunity(migrated, description=description)
    read_phases_3_5(migrated)
    accept(
        migrated,
        twin.profile.id,
        "English : A1",
        fact_type=ProfileFactType.LANGUAGE,
    )
    project_profile(migrated, twin.profile.id)
    state_preferences(
        migrated, twin.profile.id, convention=ConventionStatus.NOT_AVAILABLE
    )
    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)

    report = audit_eligibility(migrated, twin.user.id)
    assert report.decisions == 3
    assert set(report.invariant_violations.values()) == {0}
    assert set(report.blocker_dimensions) <= {
        "EDUCATION",
        "ENROLLMENT",
        "EXPERIENCE",
        "LANGUAGE",
        "WORK_AUTHORIZATION",
        "CONVENTION",
    }
    for reason in report.ineligible_reasons:
        assert reason.status is GlobalStatus.INELIGIBLE
        assert reason.reason_code.endswith("VIOLATED")


def test_every_unknown_verdict_names_the_question_it_could_not_answer(
    migrated, twin
):
    insert_opportunity(migrated, description=LANGUAGE_ONLY_DESCRIPTION)
    read_phases_3_5(migrated)
    project_profile(migrated, twin.profile.id)
    synchronize_eligibility(migrated, twin.user.id, twin.profile.id)

    report = audit_eligibility(migrated, twin.user.id)
    assert report.unknown_reasons
    for reason in report.unknown_reasons:
        assert reason.reason_code
        assert reason.requirement_ref
        assert reason.explanation


# --------------------------------------------------------------------------
# Reading both sides
# --------------------------------------------------------------------------


def test_the_loader_reports_the_dimensions_the_schema_cannot_support(migrated, twin):
    """Three fields are empty for a documented reason, not by omission."""
    project_profile(migrated, twin.profile.id)
    accept(
        migrated,
        twin.profile.id,
        "TEST ONLY | Université Example | Master Data | 2022 - 2024",
        fact_type=ProfileFactType.EDUCATION,
    )
    accept(
        migrated,
        twin.profile.id,
        "TEST ONLY | Data Analyst | Example Org | 2022 - 2024",
        fact_type=ProfileFactType.EXPERIENCE,
    )
    project_profile(migrated, twin.profile.id)

    loaded = load_profile_input(migrated, twin.profile.id)
    assert loaded.education_levels == ()
    assert loaded.currently_enrolled is None
    assert loaded.comparable_experience_months is None


def test_the_loader_reads_the_stated_preferences_it_needs(migrated, twin):
    state_preferences(
        migrated,
        twin.profile.id,
        convention=ConventionStatus.AVAILABLE,
        sponsorship=VisaSponsorshipRequired.NO,
    )
    loaded = load_profile_input(migrated, twin.profile.id)
    assert loaded.convention_capability is ConventionCapability.AVAILABLE
    assert loaded.sponsorship_need is SponsorshipNeed.NO


def test_the_loader_maps_a_written_language_onto_the_shared_registry(migrated, twin):
    accept(
        migrated,
        twin.profile.id,
        "Anglais : C1",
        fact_type=ProfileFactType.LANGUAGE,
    )
    project_profile(migrated, twin.profile.id)
    loaded = load_profile_input(migrated, twin.profile.id)
    assert [item.language_key for item in loaded.languages] == ["english"]


def test_a_posting_phase_3_5_has_not_read_loads_as_nothing(migrated):
    opportunity_id = insert_opportunity(migrated, description=BLOCKING_DESCRIPTION)
    assert load_opportunity_input(migrated, opportunity_id) is None
