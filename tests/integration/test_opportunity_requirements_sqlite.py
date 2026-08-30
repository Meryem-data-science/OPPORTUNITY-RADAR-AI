"""Migration 0013 and the Phase 3.5B requirement projection, on disposable SQLite.

Every database here is created under `tmp_path` and thrown away. Every posting
is invented; no real listing, company or description takes part. **The
operational `.data/` database is never opened**, no count taken from it appears
anywhere below, and nothing in this file writes to any database a person uses.

The projection is derived data hanging off another projection, so most of these
tests are about what it may **not** do: seed a catalogue, mutate the postings it
reads, mutate the Phase 3.5A projection it depends on, touch a profile, keep a
reading whose source changed, leave half a reading behind after a failure, or
grow a column that compares a posting to a person.
"""

import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
    split_sql_statements,
)
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)
from services.collector.extractors.opportunity_constraints.requirements.extractor import (
    extract_opportunity_requirements,
    requirement_source_fingerprint,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    REQUIREMENT_EXTRACTOR_VERSION,
    RequirementLevel,
    RequirementSource,
)
from services.collector.extractors.opportunity_constraints.requirements.repository import (
    OpportunityRequirementRepositoryError,
    delete_opportunity_requirements,
    read_opportunity_requirements,
    store_opportunity_requirements,
    stored_requirement_signature,
)
from services.collector.extractors.opportunity_constraints.requirements.service import (
    OpportunityRequirementServiceError,
    extract_one_opportunity_requirements,
    load_requirement_source,
    synchronize_opportunity_requirements,
)

REPOSITORY_SOURCE = Path(
    "services/collector/extractors/opportunity_constraints/requirements/repository.py"
)
SERVICE_SOURCE = Path(
    "services/collector/extractors/opportunity_constraints/requirements/service.py"
)
MIGRATION = Path("migrations/0013_opportunity_requirements.sql")

BEFORE_THIS_SLICE = (
    "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009",
    "0010", "0011", "0012",
)

#: The five tables `0013` adds, plus the one `0012` reserved for it.
REQUIREMENT_TABLES = (
    "opportunity_skill_requirements",
    "opportunity_skill_requirement_evidence",
    "opportunity_language_requirements",
    "opportunity_language_requirement_evidence",
    "opportunity_requirement_ambiguities",
    "opportunity_requirement_extraction_state",
)

#: The Phase 3.5A tables this slice must leave exactly as it found them.
CONSTRAINT_TABLES = (
    "opportunity_constraints",
    "opportunity_constraint_locations",
    "opportunity_education_requirements",
    "opportunity_experience_requirements",
    "opportunity_constraint_evidence",
    "opportunity_constraint_conflicts",
)

# TEST ONLY postings, invented for these tests.
RICH_DESCRIPTION = (
    "&lt;h3&gt;Required Qualifications&lt;/h3&gt;&lt;ul&gt;"
    "&lt;li&gt;Strong Python skills&lt;/li&gt;"
    "&lt;li&gt;SQL&lt;/li&gt;"
    "&lt;li&gt;Fluent English&lt;/li&gt;"
    "&lt;li&gt;Spark or Flink&lt;/li&gt;&lt;/ul&gt;"
    "&lt;h3&gt;Nice to have&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Airflow&lt;/li&gt;&lt;/ul&gt;"
    "&lt;h3&gt;Our stack&lt;/h3&gt;&lt;p&gt;We use Kafka and MongoDB daily.&lt;/p&gt;"
)
SILENT_DESCRIPTION = "We build good products with a great team in our office."


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "requirements.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def insert_opportunity(
    connection, *, title="TEST ONLY Role", description=SILENT_DESCRIPTION,
    status="new", is_active=1,
) -> int:
    row = connection.execute(
        """INSERT INTO opportunities (
               canonical_title, organization, description, discovered_at,
               first_seen_at, last_seen_at, source_url, status, is_active
           ) VALUES (?, 'TEST ONLY Org', ?, 't', 't', 't',
                     'https://example.invalid/1', ?, ?)
           RETURNING id""",
        (title, description, status, is_active),
    ).fetchone()
    connection.commit()
    return int(row[0])


