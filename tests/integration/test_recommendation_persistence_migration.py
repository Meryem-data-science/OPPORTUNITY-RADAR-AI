"""Migration 0026, on disposable SQLite databases these tests build themselves.

Every database here is created under `tmp_path` and thrown away. No real posting
and no real person takes part: the profile is a `.invalid` address, the postings
are invented, and **the operational database is never opened**.

The subject is what the schema itself refuses. Phase 9B.1's persistence layer
validates a great deal in Python, but a stored recommendation is an audit record
and the database has to state its own invariants: the ranking is 1-based and
unique inside a run, the score/coverage contract of Phase 9A holds wherever the
row came from, a recommendation cannot cite another profile's matching run, and
"current" is either a run this profile owns or nothing at all.
"""

from pathlib import Path
import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
)
from services.digital_twin.repository import ensure_user_profile

MIGRATION = "0026"
TABLES = (
    "recommendation_runs",
    "recommendation_assessments",
    "recommendation_profile_state",
)
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "recommendation.migration@example.invalid"
TEST_ONLY_OTHER_EMAIL = "recommendation.migration.other@example.invalid"


def migrations_below(tmp_path: Path, version: str) -> Path:
    directory = tmp_path / f"migrations-below-{version}"
    directory.mkdir()
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version < version:
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return directory


def tables_of(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def add_opportunity(connection: sqlite3.Connection, suffix: object) -> int:
    return int(
        connection.execute(
            """INSERT INTO opportunities
                 (canonical_title,organization,description,discovered_at,
                  first_seen_at,last_seen_at,source_url,status)
               VALUES ('TEST','TEST','TEST','t','t','t',?,'new') RETURNING id""",
            (f"https://example.invalid/{suffix}",),
        ).fetchone()[0]
    )


def add_matching_run(
    connection: sqlite3.Connection, profile_id: int, fingerprint: str
) -> int:
    """A minimal `matching_runs` row: this file tests 0026, not 0015."""
    return int(
        connection.execute(
            """INSERT INTO matching_runs
                 (profile_id,persistence_version,selection_version,
                  matching_engine_version,matching_rules_version,
                  semantic_percentile_version,corpus_fingerprint,
                  tfidf_model_fingerprint,batch_fingerprint,run_fingerprint,
                  assessment_count,batch_payload_json)
               VALUES (?,'p-v1','s-v1','e-v1','r-v1','sp-v1',?,?,?,?,1,'{}')
               RETURNING id""",
            (profile_id, DIGEST, DIGEST, DIGEST, fingerprint),
        ).fetchone()[0]
    )


def run_row(**overrides) -> dict[str, object]:
    values = {
        "persistence_version": "recommendation-persistence-v1",
        "input_assembly_version": "recommendation-input-assembly-v1",
        "recommendation_engine_version": "recommendation-engine-v1",
        "recommendation_rules_version": "recommendation-rules-v1",
        "source_matching_run_fingerprint": DIGEST,
        "batch_fingerprint": DIGEST,
        "run_fingerprint": OTHER_DIGEST,
        "assessment_count": 1,
        "batch_payload_json": "{}",
    }
    values.update(overrides)
    return values


def insert_run(connection: sqlite3.Connection, **overrides) -> int:
    values = run_row(**overrides)
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    return int(
        connection.execute(
            f"INSERT INTO recommendation_runs ({columns})"
            f" VALUES ({placeholders}) RETURNING id",
            tuple(values.values()),
        ).fetchone()[0]
    )


def insert_assessment(connection: sqlite3.Connection, **overrides) -> None:
    values = {
        "rank_position": 1,
        "disposition": "RECOMMENDED",
        "recommendation_score": 0.5,
        "evidence_coverage": 1.0,
        "assessment_fingerprint": DIGEST,
        "assessment_payload_json": "{}",
    }
    values.update(overrides)
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO recommendation_assessments ({columns})"
        f" VALUES ({placeholders})",
        tuple(values.values()),
    )


