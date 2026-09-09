"""Phase 7B.1 type targeting, on disposable SQLite databases.

Every database here is created under `tmp_path`, migrated normally, and thrown
away. Every posting and every profile is invented; no real listing, company or
person takes part. **The operational database is never opened**, no count taken
from it appears anywhere below, and nothing in this file writes to any database
a person uses: the fixtures decide what exists.

The verdict is derived data that is deliberately *not* projected, so most of
these tests are about what this slice may **not** do: add a table, add a
column, store a verdict, rewrite the preferences a person stated, rewrite the
types Phase 3.5A extracted, widen a declared target set, or turn a posting
nobody could read into a posting of another kind.

The constraint rows are written directly here rather than extracted from
invented prose. That is the point: this slice is a comparison over the stored
type, and pinning each type explicitly is what lets the six shapes that matter
— a targeted type, the other targeted type, two untargeted ones, `NULL`, and no
constraints row at all — be asserted without depending on how the Phase 3.5A
rules happen to read a sentence today.
"""

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
from services.digital_twin.preferences.models import (
    OpportunityPreferences,
    OpportunityType,
    WorkMode,
)
from services.digital_twin.preferences.repository import (
    get_profile_preferences,
    synchronize_profile_preferences,
)
from services.digital_twin.preferences.service import set_profile_preferences
from services.digital_twin.repository import ensure_user_profile
from services.targeting.opportunity_type.evaluator import evaluate_type_target
from services.targeting.opportunity_type.models import (
    TARGETING_VERSION,
    TargetVerdict,
)
from services.targeting.opportunity_type.profile_target import (
    DECLARED_TYPES_RULE,
    PREFERENCES_ABSENT_RULE,
    resolve_profile_type_target,
)
from services.targeting.opportunity_type.service import (
    TypeTargetingServiceError,
    audit_opportunity_type_targeting,
    explain_opportunity_type_target,
    load_opportunity_type,
    load_opportunity_types,
    type_distribution,
)

PACKAGE = Path("services/targeting/opportunity_type")
MIGRATIONS = Path("migrations")

#: The last migration that existed when this slice was written. Phase 7B.1 adds
#: none of its own: the verdict depends on a profile, so there is nothing to
#: store. Later slices add their own migrations after it, so what is asserted
#: below is that none of them belongs to *this* subject — not that the
#: repository stopped here.
LAST_MIGRATION_BEFORE_THIS_SLICE = "0025"

#: Tables nobody may create for a derived verdict or a derived preference.
FORBIDDEN_TABLES = (
    "target_preferences",
    "search_preferences",
    "pfe_preferences",
    "stage_preferences",
    "opportunity_type_targets",
    "opportunity_type_verdicts",
    "profile_opportunity_type_targets",
)

#: Columns nobody may add for the same reason.
FORBIDDEN_COLUMNS = (
    "type_target_verdict",
    "pfe_match",
    "internship_match",
    "is_target_type",
    "target_type",
)

# TEST ONLY values, invented for these tests.
TEST_ONLY_EMAIL = "type-targeting-tests@example.invalid"
TEST_ONLY_TARGET = (OpportunityType.PFE, OpportunityType.INTERNSHIP)


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "type-targeting.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def insert_opportunity(connection, label: str, is_active=1, status="new") -> int:
    row = connection.execute(
        """INSERT INTO opportunities (
               canonical_title, organization, location, country, description,
               discovered_at, first_seen_at, last_seen_at, source_url, status,
               is_active
           ) VALUES (?, 'TEST ONLY Org', 'TEST ONLY City', 'TEST ONLY Country',
                     'TEST ONLY description.', 't', 't', 't', ?, ?, ?)
           RETURNING id""",
        (
            f"TEST ONLY posting {label}",
            f"https://example.invalid/{label}-{status}",
            status,
            is_active,
        ),
    ).fetchone()
    connection.commit()
    return int(row[0])