@pytest.fixture
def rich_opportunity(migrated) -> int:
    """One posting, already through Phase 3.5A, as 3.5B requires."""
    opportunity_id = insert_opportunity(migrated, description=RICH_DESCRIPTION)
    synchronize_opportunity_constraints(migrated)
    return opportunity_id


def executable_sql(path: Path) -> str:
    """A migration's statements, with its `--` commentary removed."""
    return re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))


def _tables(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _counts(connection, tables=REQUIREMENT_TABLES) -> tuple[int, ...]:
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    )


def _constraint_rows(connection) -> list[tuple]:
    return [
        tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall())
        for table in CONSTRAINT_TABLES
    ]


def _opportunity_rows(connection) -> list[tuple]:
    return connection.execute(
        "SELECT id, canonical_title, organization, location, country, remote_type, "
        "description, opportunity_type, employment_type, deadline, status, "
        "is_active, relevance_score, eligibility_score, match_score, "
        "priority_score, created_at, updated_at FROM opportunities ORDER BY id"
    ).fetchall()


# --------------------------------------------------------------------------
# Migration 0013
# --------------------------------------------------------------------------


def test_migration_0013_is_discovered_after_the_earlier_ones() -> None:
    versions = [
        migration.version
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY)
    ]

    assert versions[-1] == "0013"
    assert versions[: len(BEFORE_THIS_SLICE)] == list(BEFORE_THIS_SLICE)


def test_migration_0013_creates_the_five_new_tables(migrated) -> None:
    assert set(REQUIREMENT_TABLES) <= _tables(migrated)


def test_migration_0013_alters_no_existing_table() -> None:
    body = executable_sql(MIGRATION).upper()

    assert "ALTER TABLE" not in body
    assert "DROP" not in body


def test_migration_0013_does_not_recreate_the_reserved_skill_table() -> None:
    """`0012` created it. Extending one catalogue, not starting a rival."""
    body = executable_sql(MIGRATION)

    assert "CREATE TABLE opportunity_skill_requirements" not in body
    for rival in (
        "opportunity_skill_requirements_v2", "offer_skills", "required_skills"
    ):
        assert rival not in body


def test_migration_0013_seeds_no_vocabulary() -> None:
    """The catalogue lives in code; a row means a posting was found to want it."""
    body = executable_sql(MIGRATION).upper()

    assert "INSERT" not in body


@pytest.mark.parametrize("table", REQUIREMENT_TABLES + ("skills",))
def test_applying_the_migration_states_nothing_about_any_posting(
    migrated, table
) -> None:
    assert int(migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) == 0


def test_the_earlier_migrations_are_untouched() -> None:
    """0001 to 0012 are applied history and are never edited."""
    for version in BEFORE_THIS_SLICE:
        matches = list(DEFAULT_MIGRATIONS_DIRECTORY.glob(f"{version}_*.sql"))
        assert len(matches) == 1


def test_no_requirement_table_carries_a_verdict_or_a_number_to_compare() -> None:
    """A requirement is a property of the posting. A decision is Phase 3.6."""
    forbidden = {
        "confidence", "score", "match_score", "priority_score", "eligibility_score",
        "eligible", "rank", "ranking", "weight", "profile_id", "recommendation",
        "candidate_has", "skill_gap", "missing_skill",
    }
    connection = connect_database(":memory:")
    try:
        apply_migrations(connection)
        for table in REQUIREMENT_TABLES:
            assert forbidden.isdisjoint(_columns(connection, table)), table
    finally:
        connection.close()


def test_every_requirement_row_hangs_off_the_phase_35a_projection(migrated) -> None:
    """3.5B depends on 3.5A structurally, not by convention."""
    for table in (
        "opportunity_language_requirements",
        "opportunity_requirement_ambiguities",
        "opportunity_requirement_extraction_state",
    ):
        keys = {
            (row[2], row[3], row[4], row[6])
            for row in migrated.execute(f"PRAGMA foreign_key_list({table})")
        }
        assert (
            "opportunity_constraints", "opportunity_id", "opportunity_id", "CASCADE"
        ) in keys


