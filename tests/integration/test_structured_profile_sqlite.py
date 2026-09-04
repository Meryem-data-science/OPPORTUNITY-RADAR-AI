"""Migration 0009 and the structured projection, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away. No real CV, no
real address and no real personal data takes part: every value is synthetic and
`.invalid` never resolves. The operational `.data/` database is never opened.

The projection is derived data, so most of these tests are about what it may
**not** do: read a fact nobody accepted, write to `profile_facts`, touch the
Phase 3.4A skill tables, drop an accepted fact silently, project a row twice,
keep a row nothing justifies any more, or let one profile's facts reach another
profile's rows.
"""

import io
import sqlite3
import tokenize
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
    accept_profile_fact,
    correct_profile_fact,
    propose_profile_fact,
    reject_profile_fact,
)
from services.digital_twin.repository import ensure_user_profile
from services.digital_twin.skills.repository import synchronize_profile_skills
from services.digital_twin.structured_profile.models import (
    STRUCTURED_PROFILE_VERSION,
    StructuredProfileNotFoundError,
)
from services.digital_twin.structured_profile.repository import (
    list_profile_experiences,
    list_profile_projects,
    synchronize_structured_profile_entries,
)

REPOSITORY_SOURCE = Path("services/digital_twin/structured_profile/repository.py")
PACKAGE = Path("services/digital_twin/structured_profile")

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_OTHER_EMAIL = "other.student@example.invalid"

BEFORE_THIS_SLICE = ("0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008")

# TEST ONLY wordings, invented for these tests.
STRUCTURED_EXPERIENCE = "Data Analyst | ACME | 2022 - 2024\nPremière ligne\nDeuxième"
FREE_EXPERIENCE = "Analyste de données pour ACME entre 2022 et 2024"
STRUCTURED_PROJECT = "• Analyse RH (2023 - 2024) : segmentation des effectifs"
FREE_PROJECT = "Analyse RH des effectifs sur deux ans"


def code_only(path: Path) -> str:
    """Return a module's executable source, without comments or docstrings."""
    tokens = tokenize.generate_tokens(
        io.StringIO(path.read_text(encoding="utf-8")).readline
    )
    return "".join(
        token.string
        for token in tokens
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "structured.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


@pytest.fixture
def other_profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_OTHER_EMAIL).profile_id


_provenance_counter = 0


def _provenance() -> ProvenanceInput:
    """A distinct, synthetic proof for each fact the tests create."""
    global _provenance_counter
    _provenance_counter += 1
    return ProvenanceInput(
        source_type=FactSourceType.CV,
        provenance_key=f"TEST-ONLY-{_provenance_counter}",
    )


def propose(connection, profile_id: int, value: str, *, fact_type):
    return propose_profile_fact(
        connection,
        profile_id=profile_id,
        fact_type=fact_type,
        value=value,
        provenance=_provenance(),
    )


def accepted(
    connection, profile_id: int, value: str, *, fact_type=ProfileFactType.EXPERIENCE
):
    fact = propose(connection, profile_id, value, fact_type=fact_type)
    return accept_profile_fact(connection, profile_id, fact.id)