def store_type(connection, opportunity_id: int, value: str | None) -> None:
    """Pin one posting's structured type, exactly as Phase 3.5A would store it."""
    connection.execute(
        """INSERT INTO opportunity_constraints (
               opportunity_id, opportunity_type, extractor_version,
               source_fingerprint, extracted_at
           ) VALUES (?, ?, 'test-only-extractor', ?, 't')""",
        (opportunity_id, value, f"{opportunity_id:064d}"),
    )
    connection.commit()


@pytest.fixture
def corpus(migrated) -> dict[str, int]:
    """The six shapes that matter, and nothing that depends on prose."""
    ids = {
        "pfe": insert_opportunity(migrated, "pfe"),
        "internship": insert_opportunity(migrated, "internship"),
        "junior": insert_opportunity(migrated, "junior"),
        "alternance": insert_opportunity(migrated, "alternance"),
        "first_job": insert_opportunity(migrated, "first-job"),
        "null_type": insert_opportunity(migrated, "null-type"),
        "unread": insert_opportunity(migrated, "unread"),
    }
    store_type(migrated, ids["pfe"], "PFE")
    store_type(migrated, ids["internship"], "INTERNSHIP")
    store_type(migrated, ids["junior"], "JUNIOR_ROLE")
    store_type(migrated, ids["alternance"], "ALTERNANCE")
    store_type(migrated, ids["first_job"], "FIRST_JOB")
    store_type(migrated, ids["null_type"], None)
    # `unread` deliberately gets no constraints row at all.
    return ids


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


def declare_types(connection, profile_id: int, *types: OpportunityType) -> int:
    """Record the kinds this person says they want, and project them."""
    set_profile_preferences(
        connection,
        profile_id,
        OpportunityPreferences(
            opportunity_types=types, work_modes=(WorkMode.ON_SITE,)
        ),
    )
    synchronize_profile_preferences(connection, profile_id)
    return profile_id


@pytest.fixture
def targeting_profile(migrated, profile_id) -> int:
    """A profile that said it is looking for a PFE or an internship."""
    return declare_types(migrated, profile_id, *TEST_ONLY_TARGET)


def _tables(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _preference_rows(connection) -> list[tuple]:
    return connection.execute(
        "SELECT profile_id, fact_id, opportunity_types_json, work_modes_json, "
        "preferred_domains_json, convention_status, visa_sponsorship_required, "
        "constraints_json, input_version, created_at FROM profile_preferences "
        "ORDER BY profile_id"
    ).fetchall()


def _constraint_rows(connection) -> list[tuple]:
    return connection.execute(
        "SELECT opportunity_id, opportunity_type, extractor_version, "
        "source_fingerprint, extracted_at, created_at FROM opportunity_constraints "
        "ORDER BY opportunity_id"
    ).fetchall()


def verdict_for(connection, profile_id: int, opportunity_id: int) -> TargetVerdict:
    _, assessment = explain_opportunity_type_target(
        connection, profile_id, opportunity_id
    )
    return assessment.verdict


# --------------------------------------------------------------------------
# The verdicts, over a normally migrated database
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["pfe", "internship"])
def test_a_targeted_type_matches(migrated, corpus, targeting_profile, key) -> None:
    assert verdict_for(migrated, targeting_profile, corpus[key]) is TargetVerdict.MATCH


@pytest.mark.parametrize("key", ["junior", "alternance", "first_job"])
def test_an_untargeted_known_type_is_out_of_target(
    migrated, corpus, targeting_profile, key
) -> None:
    """Nothing is implicitly accepted, however early-career it looks."""
    assert (
        verdict_for(migrated, targeting_profile, corpus[key])
        is TargetVerdict.OUT_OF_TARGET
    )


def test_a_null_type_is_unknown_not_out_of_target(
    migrated, corpus, targeting_profile
) -> None:
    reading, assessment = explain_opportunity_type_target(
        migrated, targeting_profile, corpus["null_type"]
    )
    assert reading.constraints_read is True
    assert reading.opportunity_type is None
    assert assessment.verdict is TargetVerdict.UNKNOWN