def test_evidence_hangs_off_the_requirement_it_explains(migrated) -> None:
    for table, parent in (
        ("opportunity_skill_requirement_evidence", "opportunity_skill_requirements"),
        (
            "opportunity_language_requirement_evidence",
            "opportunity_language_requirements",
        ),
    ):
        keys = {
            (row[2], row[4], row[6])
            for row in migrated.execute(f"PRAGMA foreign_key_list({table})")
        }
        assert (parent, "id", "CASCADE") in keys


def test_the_skill_link_still_restricts_the_shared_vocabulary(
    migrated, rich_opportunity
) -> None:
    """`0012`'s RESTRICT is untouched: a term an offer requires cannot be dropped."""
    migrated.execute("PRAGMA foreign_keys = ON")
    synchronize_opportunity_requirements(migrated)
    skill_id = migrated.execute(
        "SELECT skill_id FROM opportunity_skill_requirements LIMIT 1"
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("DELETE FROM skills WHERE id = ?", (skill_id,))


@pytest.mark.parametrize(
    "statement, parameters",
    (
        (
            "INSERT INTO opportunity_language_requirements (opportunity_id, "
            "language_key, language_name, requirement, extractor_version) "
            "VALUES (?, 'english', 'English', 'MAYBE', 'v')",
            None,
        ),
        (
            "INSERT INTO opportunity_requirement_ambiguities (opportunity_id, "
            "position, kind, reason, rule_id, evidence_text, extractor_version) "
            "VALUES (?, 0, 'SKILL', 'BECAUSE', 'R', 'x', 'v')",
            None,
        ),
        (
            "INSERT INTO opportunity_requirement_ambiguities (opportunity_id, "
            "position, kind, reason, rule_id, evidence_text, extractor_version) "
            "VALUES (?, 0, 'SOMETHING', 'ALTERNATIVE_GROUP_UNSUPPORTED', 'R', "
            "'x', 'v')",
            None,
        ),
        (
            "INSERT INTO opportunity_requirement_extraction_state (opportunity_id, "
            "source_fingerprint, extractor_version, extracted_at) "
            "VALUES (?, 'not-a-digest', 'v', 't')",
            None,
        ),
    ),
)
def test_the_checks_refuse_a_value_outside_a_registry(
    migrated, rich_opportunity, statement, parameters
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(statement, (rich_opportunity,))


def test_a_language_requirement_is_stored_once_per_language(
    migrated, rich_opportunity
) -> None:
    migrated.execute(
        "INSERT INTO opportunity_language_requirements (opportunity_id, "
        "language_key, language_name, requirement, extractor_version) "
        "VALUES (?, 'english', 'English', 'REQUIRED', 'v')",
        (rich_opportunity,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO opportunity_language_requirements (opportunity_id, "
            "language_key, language_name, requirement, extractor_version) "
            "VALUES (?, 'english', 'English', 'PREFERRED', 'v')",
            (rich_opportunity,),
        )


def test_an_evidence_row_cannot_hold_a_whole_description(migrated) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO opportunity_skill_requirement_evidence "
            "(opportunity_skill_requirement_id, position, source_field, "
            "observed_requirement, rule_id, evidence_text, extractor_version) "
            "VALUES (1, 0, 'DESCRIPTION', 'REQUIRED', 'R', ?, 'v')",
            ("x" * 201,),
        )


def test_deleting_a_posting_deletes_its_whole_requirement_reading(
    migrated, rich_opportunity
) -> None:
    migrated.execute("PRAGMA foreign_keys = ON")
    synchronize_opportunity_requirements(migrated)
    assert _counts(migrated)[0] > 0

    migrated.execute("DELETE FROM opportunities WHERE id = ?", (rich_opportunity,))
    migrated.commit()

    assert _counts(migrated) == (0,) * len(REQUIREMENT_TABLES)


def test_a_phase_35a_rebuild_takes_the_requirement_reading_with_it(
    migrated, rich_opportunity
) -> None:
    """A state row can never outlive the projection it was computed beside."""
    migrated.execute("PRAGMA foreign_keys = ON")
    synchronize_opportunity_requirements(migrated)
    assert stored_requirement_signature(migrated, rich_opportunity) is not None

    migrated.execute(
        "UPDATE opportunities SET description = ? WHERE id = ?",
        (RICH_DESCRIPTION + " This is a hybrid role.", rich_opportunity),
    )
    migrated.commit()
    synchronize_opportunity_constraints(migrated)

    assert _counts(migrated) == (0,) * len(REQUIREMENT_TABLES)
    assert stored_requirement_signature(migrated, rich_opportunity) is None


# --------------------------------------------------------------------------
# Storing and reading back
# --------------------------------------------------------------------------


def test_a_rich_posting_is_projected_and_read_back_unchanged(
    migrated, rich_opportunity
) -> None:
    source, projected = load_requirement_source(migrated, rich_opportunity)
    assert projected
    reading = extract_opportunity_requirements(source)
    store_opportunity_requirements(migrated, reading)

    assert read_opportunity_requirements(migrated, rich_opportunity) == reading


def test_the_projected_rows_say_what_the_posting_said(
    migrated, rich_opportunity
) -> None:
    synchronize_opportunity_requirements(migrated)

    skills = dict(
        migrated.execute(
            "SELECT s.canonical_name, r.requirement FROM opportunity_skill_requirements "
            "AS r JOIN skills AS s ON s.id = r.skill_id ORDER BY s.canonical_key"
        )
    )
    languages = dict(
        migrated.execute(
            "SELECT language_name, proficiency_text "
            "FROM opportunity_language_requirements"
        )
    )

    assert skills == {
        "Python": "REQUIRED", "SQL": "REQUIRED", "Apache Airflow": "PREFERRED"
    }
    assert languages == {"English": "Fluent"}


def test_a_stack_paragraph_produces_no_requirement(migrated, rich_opportunity) -> None:
    """`Kafka` and `MongoDB` are in the posting, under `Our stack`."""
    synchronize_opportunity_requirements(migrated)
    stored = {
        row[0]
        for row in migrated.execute(
            "SELECT s.canonical_name FROM opportunity_skill_requirements AS r "
            "JOIN skills AS s ON s.id = r.skill_id"
        )
    }

    assert "Apache Kafka" not in stored
    assert "MongoDB" not in stored


def test_a_refused_alternative_is_recorded_rather_than_lost(
    migrated, rich_opportunity
) -> None:
    """`Spark or Flink` is an explicit demand this version cannot represent."""
    synchronize_opportunity_requirements(migrated)
    rows = migrated.execute(
        "SELECT kind, reason, evidence_text, context_heading_text "
        "FROM opportunity_requirement_ambiguities"
    ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "SKILL"
    assert rows[0][1] == "ALTERNATIVE_GROUP_UNSUPPORTED"
    assert rows[0][3] == "Required Qualifications"


def test_a_posting_requiring_nothing_is_still_recorded_as_read(migrated) -> None:
    """Zero is an answer, and it is not the same answer as "never read"."""
    opportunity_id = insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    synchronize_opportunity_constraints(migrated)
    synchronize_opportunity_requirements(migrated)

    assert _counts(migrated)[:-1] == (0, 0, 0, 0, 0)
    assert stored_requirement_signature(migrated, opportunity_id) == (
        requirement_source_fingerprint(
            RequirementSource(opportunity_id, SILENT_DESCRIPTION)
        ),
        REQUIREMENT_EXTRACTOR_VERSION,
    )


def test_evidence_names_its_rule_its_section_and_stays_a_fragment(
    migrated, rich_opportunity
) -> None:
    synchronize_opportunity_requirements(migrated)
    rows = migrated.execute(
        "SELECT rule_id, evidence_text, context_heading_text, observed_requirement, "
        "source_field FROM opportunity_skill_requirement_evidence"
    ).fetchall()

    assert rows
    for rule_id, text, heading, observed, field in rows:
        assert rule_id.strip() == rule_id and rule_id
        assert 0 < len(text) <= 200
        assert heading in ("Required Qualifications", "Nice to have")
        assert observed in ("REQUIRED", "PREFERRED")
        assert field == "DESCRIPTION"
        # The heading is stored beside the fragment, never welded onto it.
        assert ">" not in text


def test_a_preferred_mention_of_a_required_skill_keeps_its_own_level(
    migrated,
) -> None:
    insert_opportunity(
        migrated, description="Nice to have\n- Python\nRequirements\n- Python"
    )
    synchronize_opportunity_constraints(migrated)
    synchronize_opportunity_requirements(migrated)

    requirement = migrated.execute(
        "SELECT requirement FROM opportunity_skill_requirements"
    ).fetchone()
    observed = [
        row[0]
        for row in migrated.execute(
            "SELECT observed_requirement FROM opportunity_skill_requirement_evidence "
            "ORDER BY position"
        )
    ]

    assert requirement[0] == "REQUIRED"
    assert observed == ["PREFERRED", "REQUIRED"]


# --------------------------------------------------------------------------
# The shared vocabulary
# --------------------------------------------------------------------------


def test_an_observed_requirement_creates_its_vocabulary_row(
    migrated, rich_opportunity
) -> None:
    assert int(migrated.execute("SELECT COUNT(*) FROM skills").fetchone()[0]) == 0

    summary = synchronize_opportunity_requirements(migrated)

    stored = {row[0] for row in migrated.execute("SELECT canonical_key FROM skills")}
    assert stored == {"python", "sql", "apache airflow"}
    assert summary.new_skill_vocabulary_rows == 3


def test_a_catalogue_term_no_posting_required_never_reaches_the_database(
    migrated, rich_opportunity
) -> None:
    """115 catalogue entries, three observed requirements, three rows."""
    synchronize_opportunity_requirements(migrated)

    assert int(migrated.execute("SELECT COUNT(*) FROM skills").fetchone()[0]) == 3


def test_an_existing_vocabulary_row_is_reused_and_never_renamed(migrated) -> None:
    migrated.execute(
        "INSERT INTO skills (canonical_key, canonical_name) VALUES ('python', 'PYTHON')"
    )
    migrated.commit()
    existing = int(
        migrated.execute("SELECT id FROM skills WHERE canonical_key='python'").fetchone()[0]
    )
    insert_opportunity(migrated, description="Requirements\n- Python")
    synchronize_opportunity_constraints(migrated)

    summary = synchronize_opportunity_requirements(migrated)

    assert summary.new_skill_vocabulary_rows == 0
    assert int(
        migrated.execute("SELECT skill_id FROM opportunity_skill_requirements").fetchone()[0]
    ) == existing
    assert migrated.execute(
        "SELECT canonical_name FROM skills WHERE id = ?", (existing,)
    ).fetchone()[0] == "PYTHON"


def test_a_removed_requirement_leaves_the_vocabulary_row_behind(
    migrated, rich_opportunity
) -> None:
    """An unused term is allowed to stay: it is vocabulary, not a claim."""
    synchronize_opportunity_requirements(migrated)
    migrated.execute(
        "UPDATE opportunities SET description = ? WHERE id = ?",
        ("We build good products.", rich_opportunity),
    )
    migrated.commit()
    synchronize_opportunity_constraints(migrated)
    synchronize_opportunity_requirements(migrated)

    assert int(
        migrated.execute("SELECT COUNT(*) FROM opportunity_skill_requirements").fetchone()[0]
    ) == 0
    assert int(migrated.execute("SELECT COUNT(*) FROM skills").fetchone()[0]) == 3


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


def test_a_second_synchronization_writes_nothing(migrated, rich_opportunity) -> None:
    first = synchronize_opportunity_requirements(migrated)
    before = migrated.execute(
        "SELECT * FROM opportunity_requirement_extraction_state"
    ).fetchall()

    second = synchronize_opportunity_requirements(migrated)

    assert first.changed and first.created == 1
    assert not second.changed and second.unchanged == 1
    assert (
        migrated.execute(
            "SELECT * FROM opportunity_requirement_extraction_state"
        ).fetchall()
        == before
    )


def test_a_posting_requiring_nothing_is_idempotent_too(migrated) -> None:
    insert_opportunity(migrated, description=SILENT_DESCRIPTION)
    synchronize_opportunity_constraints(migrated)
    synchronize_opportunity_requirements(migrated)

    second = synchronize_opportunity_requirements(migrated)

    assert not second.changed and second.unchanged == 1


def test_a_changed_description_is_re_extracted(migrated, rich_opportunity) -> None:
    synchronize_opportunity_requirements(migrated)
    migrated.execute(
        "UPDATE opportunities SET description = ? WHERE id = ?",
        ("Requirements\n- Docker", rich_opportunity),
    )
    migrated.commit()
    synchronize_opportunity_constraints(migrated)

    summary = synchronize_opportunity_requirements(migrated)

    assert summary.created + summary.replaced == 1
    stored = {
        row[0]
        for row in migrated.execute(
            "SELECT s.canonical_name FROM opportunity_skill_requirements AS r "
            "JOIN skills AS s ON s.id = r.skill_id"
        )
    }
    assert stored == {"Docker"}


def test_a_reading_stored_under_an_older_version_is_re_extracted(
    migrated, rich_opportunity
) -> None:
    synchronize_opportunity_requirements(migrated)
    migrated.execute(
        "UPDATE opportunity_requirement_extraction_state "
        "SET extractor_version = 'opportunity-requirements-v0'"
    )
    migrated.commit()

    summary = synchronize_opportunity_requirements(migrated)

    assert summary.replaced == 1
    assert stored_requirement_signature(migrated, rich_opportunity)[1] == (
        REQUIREMENT_EXTRACTOR_VERSION
    )


def test_no_caller_can_name_the_version_a_reading_is_stored_under() -> None:
    """A label naming a version that never read the posting explains nothing."""
    for entry_point in (
        synchronize_opportunity_requirements,
        extract_one_opportunity_requirements,
        store_opportunity_requirements,
    ):
        assert "extractor_version" not in inspect.signature(entry_point).parameters


def test_the_fingerprint_stored_is_the_one_the_source_produces(
    migrated, rich_opportunity
) -> None:
    synchronize_opportunity_requirements(migrated)
    source, _ = load_requirement_source(migrated, rich_opportunity)

    assert stored_requirement_signature(migrated, rich_opportunity) == (
        requirement_source_fingerprint(source),
        REQUIREMENT_EXTRACTOR_VERSION,
    )


def test_extract_one_also_re_extracts_an_older_stored_version(
    migrated, rich_opportunity
) -> None:
    extract_one_opportunity_requirements(migrated, rich_opportunity)
    _, written, _ = extract_one_opportunity_requirements(migrated, rich_opportunity)
    assert written is False

    migrated.execute(
        "UPDATE opportunity_requirement_extraction_state SET extractor_version = 'old'"
    )
    migrated.commit()
    _, rewritten, _ = extract_one_opportunity_requirements(migrated, rich_opportunity)

    assert rewritten is True


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------


def test_a_failure_mid_write_leaves_the_previous_reading_intact(
    migrated, rich_opportunity
) -> None:
    synchronize_opportunity_requirements(migrated)
    before = read_opportunity_requirements(migrated, rich_opportunity)
    source, _ = load_requirement_source(migrated, rich_opportunity)
    reading = extract_opportunity_requirements(source)

    def explode() -> None:
        raise RuntimeError("interrupted between the delete and the insert")

    with pytest.raises(RuntimeError):
        store_opportunity_requirements(migrated, reading, after_delete=explode)

    assert read_opportunity_requirements(migrated, rich_opportunity) == before


def test_a_failed_write_creates_no_vocabulary_row(migrated) -> None:
    """Vocabulary rows are inside the transaction, so they roll back with it."""
    opportunity_id = insert_opportunity(
        migrated, description="Requirements\n- Terraform"
    )
    synchronize_opportunity_constraints(migrated)
    source, _ = load_requirement_source(migrated, opportunity_id)
    reading = extract_opportunity_requirements(source)

    def explode() -> None:
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        store_opportunity_requirements(migrated, reading, after_delete=explode)

    assert int(migrated.execute("SELECT COUNT(*) FROM skills").fetchone()[0]) == 0
    assert _counts(migrated) == (0,) * len(REQUIREMENT_TABLES)


def test_a_requirement_never_survives_without_its_evidence(
    migrated, rich_opportunity
) -> None:
    synchronize_opportunity_requirements(migrated)
    orphans = int(
        migrated.execute(
            "SELECT COUNT(*) FROM opportunity_skill_requirements AS r "
            "WHERE NOT EXISTS (SELECT 1 FROM opportunity_skill_requirement_evidence "
            "AS e WHERE e.opportunity_skill_requirement_id = r.id)"
        ).fetchone()[0]
    )

    assert orphans == 0


def test_deleting_a_reading_removes_its_evidence(migrated, rich_opportunity) -> None:
    synchronize_opportunity_requirements(migrated)

    delete_opportunity_requirements(migrated, rich_opportunity)
    migrated.commit()

    assert _counts(migrated) == (0,) * len(REQUIREMENT_TABLES)


# --------------------------------------------------------------------------
# Phase 3.5A is a prerequisite, and Phase 3.5A is untouched
# --------------------------------------------------------------------------


def test_synchronizing_without_the_35a_projection_is_refused(migrated) -> None:
    insert_opportunity(migrated, description="Requirements\n- Python")

    with pytest.raises(OpportunityRequirementServiceError, match="3.5A"):
        synchronize_opportunity_requirements(migrated)

    assert _counts(migrated) == (0,) * len(REQUIREMENT_TABLES)


def test_extract_one_without_the_35a_projection_is_refused(migrated) -> None:
    opportunity_id = insert_opportunity(migrated, description="Requirements\n- Python")

    with pytest.raises(OpportunityRequirementServiceError, match="3.5A"):
        extract_one_opportunity_requirements(migrated, opportunity_id)


def test_storing_without_the_35a_projection_is_refused(migrated) -> None:
    opportunity_id = insert_opportunity(migrated, description="Requirements\n- Python")
    reading = extract_opportunity_requirements(
        RequirementSource(opportunity_id, "Requirements\n- Python")
    )

    with pytest.raises(OpportunityRequirementRepositoryError, match="3.5A"):
        store_opportunity_requirements(migrated, reading)


def test_this_slice_never_runs_the_35a_synchronization_for_you() -> None:
    """Two phases that trigger each other are two phases nobody can reason about."""
    body = SERVICE_SOURCE.read_text(encoding="utf-8")

    assert "synchronize_opportunity_constraints" not in body


def test_synchronizing_changes_no_phase_35a_row(migrated, rich_opportunity) -> None:
    before = _constraint_rows(migrated)

    synchronize_opportunity_requirements(migrated)

    assert _constraint_rows(migrated) == before


def test_synchronizing_mutates_no_posting(migrated, rich_opportunity) -> None:
    before = _opportunity_rows(migrated)

    synchronize_opportunity_requirements(migrated)

    assert _opportunity_rows(migrated) == before


def test_the_repository_writes_to_no_source_table() -> None:
    body = REPOSITORY_SOURCE.read_text(encoding="utf-8").upper()

    for forbidden in (
        "INSERT INTO OPPORTUNITIES", "UPDATE OPPORTUNITIES", "DELETE FROM OPPORTUNITIES",
        "INSERT INTO OPPORTUNITY_CONSTRAINTS", "UPDATE OPPORTUNITY_CONSTRAINTS",
        "DELETE FROM OPPORTUNITY_CONSTRAINTS",
    ):
        assert forbidden not in body


def test_the_legacy_score_columns_stay_untouched(migrated, rich_opportunity) -> None:
    """`0001` left them; nothing in this slice writes them."""
    synchronize_opportunity_requirements(migrated)
    row = migrated.execute(
        "SELECT eligibility_score, match_score, priority_score FROM opportunities "
        "WHERE id = ?",
        (rich_opportunity,),
    ).fetchone()

    assert row == (None, None, None) or all(value is None for value in row)


# --------------------------------------------------------------------------
# No profile is read, and none is written
# --------------------------------------------------------------------------


def test_no_module_in_this_slice_reads_a_profile_table() -> None:
    """No SQL in this package names a profile table.

    The check is on statements rather than on the word, because several
    docstrings name `profile_skills` and `profile_languages` precisely to say
    this slice does not touch them — a guard that read its own explanation as a
    violation would be a guard nobody can write comments around.
    """
    package = Path(
        "services/collector/extractors/opportunity_constraints/requirements"
    )
    statement = re.compile(
        r"(?:FROM|JOIN|INTO|UPDATE)\s+(?:profiles|profile_\w+)", re.IGNORECASE
    )
    for path in sorted(package.glob("*.py")):
        assert not statement.search(path.read_text(encoding="utf-8")), path.name


def test_synchronizing_creates_no_profile_fact(migrated, rich_opportunity) -> None:
    before = tuple(
        int(migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("profiles", "profile_facts", "profile_skills", "profile_languages")
    )

    synchronize_opportunity_requirements(migrated)

    after = tuple(
        int(migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("profiles", "profile_facts", "profile_skills", "profile_languages")
    )
    assert before == after == (0, 0, 0, 0)


# --------------------------------------------------------------------------
# Scope and refusals
# --------------------------------------------------------------------------


def test_an_inactive_or_merged_posting_is_not_read(migrated) -> None:
    insert_opportunity(migrated, description="Requirements\n- Python", is_active=0)
    insert_opportunity(
        migrated, description="Requirements\n- SQL", status="merged_duplicate"
    )

    summary = synchronize_opportunity_requirements(migrated)

    assert summary.total == 0
    assert _counts(migrated) == (0,) * len(REQUIREMENT_TABLES)


def test_synchronizing_before_the_migration_is_refused(tmp_path) -> None:
    connection = connect_database(tmp_path / "partial.db")
    try:
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
            if migration.version == "0013":
                continue
            for statement in split_sql_statements(
                migration.path.read_text(encoding="utf-8")
            ):
                connection.execute(statement)
        connection.commit()
        with pytest.raises(OpportunityRequirementServiceError, match="0013"):
            synchronize_opportunity_requirements(connection)
    finally:
        connection.close()


def test_an_absent_posting_is_refused(migrated) -> None:
    with pytest.raises(OpportunityRequirementServiceError, match="absent"):
        extract_one_opportunity_requirements(migrated, 4242)


def test_the_limit_bounds_how_many_postings_are_read(migrated) -> None:
    for index in range(3):
        insert_opportunity(migrated, description=f"Requirements\n- Python\n{index}")
    synchronize_opportunity_constraints(migrated)

    summary = synchronize_opportunity_requirements(migrated, limit=2)

    assert summary.total == 2 and summary.created == 2


def test_the_summary_counts_and_never_quotes(migrated, rich_opportunity) -> None:
    summary = synchronize_opportunity_requirements(migrated).as_dict()

    assert summary["required_skill_rows"] == 2
    assert summary["preferred_skill_rows"] == 1
    assert summary["required_language_rows"] == 1
    assert summary["ambiguity_rows"] == 1
    assert summary["extractor_version"] == REQUIREMENT_EXTRACTOR_VERSION
    rendered = repr(summary)
    for word in ("Python", "English", "Spark", "Airflow", "TEST ONLY"):
        assert word not in rendered
