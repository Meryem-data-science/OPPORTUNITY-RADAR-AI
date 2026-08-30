"""Migration 0012 and the Phase 3.5A projection, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away. Every posting
is invented; no real listing, company or description takes part. **The
operational `.data/` database is never opened**, no count taken from it appears
anywhere below, and nothing in this file writes to any database a person uses:
the fixtures decide what exists.

The projection is derived data, so most of these tests are about what it may
**not** do: mutate the postings it reads, keep a reading whose source changed,
leave half a reading behind after a failure, invent a value the posting did not
state, or grow a column that compares a posting to a person.
"""

import json
import re
import sqlite3
from pathlib import Path

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
)
from services.collector.extractors.opportunity_constraints.extractor import (
    extract_opportunity_constraints,
    source_fingerprint,
)
from services.collector.extractors.opportunity_constraints.models import (
    EXTRACTOR_VERSION,
    ConstraintKind,
    Slot,
    ConventionRequirement,
    EducationLevel,
    OpportunitySource,
    OpportunityType,
    VisaSponsorship,
    WorkAuthorization,
    WorkMode,
)
from services.collector.extractors.opportunity_constraints.repository import (
    OpportunityConstraintRepositoryError,
    read_opportunity_constraints,
    store_opportunity_constraints,
    stored_signature,
)
from services.collector.extractors.opportunity_constraints.service import (
    OpportunityConstraintServiceError,
    extract_one_opportunity,
    load_opportunity_source,
    synchronize_opportunity_constraints,
)

REPOSITORY_SOURCE = Path(
    "services/collector/extractors/opportunity_constraints/repository.py"
)
MIGRATION = Path("migrations/0012_opportunity_constraints.sql")

BEFORE_THIS_SLICE = (
    "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009",
    "0010", "0011",
)

#: The seven tables `0012` adds.
CONSTRAINT_TABLES = (
    "opportunity_constraints",
    "opportunity_constraint_locations",
    "opportunity_education_requirements",
    "opportunity_experience_requirements",
    "opportunity_constraint_evidence",
    "opportunity_constraint_conflicts",
    "opportunity_skill_requirements",
)

# TEST ONLY postings, invented for these tests.
RICH_TITLE = "PFE Data Engineer"
RICH_DESCRIPTION = (
    "&lt;ul&gt;&lt;li&gt;Minimum 3 years of experience required&lt;/li&gt;"
    "&lt;li&gt;Education: Bac+5 minimum&lt;/li&gt;"
    "&lt;li&gt;Visa sponsorship available&lt;/li&gt;"
    "&lt;li&gt;You must be authorized to work in France&lt;/li&gt;"
    "&lt;li&gt;Convention de stage obligatoire&lt;/li&gt;"
    "&lt;li&gt;Duration: 6 months, starting February 2027&lt;/li&gt;"
    "&lt;li&gt;This is a hybrid role&lt;/li&gt;&lt;/ul&gt;"
)
SILENT_TITLE = "Senior Data Scientist"
SILENT_DESCRIPTION = "We build good products with a great team in our office."
CONFLICTING_DESCRIPTION = (
    "This is a fully remote position. However this role is fully on-site."
)


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "constraints.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def insert_opportunity(
    connection, *, title=SILENT_TITLE, description=SILENT_DESCRIPTION,
    location=None, country=None, remote_type=None, status="new", is_active=1,
) -> int:
    row = connection.execute(
        """INSERT INTO opportunities (
               canonical_title, organization, location, country, remote_type,
               description, discovered_at, first_seen_at, last_seen_at,
               source_url, status, is_active
           ) VALUES (?, 'TEST ONLY Org', ?, ?, ?, ?, 't', 't', 't',
                     'https://example.invalid/1', ?, ?)
           RETURNING id""",
        (title, location, country, remote_type, description, status, is_active),
    ).fetchone()
    connection.commit()
    return int(row[0])


@pytest.fixture
def rich_opportunity(migrated) -> int:
    return insert_opportunity(
        migrated, title=RICH_TITLE, description=RICH_DESCRIPTION,
        location="Casablanca", country="Morocco",
    )


