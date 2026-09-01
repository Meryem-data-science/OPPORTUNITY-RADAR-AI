"""Migration 0007 and the profile-fact cycle, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away. No real CV, no
real address and no real personal data takes part: every value is synthetic and
marked TEST ONLY, and `.invalid` never resolves.
"""

import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
)
from services.digital_twin.facts.models import (
    FactSourceType,
    FactStatus,
    ProfileFactType,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    InvalidFactTransitionError,
    ProfileFactError,
    ProfileFactNotFoundError,
    accept_profile_fact,
    add_profile_fact_provenance,
    correct_profile_fact,
    get_profile_fact,
    list_profile_fact_provenance,
    list_profile_facts,
    list_verified_profile_facts,
    propose_profile_fact,
    reject_profile_fact,
)
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_OTHER_EMAIL = "other.student@example.invalid"
TEST_ONLY_VALUE = "student@example.invalid"
TEST_ONLY_CORRECTION = "corrected.student@example.invalid"
# A synthetic 64-hex digest; no real file was hashed to produce it.
TEST_ONLY_SHA256 = "ab" * 32

BEFORE_THIS_SLICE = ("0001", "0002", "0003", "0004", "0005", "0006")


def _cv_provenance(**overrides) -> ProvenanceInput:
    """Evidence shaped exactly like one Phase 3.2B `ExtractedCandidate`."""
    values = {
        "source_type": FactSourceType.CV,
        "source_locator": "TEST ONLY cv",
        "cv_sha256": TEST_ONLY_SHA256,
        "parser_version": "cv-parser-v1",
        "extractor_version": "cv-candidates-v1",
        "candidate_fingerprint": "0123456789abcdef",
        "rule_id": "EMAIL_PATTERN",
        "page_numbers": (1, 2),
        "section_type": "UNCLASSIFIED",
        "section_index": 0,
    }
    values.update(overrides)
    return ProvenanceInput(**values)


def _apply_up_to_0006(connection, tmp_path) -> list[str]:
    """Apply only the migrations that existed before this slice."""
    directory = tmp_path / "migrations-before-0007"
    directory.mkdir()
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version in BEFORE_THIS_SLICE:
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return apply_migrations(connection, directory)


def _tables(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _counts(connection) -> tuple[int, int]:
    facts = connection.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0]
    provenance = connection.execute(
        "SELECT COUNT(*) FROM profile_fact_provenance"
    ).fetchone()[0]
    return int(facts), int(provenance)


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "facts.db")
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
def proposed(migrated, profile_id):
    return propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=TEST_ONLY_VALUE,
        normalized_value=TEST_ONLY_VALUE,
        provenance=_cv_provenance(),
    )


# --------------------------------------------------------------------------
# Migration 0007
# --------------------------------------------------------------------------


def test_0007_upgrades_a_database_that_stopped_at_0006(tmp_path):
    connection = connect_database(tmp_path / "upgrade.db")
    try:
        assert _apply_up_to_0006(connection, tmp_path) == list(BEFORE_THIS_SLICE)
        existing = ensure_user_profile(connection, TEST_ONLY_EMAIL)

        assert apply_migrations(connection) == [
            "0007", "0008", "0009", "0010", "0011", "0012", "0013", "0014", "0015",
        ]

        assert {"profile_facts", "profile_fact_provenance"} <= _tables(connection)
        assert (
            connection.execute(
                "SELECT id, user_id FROM profiles ORDER BY id"
            ).fetchall()
            == [(existing.profile_id, existing.user_id)]
        )
        assert _counts(connection) == (0, 0)
    finally:
        connection.close()


def test_0007_is_recorded_once_and_seeds_nothing(migrated):
    assert apply_migrations(migrated) == []
    recorded = migrated.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()

    assert recorded[-1] == ("0015",)
    assert _counts(migrated) == (0, 0)


def test_0007_adds_no_factual_column_to_profiles(migrated):
    """A fact belongs beside its evidence, never in the root that owns it."""
    assert _columns(migrated, "profiles") == [
        "id",
        "user_id",
        "created_at",
        "updated_at",
    ]
    assert _columns(migrated, "users") == ["id", "email", "created_at", "updated_at"]


