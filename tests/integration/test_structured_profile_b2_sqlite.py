"""Migration 0010 and the Phase 3.4B2 projection, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away. No real CV, no
real address and no real personal data takes part: every value is synthetic and
`.invalid` never resolves. The operational `.data/` database is never opened,
and no count taken from it appears anywhere below — the fixtures decide how
many facts exist, so nothing about anybody's CV can become a constant of the
tests.

The projection is derived data, so most of these tests are about what it may
**not** do: read a fact nobody accepted, write to `profile_facts`, touch the
Phase 3.4A skill tables, disturb the Phase 3.4B1 rows, drop an accepted fact
silently, project a row twice, keep a row nothing justifies any more, or let
one profile's facts reach another profile's rows.
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
    list_profile_certifications,
    list_profile_educations,
    list_profile_experiences,
    list_profile_languages,
    list_profile_projects,
    synchronize_structured_profile_entries,
)

REPOSITORY_SOURCE = Path("services/digital_twin/structured_profile/repository.py")
MIGRATION = Path(
    "migrations/0010_structured_profile_education_certifications_languages.sql"
)

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_OTHER_EMAIL = "other.student@example.invalid"

BEFORE_THIS_SLICE = (
    "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009",
)

#: The three tables `0010` adds, and the two `0009` did.
B2_TABLES = ("profile_educations", "profile_certifications", "profile_languages")
B1_TABLES = ("profile_experiences", "profile_projects")

# TEST ONLY wordings, invented for these tests.
STRUCTURED_EDUCATION = (
    "Master 2 Data Science | Université de Test | 2020 - 2022\nMention"
)
PERIOD_ONLY_EDUCATION = "Master 2 Data Science | Promotion 2020 | 2020 - 2022"
DATELESS_EDUCATION = "Master fictif | Université Exemple"
INVERTING_EDUCATION = "University Diploma in AI | Sorbonne | 2024"
BULLETED_LANGUAGE = "• Langue fictive : C1"
FREE_EDUCATION = "Diplômée en 2022 après deux ans d'études"
STRUCTURED_CERTIFICATION = (
    "Certification : Test Cloud | Délivré par : Organisme Test | 2023"
)
FREE_CERTIFICATION = "Préparation à la certification Test Cloud"
STRUCTURED_LANGUAGE = "Anglais : C1"
FREE_LANGUAGE = "Anglais lu, écrit et parlé"
STRUCTURED_EXPERIENCE = "Data Analyst | ACME | 2022 - 2024\nPremière ligne"
STRUCTURED_PROJECT = "• Analyse RH (2023 - 2024) : segmentation des effectifs"


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
    connection = connect_database(tmp_path / "structured-b2.db")
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
        provenance_key=f"TEST-ONLY-B2-{_provenance_counter}",
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
    connection, profile_id: int, value: str, *, fact_type=ProfileFactType.EDUCATION
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


def _counts(connection) -> tuple[int, ...]:
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in B2_TABLES
    )


def _skill_counts(connection) -> tuple[int, ...]:
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


def _b1_rows(connection, profile_id: int) -> tuple[tuple, tuple]:
    return (
        list_profile_experiences(connection, profile_id),
        list_profile_projects(connection, profile_id),
    )


# --------------------------------------------------------------------------
# Migration 0010
# --------------------------------------------------------------------------


def _apply_up_to_0009(connection, tmp_path) -> list[str]:
    directory = tmp_path / "migrations-before-0010"
    directory.mkdir()
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version in BEFORE_THIS_SLICE:
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return apply_migrations(connection, directory)


def test_0010_upgrades_a_database_that_stopped_at_0009(tmp_path):
    connection = connect_database(tmp_path / "upgrade.db")
    try:
        assert _apply_up_to_0009(connection, tmp_path) == list(BEFORE_THIS_SLICE)
        existing = ensure_user_profile(connection, TEST_ONLY_EMAIL)
        fact = accepted(connection, existing.profile_id, STRUCTURED_EDUCATION)

        assert apply_migrations(connection) == [
            "0010", "0011", "0012", "0013", "0014", "0015", "0016",
            "0017",
            "0018",
            "0019",
            "0020",
            "0021",
            "0022",
            "0023",
            "0024",
            "0025",
            "0026",
        ]

        assert set(B2_TABLES) <= _tables(connection)
        # The facts that existed before the upgrade are untouched by it.
        assert [row[0] for row in _fact_rows(connection)] == [fact.id]
        assert _counts(connection) == (0, 0, 0)
    finally:
        connection.close()


def test_0010_is_recorded_once_and_seeds_nothing(migrated):
    """Applying the migrations again is a no-op; no row is seeded."""
    assert apply_migrations(migrated) == []
    recorded = migrated.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()

    assert recorded[-1] == ("0026",)
    assert _counts(migrated) == (0, 0, 0)


def test_0010_is_the_only_migration_this_slice_adds() -> None:
    names = [path.name for path in sorted(Path("migrations").glob("*.sql"))]

    # `0010` is this slice's own migration and stays the tenth. Later slices
    # add their own after it, so what is asserted is its position, not that it
    # is the newest migration in the repository.
    assert names[9] == MIGRATION.name
    assert names.index(MIGRATION.name) == 9


def test_0010_alters_no_existing_table() -> None:
    """It creates three tables and three indexes. It changes no column anywhere.

    It creates no index on `profile_facts` either: the composite identity the
    foreign keys need is `idx_profile_facts_id_profile`, which `0009` already
    created and this migration reuses.
    """
    script = MIGRATION.read_text(encoding="utf-8")
    created = [line for line in script.splitlines() if line.startswith("CREATE TABLE")]

    assert "ALTER TABLE" not in script
    assert created == [
        "CREATE TABLE profile_educations (",
        "CREATE TABLE profile_certifications (",
        "CREATE TABLE profile_languages (",
    ]
    assert "ON profile_facts" not in script


def test_the_columns_are_the_projection_and_nothing_else(migrated):
    assert _columns(migrated, "profile_educations") == [
        "id",
        "profile_id",
        "fact_id",
        "institution_text",
        "program_text",
        "period_text",
        "description_text",
        "structurer_version",
        "structuring_rule_id",
        "created_at",
    ]
    assert _columns(migrated, "profile_certifications") == [
        "id",
        "profile_id",
        "fact_id",
        "certification_text",
        "issuer_text",
        "period_text",
        "description_text",
        "structurer_version",
        "structuring_rule_id",
        "created_at",
    ]
    assert _columns(migrated, "profile_languages") == [
        "id",
        "profile_id",
        "fact_id",
        "language_text",
        "proficiency_text",
        "structurer_version",
        "structuring_rule_id",
        "created_at",
    ]


def test_no_0010_table_carries_a_level_a_score_or_a_verified_flag(migrated):
    """`0010` adds no second definition of truth, and no judgement of any kind.

    `proficiency_text` is exempted by its exact name: it holds the wording the
    document used, verbatim, and `proficiency` stays a forbidden word so that a
    `proficiency_level` or a `proficiency_score` column would still be caught.
    """
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
        "cefr",
        "obtained",
        "expires",
        "bac",
        "inferred",
    )

    for table in B2_TABLES:
        for column in _columns(migrated, table):
            if column == "proficiency_text":
                continue
            folded = column.casefold()
            assert not any(word in folded for word in forbidden), (table, column)


def test_0010_duplicates_no_provenance_column(migrated):
    """The audit chain is a chain: a row points at the fact for the rest."""
    provenance = set(_columns(migrated, "profile_fact_provenance"))

    for table in B2_TABLES:
        columns = set(_columns(migrated, table))
        assert columns & provenance == {"id", "created_at", "fact_id"}
    # The comments *name* what is not duplicated, so the executable SQL is read
    # rather than the file: a comment saying "no `cv_sha256` here" must not
    # make this assertion pass or fail.
    statements = "\n".join(
        line
        for line in MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    for absent in (
        "source_type",
        "cv_sha256",
        "parser_version",
        "extractor_version",
        "provenance_key",
    ):
        assert absent not in statements, absent


@pytest.mark.parametrize("table", B2_TABLES)
def test_one_fact_is_projected_at_most_once(migrated, profile_id, table):
    fact = accepted(migrated, profile_id, FREE_EDUCATION)
    migrated.execute(
        f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
        "structuring_rule_id) VALUES (?, ?, 'v1', 'EDUCATION_UNPARSED_V1')",
        (profile_id, fact.id),
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
            "structuring_rule_id) VALUES (?, ?, 'v1', 'EDUCATION_UNPARSED_V1')",
            (profile_id, fact.id),
        )


@pytest.mark.parametrize("table", B2_TABLES)
def test_a_projection_cannot_point_at_another_profiles_fact(
    migrated, profile_id, other_profile_id, table
):
    """The scope is a database rule, not only an application convention."""
    mine = accepted(migrated, profile_id, FREE_EDUCATION)

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
            "structuring_rule_id) VALUES (?, ?, 'v1', 'EDUCATION_UNPARSED_V1')",
            (other_profile_id, mine.id),
        )


@pytest.mark.parametrize("table", B2_TABLES)
def test_a_row_needs_a_real_profile_and_a_real_fact(migrated, profile_id, table):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
            "structuring_rule_id) VALUES (?, 4242, 'v1', 'EDUCATION_UNPARSED_V1')",
            (profile_id,),
        )


@pytest.mark.parametrize("table", B2_TABLES)
def test_the_version_and_the_rule_must_be_present_and_trimmed(
    migrated, profile_id, table
):
    fact = accepted(migrated, profile_id, FREE_EDUCATION)

    for version, rule in ((" ", "UNPARSED_V1"), ("v1", "  "), (" v1", "UNPARSED_V1")):
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                f"INSERT INTO {table} (profile_id, fact_id, structurer_version, "
                "structuring_rule_id) VALUES (?, ?, ?, ?)",
                (profile_id, fact.id, version, rule),
            )


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("profile_educations", "institution_text"),
        ("profile_educations", "program_text"),
        ("profile_certifications", "certification_text"),
        ("profile_certifications", "issuer_text"),
        ("profile_languages", "language_text"),
        ("profile_languages", "proficiency_text"),
    ],
)
def test_a_projected_fragment_is_absent_or_trimmed_and_never_blank(
    migrated, profile_id, table, column
):
    """A present-but-empty fragment is a defect: an absence is `NULL`."""
    fact = accepted(migrated, profile_id, FREE_EDUCATION)

    for value in ("", "   ", " Université de Test"):
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                f"INSERT INTO {table} (profile_id, fact_id, {column}, "
                "structurer_version, structuring_rule_id) "
                "VALUES (?, ?, ?, 'v1', 'EDUCATION_UNPARSED_V1')",
                (profile_id, fact.id, value),
            )


def test_the_indexes_the_projection_reads_on_exist(migrated):
    assert "idx_profile_educations_profile" in _indexes(migrated, "profile_educations")
    assert "idx_profile_certifications_profile" in _indexes(
        migrated, "profile_certifications"
    )
    assert "idx_profile_languages_profile" in _indexes(migrated, "profile_languages")
    assert "idx_profile_facts_id_profile" in _indexes(migrated, "profile_facts")


def test_the_cascades_are_the_documented_ones(migrated):
    for table in B2_TABLES:
        assert _foreign_keys(migrated, table) == {
            ("profiles", "profile_id", "id", "CASCADE"),
            ("profile_facts", "fact_id", "id", "CASCADE"),
            ("profile_facts", "profile_id", "profile_id", "CASCADE"),
        }


def test_deleting_a_profile_takes_its_projected_rows(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    accepted(
        migrated,
        profile_id,
        STRUCTURED_CERTIFICATION,
        fact_type=ProfileFactType.CERTIFICATION,
    )
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )
    synchronize_structured_profile_entries(migrated, profile_id)

    migrated.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

    assert _counts(migrated) == (0, 0, 0)


# --------------------------------------------------------------------------
# Only ACCEPTED facts of the three new types are projected
# --------------------------------------------------------------------------


def test_only_accepted_facts_reach_the_three_new_tables(migrated, profile_id):
    verified = accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    propose(migrated, profile_id, FREE_EDUCATION, fact_type=ProfileFactType.EDUCATION)
    refused = propose(
        migrated, profile_id, FREE_EDUCATION, fact_type=ProfileFactType.EDUCATION
    )
    reject_profile_fact(migrated, profile_id, refused.id)
    proposed_language = propose(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.accepted_education_facts == 1
    assert outcome.accepted_language_facts == 0
    assert outcome.accepted_certification_facts == 0
    assert [row.fact_id for row in list_profile_educations(migrated, profile_id)] == [
        verified.id
    ]
    assert list_profile_languages(migrated, profile_id) == ()
    assert proposed_language.status.value == "PROPOSED"


def test_a_corrected_education_is_ignored_and_its_replacement_is_projected(
    migrated, profile_id
):
    original = accepted(migrated, profile_id, FREE_EDUCATION)
    correction = correct_profile_fact(
        migrated, profile_id, original.id, value=STRUCTURED_EDUCATION
    )

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    rows = list_profile_educations(migrated, profile_id)

    assert outcome.accepted_education_facts == 1
    assert [row.fact_id for row in rows] == [correction.replacement.id]
    assert rows[0].structuring_rule_id == "EDUCATION_PIPE_EXPLICIT_V1"


@pytest.mark.parametrize(
    "fact_type",
    [
        ProfileFactType.SKILL,
        ProfileFactType.PROFESSIONAL_TITLE,
        ProfileFactType.CAREER_OBJECTIVE,
        ProfileFactType.AVAILABILITY,
        ProfileFactType.MOBILITY,
    ],
)
def test_an_accepted_fact_of_an_unprojected_type_is_invisible(
    migrated, profile_id, fact_type
):
    accepted(migrated, profile_id, STRUCTURED_EDUCATION, fact_type=fact_type)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.accepted_education_facts == 0
    assert _counts(migrated) == (0, 0, 0)


def test_each_type_lands_in_its_own_table_and_nowhere_else(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    accepted(
        migrated,
        profile_id,
        STRUCTURED_CERTIFICATION,
        fact_type=ProfileFactType.CERTIFICATION,
    )
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )

    synchronize_structured_profile_entries(migrated, profile_id)

    assert _counts(migrated) == (1, 1, 1)


# --------------------------------------------------------------------------
# The projection itself
# --------------------------------------------------------------------------


def test_a_structured_education_is_stored_fragment_by_fragment(migrated, profile_id):
    fact = accepted(migrated, profile_id, STRUCTURED_EDUCATION)

    synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_educations(migrated, profile_id)[0]

    assert row.fact_id == fact.id
    assert row.institution_text == "Université de Test"
    assert row.program_text == "Master 2 Data Science"
    assert row.period_text == "2020 - 2022"
    assert row.description_text == "Mention"
    assert row.structurer_version == STRUCTURED_PROFILE_VERSION
    assert row.structuring_rule_id == "EDUCATION_PIPE_EXPLICIT_V1"


def test_an_undecidable_education_stores_only_its_period(migrated, profile_id):
    """The certain part survives; the uncertain part is not invented."""
    accepted(migrated, profile_id, PERIOD_ONLY_EDUCATION)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_educations(migrated, profile_id)[0]

    assert row.structuring_rule_id == "EDUCATION_PIPE_PERIOD_ONLY_V1"
    assert row.period_text == "2020 - 2022"
    assert (row.institution_text, row.program_text) == (None, None)
    # It named a fragment, so it is a reading, not a failure to read.
    assert (outcome.structured_educations, outcome.unparsed_educations) == (1, 0)


def test_a_dateless_two_segment_education_is_stored_without_a_period(
    migrated, profile_id
):
    """The shorter shape a CV writes just as often, read by the same registry."""
    fact = accepted(migrated, profile_id, DATELESS_EDUCATION)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_educations(migrated, profile_id)[0]

    assert row.fact_id == fact.id
    assert row.structuring_rule_id == "EDUCATION_PIPE_INSTITUTION_PROGRAM_V1"
    assert row.institution_text == "Université Exemple"
    assert row.program_text == "Master fictif"
    assert row.period_text is None
    assert row.description_text is None
    assert (outcome.structured_educations, outcome.unparsed_educations) == (1, 0)


def test_a_dateless_education_the_registry_cannot_answer_stays_unparsed(
    migrated, profile_id
):
    fact = accepted(migrated, profile_id, "Sciences | Autre chose")

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_educations(migrated, profile_id)[0]

    assert row.fact_id == fact.id
    assert row.structuring_rule_id == "EDUCATION_UNPARSED_V1"
    assert (row.institution_text, row.program_text, row.period_text) == (
        None,
        None,
        None,
    )
    assert (outcome.structured_educations, outcome.unparsed_educations) == (0, 1)


def test_the_three_education_shapes_coexist_in_one_run(migrated, profile_id):
    """One reconciliation, one row per fact, three different rules recorded."""
    for value in (STRUCTURED_EDUCATION, DATELESS_EDUCATION, PERIOD_ONLY_EDUCATION):
        accepted(migrated, profile_id, value)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    rules = [row.structuring_rule_id for row in list_profile_educations(
        migrated, profile_id
    )]

    assert rules == [
        "EDUCATION_PIPE_EXPLICIT_V1",
        "EDUCATION_PIPE_INSTITUTION_PROGRAM_V1",
        "EDUCATION_PIPE_PERIOD_ONLY_V1",
    ]
    assert outcome.education_rows == outcome.accepted_education_facts == 3
    assert (outcome.structured_educations, outcome.unparsed_educations) == (3, 0)


def test_a_dateless_education_is_reconciled_like_any_other(migrated, profile_id):
    fact = accepted(migrated, profile_id, DATELESS_EDUCATION)
    synchronize_structured_profile_entries(migrated, profile_id)
    before = list_profile_educations(migrated, profile_id)

    unchanged = synchronize_structured_profile_entries(migrated, profile_id)
    reject_profile_fact(migrated, profile_id, fact.id)
    after_rejection = synchronize_structured_profile_entries(migrated, profile_id)

    assert unchanged.changed is False
    assert list_profile_educations(migrated, profile_id) == () != before
    assert (after_rejection.created, after_rejection.removed) == (0, 1)


def test_a_header_that_would_invert_the_two_fields_stores_neither(
    migrated, profile_id
):
    """`University Diploma in AI | Sorbonne` is the marker-only rule's trap.

    The first segment carries an institution marker and is the programme; the
    second carries no marker and is the school. Storing the inversion would be
    worse than storing nothing, so nothing but the certain period is stored.
    """
    fact = accepted(migrated, profile_id, INVERTING_EDUCATION)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_educations(migrated, profile_id)[0]

    assert row.fact_id == fact.id
    assert row.structuring_rule_id == "EDUCATION_PIPE_PERIOD_ONLY_V1"
    assert row.institution_text is None
    assert row.program_text is None
    assert row.period_text == "2024"
    assert (outcome.structured_educations, outcome.unparsed_educations) == (1, 0)


def test_a_bulleted_language_fact_stores_no_list_marker(migrated, profile_id):
    """The extractor keeps the source text of a bullet block; the layout is
    not part of the language's name."""
    fact = accepted(
        migrated, profile_id, BULLETED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )

    synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_languages(migrated, profile_id)[0]

    assert row.fact_id == fact.id
    assert row.structuring_rule_id == "LANGUAGE_EXPLICIT_PROFICIENCY_V1"
    assert row.language_text == "Langue fictive"
    assert row.proficiency_text == "C1"
    assert "•" not in str(row)