def source_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def executable_sql(path: Path) -> str:
    """A migration's statements, with its `--` commentary removed.

    The guards below are about what the migration *does*. `0012` explains at
    length why a `RESTRICT` foreign key refuses to drop a vocabulary row, and a
    guard that read its own explanation as a violation would be a guard nobody
    could write comments around.
    """
    return re.sub(r"--[^\n]*", "", source_of(path))


def _tables(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _counts(connection) -> tuple[int, ...]:
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in CONSTRAINT_TABLES
    )


def _opportunity_rows(connection) -> list[tuple]:
    return connection.execute(
        "SELECT id, canonical_title, organization, location, country, remote_type, "
        "description, opportunity_type, employment_type, deadline, status, "
        "is_active, relevance_score, eligibility_score, match_score, "
        "priority_score, created_at, updated_at FROM opportunities ORDER BY id"
    ).fetchall()


# --------------------------------------------------------------------------
# Migration 0012
# --------------------------------------------------------------------------


def test_migration_0012_is_discovered_after_the_earlier_ones() -> None:
    versions = [
        migration.version
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY)
    ]
    assert versions[: len(BEFORE_THIS_SLICE)] == list(BEFORE_THIS_SLICE)
    assert versions[len(BEFORE_THIS_SLICE)] == "0012"


def test_migration_0012_creates_the_seven_tables(migrated) -> None:
    assert set(CONSTRAINT_TABLES) <= _tables(migrated)


@pytest.mark.parametrize("table", CONSTRAINT_TABLES)
def test_no_constraint_table_is_seeded(migrated, table) -> None:
    """Applying `0012` states nothing about any posting."""
    assert int(migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) == 0


def test_migration_0012_alters_no_existing_table() -> None:
    # Executable SQL only: the prose explains *why* a `RESTRICT` foreign key
    # refuses to drop a vocabulary row, and a guard that reads its own
    # explanation as a violation would be a guard nobody can write comments
    # around.
    body = executable_sql(MIGRATION).upper()
    assert "ALTER TABLE" not in body
    assert "DROP" not in body


def test_no_constraint_table_carries_a_verdict_or_a_number_to_compare() -> None:
    """A constraint is a property of the posting. A decision is Phase 3.6."""
    forbidden = {
        "confidence", "score", "match_score", "priority_score",
        "eligibility_score", "eligible", "rank", "ranking", "weight",
        "profile_id", "recommendation",
    }
    for table in CONSTRAINT_TABLES:
        connection = connect_database(":memory:")
        try:
            apply_migrations(connection)
            assert forbidden.isdisjoint(_columns(connection, table)), table
        finally:
            connection.close()


def test_every_projection_row_hangs_off_its_posting(migrated) -> None:
    keys = {
        (row[2], row[3], row[4], row[6])
        for row in migrated.execute("PRAGMA foreign_key_list(opportunity_constraints)")
    }
    assert ("opportunities", "opportunity_id", "id", "CASCADE") in keys


def test_deleting_a_posting_deletes_its_whole_projection(migrated, rich_opportunity) -> None:
    migrated.execute("PRAGMA foreign_keys = ON")
    extract_one_opportunity(migrated, rich_opportunity)
    assert _counts(migrated)[0] == 1

    migrated.execute("DELETE FROM opportunities WHERE id = ?", (rich_opportunity,))
    migrated.commit()

    assert _counts(migrated) == (0,) * len(CONSTRAINT_TABLES)