def test_no_table_carries_a_persistent_verified_flag(migrated):
    """`ACCEPTED` is the only definition of verified, so nothing stores a second."""
    for table in sorted(_tables(migrated)):
        for column in _columns(migrated, table):
            assert "verified" not in column.casefold(), (table, column)


#: The three tables migration `0008` adds. They are exempted one by one rather
#: than matched on a prefix: `skill` stays a forbidden word below, so a fourth
#: table about a skill — `skill_levels`, `skill_scores`, `user_skills_extra` —
#: is caught rather than waved through.
PHASE_34A_TABLES = frozenset({"skills", "profile_skills", "profile_skill_evidence"})

#: The two tables migration `0009` adds. Same rule, same reason: `project` and
#: `experience` stay forbidden words, so a third table about either one —
#: `experience_scores`, `project_matches` — is still caught.
PHASE_34B1_TABLES = frozenset({"profile_experiences", "profile_projects"})

#: The three tables migration `0010` adds. Same rule, same reason: `education`,
#: `certification` and `language` stay forbidden words, so a fourth table about
#: any of them — `education_levels`, `certification_scores`,
#: `language_proficiency_levels` — is still caught.
PHASE_34B2_TABLES = frozenset(
    {"profile_educations", "profile_certifications", "profile_languages"}
)

#: The four tables migration `0011` adds. Same rule, same reason: `preference`,
#: `availability`, `mobility` and `career` stay forbidden words, so a fifth
#: table about any of them — `preference_scores`, `availability_matches`,
#: `mobility_ranges`, `career_levels` — is still caught.
PHASE_34C_TABLES = frozenset(
    {
        "profile_availability",
        "profile_mobility",
        "profile_preferences",
        "profile_career_objectives",
    }
)

#: The seven tables migration `0012` adds. They are the **offer** side, not a
#: second profile projection: they describe what a posting requires, and the
#: guard below is about what may be derived from `profile_facts`. `education`
#: `skill` and `experience` stay forbidden words all the same, so a second
#: profile-side education, experience or skill table is still caught.
PHASE_35A_TABLES = frozenset(
    {
        "opportunity_constraints",
        "opportunity_constraint_locations",
        "opportunity_education_requirements",
        "opportunity_experience_requirements",
        "opportunity_constraint_evidence",
        "opportunity_constraint_conflicts",
        "opportunity_skill_requirements",
    }
)

#: The five tables `0013` adds on the offer side. `opportunity_skill_
#: requirements` is not among them: `0012` created it and `0013` fills it.
PHASE_35B_TABLES = frozenset(
    {
        "opportunity_skill_requirement_evidence",
        "opportunity_language_requirements",
        "opportunity_language_requirement_evidence",
        "opportunity_requirement_ambiguities",
        "opportunity_requirement_extraction_state",
    }
)

#: Every table a projection slice is allowed to have added so far.
#: The two tables Phase 3.6 adds. They are the one legitimate place in this
#: database where a row names a person and a posting at once, and they exist
#: because the phase that compares the two sides is implemented. Everything the
#: guard below still forbids remains forbidden: a *third* table joining them, a
#: match, a score and a ranking beyond the exact persistence tables now owned
#: by Phase 4.7A.
PHASE_36_TABLES = frozenset(
    {"opportunity_eligibilities", "eligibility_rule_results"}
)
#: The exact Phase 4.7A persistence tables. Their names contain `match`, but
#: unrelated matching, score and ranking tables remain out of scope.
PHASE_47A_TABLES = frozenset(
    {"matching_runs", "matching_assessments", "matching_profile_state"}
)

PROJECTED_TABLES = (
    PHASE_34A_TABLES
    | PHASE_34B1_TABLES
    | PHASE_34B2_TABLES
    | PHASE_34C_TABLES
    | PHASE_35A_TABLES
    | PHASE_35B_TABLES
    | PHASE_36_TABLES
    | PHASE_47A_TABLES
)

#: Concepts this database must not contain. The list is the docstring of the
#: guard below, written as code so the two cannot drift apart.
OUT_OF_SCOPE_TABLE_WORDS = (
    "skill",
    "alias",
    "project",
    "experience",
    "education",
    "certification",
    "language",
    "preference",
    "availability",
    "mobility",
    "career",
    "eligib",
    "match",
    "score",
    "ranking",
)