def test_a_structured_certification_is_stored_fragment_by_fragment(
    migrated, profile_id
):
    accepted(
        migrated,
        profile_id,
        STRUCTURED_CERTIFICATION,
        fact_type=ProfileFactType.CERTIFICATION,
    )

    synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_certifications(migrated, profile_id)[0]

    assert row.certification_text == "Test Cloud"
    assert row.issuer_text == "Organisme Test"
    assert row.period_text == "2023"
    assert row.structuring_rule_id == "CERTIFICATION_EXPLICIT_V1"


def test_a_stated_intention_is_projected_without_becoming_a_certification(
    migrated, profile_id
):
    fact = accepted(
        migrated,
        profile_id,
        FREE_CERTIFICATION,
        fact_type=ProfileFactType.CERTIFICATION,
    )

    synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_certifications(migrated, profile_id)[0]

    assert row.fact_id == fact.id
    assert row.structuring_rule_id == "CERTIFICATION_UNPARSED_V1"
    assert (row.certification_text, row.issuer_text, row.period_text) == (
        None,
        None,
        None,
    )


def test_a_structured_language_keeps_its_level_verbatim(migrated, profile_id):
    accepted(
        migrated, profile_id, "Espagnol : courant", fact_type=ProfileFactType.LANGUAGE
    )

    synchronize_structured_profile_entries(migrated, profile_id)
    row = list_profile_languages(migrated, profile_id)[0]

    assert row.language_text == "Espagnol"
    assert row.proficiency_text == "courant"
    assert row.structuring_rule_id == "LANGUAGE_EXPLICIT_PROFICIENCY_V1"
    assert "C1" not in str(row)