@pytest.fixture
def migrated(tmp_path: Path):
    connection = connect_database(tmp_path / "recommendation-0026.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def seeded(migrated):
    """One profile, one posting and one matching run that profile owns."""
    profile_id = ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id
    opportunity_id = add_opportunity(migrated, "seed")
    matching_run_id = add_matching_run(migrated, profile_id, DIGEST)
    migrated.commit()
    return migrated, profile_id, opportunity_id, matching_run_id


def test_0026_creates_the_three_tables_with_the_approved_columns(migrated):
    assert set(TABLES) <= tables_of(migrated)
    assert MIGRATION in {
        row[0] for row in migrated.execute("SELECT version FROM schema_migrations")
    }

    def columns(table: str) -> list[str]:
        return [row[1] for row in migrated.execute(f"PRAGMA table_info({table})")]

    assert columns("recommendation_runs") == [
        "id",
        "profile_id",
        "source_matching_run_id",
        "persistence_version",
        "input_assembly_version",
        "recommendation_engine_version",
        "recommendation_rules_version",
        "source_matching_run_fingerprint",
        "batch_fingerprint",
        "run_fingerprint",
        "assessment_count",
        "batch_payload_json",
        "created_at",
    ]
    assert columns("recommendation_assessments") == [
        "id",
        "run_id",
        "opportunity_id",
        "rank_position",
        "disposition",
        "recommendation_score",
        "evidence_coverage",
        "assessment_fingerprint",
        "assessment_payload_json",
        "created_at",
    ]
    assert columns("recommendation_profile_state") == [
        "profile_id",
        "state",
        "current_run_id",
        "persistence_version",
        "input_assembly_version",
        "readiness_issues_json",
        "created_at",
        "updated_at",
    ]


def test_a_migrated_database_is_internally_consistent(migrated):
    assert migrated.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_database_below_0026_has_none_of_the_three_tables(tmp_path: Path):
    connection = connect_database(tmp_path / "below-0026.db")
    try:
        applied = apply_migrations(connection, migrations_below(tmp_path, MIGRATION))
        assert MIGRATION not in applied
        assert not set(TABLES) & tables_of(connection)

        # 0026 and everything recorded after it, which today is the 0027 CV
        # replacement staging. The three tables below are 0026's.
        assert apply_migrations(connection) == [MIGRATION, "0027", "0028"]
        assert set(TABLES) <= tables_of(connection)
    finally:
        connection.close()


def test_0026_is_recorded_once_and_seeds_nothing(migrated):
    assert apply_migrations(migrated) == []
    # Recorded once, which is this test's subject. It is deliberately not
    # asserted to be the *last* migration: later phases add their own, and a
    # pin on the tail would fail every time one does without saying anything
    # about 0026.
    recorded = [
        row[0]
        for row in migrated.execute("SELECT version FROM schema_migrations")
    ]
    assert recorded.count(MIGRATION) == 1
    for table in TABLES:
        assert migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_the_indexes_this_phase_reads_through_exist(migrated):
    indexes = {
        row[0]
        for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name LIKE 'recommendation%'"
        )
    }

    assert {
        "idx_recommendation_runs_profile",
        "idx_recommendation_runs_source_matching",
        "idx_recommendation_assessments_opportunity",
    } <= indexes
    # The ranked read of one run is served by `UNIQUE (run_id, rank_position)`,
    # so no second index is created for it.
    ranked = migrated.execute(
        "EXPLAIN QUERY PLAN SELECT opportunity_id FROM recommendation_assessments"
        " WHERE run_id=1 ORDER BY rank_position"
    ).fetchall()
    assert any("USING INDEX sqlite_autoindex" in str(row[3]) for row in ranked)


def test_the_matching_source_reference_is_composite_and_takes_no_action(migrated):
    """Composite, so the pair is required; NO ACTION, so nothing is cascaded.

    NO ACTION rather than RESTRICT is deliberate: both refuse the deletion of a
    cited matching run, but RESTRICT is evaluated the instant the parent row is
    touched, which would make retiring a profile depend on the order SQLite
    happens to run two independent cascades in. The two behaviours that matter
    are asserted directly below this test, on real rows.
    """
    keys = {
        (row[2], row[3], row[4], row[6])
        for row in migrated.execute("PRAGMA foreign_key_list(recommendation_runs)")
    }

    assert ("matching_runs", "source_matching_run_id", "id", "NO ACTION") in keys
    assert ("matching_runs", "profile_id", "profile_id", "NO ACTION") in keys
    assert ("profiles", "profile_id", "id", "CASCADE") in keys


def test_a_recommendation_cannot_cite_another_profiles_matching_run(seeded):
    connection, profile_id, _, _ = seeded
    other_profile = ensure_user_profile(connection, TEST_ONLY_OTHER_EMAIL).profile_id
    foreign_run = add_matching_run(connection, other_profile, OTHER_DIGEST)
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        insert_run(
            connection, profile_id=profile_id, source_matching_run_id=foreign_run
        )