def is_out_of_scope_table(name: str) -> bool:
    """True when a table with this name has no business existing here."""
    if name in PROJECTED_TABLES:
        return False
    folded = name.casefold()
    return any(word in folded for word in OUT_OF_SCOPE_TABLE_WORDS)


def test_no_table_beyond_the_projections_exists_yet(migrated):
    """3.4A projects skills, 3.4B experiences through languages, 3.4C the rest.

    The twelve tables `0008` through `0011` add are exempted one by one, and so
    are the seven `0012` adds, the five `0013` adds on the offer side and the
    two `0014` adds for Phase 3.6 and the three `0015` persistence tables;
    every other table is held to the whole out-of-scope list — no fourth skill
    table, no administrable alias table, no
    second education, certification or language table, no second preference,
    availability, mobility or career objective table, no *second* eligibility
    table, and no matching, score or ranking table beyond the exact Phase 4.7A
    persistence allowlist. Phase 3.5A adds constraints a posting states and
    Phase 3.5B the skills and languages it asks for; neither adds a comparison
    of one to a person. Phase 3.6 adds exactly that comparison, in exactly two
    tables.
    """
    for table in _tables(migrated):
        assert not is_out_of_scope_table(table), table


@pytest.mark.parametrize(
    "name",
    [
        "skill_levels",
        "skill_scores",
        "user_skills_extra",
        "profile_skill_levels",
        "skill_aliases",
        "profile_preference_scores",
        "availability_matches",
        "mobility_ranges",
        "career_levels",
        "opportunity_matches",
        "eligibility_rules",
        "experience_scores",
        "project_matches",
        "profile_experience_levels",
        "education_levels",
        "profile_education_levels",
        "certification_scores",
        "language_proficiency_levels",
        "profile_language_levels",
    ],
)
def test_the_guard_refuses_an_extra_out_of_scope_table(name: str) -> None:
    """The exemption is twelve names, not the words `skill`, `project`,
    `experience`, `education`, `certification`, `language`, `preference`,
    `availability`, `mobility` or `career`."""
    assert is_out_of_scope_table(name)


@pytest.mark.parametrize("name", sorted(PROJECTED_TABLES))
def test_the_guard_still_allows_the_projected_tables(name: str) -> None:
    assert not is_out_of_scope_table(name)


def test_the_fact_columns_are_exactly_the_cycle_and_its_history(migrated):
    assert _columns(migrated, "profile_facts") == [
        "id",
        "profile_id",
        "fact_type",
        "value",
        "normalized_value",
        "status",
        "replaced_by_fact_id",
        "created_at",
        "updated_at",
        "decided_at",
    ]
    assert _columns(migrated, "profile_fact_provenance") == [
        "id",
        "fact_id",
        "source_type",
        "provenance_key",
        "source_locator",
        "cv_sha256",
        "parser_version",
        "extractor_version",
        "candidate_fingerprint",
        "rule_id",
        "page_numbers",
        "section_type",
        "section_index",
        "created_at",
    ]


def test_a_fact_needs_the_profile_it_belongs_to(migrated):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_facts (profile_id, fact_type, value, status) "
            "VALUES (?, 'EMAIL', 'TEST ONLY', 'PROPOSED')",
            (4242,),
        )

    assert _counts(migrated) == (0, 0)


def test_evidence_needs_the_fact_it_justifies(migrated):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_fact_provenance (fact_id, source_type, provenance_key)"
            " VALUES (?, 'CV', 'TEST-ONLY')",
            (4242,),
        )

    assert _counts(migrated) == (0, 0)