def test_an_unreadable_fact_is_projected_with_null_fragments(migrated, profile_id):
    """No accepted fact is ever abandoned silently."""
    education = accepted(migrated, profile_id, FREE_EDUCATION)
    language = accepted(
        migrated, profile_id, FREE_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    education_row = list_profile_educations(migrated, profile_id)[0]
    language_row = list_profile_languages(migrated, profile_id)[0]

    assert outcome.unparsed_educations == 1
    assert outcome.unparsed_languages == 1
    assert education_row.fact_id == education.id
    assert education_row.structuring_rule_id == "EDUCATION_UNPARSED_V1"
    assert (
        education_row.institution_text,
        education_row.program_text,
        education_row.period_text,
        education_row.description_text,
    ) == (None, None, None, None)
    assert language_row.fact_id == language.id
    assert language_row.structuring_rule_id == "LANGUAGE_UNPARSED_V1"
    assert (language_row.language_text, language_row.proficiency_text) == (None, None)


def test_every_accepted_fact_is_projected_exactly_once(migrated, profile_id):
    for value in (STRUCTURED_EDUCATION, PERIOD_ONLY_EDUCATION, FREE_EDUCATION):
        accepted(migrated, profile_id, value)
    for value in (STRUCTURED_CERTIFICATION, FREE_CERTIFICATION):
        accepted(
            migrated, profile_id, value, fact_type=ProfileFactType.CERTIFICATION
        )
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.education_rows == outcome.accepted_education_facts == 3
    assert outcome.certification_rows == outcome.accepted_certification_facts == 2
    assert outcome.language_rows == outcome.accepted_language_facts == 1
    assert outcome.structured_educations + outcome.unparsed_educations == 3
    assert outcome.structured_certifications + outcome.unparsed_certifications == 2
    assert outcome.structured_languages + outcome.unparsed_languages == 1
    assert outcome.created == 6


def test_the_summary_is_counters_a_version_and_a_flag(migrated, profile_id):
    """Privacy-safe: no institution, diploma, certification, language or level."""
    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )
    synchronize_structured_profile_entries(migrated, profile_id)

    summary = synchronize_structured_profile_entries(migrated, profile_id).as_dict()

    assert summary == {
        "accepted_experience_facts": 0,
        "accepted_project_facts": 0,
        "accepted_education_facts": 1,
        "accepted_certification_facts": 0,
        "accepted_language_facts": 1,
        "experience_rows": 0,
        "project_rows": 0,
        "education_rows": 1,
        "certification_rows": 0,
        "language_rows": 1,
        "structured_experiences": 0,
        "unparsed_experiences": 0,
        "structured_projects": 0,
        "unparsed_projects": 0,
        "structured_educations": 1,
        "unparsed_educations": 0,
        "structured_certifications": 0,
        "unparsed_certifications": 0,
        "structured_languages": 1,
        "unparsed_languages": 0,
        "created": 0,
        "removed": 0,
        "structurer_version": STRUCTURED_PROFILE_VERSION,
        "changed": False,
    }
    rendered = str(summary)
    for fragment in ("Université", "Master", "Anglais", "C1", "Test Cloud"):
        assert fragment not in rendered, fragment