def test_a_posting_never_read_is_unknown_and_still_counted(
    migrated, corpus, targeting_profile
) -> None:
    """No constraints row is a different fact from a NULL type, and neither
    is a posting of another kind. Both are UNKNOWN; both stay in the total."""
    reading, assessment = explain_opportunity_type_target(
        migrated, targeting_profile, corpus["unread"]
    )
    assert reading.constraints_read is False
    assert reading.opportunity_type is None
    assert assessment.verdict is TargetVerdict.UNKNOWN

    audit = audit_opportunity_type_targeting(migrated, targeting_profile)
    assert audit.total_opportunities == len(corpus)


def test_a_profile_with_no_preferences_targets_nothing_known(
    migrated, corpus, profile_id
) -> None:
    """Absent preferences are UNKNOWN everywhere, never OUT_OF_TARGET."""
    target = resolve_profile_type_target(migrated, profile_id)
    assert target.known is False
    assert target.rule_id == PREFERENCES_ABSENT_RULE

    audit = audit_opportunity_type_targeting(migrated, profile_id)
    assert audit.profile_target_known is False
    assert audit.target_type_count == 0
    assert audit.unknown == audit.total_opportunities
    assert audit.match == 0
    assert audit.out_of_target == 0


def test_the_declared_target_is_read_from_the_stated_preference(
    migrated, targeting_profile
) -> None:
    target = resolve_profile_type_target(migrated, targeting_profile)
    assert target.rule_id == DECLARED_TYPES_RULE
    assert target.opportunity_types == TEST_ONLY_TARGET


def test_a_differently_declared_target_gives_different_verdicts(
    migrated, corpus, profile_id
) -> None:
    """The profile is the source of truth, so editing it moves the verdicts."""
    declare_types(migrated, profile_id, OpportunityType.JUNIOR_ROLE)
    assert verdict_for(migrated, profile_id, corpus["junior"]) is TargetVerdict.MATCH
    assert (
        verdict_for(migrated, profile_id, corpus["pfe"]) is TargetVerdict.OUT_OF_TARGET
    )


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------


def test_the_audit_partitions_the_corpus_and_reports_the_distribution(
    migrated, corpus, targeting_profile
) -> None:
    audit = audit_opportunity_type_targeting(migrated, targeting_profile)
    assert audit.total_opportunities == 7
    assert audit.constraints_read == 6
    assert audit.opportunity_type_known == 5
    assert audit.opportunity_type_unknown == 2
    assert audit.match == 2
    assert audit.out_of_target == 3
    assert audit.unknown == 2
    assert audit.match + audit.out_of_target + audit.unknown == 7
    assert audit.target_type_count == 2
    assert audit.targeting_version == TARGETING_VERSION
    assert audit.distribution["PFE"] == 1
    assert audit.distribution["INTERNSHIP"] == 1
    assert audit.distribution["JUNIOR_ROLE"] == 1
    assert audit.distribution["ALTERNANCE"] == 1
    assert audit.distribution["FIRST_JOB"] == 1
    assert audit.distribution["UNKNOWN"] == 2
    assert audit.distribution["PFA"] == 0


def test_the_distribution_names_every_registry_value(
    migrated, corpus, targeting_profile
) -> None:
    distribution = type_distribution(load_opportunity_types(migrated))
    assert set(distribution) == {member.value for member in OpportunityType} | {
        "UNKNOWN"
    }
    assert sum(distribution.values()) == len(corpus)


def test_the_audit_excludes_what_every_other_pipeline_excludes(
    migrated, corpus, targeting_profile
) -> None:
    """One scope: active, not a merged duplicate."""
    insert_opportunity(migrated, "inactive", is_active=0)
    insert_opportunity(migrated, "merged", status="merged_duplicate")
    audit = audit_opportunity_type_targeting(migrated, targeting_profile)
    assert audit.total_opportunities == len(corpus)
    assert load_opportunity_type(migrated, corpus["pfe"]) is not None