@pytest.mark.parametrize(
    "columns, values",
    [
        # An unknown status is not part of the cycle.
        ("fact_type, value, status", "'EMAIL', 'TEST ONLY', 'MAYBE'"),
        # An empty or blank value asserts nothing.
        ("fact_type, value, status", "'EMAIL', '   ', 'PROPOSED'"),
        ("fact_type, value, status", "'  ', 'TEST ONLY', 'PROPOSED'"),
        # A proposal has not been decided.
        (
            "fact_type, value, status, decided_at",
            "'EMAIL', 'TEST ONLY', 'PROPOSED', '2026-01-01 00:00:00'",
        ),
        # A decision is always dated.
        ("fact_type, value, status", "'EMAIL', 'TEST ONLY', 'ACCEPTED'"),
        ("fact_type, value, status", "'EMAIL', 'TEST ONLY', 'REJECTED'"),
        # A correction always points at what replaced it.
        (
            "fact_type, value, status, decided_at",
            "'EMAIL', 'TEST ONLY', 'CORRECTED', '2026-01-01 00:00:00'",
        ),
    ],
)
def test_the_schema_refuses_an_incoherent_fact(migrated, profile_id, columns, values):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            f"INSERT INTO profile_facts (profile_id, {columns}) "
            f"VALUES ({profile_id}, {values})"
        )

    assert _counts(migrated) == (0, 0)


def test_only_a_corrected_fact_may_carry_a_replacement(migrated, proposed, profile_id):
    accepted = accept_profile_fact(migrated, profile_id, proposed.id)
    replacement = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=TEST_ONLY_CORRECTION,
        provenance=_cv_provenance(page_numbers=(3,)),
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "UPDATE profile_facts SET replaced_by_fact_id = ? WHERE id = ?",
            (replacement.id, accepted.id),
        )


def test_a_replacement_must_be_a_real_profile_fact(migrated, proposed, profile_id):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "UPDATE profile_facts "
            "SET status = 'CORRECTED', decided_at = CURRENT_TIMESTAMP, "
            "    replaced_by_fact_id = 4242 "
            "WHERE id = ?",
            (proposed.id,),
        )


def test_the_evidence_taxonomy_is_closed(migrated, proposed):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO profile_fact_provenance (fact_id, source_type, provenance_key)"
            " VALUES (?, 'LLM_GUESS', 'TEST-ONLY')",
            (proposed.id,),
        )


def test_the_same_proof_cannot_be_recorded_twice_for_one_fact(
    migrated, proposed, profile_id
):
    with pytest.raises(sqlite3.IntegrityError):
        add_profile_fact_provenance(
            migrated,
            profile_id=profile_id,
            fact_id=proposed.id,
            provenance=_cv_provenance(),
        )

    assert len(list_profile_fact_provenance(migrated, profile_id, proposed.id)) == 1


def test_a_second_distinct_proof_corroborates_the_same_fact(
    migrated, proposed, profile_id
):
    add_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=proposed.id,
        provenance=ProvenanceInput(
            source_type=FactSourceType.GITHUB, source_locator="TEST ONLY account"
        ),
    )

    recorded = list_profile_fact_provenance(migrated, profile_id, proposed.id)

    assert [item.source_type for item in recorded] == [
        FactSourceType.CV,
        FactSourceType.GITHUB,
    ]


# --------------------------------------------------------------------------
# Proposing, reading and listing
# --------------------------------------------------------------------------


def test_a_proposal_is_stored_with_its_evidence_and_is_not_verified(
    migrated, proposed, profile_id
):
    assert proposed.status is FactStatus.PROPOSED
    assert proposed.is_verified is False
    assert proposed.decided_at is None
    assert proposed.replaced_by_fact_id is None
    assert proposed.value == TEST_ONLY_VALUE

    evidence = list_profile_fact_provenance(migrated, profile_id, proposed.id)

    assert len(evidence) == 1
    assert evidence[0].source_type is FactSourceType.CV
    assert evidence[0].cv_sha256 == TEST_ONLY_SHA256
    assert evidence[0].parser_version == "cv-parser-v1"
    assert evidence[0].extractor_version == "cv-candidates-v1"
    assert evidence[0].rule_id == "EMAIL_PATTERN"
    assert evidence[0].page_numbers == (1, 2)
    assert evidence[0].section_type == "UNCLASSIFIED"
    assert evidence[0].section_index == 0
    assert evidence[0].candidate_fingerprint == "0123456789abcdef"


def test_an_unusable_proof_leaves_no_half_written_fact(migrated, profile_id):
    """The schema refuses the evidence, so the claim it justified is refused too."""
    with pytest.raises(sqlite3.IntegrityError):
        propose_profile_fact(
            migrated,
            profile_id=profile_id,
            fact_type=ProfileFactType.EMAIL,
            value=TEST_ONLY_VALUE,
            provenance=_cv_provenance(cv_sha256="not-a-digest"),
        )

    assert _counts(migrated) == (0, 0)
    assert list_profile_facts(migrated, profile_id) == ()