# --------------------------------------------------------------------------
# Idempotence and reconciliation
# --------------------------------------------------------------------------


def test_a_second_synchronization_changes_nothing(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    accepted(
        migrated,
        profile_id,
        STRUCTURED_CERTIFICATION,
        fact_type=ProfileFactType.CERTIFICATION,
    )
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )
    synchronize_structured_profile_entries(migrated, profile_id)
    educations = list_profile_educations(migrated, profile_id)
    certifications = list_profile_certifications(migrated, profile_id)
    languages = list_profile_languages(migrated, profile_id)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.changed is False
    assert (outcome.created, outcome.removed) == (0, 0)
    assert list_profile_educations(migrated, profile_id) == educations
    assert list_profile_certifications(migrated, profile_id) == certifications
    assert list_profile_languages(migrated, profile_id) == languages


def test_a_new_accepted_fact_is_added_without_disturbing_the_others(
    migrated, profile_id
):
    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    synchronize_structured_profile_entries(migrated, profile_id)
    before = list_profile_educations(migrated, profile_id)[0]
    accepted(migrated, profile_id, FREE_EDUCATION)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (1, 0)
    assert list_profile_educations(migrated, profile_id)[0] == before


def test_a_stale_reading_is_replaced_whole(migrated, profile_id):
    fact = accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    synchronize_structured_profile_entries(migrated, profile_id)
    migrated.execute(
        "UPDATE profile_educations SET institution_text = 'Autre' WHERE fact_id = ?",
        (fact.id,),
    )
    migrated.commit()

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (1, 1)
    assert (
        list_profile_educations(migrated, profile_id)[0].institution_text
        == "Université de Test"
    )


