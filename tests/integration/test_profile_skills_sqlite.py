"""Migration 0008 and the skill projection, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away. No real CV, no
real address and no real personal data takes part: every value is synthetic and
`.invalid` never resolves. The operational `.data/` database is never opened.

The projection is derived data, so most of these tests are about what it may
**not** do: read a fact nobody accepted, write to `profile_facts`, duplicate an
association, keep an association nothing justifies any more, or let one
profile's facts reach another profile's skills.
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
from services.digital_twin.skills.models import SKILL_NORMALIZER_VERSION
from services.digital_twin.skills.repository import (
    ProfileSkillNotFoundError,
    get_skill_by_canonical_key,
    list_profile_skills,
    synchronize_profile_skills,
)

REPOSITORY_SOURCE = Path("services/digital_twin/skills/repository.py")

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_OTHER_EMAIL = "other.student@example.invalid"

BEFORE_THIS_SLICE = ("0001", "0002", "0003", "0004", "0005", "0006", "0007")


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
    connection = connect_database(tmp_path / "skills.db")
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


def propose(
    connection, profile_id: int, value: str, *, fact_type=ProfileFactType.SKILL
):
    return propose_profile_fact(
        connection,
        profile_id=profile_id,
        fact_type=fact_type,
        value=value,
        provenance=_provenance(),
    )


def accepted(
    connection, profile_id: int, value: str, *, fact_type=ProfileFactType.SKILL
):
    fact = propose(connection, profile_id, value, fact_type=fact_type)
    return accept_profile_fact(connection, profile_id, fact.id)


def keys_of(connection, profile_id: int) -> list[str]:
    return [
        skill.canonical_key
        for skill in list_profile_skills(connection, profile_id)
    ]


def evidence_facts(connection, profile_id: int) -> dict[str, list[int]]:
    return {
        skill.canonical_key: [item.fact_id for item in skill.evidence]
        for skill in list_profile_skills(connection, profile_id)
    }


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


def _counts(connection) -> tuple[int, int, int]:
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


# --------------------------------------------------------------------------
# Migration 0008
# --------------------------------------------------------------------------


def _apply_up_to_0007(connection, tmp_path) -> list[str]:
    directory = tmp_path / "migrations-before-0008"
    directory.mkdir()
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version in BEFORE_THIS_SLICE:
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return apply_migrations(connection, directory)


def test_0008_upgrades_a_database_that_stopped_at_0007(tmp_path):
    connection = connect_database(tmp_path / "upgrade.db")
    try:
        assert _apply_up_to_0007(connection, tmp_path) == list(BEFORE_THIS_SLICE)
        existing = ensure_user_profile(connection, TEST_ONLY_EMAIL)
        fact = accepted(connection, existing.profile_id, "Python")

        assert apply_migrations(connection) == [
            "0008", "0009", "0010", "0011", "0012", "0013", "0014",
        ]

        assert {
            "skills",
            "profile_skills",
            "profile_skill_evidence",
        } <= _tables(connection)
        # The facts that existed before the upgrade are untouched by it.
        assert [row[0] for row in _fact_rows(connection)] == [fact.id]
        assert _counts(connection) == (0, 0, 0)
    finally:
        connection.close()


def test_0008_is_recorded_once_and_seeds_nothing(migrated):
    """Applying the migrations again is a no-op; no vocabulary is seeded."""
    assert apply_migrations(migrated) == []
    recorded = migrated.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()

    assert recorded[-1] == ("0014",)
    assert _counts(migrated) == (0, 0, 0)


def test_the_columns_are_the_projection_and_nothing_else(migrated):
    assert _columns(migrated, "skills") == [
        "id",
        "canonical_key",
        "canonical_name",
        "created_at",
    ]
    assert _columns(migrated, "profile_skills") == [
        "id",
        "profile_id",
        "skill_id",
        "created_at",
    ]
    assert _columns(migrated, "profile_skill_evidence") == [
        "id",
        "profile_skill_id",
        "fact_id",
        "normalizer_version",
        "normalization_rule_id",
        "created_at",
    ]


def test_no_0008_table_carries_a_level_a_score_or_a_verified_flag(migrated):
    """0008 adds no second definition of truth, and no level of any kind."""
    forbidden = (
        "verified",
        "level",
        "proficiency",
        "seniority",
        "score",
        "confidence",
        "occurrence",
        "count",
    )

    for table in ("skills", "profile_skills", "profile_skill_evidence"):
        for column in _columns(migrated, table):
            folded = column.casefold()
            assert not any(word in folded for word in forbidden), (table, column)


def test_0008_duplicates_no_provenance_column(migrated):
    """The audit chain is a chain: evidence points at the fact for the rest.

    `fact_id` is the pointer both tables need and is not a duplication; every
    other provenance column — the source type, the digest, the parser and
    extractor versions, the fingerprint, the rule, the pages and the section —
    stays on the provenance side, where it is written once.
    """
    columns = set(_columns(migrated, "profile_skill_evidence"))
    provenance = set(_columns(migrated, "profile_fact_provenance"))

    assert columns & provenance == {"id", "created_at", "fact_id"}
    assert not (columns & (provenance - {"id", "created_at", "fact_id"}))


def test_a_canonical_key_is_unique_non_empty_and_trimmed(migrated):
    migrated.execute(
        "INSERT INTO skills (canonical_key, canonical_name) VALUES ('python', 'Python')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO skills (canonical_key, canonical_name) "
            "VALUES ('python', 'Python autre')"
        )
    for key, name in (("   ", "Python"), (" python", "Python"), ("python3", "  ")):
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                "INSERT INTO skills (canonical_key, canonical_name) VALUES (?, ?)",
                (key, name),
            )


def test_one_profile_holds_one_skill_at_most_once(migrated, profile_id):
    skill_id = migrated.execute(
        "INSERT INTO skills (canonical_key, canonical_name) "
        "VALUES ('python', 'Python') RETURNING id"
    ).fetchone()[0]
    migrated.execute(
        "INSERT INTO profile_skills (profile_id, skill_id) VALUES (?, ?)",
        (profile_id, skill_id),
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_skills (profile_id, skill_id) VALUES (?, ?)",
            (profile_id, skill_id),
        )


def test_an_association_needs_a_real_profile_and_a_real_skill(migrated, profile_id):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_skills (profile_id, skill_id) VALUES (?, 4242)",
            (profile_id,),
        )
    skill_id = migrated.execute(
        "INSERT INTO skills (canonical_key, canonical_name) "
        "VALUES ('python', 'Python') RETURNING id"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_skills (profile_id, skill_id) VALUES (4242, ?)",
            (skill_id,),
        )


def test_evidence_needs_a_real_association_and_a_real_fact(migrated, profile_id):
    fact = accepted(migrated, profile_id, "Python")
    synchronize_profile_skills(migrated, profile_id)
    association = list_profile_skills(migrated, profile_id)[0]

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_skill_evidence (profile_skill_id, fact_id, "
            "normalizer_version, normalization_rule_id) "
            "VALUES (4242, ?, 'v', 'LITERAL_V1')",
            (fact.id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_skill_evidence (profile_skill_id, fact_id, "
            "normalizer_version, normalization_rule_id) "
            "VALUES (?, 4242, 'v', 'LITERAL_V1')",
            (association.id,),
        )


def test_one_fact_can_justify_only_one_skill(migrated, profile_id):
    """A fact read two ways would be a defect, so the schema refuses it."""
    fact = accepted(migrated, profile_id, "Python")
    synchronize_profile_skills(migrated, profile_id)
    other_skill = migrated.execute(
        "INSERT INTO skills (canonical_key, canonical_name) "
        "VALUES ('sql', 'SQL') RETURNING id"
    ).fetchone()[0]
    other_association = migrated.execute(
        "INSERT INTO profile_skills (profile_id, skill_id) VALUES (?, ?) RETURNING id",
        (profile_id, other_skill),
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_skill_evidence (profile_skill_id, fact_id, "
            "normalizer_version, normalization_rule_id) "
            "VALUES (?, ?, 'skill-normalizer-v1', 'LITERAL_V1')",
            (other_association, fact.id),
        )


def test_several_facts_may_justify_the_same_skill(migrated, profile_id):
    first = accepted(migrated, profile_id, "PowerBI")
    second = accepted(migrated, profile_id, "Power BI")

    synchronize_profile_skills(migrated, profile_id)

    assert evidence_facts(migrated, profile_id) == {"power bi": [first.id, second.id]}


def test_the_evidence_version_and_rule_must_be_present_and_trimmed(
    migrated, profile_id
):
    accepted(migrated, profile_id, "Python")
    synchronize_profile_skills(migrated, profile_id)
    association = list_profile_skills(migrated, profile_id)[0]
    second = accepted(migrated, profile_id, "SQL")

    for version, rule in ((" ", "LITERAL_V1"), ("v1", "  "), (" v1", "LITERAL_V1")):
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                "INSERT INTO profile_skill_evidence (profile_skill_id, fact_id, "
                "normalizer_version, normalization_rule_id) VALUES (?, ?, ?, ?)",
                (association.id, second.id, version, rule),
            )


def test_the_indexes_the_projection_reads_on_exist(migrated):
    assert "idx_profile_skills_profile" in _indexes(migrated, "profile_skills")
    assert "idx_profile_skills_skill" in _indexes(migrated, "profile_skills")
    assert "idx_profile_skill_evidence_profile_skill" in _indexes(
        migrated, "profile_skill_evidence"
    )


def test_the_cascades_and_restrictions_are_the_documented_ones(migrated):
    assert _foreign_keys(migrated, "profile_skills") == {
        ("profiles", "profile_id", "id", "CASCADE"),
        ("skills", "skill_id", "id", "RESTRICT"),
    }
    assert _foreign_keys(migrated, "profile_skill_evidence") == {
        ("profile_skills", "profile_skill_id", "id", "CASCADE"),
        ("profile_facts", "fact_id", "id", "CASCADE"),
    }


def test_a_skill_a_profile_still_holds_cannot_be_deleted(migrated, profile_id):
    accepted(migrated, profile_id, "Python")
    synchronize_profile_skills(migrated, profile_id)
    skill = get_skill_by_canonical_key(migrated, "python")

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("DELETE FROM skills WHERE id = ?", (skill.id,))


def test_deleting_a_profile_takes_its_skills_and_leaves_the_vocabulary(
    migrated, profile_id
):
    accepted(migrated, profile_id, "Python")
    synchronize_profile_skills(migrated, profile_id)

    migrated.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

    assert _counts(migrated) == (1, 0, 0)


# --------------------------------------------------------------------------
# Only ACCEPTED SKILL facts are projected
# --------------------------------------------------------------------------


def test_only_accepted_skill_facts_reach_the_projection(migrated, profile_id):
    verified = accepted(migrated, profile_id, "Python")
    propose(migrated, profile_id, "Rust")
    rejected = propose(migrated, profile_id, "Fortran")
    reject_profile_fact(migrated, profile_id, rejected.id)
    corrected = accepted(migrated, profile_id, "Cobol")
    correct_profile_fact(migrated, profile_id, corrected.id, value="Scala")
    accepted(migrated, profile_id, "Docker", fact_type=ProfileFactType.CERTIFICATION)
    accepted(migrated, profile_id, "Anglais", fact_type=ProfileFactType.LANGUAGE)

    outcome = synchronize_profile_skills(migrated, profile_id)

    # `Python` and the ACCEPTED replacement `Scala`; nothing else qualifies.
    assert outcome.verified_skill_facts == 2
    assert keys_of(migrated, profile_id) == ["python", "scala"]
    assert evidence_facts(migrated, profile_id)["python"] == [verified.id]


@pytest.mark.parametrize(
    "fact_type",
    [
        ProfileFactType.CERTIFICATION,
        ProfileFactType.LANGUAGE,
        ProfileFactType.EXPERIENCE,
        ProfileFactType.PROJECT,
        ProfileFactType.EDUCATION,
        ProfileFactType.PROFESSIONAL_TITLE,
    ],
)
def test_an_accepted_fact_of_another_type_is_ignored(
    migrated, profile_id, fact_type
):
    accepted(migrated, profile_id, "Python", fact_type=fact_type)

    outcome = synchronize_profile_skills(migrated, profile_id)

    assert outcome.verified_skill_facts == 0
    assert keys_of(migrated, profile_id) == []
    assert _counts(migrated) == (0, 0, 0)


def test_a_proposed_skill_fact_creates_no_vocabulary_row_either(migrated, profile_id):
    propose(migrated, profile_id, "Rust")

    synchronize_profile_skills(migrated, profile_id)

    assert _counts(migrated) == (0, 0, 0)


# --------------------------------------------------------------------------
# The projection itself
# --------------------------------------------------------------------------


def test_one_accepted_fact_gives_one_skill_and_one_evidence(migrated, profile_id):
    fact = accepted(migrated, profile_id, "Python")

    outcome = synchronize_profile_skills(migrated, profile_id)
    skills = list_profile_skills(migrated, profile_id)

    assert outcome.changed is True
    assert outcome.profile_skills == 1
    assert outcome.evidence_created == 1
    assert len(skills) == 1
    assert skills[0].canonical_name == "Python"
    assert [item.fact_id for item in skills[0].evidence] == [fact.id]
    assert skills[0].evidence[0].normalizer_version == SKILL_NORMALIZER_VERSION
    assert skills[0].evidence[0].normalization_rule_id == "LITERAL_V1"


def test_two_alias_facts_give_one_skill_and_two_evidences(migrated, profile_id):
    first = accepted(migrated, profile_id, "PowerBI")
    second = accepted(migrated, profile_id, "power bi")

    outcome = synchronize_profile_skills(migrated, profile_id)
    skills = list_profile_skills(migrated, profile_id)

    assert outcome.profile_skills == 1
    assert outcome.evidence_created == 2
    assert len(skills) == 1
    assert skills[0].canonical_name == "Power BI"
    assert [item.fact_id for item in skills[0].evidence] == [first.id, second.id]
    assert [item.normalization_rule_id for item in skills[0].evidence] == [
        "ALIAS_REGISTRY_V1",
        "CANONICAL_FORM_V1",
    ]


def test_two_evidences_are_two_proofs_and_never_a_level(migrated, profile_id):
    """The only thing several facts add is auditability."""
    accepted(migrated, profile_id, "PowerBI")
    accepted(migrated, profile_id, "Power BI")
    accepted(migrated, profile_id, "PowerBI ")

    synchronize_profile_skills(migrated, profile_id)
    skills = list_profile_skills(migrated, profile_id)

    assert len(skills) == 1
    assert len(skills[0].evidence) == 3
    assert not hasattr(skills[0], "level")


def test_the_c_family_stays_three_skills_in_the_database(migrated, profile_id):
    for written in ("C", "C++", "C#"):
        accepted(migrated, profile_id, written)

    synchronize_profile_skills(migrated, profile_id)

    assert sorted(keys_of(migrated, profile_id)) == ["c", "c#", "c++"]


def test_a_mention_with_a_parenthesis_is_stored_whole(migrated, profile_id):
    accepted(migrated, profile_id, "Azure Data Platform (avancé)")

    synchronize_profile_skills(migrated, profile_id)
    skills = list_profile_skills(migrated, profile_id)

    assert len(skills) == 1
    assert skills[0].canonical_name == "Azure Data Platform (avancé)"


# --------------------------------------------------------------------------
# Idempotence and id stability
# --------------------------------------------------------------------------


def test_a_second_synchronization_changes_nothing(migrated, profile_id):
    accepted(migrated, profile_id, "Python")
    accepted(migrated, profile_id, "PowerBI")
    synchronize_profile_skills(migrated, profile_id)
    before = _counts(migrated)

    outcome = synchronize_profile_skills(migrated, profile_id)

    assert outcome.changed is False
    assert outcome.as_dict() == {
        "verified_skill_facts": 2,
        "profile_skills": 2,
        "skills_created": 0,
        "profile_skills_created": 0,
        "profile_skills_removed": 0,
        "evidence_created": 0,
        "evidence_removed": 0,
        "normalizer_version": SKILL_NORMALIZER_VERSION,
        "changed": False,
    }
    assert _counts(migrated) == before


def test_ids_and_timestamps_are_stable_when_nothing_changed(migrated, profile_id):
    accepted(migrated, profile_id, "Python")
    accepted(migrated, profile_id, "PowerBI")
    synchronize_profile_skills(migrated, profile_id)
    before = list_profile_skills(migrated, profile_id)

    synchronize_profile_skills(migrated, profile_id)

    assert list_profile_skills(migrated, profile_id) == before


def test_a_new_accepted_fact_is_added_without_disturbing_the_others(
    migrated, profile_id
):
    accepted(migrated, profile_id, "Python")
    synchronize_profile_skills(migrated, profile_id)
    before = list_profile_skills(migrated, profile_id)[0]
    accepted(migrated, profile_id, "SQL")

    outcome = synchronize_profile_skills(migrated, profile_id)
    after = {
        skill.canonical_key: skill
        for skill in list_profile_skills(migrated, profile_id)
    }

    assert outcome.profile_skills_created == 1
    assert outcome.evidence_created == 1
    assert after["python"] == before


# --------------------------------------------------------------------------
# Reconciliation after a human changes their mind
# --------------------------------------------------------------------------


def test_a_corrected_fact_stops_proving_and_its_replacement_takes_over(
    migrated, profile_id
):
    fact = accepted(migrated, profile_id, "Pyton")
    synchronize_profile_skills(migrated, profile_id)
    correction = correct_profile_fact(migrated, profile_id, fact.id, value="Python")

    outcome = synchronize_profile_skills(migrated, profile_id)

    assert outcome.evidence_removed == 1
    assert outcome.evidence_created == 1
    assert outcome.profile_skills_removed == 1
    assert evidence_facts(migrated, profile_id) == {
        "python": [correction.replacement.id]
    }


def test_a_rejection_after_a_synchronization_is_reconciled(migrated, profile_id):
    fact = accepted(migrated, profile_id, "Fortran")
    synchronize_profile_skills(migrated, profile_id)
    reject_profile_fact(migrated, profile_id, fact.id)

    outcome = synchronize_profile_skills(migrated, profile_id)

    assert outcome.evidence_removed == 1
    assert outcome.profile_skills_removed == 1
    assert keys_of(migrated, profile_id) == []


def test_a_remaining_evidence_keeps_the_skill(migrated, profile_id):
    first = accepted(migrated, profile_id, "PowerBI")
    second = accepted(migrated, profile_id, "Power BI")
    synchronize_profile_skills(migrated, profile_id)
    before = list_profile_skills(migrated, profile_id)[0]
    reject_profile_fact(migrated, profile_id, first.id)

    outcome = synchronize_profile_skills(migrated, profile_id)
    after = list_profile_skills(migrated, profile_id)

    assert outcome.profile_skills_removed == 0
    assert len(after) == 1
    assert after[0].id == before.id
    assert [item.fact_id for item in after[0].evidence] == [second.id]


def test_the_last_evidence_removed_removes_the_skill(migrated, profile_id):
    first = accepted(migrated, profile_id, "PowerBI")
    second = accepted(migrated, profile_id, "Power BI")
    synchronize_profile_skills(migrated, profile_id)
    reject_profile_fact(migrated, profile_id, first.id)
    reject_profile_fact(migrated, profile_id, second.id)

    outcome = synchronize_profile_skills(migrated, profile_id)

    assert outcome.profile_skills_removed == 1
    assert keys_of(migrated, profile_id) == []


def test_the_vocabulary_row_survives_and_means_nothing_on_its_own(
    migrated, profile_id
):
    fact = accepted(migrated, profile_id, "Fortran")
    synchronize_profile_skills(migrated, profile_id)
    reject_profile_fact(migrated, profile_id, fact.id)

    synchronize_profile_skills(migrated, profile_id)

    assert get_skill_by_canonical_key(migrated, "fortran") is not None
    assert keys_of(migrated, profile_id) == []


# --------------------------------------------------------------------------
# Isolation, refusals and what is never written
# --------------------------------------------------------------------------


def test_two_profiles_are_strictly_separate(migrated, profile_id, other_profile_id):
    accepted(migrated, profile_id, "Python")
    accepted(migrated, other_profile_id, "SQL")

    synchronize_profile_skills(migrated, profile_id)
    synchronize_profile_skills(migrated, other_profile_id)

    assert keys_of(migrated, profile_id) == ["python"]
    assert keys_of(migrated, other_profile_id) == ["sql"]


def test_one_profiles_rejection_leaves_the_other_profile_alone(
    migrated, profile_id, other_profile_id
):
    mine = accepted(migrated, profile_id, "Python")
    accepted(migrated, other_profile_id, "Python")
    synchronize_profile_skills(migrated, profile_id)
    synchronize_profile_skills(migrated, other_profile_id)
    reject_profile_fact(migrated, profile_id, mine.id)

    synchronize_profile_skills(migrated, profile_id)

    assert keys_of(migrated, profile_id) == []
    assert keys_of(migrated, other_profile_id) == ["python"]


def test_an_unknown_profile_is_refused_and_creates_nothing(migrated):
    with pytest.raises(ProfileSkillNotFoundError):
        synchronize_profile_skills(migrated, 4242)

    assert _counts(migrated) == (0, 0, 0)


def test_a_failure_rolls_the_whole_projection_back(migrated, profile_id):
    accepted(migrated, profile_id, "Python")
    accepted(migrated, profile_id, "SQL")

    def fail() -> None:
        raise RuntimeError("TEST ONLY failure after the facts were read")

    with pytest.raises(RuntimeError):
        synchronize_profile_skills(migrated, profile_id, after_read=fail)

    assert _counts(migrated) == (0, 0, 0)


def test_a_failure_after_a_first_run_leaves_that_run_intact(migrated, profile_id):
    accepted(migrated, profile_id, "Python")
    synchronize_profile_skills(migrated, profile_id)
    before = list_profile_skills(migrated, profile_id)
    accepted(migrated, profile_id, "SQL")

    def fail() -> None:
        raise RuntimeError("TEST ONLY failure after the facts were read")

    with pytest.raises(RuntimeError):
        synchronize_profile_skills(migrated, profile_id, after_read=fail)

    assert list_profile_skills(migrated, profile_id) == before


def test_the_projection_never_touches_profile_facts(migrated, profile_id):
    accepted(migrated, profile_id, "Python")
    accepted(migrated, profile_id, "PowerBI")
    facts_before = _fact_rows(migrated)
    provenance_before = _provenance_rows(migrated)

    synchronize_profile_skills(migrated, profile_id)
    synchronize_profile_skills(migrated, profile_id)

    assert _fact_rows(migrated) == facts_before
    assert _provenance_rows(migrated) == provenance_before


def test_the_repository_writes_no_statement_against_the_fact_tables() -> None:
    """Read the SQL the module executes, not only what it says it does."""
    statements = code_only(REPOSITORY_SOURCE).casefold()

    for forbidden in (
        "insert into profile_facts",
        "update profile_facts",
        "delete from profile_facts",
        "insert into profile_fact_provenance",
        "update profile_fact_provenance",
        "delete from profile_fact_provenance",
        "accept_profile_fact",
        "reject_profile_fact",
        "correct_profile_fact",
    ):
        assert forbidden not in statements, forbidden


def test_the_repository_reads_only_accepted_skill_facts() -> None:
    """The filter is written into the SQL, so no caller can widen it.

    This one assertion reads the raw source rather than the stripped code,
    because the statement it is about *is* a string literal.
    """
    source = REPOSITORY_SOURCE.read_text(encoding="utf-8").casefold()

    assert "fact_type = 'skill' and status = 'accepted'" in source
    for forbidden in ("'proposed'", "'rejected'", "'corrected'"):
        assert forbidden not in source, forbidden