def test_a_failure_between_the_fact_and_its_proof_rolls_everything_back(
    migrated, profile_id
):
    def explode() -> None:
        raise RuntimeError("TEST ONLY failure before the provenance")

    with pytest.raises(RuntimeError):
        propose_profile_fact(
            migrated,
            profile_id=profile_id,
            fact_type=ProfileFactType.EMAIL,
            value=TEST_ONLY_VALUE,
            provenance=_cv_provenance(),
            after_fact=explode,
        )

    assert _counts(migrated) == (0, 0)


def test_a_proposal_for_an_unknown_profile_writes_nothing(migrated):
    with pytest.raises(ProfileFactNotFoundError):
        propose_profile_fact(
            migrated,
            profile_id=4242,
            fact_type=ProfileFactType.EMAIL,
            value=TEST_ONLY_VALUE,
            provenance=_cv_provenance(),
        )

    assert _counts(migrated) == (0, 0)


def test_reading_a_fact_back(migrated, proposed, profile_id):
    read = get_profile_fact(migrated, profile_id, proposed.id)

    assert read == proposed
    assert get_profile_fact(migrated, profile_id, 4242) is None


def test_listing_is_scoped_to_one_profile(
    migrated, proposed, profile_id, other_profile_id
):
    theirs = propose_profile_fact(
        migrated,
        profile_id=other_profile_id,
        fact_type=ProfileFactType.NAME,
        value="TEST ONLY name",
        provenance=ProvenanceInput(source_type=FactSourceType.USER_INPUT),
    )

    assert [fact.id for fact in list_profile_facts(migrated, profile_id)] == [
        proposed.id
    ]
    assert [fact.id for fact in list_profile_facts(migrated, other_profile_id)] == [
        theirs.id
    ]
    assert get_profile_fact(migrated, profile_id, theirs.id) is None


def test_listing_can_be_narrowed_to_one_fact_type(migrated, proposed, profile_id):
    skill = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.SKILL,
        value="TEST ONLY skill",
        provenance=ProvenanceInput(source_type=FactSourceType.USER_INPUT),
    )

    listed = list_profile_facts(migrated, profile_id, fact_type=ProfileFactType.SKILL)

    assert [fact.id for fact in listed] == [skill.id]
    assert len(list_profile_facts(migrated, profile_id)) == 2


# --------------------------------------------------------------------------
# The verified reading
# --------------------------------------------------------------------------


def test_accepting_is_what_makes_a_fact_verified(migrated, proposed, profile_id):
    accepted = accept_profile_fact(migrated, profile_id, proposed.id)

    assert accepted.status is FactStatus.ACCEPTED
    assert accepted.is_verified is True
    assert accepted.decided_at is not None
    assert accepted.value == proposed.value
    assert [fact.id for fact in list_verified_profile_facts(migrated, profile_id)] == [
        proposed.id
    ]


def test_a_proposal_never_reaches_the_verified_reading(migrated, proposed, profile_id):
    assert list_verified_profile_facts(migrated, profile_id) == ()
    assert list_profile_facts(migrated, profile_id)[0].status is FactStatus.PROPOSED


def test_a_rejection_never_reaches_the_verified_reading(migrated, proposed, profile_id):
    rejected = reject_profile_fact(migrated, profile_id, proposed.id)

    assert rejected.status is FactStatus.REJECTED
    assert rejected.is_verified is False
    assert list_verified_profile_facts(migrated, profile_id) == ()
    assert len(list_profile_facts(migrated, profile_id)) == 1


def test_a_corrected_fact_never_reaches_the_verified_reading(
    migrated, proposed, profile_id
):
    correction = correct_profile_fact(
        migrated, profile_id, proposed.id, value=TEST_ONLY_CORRECTION
    )

    verified = list_verified_profile_facts(migrated, profile_id)

    assert [fact.id for fact in verified] == [correction.replacement.id]
    assert correction.corrected.id not in {fact.id for fact in verified}