def test_the_audit_is_bounded_by_limit(migrated, corpus, targeting_profile) -> None:
    audit = audit_opportunity_type_targeting(migrated, targeting_profile, limit=2)
    assert audit.total_opportunities == 2


def test_the_audit_refuses_a_database_without_phase_3_5(tmp_path) -> None:
    connection = connect_database(tmp_path / "bare.db")
    try:
        with pytest.raises(TypeTargetingServiceError):
            audit_opportunity_type_targeting(connection, 1)
    finally:
        connection.close()


# --------------------------------------------------------------------------
# What this slice may not do: write
# --------------------------------------------------------------------------


def test_auditing_leaves_the_stated_preferences_byte_for_byte(
    migrated, corpus, targeting_profile
) -> None:
    before = _preference_rows(migrated)
    stored_before = get_profile_preferences(migrated, targeting_profile)
    audit_opportunity_type_targeting(migrated, targeting_profile)
    explain_opportunity_type_target(migrated, targeting_profile, corpus["pfe"])
    assert _preference_rows(migrated) == before
    assert get_profile_preferences(migrated, targeting_profile) == stored_before


def test_auditing_leaves_the_extracted_constraints_untouched(
    migrated, corpus, targeting_profile
) -> None:
    before = _constraint_rows(migrated)
    audit_opportunity_type_targeting(migrated, targeting_profile)
    explain_opportunity_type_target(migrated, targeting_profile, corpus["junior"])
    assert _constraint_rows(migrated) == before


def test_auditing_creates_no_table_and_no_column(
    migrated, corpus, targeting_profile
) -> None:
    tables_before = _tables(migrated)
    columns_before = {table: _columns(migrated, table) for table in tables_before}
    audit_opportunity_type_targeting(migrated, targeting_profile)
    explain_opportunity_type_target(migrated, targeting_profile, corpus["pfe"])
    assert _tables(migrated) == tables_before
    assert {
        table: _columns(migrated, table) for table in _tables(migrated)
    } == columns_before


def test_the_audit_runs_against_a_read_only_connection(
    tmp_path, corpus, migrated, targeting_profile
) -> None:
    """The strongest form of "it writes nothing": SQLite refuses if it tries."""
    path = Path(migrated.execute("PRAGMA database_list").fetchone()[2])
    read_only = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        audit = audit_opportunity_type_targeting(read_only, targeting_profile)
        assert audit.match == 2
        explain_opportunity_type_target(read_only, targeting_profile, corpus["pfe"])
    finally:
        read_only.close()