def test_a_cited_matching_run_cannot_be_deleted_out_from_under_history(seeded):
    connection, profile_id, _, matching_run_id = seeded
    insert_run(
        connection, profile_id=profile_id, source_matching_run_id=matching_run_id
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM matching_runs WHERE id=?", (matching_run_id,))
    assert (
        connection.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0]
        == 1
    )


def test_retiring_the_profile_still_removes_every_row_it_owns(seeded):
    connection, profile_id, opportunity_id, matching_run_id = seeded
    run_id = insert_run(
        connection, profile_id=profile_id, source_matching_run_id=matching_run_id
    )
    insert_assessment(connection, run_id=run_id, opportunity_id=opportunity_id)
    connection.execute(
        """INSERT INTO recommendation_profile_state
             (profile_id,state,current_run_id,persistence_version,
              input_assembly_version,readiness_issues_json)
           VALUES (?,'READY',?,'p-v1','a-v1','[]')""",
        (profile_id, run_id),
    )
    connection.commit()

    connection.execute("DELETE FROM profiles WHERE id=?", (profile_id,))

    for table in TABLES:
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    # The posting itself is nobody's to delete on a profile's way out.
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("empty persistence version", {"persistence_version": ""}),
        ("untrimmed persistence version", {"persistence_version": " v1 "}),
        ("empty assembly version", {"input_assembly_version": ""}),
        ("empty engine version", {"recommendation_engine_version": ""}),
        ("empty rules version", {"recommendation_rules_version": ""}),
        ("short source fingerprint", {"source_matching_run_fingerprint": "a" * 63}),
        ("non hex source fingerprint", {"source_matching_run_fingerprint": "A" * 64}),
        ("non hex batch fingerprint", {"batch_fingerprint": "z" * 64}),
        ("non hex run fingerprint", {"run_fingerprint": "g" * 64}),
        ("zero assessment count", {"assessment_count": 0}),
        ("empty batch payload", {"batch_payload_json": ""}),
        ("untrimmed batch payload", {"batch_payload_json": " {} "}),
    ],
)
def test_the_run_table_refuses_a_malformed_row(seeded, label, overrides):
    connection, profile_id, _, matching_run_id = seeded

    with pytest.raises(sqlite3.IntegrityError):
        insert_run(
            connection,
            profile_id=profile_id,
            source_matching_run_id=matching_run_id,
            **overrides,
        )


def test_the_run_fingerprint_is_unique_across_the_whole_table(seeded):
    connection, profile_id, _, matching_run_id = seeded
    insert_run(
        connection, profile_id=profile_id, source_matching_run_id=matching_run_id
    )

    with pytest.raises(sqlite3.IntegrityError):
        insert_run(
            connection, profile_id=profile_id, source_matching_run_id=matching_run_id
        )


@pytest.fixture
def run_with_posting(seeded):
    connection, profile_id, opportunity_id, matching_run_id = seeded
    run_id = insert_run(
        connection, profile_id=profile_id, source_matching_run_id=matching_run_id
    )
    connection.commit()
    return connection, run_id, opportunity_id


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("rank position zero", {"rank_position": 0}),
        ("negative rank position", {"rank_position": -1}),
        ("unknown disposition", {"disposition": "MAYBE"}),
        ("lowercase disposition", {"disposition": "recommended"}),
        ("score above one", {"recommendation_score": 1.5}),
        ("score below zero", {"recommendation_score": -0.1}),
        ("coverage above one", {"evidence_coverage": 1.5}),
        ("coverage below zero", {"evidence_coverage": -0.1}),
        ("null coverage", {"evidence_coverage": None}),
        (
            "no coverage but a score",
            {"evidence_coverage": 0.0, "recommendation_score": 0.0},
        ),
        (
            "coverage but no score",
            {"evidence_coverage": 0.5, "recommendation_score": None},
        ),
        ("short assessment fingerprint", {"assessment_fingerprint": "a" * 10}),
        ("non hex assessment fingerprint", {"assessment_fingerprint": "A" * 64}),
        ("empty assessment payload", {"assessment_payload_json": ""}),
        ("untrimmed assessment payload", {"assessment_payload_json": " {} "}),
    ],
)
def test_the_assessment_table_refuses_a_malformed_row(
    run_with_posting, label, overrides
):
    connection, run_id, opportunity_id = run_with_posting

    with pytest.raises(sqlite3.IntegrityError):
        insert_assessment(
            connection, run_id=run_id, opportunity_id=opportunity_id, **overrides
        )