def _tables(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _indexes(connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA index_list({table})")}


def _foreign_keys(connection, table: str) -> set[tuple[str, str, str, str]]:
    return {
        (row[2], row[3], row[4], row[6])
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    }


def _counts(connection) -> tuple[int, int]:
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("profile_experiences", "profile_projects")
    )


def _skill_counts(connection) -> tuple[int, int, int]:
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("skills", "profile_skills", "profile_skill_evidence")
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


def _skill_rows(connection) -> list[tuple]:
    return connection.execute(
        "SELECT id, profile_skill_id, fact_id, normalizer_version, "
        "normalization_rule_id FROM profile_skill_evidence ORDER BY id"
    ).fetchall()


def rules_of(connection, profile_id: int) -> tuple[list[str], list[str]]:
    return (
        [row.structuring_rule_id for row in list_profile_experiences(
            connection, profile_id
        )],
        [row.structuring_rule_id for row in list_profile_projects(
            connection, profile_id
        )],
    )


# --------------------------------------------------------------------------
# Migration 0009
# --------------------------------------------------------------------------


def _apply_up_to_0008(connection, tmp_path) -> list[str]:
    directory = tmp_path / "migrations-before-0009"
    directory.mkdir()
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version in BEFORE_THIS_SLICE:
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return apply_migrations(connection, directory)


def test_0009_upgrades_a_database_that_stopped_at_0008(tmp_path):
    connection = connect_database(tmp_path / "upgrade.db")
    try:
        assert _apply_up_to_0008(connection, tmp_path) == list(BEFORE_THIS_SLICE)
        existing = ensure_user_profile(connection, TEST_ONLY_EMAIL)
        fact = accepted(connection, existing.profile_id, STRUCTURED_EXPERIENCE)

        assert apply_migrations(connection) == [
            "0009", "0010", "0011", "0012", "0013", "0014", "0015", "0016",
            "0017",
            "0018",
            "0019",
            "0020",
            "0021",
            "0022",
            "0023",
        ]

        assert {"profile_experiences", "profile_projects"} <= _tables(connection)
        # The facts that existed before the upgrade are untouched by it.
        assert [row[0] for row in _fact_rows(connection)] == [fact.id]
        assert _counts(connection) == (0, 0)
    finally:
        connection.close()


def test_0009_is_recorded_once_and_seeds_nothing(migrated):
    """Applying the migrations again is a no-op; no row is seeded."""
    assert apply_migrations(migrated) == []
    recorded = migrated.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()

    assert ("0009",) in recorded
    assert _counts(migrated) == (0, 0)


def test_0009_is_the_one_migration_this_slice_added() -> None:
    """Phase 3.4B1 owns exactly `0009`, wherever the list has grown to since.

    `0010` belongs to the Phase 3.4B2 projection of education, certification
    and language facts, and is asserted by its own test module.
    """
    names = [path.name for path in sorted(Path("migrations").glob("*.sql"))]

    assert names[8] == "0009_structured_profile_experiences_projects.sql"
    assert [name for name in names if name.startswith("0009")] == [names[8]]


def test_0009_alters_no_existing_table() -> None:
    """It creates two tables and one index. It changes no column anywhere."""
    script = Path(
        "migrations/0009_structured_profile_experiences_projects.sql"
    ).read_text(encoding="utf-8")
    created = [line for line in script.splitlines() if line.startswith("CREATE TABLE")]

    assert "ALTER TABLE" not in script
    assert created == ["CREATE TABLE profile_experiences (", "CREATE TABLE profile_projects ("]


def test_the_columns_are_the_projection_and_nothing_else(migrated):
    assert _columns(migrated, "profile_experiences") == [
        "id",
        "profile_id",
        "fact_id",
        "role_text",
        "organization_text",
        "period_text",
        "description_text",
        "structurer_version",
        "structuring_rule_id",
        "created_at",
    ]
    assert _columns(migrated, "profile_projects") == [
        "id",
        "profile_id",
        "fact_id",
        "title_text",
        "period_text",
        "description_text",
        "structurer_version",
        "structuring_rule_id",
        "created_at",
    ]


def test_no_0009_table_carries_a_level_a_score_or_a_verified_flag(migrated):
    """0009 adds no second definition of truth, and no judgement of any kind."""
    forbidden = (
        "verified",
        "level",
        "proficiency",
        "seniority",
        "score",
        "confidence",
        "duration",
        "months",
        "match",
    )

    for table in ("profile_experiences", "profile_projects"):
        for column in _columns(migrated, table):
            folded = column.casefold()
            assert not any(word in folded for word in forbidden), (table, column)


def test_0009_duplicates_no_provenance_column(migrated):
    """The audit chain is a chain: a row points at the fact for the rest.

    `fact_id` is the pointer both tables need and is not a duplication; the
    source type, the digest, the parser and extractor versions, the
    fingerprint, the provenance key, the pages and the section all stay on the
    provenance side, where they are written once.
    """
    provenance = set(_columns(migrated, "profile_fact_provenance"))

    for table in ("profile_experiences", "profile_projects"):
        columns = set(_columns(migrated, table))
        assert columns & provenance == {"id", "created_at", "fact_id"}
    assert "source_type" not in _columns(migrated, "profile_experiences")
    assert "cv_sha256" not in _columns(migrated, "profile_projects")


@pytest.mark.parametrize("table", ["profile_experiences", "profile_projects"])
def test_one_fact_is_projected_at_most_once(migrated, profile_id, table):
    fact = accepted(migrated, profile_id, FREE_EXPERIENCE)
    migrated.execute(
        f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
        "structuring_rule_id) VALUES (?, ?, 'v1', 'UNPARSED_V1')",
        (profile_id, fact.id),
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
            "structuring_rule_id) VALUES (?, ?, 'v1', 'UNPARSED_V1')",
            (profile_id, fact.id),
        )


@pytest.mark.parametrize("table", ["profile_experiences", "profile_projects"])
def test_a_projection_cannot_point_at_another_profiles_fact(
    migrated, profile_id, other_profile_id, table
):
    """The scope is a database rule, not only an application convention."""
    mine = accepted(migrated, profile_id, FREE_EXPERIENCE)

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
            "structuring_rule_id) VALUES (?, ?, 'v1', 'UNPARSED_V1')",
            (other_profile_id, mine.id),
        )