def test_a_row_written_by_another_version_is_replaced(migrated, profile_id):
    fact = accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )
    synchronize_structured_profile_entries(migrated, profile_id)
    migrated.execute(
        "UPDATE profile_languages SET structurer_version = 'structured-profile-v0' "
        "WHERE fact_id = ?",
        (fact.id,),
    )
    migrated.commit()

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (1, 1)
    assert (
        list_profile_languages(migrated, profile_id)[0].structurer_version
        == STRUCTURED_PROFILE_VERSION
    )


@pytest.mark.parametrize(
    ("fact_type", "value", "read_back"),
    [
        (ProfileFactType.EDUCATION, STRUCTURED_EDUCATION, list_profile_educations),
        (
            ProfileFactType.CERTIFICATION,
            STRUCTURED_CERTIFICATION,
            list_profile_certifications,
        ),
        (ProfileFactType.LANGUAGE, STRUCTURED_LANGUAGE, list_profile_languages),
    ],
)
def test_a_rejection_after_a_synchronization_removes_the_row(
    migrated, profile_id, fact_type, value, read_back
):
    fact = accepted(migrated, profile_id, value, fact_type=fact_type)
    synchronize_structured_profile_entries(migrated, profile_id)
    reject_profile_fact(migrated, profile_id, fact.id)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (0, 1)
    assert read_back(migrated, profile_id) == ()