def test_no_evidence_stores_no_score_at_all(run_with_posting):
    connection, run_id, opportunity_id = run_with_posting
    insert_assessment(
        connection,
        run_id=run_id,
        opportunity_id=opportunity_id,
        evidence_coverage=0.0,
        recommendation_score=None,
    )

    assert connection.execute(
        "SELECT recommendation_score,evidence_coverage FROM recommendation_assessments"
    ).fetchone() == (None, 0.0)


def test_one_run_ranks_each_posting_once_and_each_place_once(run_with_posting):
    connection, run_id, opportunity_id = run_with_posting
    other = add_opportunity(connection, "second")
    insert_assessment(connection, run_id=run_id, opportunity_id=opportunity_id)

    with pytest.raises(sqlite3.IntegrityError):
        insert_assessment(
            connection, run_id=run_id, opportunity_id=opportunity_id, rank_position=2
        )
    with pytest.raises(sqlite3.IntegrityError):
        insert_assessment(
            connection, run_id=run_id, opportunity_id=other, rank_position=1
        )
    insert_assessment(connection, run_id=run_id, opportunity_id=other, rank_position=2)
    assert connection.execute(
        "SELECT opportunity_id FROM recommendation_assessments"
        " WHERE run_id=? ORDER BY rank_position",
        (run_id,),
    ).fetchall() == [(opportunity_id,), (other,)]


def test_an_assessment_must_name_a_posting_that_exists(run_with_posting):
    connection, run_id, _ = run_with_posting

    with pytest.raises(sqlite3.IntegrityError):
        insert_assessment(connection, run_id=run_id, opportunity_id=987654)


def test_a_ranked_posting_cannot_be_deleted_while_the_run_cites_it(run_with_posting):
    connection, run_id, opportunity_id = run_with_posting
    insert_assessment(connection, run_id=run_id, opportunity_id=opportunity_id)
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM opportunities WHERE id=?", (opportunity_id,))


@pytest.mark.parametrize(
    ("label", "state", "current"),
    [
        ("READY pointing nowhere", "READY", None),
        ("INCOMPLETE pointing somewhere", "INCOMPLETE", "run"),
        ("an invented state", "NOT_SYNCED", None),
    ],
)
def test_the_state_row_refuses_a_contradictory_pair(
    run_with_posting, label, state, current
):
    connection, run_id, _ = run_with_posting
    profile_id = connection.execute(
        "SELECT profile_id FROM recommendation_runs WHERE id=?", (run_id,)
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO recommendation_profile_state
                 (profile_id,state,current_run_id,persistence_version,
                  input_assembly_version,readiness_issues_json)
               VALUES (?,?,?,'p-v1','a-v1','[]')""",
            (profile_id, state, run_id if current == "run" else None),
        )


def test_the_state_row_can_only_point_at_a_run_this_profile_owns(run_with_posting):
    connection, run_id, _ = run_with_posting
    other_profile = ensure_user_profile(connection, TEST_ONLY_OTHER_EMAIL).profile_id

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO recommendation_profile_state
                 (profile_id,state,current_run_id,persistence_version,
                  input_assembly_version,readiness_issues_json)
               VALUES (?,'READY',?,'p-v1','a-v1','[]')""",
            (other_profile, run_id),
        )


def test_the_state_row_requires_versions_and_a_readiness_issue_payload(
    run_with_posting,
):
    connection, run_id, _ = run_with_posting
    profile_id = connection.execute(
        "SELECT profile_id FROM recommendation_runs WHERE id=?", (run_id,)
    ).fetchone()[0]

    for values in (
        (profile_id, run_id, "", "a-v1", "[]"),
        (profile_id, run_id, "p-v1", "", "[]"),
        (profile_id, run_id, "p-v1", "a-v1", ""),
        (profile_id, run_id, "p-v1", "a-v1", " [] "),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO recommendation_profile_state
                     (profile_id,state,current_run_id,persistence_version,
                      input_assembly_version,readiness_issues_json)
                   VALUES (?,'READY',?,?,?,?)""",
                values,
            )


def test_the_state_table_holds_at_most_one_row_per_profile(run_with_posting):
    connection, run_id, _ = run_with_posting
    profile_id = connection.execute(
        "SELECT profile_id FROM recommendation_runs WHERE id=?", (run_id,)
    ).fetchone()[0]
    statement = """INSERT INTO recommendation_profile_state
                     (profile_id,state,current_run_id,persistence_version,
                      input_assembly_version,readiness_issues_json)
                   VALUES (?,'READY',?,'p-v1','a-v1','[]')"""
    connection.execute(statement, (profile_id, run_id))

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement, (profile_id, run_id))