@pytest.mark.parametrize("table", ["profile_experiences", "profile_projects"])
def test_a_row_needs_a_real_profile_and_a_real_fact(migrated, profile_id, table):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
            "structuring_rule_id) VALUES (?, 4242, 'v1', 'UNPARSED_V1')",
            (profile_id,),
        )


@pytest.mark.parametrize("table", ["profile_experiences", "profile_projects"])
def test_the_version_and_the_rule_must_be_present_and_trimmed(
    migrated, profile_id, table
):
    fact = accepted(migrated, profile_id, FREE_EXPERIENCE)

    for version, rule in ((" ", "UNPARSED_V1"), ("v1", "  "), (" v1", "UNPARSED_V1")):
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
                "structuring_rule_id) VALUES (?, ?, ?, ?)",
                (profile_id, fact.id, version, rule),
            )


def test_a_projected_fragment_is_absent_or_trimmed_and_never_blank(
    migrated, profile_id
):
    """A present-but-empty fragment is a defect: an absence is `NULL`."""
    fact = accepted(migrated, profile_id, FREE_EXPERIENCE)

    for role in ("", "   ", " Data Analyst"):
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                "INSERT INTO profile_experiences (profile_id, fact_id, role_text, "
                "structurer_version, structuring_rule_id) "
                "VALUES (?, ?, ?, 'v1', 'UNPARSED_V1')",
                (profile_id, fact.id, role),
            )


def test_the_indexes_the_projection_reads_on_exist(migrated):
    assert "idx_profile_experiences_profile" in _indexes(migrated, "profile_experiences")
    assert "idx_profile_projects_profile" in _indexes(migrated, "profile_projects")
    assert "idx_profile_facts_id_profile" in _indexes(migrated, "profile_facts")


def test_the_cascades_are_the_documented_ones(migrated):
    for table in ("profile_experiences", "profile_projects"):
        assert _foreign_keys(migrated, table) == {
            ("profiles", "profile_id", "id", "CASCADE"),
            ("profile_facts", "fact_id", "id", "CASCADE"),
            ("profile_facts", "profile_id", "profile_id", "CASCADE"),
        }