def test_the_verified_reading_shows_only_accepted_facts(migrated, profile_id):
    """One profile carrying all four statuses at once."""
    kept = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=TEST_ONLY_VALUE,
        provenance=_cv_provenance(),
    )
    accept_profile_fact(migrated, profile_id, kept.id)
    pending = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.PHONE,
        value="TEST ONLY phone",
        provenance=_cv_provenance(rule_id="PHONE_LABELLED_LINE", page_numbers=(1,)),
    )
    refused = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.MOBILITY,
        value="TEST ONLY mobility",
        provenance=ProvenanceInput(source_type=FactSourceType.USER_INPUT),
    )
    reject_profile_fact(migrated, profile_id, refused.id)
    superseded = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.NAME,
        value="TEST ONLY name",
        provenance=ProvenanceInput(source_type=FactSourceType.USER_INPUT),
    )
    correction = correct_profile_fact(
        migrated, profile_id, superseded.id, value="TEST ONLY corrected name"
    )

    verified = list_verified_profile_facts(migrated, profile_id)

    assert {fact.id for fact in verified} == {kept.id, correction.replacement.id}
    assert {fact.status for fact in verified} == {FactStatus.ACCEPTED}
    assert all(fact.is_verified for fact in verified)
    assert {pending.id, refused.id, superseded.id}.isdisjoint(
        {fact.id for fact in verified}
    )
    assert len(list_profile_facts(migrated, profile_id)) == 5


def test_the_verified_reading_is_scoped_to_one_profile(
    migrated, proposed, profile_id, other_profile_id
):
    accept_profile_fact(migrated, profile_id, proposed.id)
    theirs = propose_profile_fact(
        migrated,
        profile_id=other_profile_id,
        fact_type=ProfileFactType.EMAIL,
        value="another@example.invalid",
        provenance=ProvenanceInput(source_type=FactSourceType.USER_INPUT),
    )
    accept_profile_fact(migrated, other_profile_id, theirs.id)

    assert [fact.id for fact in list_verified_profile_facts(migrated, profile_id)] == [
        proposed.id
    ]
    assert [
        fact.id for fact in list_verified_profile_facts(migrated, other_profile_id)
    ] == [theirs.id]


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


def test_accepting_an_accepted_fact_changes_nothing(migrated, proposed, profile_id):
    first = accept_profile_fact(migrated, profile_id, proposed.id)

    again = accept_profile_fact(migrated, profile_id, proposed.id)

    assert again == first
    assert again.decided_at == first.decided_at
    assert len(list_profile_facts(migrated, profile_id)) == 1


def test_rejecting_a_rejected_fact_changes_nothing(migrated, proposed, profile_id):
    first = reject_profile_fact(migrated, profile_id, proposed.id)

    again = reject_profile_fact(migrated, profile_id, proposed.id)

    assert again == first
    assert again.decided_at == first.decided_at
    assert len(list_profile_facts(migrated, profile_id)) == 1


def test_an_accepted_fact_can_still_be_rejected_later(migrated, proposed, profile_id):
    accept_profile_fact(migrated, profile_id, proposed.id)

    rejected = reject_profile_fact(migrated, profile_id, proposed.id)

    assert rejected.status is FactStatus.REJECTED
    assert list_verified_profile_facts(migrated, profile_id) == ()


def test_a_rejected_fact_cannot_be_accepted(migrated, proposed, profile_id):
    reject_profile_fact(migrated, profile_id, proposed.id)

    with pytest.raises(InvalidFactTransitionError):
        accept_profile_fact(migrated, profile_id, proposed.id)

    assert get_profile_fact(migrated, profile_id, proposed.id).status is (
        FactStatus.REJECTED
    )


def test_a_rejected_fact_cannot_be_corrected(migrated, proposed, profile_id):
    reject_profile_fact(migrated, profile_id, proposed.id)

    with pytest.raises(InvalidFactTransitionError):
        correct_profile_fact(
            migrated, profile_id, proposed.id, value=TEST_ONLY_CORRECTION
        )

    assert len(list_profile_facts(migrated, profile_id)) == 1