def test_the_start_precision_check_refuses_a_row_that_lacks_its_parts(
    migrated, rich_opportunity
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO opportunity_constraints (opportunity_id, start_precision, "
            "start_month, extractor_version, source_fingerprint, extracted_at) "
            "VALUES (?, 'DATE', 2, ?, ?, 't')",
            (rich_opportunity, EXTRACTOR_VERSION, "0" * 64),
        )


def test_the_scalar_checks_refuse_a_value_outside_a_registry(
    migrated, rich_opportunity
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO opportunity_constraints (opportunity_id, visa_sponsorship, "
            "extractor_version, source_fingerprint, extracted_at) "
            "VALUES (?, 'MAYBE', ?, ?, 't')",
            (rich_opportunity, EXTRACTOR_VERSION, "0" * 64),
        )


def test_unknown_is_never_stored_as_a_word(migrated, rich_opportunity) -> None:
    """`NULL` is the single spelling of "not asserted" in the database."""
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO opportunity_constraints (opportunity_id, convention_requirement, "
            "extractor_version, source_fingerprint, extracted_at) "
            "VALUES (?, 'UNKNOWN', ?, ?, 't')",
            (rich_opportunity, EXTRACTOR_VERSION, "0" * 64),
        )


# --------------------------------------------------------------------------
# The reserved skills link
# --------------------------------------------------------------------------


def test_the_skill_link_points_at_the_shared_vocabulary(migrated) -> None:
    """One catalogue, so 3.5B extends `skills` instead of starting a rival."""
    keys = {
        (row[2], row[3], row[4])
        for row in migrated.execute(
            "PRAGMA foreign_key_list(opportunity_skill_requirements)"
        )
    }
    assert ("skills", "skill_id", "id") in keys


def test_phase_35a_writes_no_skill_requirement(migrated, rich_opportunity) -> None:
    """Extraction is 3.5B. A naive one would fill this with a stack listing."""
    synchronize_opportunity_constraints(migrated)

    assert int(
        migrated.execute("SELECT COUNT(*) FROM opportunity_skill_requirements").fetchone()[0]
    ) == 0


# --------------------------------------------------------------------------
# Storing and reading back
# --------------------------------------------------------------------------


def test_a_rich_posting_is_projected_and_read_back_unchanged(
    migrated, rich_opportunity
) -> None:
    reading, written = extract_one_opportunity(migrated, rich_opportunity)
    stored = read_opportunity_constraints(migrated, rich_opportunity)

    assert written
    assert stored == reading
    assert stored.opportunity_type is OpportunityType.PFE
    assert [item.min_months for item in stored.experience] == [36]
    assert [item.level for item in stored.education] == [EducationLevel.BAC_PLUS_5]
    assert stored.duration.min_months == 6
    assert (stored.start.year, stored.start.month) == (2027, 2)
    assert stored.locations == ("Casablanca", "Morocco")
    assert stored.work_mode is WorkMode.HYBRID
    assert stored.visa_sponsorship is VisaSponsorship.AVAILABLE
    assert stored.work_authorization is WorkAuthorization.REQUIRED
    assert stored.convention is ConventionRequirement.REQUIRED
    assert stored.evidence


def test_a_silent_posting_is_projected_with_nothing_asserted(migrated) -> None:
    """A row exists, and every scalar in it is NULL. Absence is not FALSE."""
    opportunity_id = insert_opportunity(migrated, location="Paris")
    extract_one_opportunity(migrated, opportunity_id)

    row = migrated.execute(
        "SELECT opportunity_type, duration_min_months, "
        "start_precision, work_mode, visa_sponsorship, work_authorization, "
        "convention_requirement FROM opportunity_constraints WHERE opportunity_id = ?",
        (opportunity_id,),
    ).fetchone()

    assert list(row) == [None] * 7
    stored = read_opportunity_constraints(migrated, opportunity_id)
    assert stored.experience == ()
    assert stored.visa_sponsorship is VisaSponsorship.UNKNOWN
    assert stored.work_authorization is WorkAuthorization.UNKNOWN
    assert stored.convention is ConventionRequirement.UNKNOWN
    # The address was collected, and it did not become an attendance policy.
    assert stored.locations == ("Paris",)
    assert stored.work_mode is None


def test_a_contradiction_is_stored_as_a_conflict_and_no_value(migrated) -> None:
    opportunity_id = insert_opportunity(migrated, description=CONFLICTING_DESCRIPTION)
    extract_one_opportunity(migrated, opportunity_id)

    row = migrated.execute(
        "SELECT constraint_kind, conflicting_values_json, rule_ids_json "
        "FROM opportunity_constraint_conflicts WHERE opportunity_id = ?",
        (opportunity_id,),
    ).fetchone()

    assert row[0] == ConstraintKind.WORK_MODE.value
    assert json.loads(row[1]) == ["ON_SITE", "REMOTE"]
    assert len(json.loads(row[2])) == 2
    assert migrated.execute(
        "SELECT work_mode FROM opportunity_constraints WHERE opportunity_id = ?",
        (opportunity_id,),
    ).fetchone()[0] is None


def test_evidence_rows_name_their_rule_and_stay_fragments(
    migrated, rich_opportunity
) -> None:
    extract_one_opportunity(migrated, rich_opportunity)

    rows = migrated.execute(
        "SELECT constraint_kind, source_field, rule_id, evidence_text "
        "FROM opportunity_constraint_evidence WHERE opportunity_id = ? ORDER BY position",
        (rich_opportunity,),
    ).fetchall()

    assert rows
    for kind, source_field, rule_id, text in rows:
        assert kind in {member.value for member in ConstraintKind}
        assert rule_id.strip() and re.fullmatch(r"[A-Z_]+_V\d+", rule_id)
        assert 0 < len(text) <= 200
    # The whole description is never one of them.
    assert all(len(row[3]) < len(RICH_DESCRIPTION) for row in rows)


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


def test_a_second_synchronization_writes_nothing(migrated, rich_opportunity) -> None:
    insert_opportunity(migrated, location="Paris")
    first = synchronize_opportunity_constraints(migrated)
    before = migrated.execute(
        "SELECT opportunity_id, extracted_at, created_at FROM opportunity_constraints "
        "ORDER BY opportunity_id"
    ).fetchall()

    second = synchronize_opportunity_constraints(migrated)

    assert (first.created, first.unchanged) == (2, 0)
    assert (second.created, second.replaced, second.unchanged) == (0, 0, 2)
    assert not second.changed
    # Same rows, timestamps included: nothing was rewritten.
    assert migrated.execute(
        "SELECT opportunity_id, extracted_at, created_at FROM opportunity_constraints "
        "ORDER BY opportunity_id"
    ).fetchall() == before


def test_a_changed_description_is_re_extracted(migrated, rich_opportunity) -> None:
    synchronize_opportunity_constraints(migrated)
    before = stored_signature(migrated, rich_opportunity)

    migrated.execute(
        "UPDATE opportunities SET description = ? WHERE id = ?",
        ("We do not sponsor visas for this role.", rich_opportunity),
    )
    migrated.commit()
    summary = synchronize_opportunity_constraints(migrated)

    assert summary.replaced == 1
    assert stored_signature(migrated, rich_opportunity) != before
    stored = read_opportunity_constraints(migrated, rich_opportunity)
    assert stored.visa_sponsorship is VisaSponsorship.NOT_AVAILABLE
    # The previous reading is gone whole, not layered under the new one.
    assert stored.experience == ()
    assert stored.education == ()


def test_a_projection_stored_under_an_older_version_is_re_extracted(
    migrated, rich_opportunity
) -> None:
    """And comes back labelled with the version that actually read it.

    The stored label has to describe the code that produced the rows. Before
    this test the service accepted an `extractor_version` argument: passing a
    different one forced a recomputation, but the reading carried
    `EXTRACTOR_VERSION` of its own, so the projection was written back under
    v1 while the caller believed it had asked for v2. The run looked right and
    the label lied. There is no such argument now, and this asserts the label.
    """
    synchronize_opportunity_constraints(migrated)
    migrated.execute(
        "UPDATE opportunity_constraints SET extractor_version = ? WHERE opportunity_id = ?",
        ("opportunity-constraints-v0", rich_opportunity),
    )
    migrated.commit()

    summary = synchronize_opportunity_constraints(migrated)

    assert (summary.replaced, summary.unchanged) == (1, 0)
    assert summary.extractor_version == EXTRACTOR_VERSION
    # The label now names the rules that read the posting, not a caller's wish.
    assert stored_signature(migrated, rich_opportunity)[1] == EXTRACTOR_VERSION
    assert read_opportunity_constraints(
        migrated, rich_opportunity
    ).extractor_version == EXTRACTOR_VERSION


def test_no_caller_can_name_the_version_a_projection_is_stored_under() -> None:
    """`EXTRACTOR_VERSION` is the contract; changing it means editing the code."""
    import inspect

    for function in (synchronize_opportunity_constraints, extract_one_opportunity):
        assert "extractor_version" not in inspect.signature(function).parameters


def test_extract_one_also_re_extracts_an_older_stored_version(
    migrated, rich_opportunity
) -> None:
    extract_one_opportunity(migrated, rich_opportunity)
    migrated.execute(
        "UPDATE opportunity_constraints SET extractor_version = ? WHERE opportunity_id = ?",
        ("opportunity-constraints-v0", rich_opportunity),
    )
    migrated.commit()

    _reading, written = extract_one_opportunity(migrated, rich_opportunity)

    assert written
    assert stored_signature(migrated, rich_opportunity)[1] == EXTRACTOR_VERSION


def test_the_fingerprint_stored_is_the_one_the_source_produces(
    migrated, rich_opportunity
) -> None:
    extract_one_opportunity(migrated, rich_opportunity)
    source = load_opportunity_source(migrated, rich_opportunity)

    assert stored_signature(migrated, rich_opportunity) == (
        source_fingerprint(source),
        EXTRACTOR_VERSION,
    )


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------


def test_a_failure_mid_write_leaves_the_previous_projection_intact(
    migrated, rich_opportunity
) -> None:
    extract_one_opportunity(migrated, rich_opportunity)
    before = read_opportunity_constraints(migrated, rich_opportunity)
    counts = _counts(migrated)

    def fail() -> None:
        raise RuntimeError("interrupted between the delete and the insert")

    source = load_opportunity_source(migrated, rich_opportunity)
    replacement = extract_opportunity_constraints(source)
    with pytest.raises(RuntimeError):
        store_opportunity_constraints(migrated, replacement, after_delete=fail)

    # The delete rolled back with the rest: the old reading is still whole.
    assert read_opportunity_constraints(migrated, rich_opportunity) == before
    assert _counts(migrated) == counts


def test_storing_for_an_absent_posting_is_refused(migrated) -> None:
    reading = extract_opportunity_constraints(
        OpportunitySource(9999, description="A role.")
    )

    with pytest.raises(OpportunityConstraintRepositoryError):
        store_opportunity_constraints(migrated, reading)
    assert _counts(migrated) == (0,) * len(CONSTRAINT_TABLES)


def test_synchronizing_before_the_migration_is_refused(tmp_path) -> None:
    connection = connect_database(tmp_path / "partial.db")
    try:
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
            if migration.version == "0012":
                continue
            connection.executescript(migration.path.read_text(encoding="utf-8"))
        with pytest.raises(OpportunityConstraintServiceError):
            synchronize_opportunity_constraints(connection)
    finally:
        connection.close()


# --------------------------------------------------------------------------
# The postings are the source, and they are not touched
# --------------------------------------------------------------------------


def test_synchronizing_mutates_no_posting(migrated, rich_opportunity) -> None:
    insert_opportunity(migrated, location="Paris")
    before = _opportunity_rows(migrated)

    synchronize_opportunity_constraints(migrated)

    assert _opportunity_rows(migrated) == before


def test_the_repository_writes_to_no_source_table() -> None:
    source = source_of(REPOSITORY_SOURCE)
    for table in ("opportunities", "opportunity_sources", "opportunity_qualifications"):
        for verb in ("INSERT INTO", "UPDATE", "DELETE FROM"):
            assert f"{verb} {table}" not in source


def test_the_legacy_score_columns_stay_untouched(migrated, rich_opportunity) -> None:
    """`0001` left `eligibility_score` and `match_score` on `opportunities`.

    Phase 3.5A does not compute them, does not read them and does not write
    them: a constraint is what a posting asks for, not a verdict about anybody.
    """
    synchronize_opportunity_constraints(migrated)

    row = migrated.execute(
        "SELECT relevance_score, eligibility_score, match_score, priority_score, "
        "interview_potential_score FROM opportunities WHERE id = ?",
        (rich_opportunity,),
    ).fetchone()
    assert list(row) == [None] * 5


def test_an_inactive_or_merged_posting_is_not_projected(migrated) -> None:
    inactive = insert_opportunity(migrated, is_active=0)
    merged = insert_opportunity(migrated, status="merged_duplicate")

    summary = synchronize_opportunity_constraints(migrated)

    assert summary.total == 0
    assert read_opportunity_constraints(migrated, inactive) is None
    assert read_opportunity_constraints(migrated, merged) is None
    with pytest.raises(OpportunityConstraintServiceError):
        extract_one_opportunity(migrated, inactive)


def test_the_qualification_type_is_read_when_the_classifier_stored_one(
    migrated,
) -> None:
    opportunity_id = insert_opportunity(
        migrated, title="Data Role", description="A role in our team."
    )
    migrated.execute(
        """INSERT INTO opportunity_qualifications (
               opportunity_id, qualification, primary_domain, opportunity_type,
               employment_type, listing_quality, matched_domains_json,
               matched_title_signals_json, matched_description_signals_json,
               matched_exclusion_signals_json, reasons_json, classifier_version,
               input_fingerprint, classified_at
           ) VALUES (?, 'CORE_TARGET', 'DATA_ENGINEERING', 'APPRENTICESHIP',
                     'UNKNOWN', 'NORMAL_LISTING', '[]', '[]', '[]', '[]', '[]',
                     'test-v1', ?, 't')""",
        (opportunity_id, "0" * 64),
    )
    migrated.commit()

    extract_one_opportunity(migrated, opportunity_id)
    stored = read_opportunity_constraints(migrated, opportunity_id)

    assert stored.opportunity_type is OpportunityType.ALTERNANCE


def test_the_summary_counts_and_never_quotes(migrated, rich_opportunity) -> None:
    insert_opportunity(migrated, description=CONFLICTING_DESCRIPTION)

    summary = synchronize_opportunity_constraints(migrated).as_dict()
    rendered = " ".join(str(value) for value in summary.values())

    assert summary["total_opportunities"] == 2
    assert summary["known_visa_sponsorship"] == 1
    assert summary["conflicts"] == 1
    for fragment in ("Casablanca", "sponsorship", "Convention", "remote", RICH_TITLE):
        assert fragment not in rendered


# --------------------------------------------------------------------------
# Several experience requirements, and no contradiction between them
# --------------------------------------------------------------------------

#: One posting asking for two different things, which `v1` recorded as a
#: contradiction and asserted neither of.
MULTI_EXPERIENCE_DESCRIPTION = (
    "7+ years of engineering experience. "
    "2+ years of AI/ML production experience. "
    "Prior experience with data platforms is a plus."
)


def test_several_experience_requirements_are_stored_as_several_rows(migrated) -> None:
    opportunity_id = insert_opportunity(migrated, description=MULTI_EXPERIENCE_DESCRIPTION)

    _reading, written = extract_one_opportunity(migrated, opportunity_id)

    assert written
    rows = migrated.execute(
        "SELECT position, min_months, max_months, obligation "
        "FROM opportunity_experience_requirements WHERE opportunity_id = ? "
        "ORDER BY position",
        (opportunity_id,),
    ).fetchall()
    assert [(row[1], row[2], row[3]) for row in rows] == [
        (84, None, None),
        (24, None, None),
        (None, None, "PREFERRED"),
    ]
    assert int(
        migrated.execute(
            "SELECT COUNT(*) FROM opportunity_constraint_conflicts WHERE opportunity_id = ?",
            (opportunity_id,),
        ).fetchone()[0]
    ) == 0


def test_several_experience_requirements_survive_a_round_trip(migrated) -> None:
    opportunity_id = insert_opportunity(migrated, description=MULTI_EXPERIENCE_DESCRIPTION)
    reading, _written = extract_one_opportunity(migrated, opportunity_id)

    stored = read_opportunity_constraints(migrated, opportunity_id)

    assert stored == reading
    assert [item.min_months for item in stored.experience] == [84, 24, None]


def test_a_posting_with_several_requirements_stays_idempotent(migrated) -> None:
    insert_opportunity(migrated, description=MULTI_EXPERIENCE_DESCRIPTION)
    first = synchronize_opportunity_constraints(migrated)
    before = migrated.execute(
        "SELECT id, opportunity_id, position, min_months, max_months, obligation, "
        "created_at FROM opportunity_experience_requirements ORDER BY position"
    ).fetchall()

    second = synchronize_opportunity_constraints(migrated)

    assert (first.created, first.known_experience) == (1, 1)
    assert (second.created, second.replaced, second.unchanged) == (0, 0, 1)
    assert not second.changed
    assert migrated.execute(
        "SELECT id, opportunity_id, position, min_months, max_months, obligation, "
        "created_at FROM opportunity_experience_requirements ORDER BY position"
    ).fetchall() == before


def test_known_experience_counts_postings_not_requirements(migrated) -> None:
    insert_opportunity(migrated, description=MULTI_EXPERIENCE_DESCRIPTION)
    insert_opportunity(migrated, description="Minimum 2 years of experience.")

    summary = synchronize_opportunity_constraints(migrated)

    assert summary.known_experience == 2
    assert int(
        migrated.execute(
            "SELECT COUNT(*) FROM opportunity_experience_requirements"
        ).fetchone()[0]
    ) == 4


def test_an_experience_row_must_require_something(migrated, rich_opportunity) -> None:
    extract_one_opportunity(migrated, rich_opportunity)

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO opportunity_experience_requirements "
            "(opportunity_id, position, min_months, max_months, obligation) "
            "VALUES (?, 99, NULL, NULL, NULL)",
            (rich_opportunity,),
        )


def test_a_salary_range_never_becomes_a_stored_experience(migrated) -> None:
    opportunity_id = insert_opportunity(
        migrated,
        description=(
            "The range displayed reflects the minimum and maximum target for new "
            "hire salaries across career levels and experience."
        ),
    )

    extract_one_opportunity(migrated, opportunity_id)

    assert int(
        migrated.execute(
            "SELECT COUNT(*) FROM opportunity_experience_requirements "
            "WHERE opportunity_id = ?",
            (opportunity_id,),
        ).fetchone()[0]
    ) == 0


@pytest.mark.parametrize("slot", ("EDUCATION", "EXPERIENCE", "LOCATION"))
def test_the_conflict_table_refuses_a_multi_valued_slot(
    migrated, rich_opportunity, slot
) -> None:
    """Several answers are not a disagreement, so none of these can appear."""
    extract_one_opportunity(migrated, rich_opportunity)

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO opportunity_constraint_conflicts (opportunity_id, "
            "constraint_slot, constraint_kind, conflicting_values_json, rule_ids_json) "
            "VALUES (?, ?, ?, '[\"A\",\"B\"]', '[\"R\"]')",
            (rich_opportunity, slot, slot),
        )


def test_one_slot_holds_at_most_one_conflict(migrated, rich_opportunity) -> None:
    extract_one_opportunity(migrated, rich_opportunity)
    values = (rich_opportunity, "WORK_MODE", "WORK_MODE", '["A","B"]', '["R"]')
    migrated.execute(
        "INSERT INTO opportunity_constraint_conflicts (opportunity_id, "
        "constraint_slot, constraint_kind, conflicting_values_json, rule_ids_json) "
        "VALUES (?, ?, ?, ?, ?)",
        values,
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO opportunity_constraint_conflicts (opportunity_id, "
            "constraint_slot, constraint_kind, conflicting_values_json, rule_ids_json) "
            "VALUES (?, ?, ?, ?, ?)",
            values,
        )


# --------------------------------------------------------------------------
# A projected place is explainable
# --------------------------------------------------------------------------


def test_a_projected_location_carries_its_own_evidence(migrated) -> None:
    opportunity_id = insert_opportunity(
        migrated, location="Ville Exemple", country="Pays Exemple"
    )
    extract_one_opportunity(migrated, opportunity_id)

    places = [
        row[0]
        for row in migrated.execute(
            "SELECT location_text FROM opportunity_constraint_locations "
            "WHERE opportunity_id = ? ORDER BY position",
            (opportunity_id,),
        )
    ]
    evidence = migrated.execute(
        "SELECT source_field, rule_id, evidence_text, normalized_value "
        "FROM opportunity_constraint_evidence "
        "WHERE opportunity_id = ? AND constraint_kind = 'LOCATION' ORDER BY position",
        (opportunity_id,),
    ).fetchall()

    assert places == ["Ville Exemple", "Pays Exemple"]
    assert [row[0] for row in evidence] == ["LOCATION", "COUNTRY"]
    assert [row[1] for row in evidence] == [
        "LOCATION_COLLECTED_FIELD_V1",
        "COUNTRY_COLLECTED_FIELD_V1",
    ]
    # Every projected place is explained, and nothing else is.
    assert [row[3] for row in evidence] == places


def test_a_posting_with_no_collected_place_projects_none(migrated) -> None:
    opportunity_id = insert_opportunity(migrated)
    extract_one_opportunity(migrated, opportunity_id)

    assert int(
        migrated.execute(
            "SELECT COUNT(*) FROM opportunity_constraint_locations WHERE opportunity_id = ?",
            (opportunity_id,),
        ).fetchone()[0]
    ) == 0
    assert int(
        migrated.execute(
            "SELECT COUNT(*) FROM opportunity_constraint_evidence "
            "WHERE opportunity_id = ? AND constraint_kind = 'LOCATION'",
            (opportunity_id,),
        ).fetchone()[0]
    ) == 0


# --------------------------------------------------------------------------
# The shared skills vocabulary is protected
# --------------------------------------------------------------------------


def test_a_skill_an_offer_requires_cannot_be_deleted_from_the_vocabulary(
    migrated, rich_opportunity
) -> None:
    """`RESTRICT`, like `profile_skills` in `0008`.

    `CASCADE` would let a vocabulary cleanup silently drop a requirement a
    posting stated: the constraint would vanish without anybody deciding it
    should.
    """
    migrated.execute("PRAGMA foreign_keys = ON")
    extract_one_opportunity(migrated, rich_opportunity)
    skill_id = int(
        migrated.execute(
            "INSERT INTO skills (canonical_key, canonical_name) "
            "VALUES ('test-only-skill', 'TEST ONLY Skill') RETURNING id"
        ).fetchone()[0]
    )
    # Written by hand: Phase 3.5A extracts no skill. This is the 3.5B socle.
    migrated.execute(
        "INSERT INTO opportunity_skill_requirements (opportunity_id, skill_id, "
        "requirement, extractor_version) VALUES (?, ?, 'REQUIRED', ?)",
        (rich_opportunity, skill_id, EXTRACTOR_VERSION),
    )
    migrated.commit()

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("DELETE FROM skills WHERE id = ?", (skill_id,))


def test_removing_the_posting_releases_the_vocabulary_reference(
    migrated, rich_opportunity
) -> None:
    """The requirement goes with its posting, and then the term is free."""
    migrated.execute("PRAGMA foreign_keys = ON")
    extract_one_opportunity(migrated, rich_opportunity)
    skill_id = int(
        migrated.execute(
            "INSERT INTO skills (canonical_key, canonical_name) "
            "VALUES ('test-only-skill', 'TEST ONLY Skill') RETURNING id"
        ).fetchone()[0]
    )
    migrated.execute(
        "INSERT INTO opportunity_skill_requirements (opportunity_id, skill_id, "
        "requirement, extractor_version) VALUES (?, ?, 'REQUIRED', ?)",
        (rich_opportunity, skill_id, EXTRACTOR_VERSION),
    )
    migrated.commit()

    migrated.execute("DELETE FROM opportunities WHERE id = ?", (rich_opportunity,))
    migrated.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
    migrated.commit()

    assert int(
        migrated.execute("SELECT COUNT(*) FROM opportunity_skill_requirements").fetchone()[0]
    ) == 0
    assert int(migrated.execute("SELECT COUNT(*) FROM skills").fetchone()[0]) == 0