def test_deleting_a_profile_takes_its_projected_rows(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    accepted(migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT)
    synchronize_structured_profile_entries(migrated, profile_id)

    migrated.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

    assert _counts(migrated) == (0, 0)


# --------------------------------------------------------------------------
# Only ACCEPTED EXPERIENCE and PROJECT facts are projected
# --------------------------------------------------------------------------


def test_only_accepted_facts_of_the_two_types_reach_the_projection(
    migrated, profile_id
):
    verified = accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    propose(migrated, profile_id, FREE_EXPERIENCE, fact_type=ProfileFactType.EXPERIENCE)
    refused = propose(
        migrated, profile_id, FREE_EXPERIENCE, fact_type=ProfileFactType.EXPERIENCE
    )
    reject_profile_fact(migrated, profile_id, refused.id)
    accepted(migrated, profile_id, "Python", fact_type=ProfileFactType.SKILL)
    accepted(migrated, profile_id, "Master", fact_type=ProfileFactType.EDUCATION)
    accepted(migrated, profile_id, "Anglais", fact_type=ProfileFactType.LANGUAGE)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.accepted_experience_facts == 1
    assert outcome.accepted_project_facts == 0
    assert [row.fact_id for row in list_profile_experiences(migrated, profile_id)] == [
        verified.id
    ]


def test_a_corrected_fact_is_ignored_and_its_replacement_is_projected(
    migrated, profile_id
):
    original = accepted(migrated, profile_id, FREE_EXPERIENCE)
    correction = correct_profile_fact(
        migrated, profile_id, original.id, value=STRUCTURED_EXPERIENCE
    )

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    rows = list_profile_experiences(migrated, profile_id)

    assert outcome.accepted_experience_facts == 1
    assert [row.fact_id for row in rows] == [correction.replacement.id]
    assert rows[0].structuring_rule_id == "EXPERIENCE_PIPE_HEADER_V1"


@pytest.mark.parametrize(
    "fact_type",
    [
        ProfileFactType.SKILL,
        ProfileFactType.EDUCATION,
        ProfileFactType.CERTIFICATION,
        ProfileFactType.LANGUAGE,
        ProfileFactType.PROFESSIONAL_TITLE,
        ProfileFactType.CAREER_OBJECTIVE,
        ProfileFactType.AVAILABILITY,
        ProfileFactType.MOBILITY,
    ],
)
def test_an_accepted_fact_of_another_type_is_invisible(migrated, profile_id, fact_type):
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE, fact_type=fact_type)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.accepted_experience_facts == 0
    assert outcome.accepted_project_facts == 0
    assert _counts(migrated) == (0, 0)