def test_the_package_contains_no_write_statement() -> None:
    for source in sorted(PACKAGE.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE", "commit("):
            assert forbidden not in text, f"{source}: {forbidden}"


# --------------------------------------------------------------------------
# What this slice may not add: schema
# --------------------------------------------------------------------------


def test_this_slice_adds_no_migration() -> None:
    """The verdict depends on a profile, so there is nothing to store."""
    names = sorted(
        migration.path.name
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY)
    )
    assert LAST_MIGRATION_BEFORE_THIS_SLICE in {name[:4] for name in names}
    # Whatever later slices added, none of it stores a targeting verdict or a
    # targeting preference.
    assert not [
        name
        for name in names
        if name[:4] > LAST_MIGRATION_BEFORE_THIS_SLICE
        and ("target" in name or "opportunity_type" in name)
    ]


def test_no_targeting_table_exists(migrated) -> None:
    tables = _tables(migrated)
    for forbidden in FORBIDDEN_TABLES:
        assert forbidden not in tables, forbidden


def test_no_verdict_column_exists_anywhere(migrated) -> None:
    for table in _tables(migrated):
        columns = set(_columns(migrated, table))
        for forbidden in FORBIDDEN_COLUMNS:
            assert forbidden not in columns, f"{table}.{forbidden}"


def test_no_migration_mentions_a_type_verdict() -> None:
    """Not even in a comment, so nobody reserves a column for one later."""
    for path in sorted(MIGRATIONS.glob("*.sql")):
        executable = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        for forbidden in FORBIDDEN_TABLES + FORBIDDEN_COLUMNS:
            assert forbidden not in executable, f"{path.name}: {forbidden}"


# --------------------------------------------------------------------------
# What this slice may not touch: the neighbouring closed slices
# --------------------------------------------------------------------------


def test_the_package_reads_the_one_shared_type_registry() -> None:
    """No second normalized type, and no third taxonomy."""
    for source in sorted(PACKAGE.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        assert re.search(r"class OpportunityType\s*[(:]", text) is None
        assert "STAGE" not in text


def test_the_package_does_not_read_the_raw_opportunity_type_column() -> None:
    """`opportunities.opportunity_type` is not the truth of this slice."""
    for source in sorted(PACKAGE.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        assert "o.opportunity_type" not in text
        assert "opportunities.opportunity_type" not in text


def test_the_package_imports_no_neighbouring_slice() -> None:
    """Geography, eligibility, matching, qualification and freshness are all
    out of scope, and the import list is where that is enforced: this package
    reads the shared registry, the preferences repository and the collector's
    own configuration, and nothing else of the domain."""
    for source in sorted(PACKAGE.glob("*.py")):
        imported = re.findall(
            r"^\s*(?:from|import)\s+([\w.]+)",
            source.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
        for module in imported:
            for forbidden in (
                "services.geography",
                "services.eligibility",
                "services.priority",
                "services.portfolio",
                "services.collector.qualification",
                "services.collector.matching",
            ):
                assert not module.startswith(forbidden), f"{source}: {module}"


def test_the_package_has_no_special_case_for_any_posting() -> None:
    """An anomaly is made visible by the audit, never patched around."""
    for source in sorted(PACKAGE.glob("*.py")):
        body = "\n".join(
            segment
            for index, segment in enumerate(
                source.read_text(encoding="utf-8").split('"""')
            )
            if index % 2 == 0
        )
        assert re.search(r"opportunity_id\s*==", body) is None, source


def test_an_anomalous_stored_type_is_reported_and_not_corrected(
    migrated, targeting_profile
) -> None:
    """A posting stored as PFE under a title that reads nothing like one.

    This is the shape of the reading the operational audit turned up. This
    slice must answer MATCH — the stored type *is* targeted — and leave the row
    exactly as it found it. Deciding that the extractor was wrong is a
    different slice's job, and doing it silently here would hide the defect
    behind a verdict that looked right.
    """
    anomalous = insert_opportunity(migrated, "consulting-manager")
    migrated.execute(
        "UPDATE opportunities SET canonical_title = ? WHERE id = ?",
        ("TEST ONLY Consulting Manager - Strategy & Transformation", anomalous),
    )
    migrated.commit()
    store_type(migrated, anomalous, "PFE")
    before = _constraint_rows(migrated)

    reading, assessment = explain_opportunity_type_target(
        migrated, targeting_profile, anomalous
    )
    assert reading.opportunity_type is OpportunityType.PFE
    assert assessment.verdict is TargetVerdict.MATCH
    assert _constraint_rows(migrated) == before

    audit = audit_opportunity_type_targeting(migrated, targeting_profile)
    assert audit.distribution["PFE"] == 1


# --------------------------------------------------------------------------
# The evaluator, from the database side
# --------------------------------------------------------------------------


def test_the_evaluator_needs_no_database_to_agree_with_one(
    migrated, corpus, targeting_profile
) -> None:
    """The same answer, from stored values and from values alone."""
    target = resolve_profile_type_target(migrated, targeting_profile)
    for reading in load_opportunity_types(migrated):
        _, stored = explain_opportunity_type_target(
            migrated, targeting_profile, reading.opportunity_id, target=target
        )
        assert stored == evaluate_type_target(
            target.opportunity_types, reading.opportunity_type
        )