def test_a_correction_retires_the_old_row_and_projects_the_replacement(
    migrated, profile_id
):
    fact = accepted(migrated, profile_id, FREE_EDUCATION)
    synchronize_structured_profile_entries(migrated, profile_id)
    correction = correct_profile_fact(
        migrated, profile_id, fact.id, value=STRUCTURED_EDUCATION
    )

    outcome = synchronize_structured_profile_entries(migrated, profile_id)
    rows = list_profile_educations(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (1, 1)
    assert [row.fact_id for row in rows] == [correction.replacement.id]
    assert rows[0].institution_text == "Université de Test"


# --------------------------------------------------------------------------
# Isolation, refusals and what is never written
# --------------------------------------------------------------------------


def test_two_profiles_are_strictly_separate(migrated, profile_id, other_profile_id):
    mine = accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    theirs = accepted(migrated, other_profile_id, FREE_EDUCATION)

    synchronize_structured_profile_entries(migrated, profile_id)
    synchronize_structured_profile_entries(migrated, other_profile_id)

    assert [row.fact_id for row in list_profile_educations(migrated, profile_id)] == [
        mine.id
    ]
    assert [
        row.fact_id for row in list_profile_educations(migrated, other_profile_id)
    ] == [theirs.id]


def test_one_profiles_rejection_leaves_the_other_profile_alone(
    migrated, profile_id, other_profile_id
):
    mine = accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )
    accepted(
        migrated,
        other_profile_id,
        STRUCTURED_LANGUAGE,
        fact_type=ProfileFactType.LANGUAGE,
    )
    synchronize_structured_profile_entries(migrated, profile_id)
    synchronize_structured_profile_entries(migrated, other_profile_id)
    reject_profile_fact(migrated, profile_id, mine.id)

    synchronize_structured_profile_entries(migrated, profile_id)

    assert list_profile_languages(migrated, profile_id) == ()
    assert len(list_profile_languages(migrated, other_profile_id)) == 1