def test_an_experience_fact_never_lands_in_the_project_table(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    accepted(migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT)

    synchronize_structured_profile_entries(migrated, profile_id)

    assert len(list_profile_experiences(migrated, profile_id)) == 1
    assert len(list_profile_projects(migrated, profile_id)) == 1


# --------------------------------------------------------------------------
# The projection itself
# --------------------------------------------------------------------------


def test_a_structured_experience_is_stored_fragment_by_fragment(migrated, profile_id):
    fact = accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_experiences(migrated, profile_id)[0]

    assert outcome.changed is True
    assert row.fact_id == fact.id
    assert row.role_text == "Data Analyst"
    assert row.organization_text == "ACME"
    assert row.period_text == "2022 - 2024"
    assert row.description_text == "Première ligne\nDeuxième"
    assert row.structurer_version == STRUCTURED_PROFILE_VERSION
    assert row.structuring_rule_id == "EXPERIENCE_PIPE_HEADER_V1"


def test_a_structured_project_is_stored_fragment_by_fragment(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT)

    synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_projects(migrated, profile_id)[0]

    assert row.title_text == "Analyse RH"
    assert row.period_text == "2023 - 2024"
    assert row.description_text == "segmentation des effectifs"
    assert row.structuring_rule_id == "PROJECT_BULLET_COLON_V1"


def test_an_unreadable_fact_is_projected_with_null_fragments(migrated, profile_id):
    """No accepted fact is ever abandoned silently."""
    fact = accepted(migrated, profile_id, FREE_EXPERIENCE)
    project = accepted(
        migrated, profile_id, FREE_PROJECT, fact_type=ProfileFactType.PROJECT
    )

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    experience_row = list_profile_experiences(migrated, profile_id)[0]
    project_row = list_profile_projects(migrated, profile_id)[0]

    assert outcome.unparsed_experiences == 1
    assert outcome.unparsed_projects == 1
    assert experience_row.fact_id == fact.id
    assert experience_row.structuring_rule_id == "UNPARSED_V1"
    assert (
        experience_row.role_text,
        experience_row.organization_text,
        experience_row.period_text,
        experience_row.description_text,
    ) == (None, None, None, None)
    assert project_row.fact_id == project.id
    assert (project_row.title_text, project_row.period_text) == (None, None)


def test_every_accepted_fact_is_projected_exactly_once(migrated, profile_id):
    """`experience_rows` equals `accepted_experience_facts`, always."""
    for value in (STRUCTURED_EXPERIENCE, FREE_EXPERIENCE, "Stagiaire | ACME | 2024"):
        accepted(migrated, profile_id, value)
    for value in (STRUCTURED_PROJECT, FREE_PROJECT):
        accepted(migrated, profile_id, value, fact_type=ProfileFactType.PROJECT)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.experience_rows == outcome.accepted_experience_facts == 3
    assert outcome.project_rows == outcome.accepted_project_facts == 2
    assert outcome.structured_experiences + outcome.unparsed_experiences == 3
    assert outcome.structured_projects + outcome.unparsed_projects == 2
    assert outcome.created == 5


# --------------------------------------------------------------------------
# Idempotence and id stability
# --------------------------------------------------------------------------


def test_a_second_synchronization_changes_nothing(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    accepted(migrated, profile_id, FREE_EXPERIENCE)
    accepted(migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT)
    synchronize_structured_profile_entries(migrated, profile_id)
    before = _counts(migrated)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.changed is False
    assert outcome.as_dict() == {
        "accepted_experience_facts": 2,
        "accepted_project_facts": 1,
        "accepted_education_facts": 0,
        "accepted_certification_facts": 0,
        "accepted_language_facts": 0,
        "experience_rows": 2,
        "project_rows": 1,
        "education_rows": 0,
        "certification_rows": 0,
        "language_rows": 0,
        "structured_experiences": 1,
        "unparsed_experiences": 1,
        "structured_projects": 1,
        "unparsed_projects": 0,
        "structured_educations": 0,
        "unparsed_educations": 0,
        "structured_certifications": 0,
        "unparsed_certifications": 0,
        "structured_languages": 0,
        "unparsed_languages": 0,
        "created": 0,
        "removed": 0,
        "structurer_version": STRUCTURED_PROFILE_VERSION,
        "changed": False,
    }
    assert _counts(migrated) == before


def test_ids_and_timestamps_are_stable_when_nothing_changed(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    accepted(migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT)
    synchronize_structured_profile_entries(migrated, profile_id)
    experiences = list_profile_experiences(migrated, profile_id)
    projects = list_profile_projects(migrated, profile_id)

    synchronize_structured_profile_entries(migrated, profile_id)

    assert list_profile_experiences(migrated, profile_id) == experiences
    assert list_profile_projects(migrated, profile_id) == projects


def test_a_new_accepted_fact_is_added_without_disturbing_the_others(
    migrated, profile_id
):
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    synchronize_structured_profile_entries(migrated, profile_id)
    before = list_profile_experiences(migrated, profile_id)[0]
    accepted(migrated, profile_id, FREE_EXPERIENCE)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (1, 0)
    assert list_profile_experiences(migrated, profile_id)[0] == before


def test_a_stale_reading_is_replaced_whole(migrated, profile_id):
    """A row the current rules would write differently is deleted and rewritten."""
    fact = accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    synchronize_structured_profile_entries(migrated, profile_id)
    migrated.execute(
        "UPDATE profile_experiences SET role_text = 'Ingénieure' WHERE fact_id = ?",
        (fact.id,),
    )
    # The direct write opened an implicit transaction; close it so the
    # reconciliation can open its own `BEGIN IMMEDIATE`.
    migrated.commit()

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (1, 1)
    assert outcome.changed is True
    assert list_profile_experiences(migrated, profile_id)[0].role_text == "Data Analyst"


def test_a_row_written_by_another_version_is_replaced(migrated, profile_id):
    fact = accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    synchronize_structured_profile_entries(migrated, profile_id)
    migrated.execute(
        "UPDATE profile_experiences SET structurer_version = 'structured-profile-v0' "
        "WHERE fact_id = ?",
        (fact.id,),
    )
    migrated.commit()

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (1, 1)
    assert (
        list_profile_experiences(migrated, profile_id)[0].structurer_version
        == STRUCTURED_PROFILE_VERSION
    )


# --------------------------------------------------------------------------
# Reconciliation after a human changes their mind
# --------------------------------------------------------------------------


def test_a_rejection_after_a_synchronization_removes_the_row(migrated, profile_id):
    fact = accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    synchronize_structured_profile_entries(migrated, profile_id)
    reject_profile_fact(migrated, profile_id, fact.id)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (0, 1)
    assert list_profile_experiences(migrated, profile_id) == ()


def test_a_correction_retires_the_old_row_and_projects_the_replacement(
    migrated, profile_id
):
    fact = accepted(migrated, profile_id, FREE_EXPERIENCE)
    synchronize_structured_profile_entries(migrated, profile_id)
    correction = correct_profile_fact(
        migrated, profile_id, fact.id, value=STRUCTURED_EXPERIENCE
    )

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    rows = list_profile_experiences(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (1, 1)
    assert [row.fact_id for row in rows] == [correction.replacement.id]
    assert rows[0].role_text == "Data Analyst"


def test_a_rejected_project_is_reconciled_too(migrated, profile_id):
    fact = accepted(
        migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT
    )
    synchronize_structured_profile_entries(migrated, profile_id)
    reject_profile_fact(migrated, profile_id, fact.id)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.project_rows == 0
    assert list_profile_projects(migrated, profile_id) == ()


# --------------------------------------------------------------------------
# Isolation, refusals and what is never written
# --------------------------------------------------------------------------


def test_two_profiles_are_strictly_separate(migrated, profile_id, other_profile_id):
    mine = accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    theirs = accepted(migrated, other_profile_id, FREE_EXPERIENCE)

    synchronize_structured_profile_entries(migrated, profile_id)
    synchronize_structured_profile_entries(migrated, other_profile_id)

    assert [row.fact_id for row in list_profile_experiences(migrated, profile_id)] == [
        mine.id
    ]
    assert [
        row.fact_id for row in list_profile_experiences(migrated, other_profile_id)
    ] == [theirs.id]


def test_one_profiles_rejection_leaves_the_other_profile_alone(
    migrated, profile_id, other_profile_id
):
    mine = accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    accepted(migrated, other_profile_id, STRUCTURED_EXPERIENCE)
    synchronize_structured_profile_entries(migrated, profile_id)
    synchronize_structured_profile_entries(migrated, other_profile_id)
    reject_profile_fact(migrated, profile_id, mine.id)

    synchronize_structured_profile_entries(migrated, profile_id)

    assert list_profile_experiences(migrated, profile_id) == ()
    assert len(list_profile_experiences(migrated, other_profile_id)) == 1


def test_an_unknown_profile_is_refused_and_creates_nothing(migrated):
    with pytest.raises(StructuredProfileNotFoundError):
        synchronize_structured_profile_entries(migrated, 4242)

    assert _counts(migrated) == (0, 0)


def test_a_failure_rolls_the_whole_projection_back(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    accepted(migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT)

    def fail() -> None:
        raise RuntimeError("TEST ONLY failure after the facts were read")

    with pytest.raises(RuntimeError):
        synchronize_structured_profile_entries(migrated, profile_id, after_read=fail)

    assert _counts(migrated) == (0, 0)


def test_a_failure_after_a_first_run_leaves_that_run_intact(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    synchronize_structured_profile_entries(migrated, profile_id)
    before = list_profile_experiences(migrated, profile_id)
    accepted(migrated, profile_id, FREE_EXPERIENCE)

    def fail() -> None:
        raise RuntimeError("TEST ONLY failure after the facts were read")

    with pytest.raises(RuntimeError):
        synchronize_structured_profile_entries(migrated, profile_id, after_read=fail)

    assert list_profile_experiences(migrated, profile_id) == before


def test_the_projection_never_touches_profile_facts(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    accepted(migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT)
    facts_before = _fact_rows(migrated)
    provenance_before = _provenance_rows(migrated)

    synchronize_structured_profile_entries(migrated, profile_id)
    synchronize_structured_profile_entries(migrated, profile_id)

    assert _fact_rows(migrated) == facts_before
    assert _provenance_rows(migrated) == provenance_before


def test_the_projection_never_touches_the_phase_34a_skill_tables(
    migrated, profile_id
):
    """No skill is inferred from an experience or a project, ever."""
    accepted(migrated, profile_id, "Python", fact_type=ProfileFactType.SKILL)
    synchronize_profile_skills(migrated, profile_id)
    skills_before = _skill_counts(migrated)
    evidence_before = _skill_rows(migrated)
    accepted(migrated, profile_id, STRUCTURED_EXPERIENCE)
    accepted(
        migrated,
        profile_id,
        "• Analyse RH : réalisée en Python, SQL et Power BI",
        fact_type=ProfileFactType.PROJECT,
    )

    synchronize_structured_profile_entries(migrated, profile_id)

    assert _skill_counts(migrated) == skills_before == (1, 1, 1)
    assert _skill_rows(migrated) == evidence_before


def test_the_repository_writes_no_statement_against_the_fact_or_skill_tables() -> None:
    """Read the SQL the module executes, not only what it says it does."""
    statements = code_only(REPOSITORY_SOURCE).casefold()

    for forbidden in (
        "insert into profile_facts",
        "update profile_facts",
        "delete from profile_facts",
        "insert into profile_fact_provenance",
        "update profile_fact_provenance",
        "delete from profile_fact_provenance",
        "profile_skills",
        "profile_skill_evidence",
        "accept_profile_fact",
        "reject_profile_fact",
        "correct_profile_fact",
    ):
        assert forbidden not in statements, forbidden


def test_the_repository_reads_only_accepted_facts_of_the_two_types() -> None:
    """The filter is written into the SQL, so no caller can widen it.

    This one assertion reads the raw source rather than the stripped code,
    because the statement it is about *is* a string literal.
    """
    source = REPOSITORY_SOURCE.read_text(encoding="utf-8").casefold()

    assert "fact_type = 'experience' and status = 'accepted'" in source
    assert "fact_type = 'project' and status = 'accepted'" in source
    for forbidden in ("'proposed'", "'rejected'", "'corrected'"):
        assert forbidden not in source, forbidden


def test_the_package_depends_on_no_matching_and_no_phase_4_module() -> None:
    """Phase 3.4B1 stops at the projection: nothing downstream is imported."""
    for path in sorted(PACKAGE.glob("*.py")):
        # The executable source only: the module docstrings *name* what the
        # slice refuses to do, and naming a refusal is not importing it.
        source = code_only(path).casefold()
        for forbidden in (
            "matching",
            "opportunit",
            "tfidf",
            "tf_idf",
            "cosine",
            "similarity",
            "match_score",
            "eligib",
            "recommend",
            "ranking",
            "digital_twin.cv",
        ):
            assert forbidden not in source, (path.name, forbidden)


def test_the_package_writes_to_no_skill_module() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        source = code_only(path)
        assert "synchronize_profile_skills" not in source
        assert "digital_twin.skills" not in source