def test_a_corrected_fact_is_terminal(migrated, proposed, profile_id):
    correction = correct_profile_fact(
        migrated, profile_id, proposed.id, value=TEST_ONLY_CORRECTION
    )

    for decide in (accept_profile_fact, reject_profile_fact):
        with pytest.raises(InvalidFactTransitionError):
            decide(migrated, profile_id, proposed.id)
    with pytest.raises(InvalidFactTransitionError):
        correct_profile_fact(
            migrated, profile_id, proposed.id, value="TEST ONLY second attempt"
        )

    still = get_profile_fact(migrated, profile_id, proposed.id)
    assert still.status is FactStatus.CORRECTED
    assert still.replaced_by_fact_id == correction.replacement.id


@pytest.mark.parametrize(
    "decide", [accept_profile_fact, reject_profile_fact], ids=["accept", "reject"]
)
def test_a_fact_of_another_profile_cannot_be_decided(
    migrated, proposed, profile_id, other_profile_id, decide
):
    with pytest.raises(ProfileFactNotFoundError):
        decide(migrated, other_profile_id, proposed.id)

    assert get_profile_fact(migrated, profile_id, proposed.id) == proposed


def test_a_fact_of_another_profile_cannot_be_corrected(
    migrated, proposed, profile_id, other_profile_id
):
    with pytest.raises(ProfileFactNotFoundError):
        correct_profile_fact(
            migrated, other_profile_id, proposed.id, value=TEST_ONLY_CORRECTION
        )

    assert get_profile_fact(migrated, profile_id, proposed.id) == proposed
    assert _counts(migrated) == (1, 1)


def test_a_fact_of_another_profile_cannot_receive_evidence(
    migrated, proposed, profile_id, other_profile_id
):
    with pytest.raises(ProfileFactNotFoundError):
        add_profile_fact_provenance(
            migrated,
            profile_id=other_profile_id,
            fact_id=proposed.id,
            provenance=ProvenanceInput(source_type=FactSourceType.GITHUB),
        )

    assert len(list_profile_fact_provenance(migrated, profile_id, proposed.id)) == 1


def test_deciding_a_fact_that_does_not_exist_is_refused(migrated, profile_id):
    for decide in (accept_profile_fact, reject_profile_fact):
        with pytest.raises(ProfileFactNotFoundError):
            decide(migrated, profile_id, 4242)


# --------------------------------------------------------------------------
# Correction never overwrites
# --------------------------------------------------------------------------


def test_a_correction_creates_a_new_accepted_fact_and_keeps_the_old_value(
    migrated, proposed, profile_id
):
    correction = correct_profile_fact(
        migrated,
        profile_id,
        proposed.id,
        value=TEST_ONLY_CORRECTION,
        normalized_value=TEST_ONLY_CORRECTION,
    )

    assert correction.replacement.id != proposed.id
    assert correction.replacement.status is FactStatus.ACCEPTED
    assert correction.replacement.is_verified is True
    assert correction.replacement.value == TEST_ONLY_CORRECTION
    assert correction.replacement.normalized_value == TEST_ONLY_CORRECTION
    assert correction.replacement.fact_type == proposed.fact_type
    assert correction.replacement.decided_at is not None
    assert correction.replacement.replaced_by_fact_id is None

    assert correction.corrected.id == proposed.id
    assert correction.corrected.status is FactStatus.CORRECTED
    assert correction.corrected.is_verified is False
    # The whole point: the previous reading is still readable.
    assert correction.corrected.value == TEST_ONLY_VALUE
    assert correction.corrected.normalized_value == proposed.normalized_value
    assert correction.corrected.replaced_by_fact_id == correction.replacement.id
    assert correction.corrected.decided_at is not None

    stored = get_profile_fact(migrated, profile_id, proposed.id)
    assert stored == correction.corrected


def test_a_correction_carries_the_persons_own_evidence(
    migrated, proposed, profile_id
):
    correction = correct_profile_fact(
        migrated, profile_id, proposed.id, value=TEST_ONLY_CORRECTION
    )

    evidence = list_profile_fact_provenance(
        migrated, profile_id, correction.replacement.id
    )

    assert [item.source_type for item in evidence] == [FactSourceType.USER_INPUT]
    # A correction invents nothing about a document it never read.
    assert evidence[0].cv_sha256 is None
    assert evidence[0].rule_id is None
    assert evidence[0].page_numbers == ()
    # The evidence of the corrected fact is left exactly as it was.
    assert [
        item.source_type
        for item in list_profile_fact_provenance(migrated, profile_id, proposed.id)
    ] == [FactSourceType.CV]