def test_an_unknown_profile_is_refused_and_creates_nothing(migrated):
    with pytest.raises(StructuredProfileNotFoundError):
        synchronize_structured_profile_entries(migrated, 4242)

    assert _counts(migrated) == (0, 0, 0)


def test_a_failure_rolls_the_whole_projection_back(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    accepted(
        migrated,
        profile_id,
        STRUCTURED_CERTIFICATION,
        fact_type=ProfileFactType.CERTIFICATION,
    )
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )
    accepted(
        migrated,
        profile_id,
        STRUCTURED_EXPERIENCE,
        fact_type=ProfileFactType.EXPERIENCE,
    )

    def fail() -> None:
        raise RuntimeError("TEST ONLY failure after the facts were read")

    with pytest.raises(RuntimeError):
        synchronize_structured_profile_entries(migrated, profile_id, after_read=fail)

    assert _counts(migrated) == (0, 0, 0)
    assert _b1_rows(migrated, profile_id) == ((), ())


def test_a_failure_after_a_first_run_leaves_that_run_intact(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    synchronize_structured_profile_entries(migrated, profile_id)
    before = list_profile_educations(migrated, profile_id)
    accepted(migrated, profile_id, FREE_EDUCATION)

    def fail() -> None:
        raise RuntimeError("TEST ONLY failure after the facts were read")

    with pytest.raises(RuntimeError):
        synchronize_structured_profile_entries(migrated, profile_id, after_read=fail)

    assert list_profile_educations(migrated, profile_id) == before


def test_the_projection_never_touches_profile_facts(migrated, profile_id):
    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    accepted(
        migrated,
        profile_id,
        STRUCTURED_CERTIFICATION,
        fact_type=ProfileFactType.CERTIFICATION,
    )
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )
    facts_before = _fact_rows(migrated)
    provenance_before = _provenance_rows(migrated)

    synchronize_structured_profile_entries(migrated, profile_id)
    synchronize_structured_profile_entries(migrated, profile_id)

    assert _fact_rows(migrated) == facts_before
    assert _provenance_rows(migrated) == provenance_before


def test_the_repository_reads_only_accepted_facts_of_the_three_new_types() -> None:
    """The filter is written into the SQL, so no caller can widen it."""
    source = REPOSITORY_SOURCE.read_text(encoding="utf-8").casefold()

    for fact_type in ("education", "certification", "language"):
        assert f"fact_type = '{fact_type}' and status = 'accepted'" in source
    for forbidden in ("'proposed'", "'rejected'", "'corrected'"):
        assert forbidden not in source, forbidden


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


# --------------------------------------------------------------------------
# Non-regression: Phase 3.4A skills, Phase 3.4B1 experiences and projects
# --------------------------------------------------------------------------


def test_the_projection_never_touches_the_phase_34a_skill_tables(
    migrated, profile_id
):
    """No skill is inferred from a diploma, a certification or a language."""
    accepted(migrated, profile_id, "Python", fact_type=ProfileFactType.SKILL)
    synchronize_profile_skills(migrated, profile_id)
    skills_before = _skill_counts(migrated)
    evidence_before = _skill_rows(migrated)
    accepted(migrated, profile_id, "Master Python | Université de Test | 2020 - 2022")
    accepted(
        migrated,
        profile_id,
        "Certification : Python Test | Délivré par : Organisme Test",
        fact_type=ProfileFactType.CERTIFICATION,
    )
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )

    synchronize_structured_profile_entries(migrated, profile_id)

    assert _skill_counts(migrated) == skills_before == (1, 1, 1)
    assert _skill_rows(migrated) == evidence_before


def test_the_phase_34b1_rows_are_written_exactly_as_before(migrated, profile_id):
    """The two older rules and their version are untouched by this slice."""
    accepted(
        migrated,
        profile_id,
        STRUCTURED_EXPERIENCE,
        fact_type=ProfileFactType.EXPERIENCE,
    )
    accepted(
        migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT
    )

    synchronize_structured_profile_entries(migrated, profile_id)
    experience = list_profile_experiences(migrated, profile_id)[0]
    project = list_profile_projects(migrated, profile_id)[0]

    assert experience.role_text == "Data Analyst"
    assert experience.organization_text == "ACME"
    assert experience.period_text == "2022 - 2024"
    assert experience.description_text == "Première ligne"
    assert experience.structuring_rule_id == "EXPERIENCE_PIPE_HEADER_V1"
    assert experience.structurer_version == "structured-profile-v1"
    assert project.title_text == "Analyse RH"
    assert project.period_text == "2023 - 2024"
    assert project.description_text == "segmentation des effectifs"
    assert project.structuring_rule_id == "PROJECT_BULLET_COLON_V1"
    assert project.structurer_version == "structured-profile-v1"


def test_adding_the_new_types_rewrites_no_phase_34b1_row(migrated, profile_id):
    """Ids and timestamps survive: the new types are additions, not a rewrite."""
    accepted(
        migrated,
        profile_id,
        STRUCTURED_EXPERIENCE,
        fact_type=ProfileFactType.EXPERIENCE,
    )
    accepted(
        migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT
    )
    synchronize_structured_profile_entries(migrated, profile_id)
    before = _b1_rows(migrated, profile_id)

    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    accepted(
        migrated,
        profile_id,
        STRUCTURED_CERTIFICATION,
        fact_type=ProfileFactType.CERTIFICATION,
    )
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )
    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (3, 0)
    assert _b1_rows(migrated, profile_id) == before


def test_a_second_sync_rewrites_no_row_of_any_of_the_five_tables(
    migrated, profile_id
):
    accepted(
        migrated,
        profile_id,
        STRUCTURED_EXPERIENCE,
        fact_type=ProfileFactType.EXPERIENCE,
    )
    accepted(
        migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT
    )
    accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    accepted(
        migrated,
        profile_id,
        STRUCTURED_CERTIFICATION,
        fact_type=ProfileFactType.CERTIFICATION,
    )
    accepted(
        migrated, profile_id, STRUCTURED_LANGUAGE, fact_type=ProfileFactType.LANGUAGE
    )
    synchronize_structured_profile_entries(migrated, profile_id)
    readers = (
        list_profile_experiences,
        list_profile_projects,
        list_profile_educations,
        list_profile_certifications,
        list_profile_languages,
    )
    before = [read_back(migrated, profile_id) for read_back in readers]

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert outcome.changed is False
    assert [read_back(migrated, profile_id) for read_back in readers] == before


def test_rejecting_an_education_leaves_the_experiences_and_projects_alone(
    migrated, profile_id
):
    accepted(
        migrated,
        profile_id,
        STRUCTURED_EXPERIENCE,
        fact_type=ProfileFactType.EXPERIENCE,
    )
    accepted(
        migrated, profile_id, STRUCTURED_PROJECT, fact_type=ProfileFactType.PROJECT
    )
    education = accepted(migrated, profile_id, STRUCTURED_EDUCATION)
    synchronize_structured_profile_entries(migrated, profile_id)
    before = _b1_rows(migrated, profile_id)
    reject_profile_fact(migrated, profile_id, education.id)

    outcome = synchronize_structured_profile_entries(migrated, profile_id)

    assert (outcome.created, outcome.removed) == (0, 1)
    assert _b1_rows(migrated, profile_id) == before