def test_a_correction_refuses_evidence_that_is_not_the_persons_own(
    migrated, proposed, profile_id
):
    with pytest.raises(ProfileFactError):
        correct_profile_fact(
            migrated,
            profile_id,
            proposed.id,
            value=TEST_ONLY_CORRECTION,
            provenance=_cv_provenance(),
        )

    assert get_profile_fact(migrated, profile_id, proposed.id) == proposed
    assert _counts(migrated) == (1, 1)


def test_a_correction_refuses_an_empty_value(migrated, proposed, profile_id):
    with pytest.raises(ProfileFactError):
        correct_profile_fact(migrated, profile_id, proposed.id, value="   ")

    assert get_profile_fact(migrated, profile_id, proposed.id) == proposed
    assert _counts(migrated) == (1, 1)


def test_a_correction_that_fails_midway_leaves_the_old_fact_untouched(
    migrated, proposed, profile_id
):
    def explode() -> None:
        raise RuntimeError("TEST ONLY failure before the old fact is marked")

    with pytest.raises(RuntimeError):
        correct_profile_fact(
            migrated,
            profile_id,
            proposed.id,
            value=TEST_ONLY_CORRECTION,
            after_replacement=explode,
        )

    assert get_profile_fact(migrated, profile_id, proposed.id) == proposed
    assert _counts(migrated) == (1, 1)
    assert list_profile_facts(migrated, profile_id) == (proposed,)
    assert list_verified_profile_facts(migrated, profile_id) == ()


def test_successive_corrections_keep_every_value_that_was_ever_proposed(
    migrated, proposed, profile_id
):
    first = correct_profile_fact(
        migrated, profile_id, proposed.id, value="first@example.invalid"
    )
    second = correct_profile_fact(
        migrated, profile_id, first.replacement.id, value="second@example.invalid"
    )
    third = correct_profile_fact(
        migrated, profile_id, second.replacement.id, value="third@example.invalid"
    )

    chain = list_profile_facts(migrated, profile_id)

    assert [fact.value for fact in chain] == [
        TEST_ONLY_VALUE,
        "first@example.invalid",
        "second@example.invalid",
        "third@example.invalid",
    ]
    assert [fact.status for fact in chain] == [
        FactStatus.CORRECTED,
        FactStatus.CORRECTED,
        FactStatus.CORRECTED,
        FactStatus.ACCEPTED,
    ]
    assert [fact.replaced_by_fact_id for fact in chain] == [
        first.replacement.id,
        second.replacement.id,
        third.replacement.id,
        None,
    ]
    verified = list_verified_profile_facts(migrated, profile_id)
    assert [fact.id for fact in verified] == [third.replacement.id]
    assert [fact.value for fact in verified] == ["third@example.invalid"]


def test_a_correction_can_follow_an_acceptance(migrated, proposed, profile_id):
    accepted = accept_profile_fact(migrated, profile_id, proposed.id)

    correction = correct_profile_fact(
        migrated, profile_id, accepted.id, value=TEST_ONLY_CORRECTION
    )

    assert correction.corrected.status is FactStatus.CORRECTED
    assert correction.corrected.value == TEST_ONLY_VALUE
    assert [fact.id for fact in list_verified_profile_facts(migrated, profile_id)] == [
        correction.replacement.id
    ]


def test_two_facts_cannot_claim_the_same_replacement(migrated, profile_id):
    first = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=TEST_ONLY_VALUE,
        provenance=_cv_provenance(),
    )
    second = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value="second@example.invalid",
        provenance=_cv_provenance(page_numbers=(3,)),
    )
    correction = correct_profile_fact(
        migrated, profile_id, first.id, value=TEST_ONLY_CORRECTION
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "UPDATE profile_facts "
            "SET status = 'CORRECTED', decided_at = CURRENT_TIMESTAMP, "
            "    replaced_by_fact_id = ? "
            "WHERE id = ?",
            (correction.replacement.id, second.id),
        )
