"""Migration 0024 and the Phase 7A.1 projection, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away. Every posting
and every profile is invented; no real listing, company, address or person
takes part. **The operational database is never opened**, no count taken from
it appears anywhere below, and nothing in this file writes to any database a
person uses: the fixtures decide what exists.

The projection is derived data, so most of these tests are about what it may
**not** do: rewrite the raw locations it reads, touch the mobility a person
typed, keep a reading whose source changed, leave half a reading behind after a
failure, store a verdict, or turn a segment nobody could place into a posting
that is somewhere else.
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
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)
from services.digital_twin.preferences.models import (
    MobilityPreference,
    MobilityScope,
)
from services.digital_twin.preferences.repository import (
    get_profile_mobility,
    synchronize_profile_preferences,
)
from services.digital_twin.preferences.service import set_profile_mobility
from services.digital_twin.repository import ensure_user_profile
from services.geography.evaluator import evaluate_target
from services.geography.models import (
    RESOLVER_VERSION,
    LocationSource,
    ResolutionStatus,
    TargetVerdict,
)
from services.geography.profile_target import (
    MOBILITY_ABSENT_RULE,
    RESTRICTED_COUNTRY_RULE,
    resolve_profile_target,
)
from services.geography.repository import (
    GeographicRepositoryError,
    read_opportunity_resolutions,
    store_location_resolutions,
    stored_signature,
)
from services.geography.resolver import location_fingerprint, resolve_location_text
from services.geography.service import (
    GeographicServiceError,
    audit_geographic_targeting,
    load_location_sources,
    synchronize_location_resolutions,
)

REPOSITORY_SOURCE = Path("services/geography/repository.py")
MIGRATION = Path("migrations/0024_opportunity_location_resolutions.sql")
PROJECTION_TABLE = "opportunity_location_resolutions"

BEFORE_THIS_SLICE = tuple(f"{number:04d}" for number in range(1, 24))

# TEST ONLY values, invented for these tests.
TEST_ONLY_EMAIL = "resolver-tests@example.invalid"
MOROCCAN_LOCATION = "Casablanca, Maroc"
FRENCH_LOCATION = "Paris, France"
MULTI_LOCATION = "Doha, Qatar; London, UK"
UNPLACEABLE_LOCATION = "Any Office"


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "geography.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def insert_opportunity(connection, location=None, country=None, is_active=1,
                       status="new") -> int:
    row = connection.execute(
        """INSERT INTO opportunities (
               canonical_title, organization, location, country, description,
               discovered_at, first_seen_at, last_seen_at, source_url, status,
               is_active
           ) VALUES ('TEST ONLY Data Internship', 'TEST ONLY Org', ?, ?,
                     'TEST ONLY description.', 't', 't', 't', ?, ?, ?)
           RETURNING id""",
        (location, country, f"https://example.invalid/{location}-{status}",
         status, is_active),
    ).fetchone()
    connection.commit()
    return int(row[0])


@pytest.fixture
def corpus(migrated) -> dict[str, int]:
    """Four postings, projected by Phase 3.5A: the four shapes that matter."""
    ids = {
        "moroccan": insert_opportunity(migrated, MOROCCAN_LOCATION),
        "french": insert_opportunity(migrated, FRENCH_LOCATION),
        "multi": insert_opportunity(migrated, MULTI_LOCATION),
        "unplaceable": insert_opportunity(migrated, UNPLACEABLE_LOCATION),
        "silent": insert_opportunity(migrated, None),
    }
    synchronize_opportunity_constraints(migrated)
    return ids


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


@pytest.fixture
def moroccan_profile(migrated, profile_id) -> int:
    """A profile that restricted itself to Morocco, in its own words."""
    set_profile_mobility(
        migrated,
        profile_id,
        MobilityPreference(scope=MobilityScope.RESTRICTED, locations=("Maroc",)),
    )
    synchronize_profile_preferences(migrated, profile_id)
    return profile_id


def source_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def executable_sql(path: Path) -> str:
    """A migration's statements, with its `--` commentary removed."""
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


def _location_rows(connection) -> list[tuple]:
    return connection.execute(
        "SELECT id, opportunity_id, position, location_text, created_at "
        "FROM opportunity_constraint_locations ORDER BY id"
    ).fetchall()


def _mobility_rows(connection) -> list[tuple]:
    return connection.execute(
        "SELECT profile_id, fact_id, mobility_scope, locations_json, input_version "
        "FROM profile_mobility ORDER BY profile_id"
    ).fetchall()


def _resolution_rows(connection) -> list[tuple]:
    return connection.execute(
        f"SELECT opportunity_id, source_location_id, segment_position, raw_segment, "
        f"country_code, city_key, resolution_status, resolution_rule_id, "
        f"resolver_version, source_fingerprint, resolved_at FROM {PROJECTION_TABLE} "
        "ORDER BY source_location_id, segment_position"
    ).fetchall()


def _insert_resolution(connection, **overrides):
    values = {
        "opportunity_id": 1,
        "source_location_id": 1,
        "segment_position": 0,
        "raw_segment": "Casablanca, Maroc",
        "country_code": "MA",
        "city_key": "casablanca",
        "resolution_status": "RESOLVED",
        "resolution_rule_id": "country-alias-v1",
        "resolver_version": RESOLVER_VERSION,
        "source_fingerprint": "a" * 64,
        "resolved_at": "2026-01-01T00:00:00+00:00",
    }
    values.update(overrides)
    connection.execute(
        f"INSERT INTO {PROJECTION_TABLE} ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


# --------------------------------------------------------------------------
# Migration 0024
# --------------------------------------------------------------------------


def test_migration_0024_is_discovered_after_the_earlier_ones() -> None:
    versions = [
        migration.version
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY)
    ]
    assert versions[: len(BEFORE_THIS_SLICE)] == list(BEFORE_THIS_SLICE)
    assert versions[len(BEFORE_THIS_SLICE)] == "0024"


def test_migration_0024_creates_one_table(migrated) -> None:
    assert PROJECTION_TABLE in _tables(migrated)


def test_the_projection_holds_the_documented_columns(migrated) -> None:
    assert _columns(migrated, PROJECTION_TABLE) == [
        "id", "opportunity_id", "source_location_id", "segment_position",
        "raw_segment", "country_code", "city_key", "resolution_status",
        "resolution_rule_id", "resolver_version", "source_fingerprint",
        "resolved_at", "created_at",
    ]


def test_the_projection_carries_no_profile_verdict_or_score(migrated) -> None:
    """A verdict is about a person, and a person changes. None is storable."""
    forbidden = {
        "profile_id", "user_id", "verdict", "target", "target_verdict",
        "eligibility", "eligible", "match", "match_score", "score",
        "priority", "priority_score", "rank", "ranking", "confidence",
    }
    assert forbidden.isdisjoint(_columns(migrated, PROJECTION_TABLE))


def test_the_projection_is_not_seeded(migrated) -> None:
    """Applying `0024` states nothing about any posting."""
    assert int(
        migrated.execute(f"SELECT COUNT(*) FROM {PROJECTION_TABLE}").fetchone()[0]
    ) == 0


def test_migration_0024_alters_no_existing_table() -> None:
    statements = executable_sql(MIGRATION)
    assert "ALTER TABLE" not in statements.upper()
    assert "DROP" not in statements.upper()
    for table in ("opportunity_constraint_locations", "profile_mobility",
                  "opportunities"):
        assert f"CREATE TABLE {table}" not in statements


def test_the_earlier_migrations_are_untouched() -> None:
    """`0024` adds a file; it does not edit the schema history."""
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version in BEFORE_THIS_SLICE:
            assert PROJECTION_TABLE not in source_of(migration.path)


# --------------------------------------------------------------------------
# What the schema refuses to store
# --------------------------------------------------------------------------


def test_a_resolved_row_without_a_country_is_refused(migrated, corpus) -> None:
    synchronize_location_resolutions(migrated)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_resolution(
            migrated, source_location_id=1, segment_position=9,
            resolution_status="RESOLVED", country_code=None, city_key=None,
        )


@pytest.mark.parametrize("status", ["AMBIGUOUS", "UNKNOWN"])
def test_an_unresolved_row_carrying_a_country_is_refused(
    migrated, corpus, status
) -> None:
    """A guess wearing a warning label is still a guess."""
    with pytest.raises(sqlite3.IntegrityError):
        _insert_resolution(
            migrated, source_location_id=1, segment_position=9,
            resolution_status=status, country_code="MA", city_key=None,
        )


def test_an_unknown_status_is_refused(migrated, corpus) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_resolution(
            migrated, source_location_id=1, segment_position=9,
            resolution_status="OUT_OF_TARGET", country_code=None, city_key=None,
        )


def test_a_lower_case_or_three_letter_country_is_refused(migrated, corpus) -> None:
    for code in ("ma", "MAR", "M"):
        with pytest.raises(sqlite3.IntegrityError):
            _insert_resolution(
                migrated, source_location_id=1, segment_position=9,
                country_code=code, city_key=None,
            )


def test_a_city_without_a_country_is_refused(migrated, corpus) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_resolution(
            migrated, source_location_id=1, segment_position=9,
            resolution_status="UNKNOWN", country_code=None, city_key="casablanca",
        )


def test_a_negative_segment_position_is_refused(migrated, corpus) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_resolution(migrated, source_location_id=1, segment_position=-1)


def test_two_readings_of_one_segment_are_refused(migrated, corpus) -> None:
    synchronize_location_resolutions(migrated)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_resolution(migrated, source_location_id=1, segment_position=0)


def test_a_resolution_of_an_absent_source_row_is_refused(migrated, corpus) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_resolution(migrated, source_location_id=9999)


# --------------------------------------------------------------------------
# Synchronization
# --------------------------------------------------------------------------


def test_synchronization_reads_every_collected_location(migrated, corpus) -> None:
    summary = synchronize_location_resolutions(migrated).as_dict()
    assert summary["total_opportunities"] == 5
    assert summary["source_location_rows"] == 4
    assert summary["created"] == 4
    assert summary["replaced"] == 0
    # Four source rows, five segments: `Doha, Qatar; London, UK` is two places.
    assert summary["segments"] == 5
    assert summary["resolved"] == 4
    assert summary["ambiguous"] == 0
    assert summary["unknown"] == 1
    assert summary["resolver_version"] == RESOLVER_VERSION
    assert summary["changed"] is True


def test_a_multi_location_string_becomes_one_row_per_place(migrated, corpus) -> None:
    synchronize_location_resolutions(migrated)
    rows = read_opportunity_resolutions(migrated, corpus["multi"])
    assert [row.segment_position for row in rows] == [0, 1]
    assert [row.raw_segment for row in rows] == ["Doha, Qatar", "London, UK"]
    assert [row.country_code for row in rows] == ["QA", "GB"]


def test_a_posting_nobody_could_place_is_stored_as_unknown(migrated, corpus) -> None:
    """UNKNOWN is a row that says so, not a missing row and not a country."""
    synchronize_location_resolutions(migrated)
    rows = read_opportunity_resolutions(migrated, corpus["unplaceable"])
    assert len(rows) == 1
    assert rows[0].status is ResolutionStatus.UNKNOWN
    assert rows[0].country_code is None


def test_a_posting_with_no_location_gets_no_row(migrated, corpus) -> None:
    synchronize_location_resolutions(migrated)
    assert read_opportunity_resolutions(migrated, corpus["silent"]) == ()


def test_synchronization_never_writes_to_the_locations_it_reads(
    migrated, corpus
) -> None:
    before = _location_rows(migrated)
    synchronize_location_resolutions(migrated)
    assert _location_rows(migrated) == before


def test_synchronization_never_writes_to_the_postings_it_reads(
    migrated, corpus
) -> None:
    before = migrated.execute(
        "SELECT id, canonical_title, location, country, status, is_active, "
        "eligibility_score, match_score, priority_score, updated_at "
        "FROM opportunities ORDER BY id"
    ).fetchall()
    synchronize_location_resolutions(migrated)
    assert migrated.execute(
        "SELECT id, canonical_title, location, country, status, is_active, "
        "eligibility_score, match_score, priority_score, updated_at "
        "FROM opportunities ORDER BY id"
    ).fetchall() == before


def test_an_inactive_posting_is_out_of_scope(migrated) -> None:
    insert_opportunity(migrated, MOROCCAN_LOCATION)
    merged = insert_opportunity(
        migrated, FRENCH_LOCATION, status="merged_duplicate"
    )
    synchronize_opportunity_constraints(migrated)
    migrated.execute(
        "UPDATE opportunities SET status = 'merged_duplicate' WHERE id = ?",
        (merged,),
    )
    migrated.commit()
    summary = synchronize_location_resolutions(migrated).as_dict()
    assert summary["total_opportunities"] == 1
    assert summary["source_location_rows"] == 1


def test_the_repository_writes_to_no_table_but_its_own() -> None:
    """Read the file: the projection may only ever insert into its own table."""
    text = source_of(REPOSITORY_SOURCE)
    for statement in re.findall(r"(?:INSERT INTO|UPDATE|DELETE FROM) \w+", text):
        assert PROJECTION_TABLE in statement, statement


def test_a_failed_write_leaves_the_previous_reading_intact(migrated, corpus) -> None:
    synchronize_location_resolutions(migrated, resolved_at="2026-01-01T00:00:00+00:00")
    before = _resolution_rows(migrated)
    source = load_location_sources(migrated)[0]

    def explode() -> None:
        raise RuntimeError("interrupted between the delete and the insert")

    with pytest.raises(RuntimeError):
        store_location_resolutions(
            migrated,
            source,
            resolve_location_text(source.location_text),
            resolver_version=RESOLVER_VERSION,
            source_fingerprint=location_fingerprint(source.location_text),
            resolved_at="2026-02-02T00:00:00+00:00",
            after_delete=explode,
        )
    assert _resolution_rows(migrated) == before


def test_a_source_row_of_another_posting_is_refused(migrated, corpus) -> None:
    source = load_location_sources(migrated)[0]
    impostor = LocationSource(
        opportunity_id=corpus["french"],
        source_location_id=source.source_location_id,
        position=0,
        location_text=source.location_text,
    )
    with pytest.raises(GeographicRepositoryError):
        store_location_resolutions(
            migrated,
            impostor,
            resolve_location_text(source.location_text),
            resolver_version=RESOLVER_VERSION,
            source_fingerprint=location_fingerprint(source.location_text),
        )


def test_synchronization_refuses_to_run_before_its_migration(tmp_path) -> None:
    connection = connect_database(tmp_path / "partial.db")
    try:
        apply_migrations(connection)
        connection.execute(f"DROP TABLE {PROJECTION_TABLE}")
        connection.commit()
        with pytest.raises(GeographicServiceError):
            synchronize_location_resolutions(connection)
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


def test_the_same_input_at_the_same_version_rewrites_nothing(
    migrated, corpus
) -> None:
    synchronize_location_resolutions(migrated, resolved_at="2026-01-01T00:00:00+00:00")
    before = _resolution_rows(migrated)
    summary = synchronize_location_resolutions(
        migrated, resolved_at="2026-02-02T00:00:00+00:00"
    ).as_dict()
    assert summary["unchanged"] == 4
    assert summary["created"] == 0
    assert summary["replaced"] == 0
    assert summary["changed"] is False
    # Not even a timestamp moved.
    assert _resolution_rows(migrated) == before


def test_a_new_resolver_version_recomputes_an_identical_string(
    migrated, corpus, monkeypatch
) -> None:
    synchronize_location_resolutions(migrated, resolved_at="2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(
        "services.geography.service.RESOLVER_VERSION", "geographic-resolver-v2"
    )
    summary = synchronize_location_resolutions(
        migrated, resolved_at="2026-02-02T00:00:00+00:00"
    ).as_dict()
    assert summary["replaced"] == 4
    assert summary["created"] == 0
    versions = {row[8] for row in _resolution_rows(migrated)}
    assert versions == {"geographic-resolver-v2"}


def test_a_changed_source_string_recomputes_that_row_only(migrated, corpus) -> None:
    synchronize_location_resolutions(migrated, resolved_at="2026-01-01T00:00:00+00:00")
    source = next(
        item
        for item in load_location_sources(migrated)
        if item.opportunity_id == corpus["french"]
    )
    # A re-extraction that rewrote this row's text: the fingerprint moves.
    migrated.execute(
        "UPDATE opportunity_constraint_locations SET location_text = ? WHERE id = ?",
        ("Rabat, Morocco", source.source_location_id),
    )
    migrated.commit()
    summary = synchronize_location_resolutions(
        migrated, resolved_at="2026-02-02T00:00:00+00:00"
    ).as_dict()
    assert summary["replaced"] == 1
    assert summary["unchanged"] == 3
    rows = read_opportunity_resolutions(migrated, corpus["french"])
    assert [row.country_code for row in rows] == ["MA"]


def test_a_reextracted_posting_loses_the_reading_of_its_old_strings(
    migrated, corpus
) -> None:
    """`0012` replaces locations by deleting them; a stale reading goes too."""
    synchronize_location_resolutions(migrated)
    migrated.execute(
        "UPDATE opportunities SET location = ? WHERE id = ?",
        ("Rabat, Morocco", corpus["french"]),
    )
    migrated.commit()
    synchronize_opportunity_constraints(migrated)
    assert read_opportunity_resolutions(migrated, corpus["french"]) == ()
    synchronize_location_resolutions(migrated)
    assert [
        row.country_code
        for row in read_opportunity_resolutions(migrated, corpus["french"])
    ] == ["MA"]


def test_a_deleted_posting_takes_its_readings_with_it(migrated, corpus) -> None:
    synchronize_location_resolutions(migrated)
    migrated.execute("DELETE FROM opportunities WHERE id = ?", (corpus["multi"],))
    migrated.commit()
    assert read_opportunity_resolutions(migrated, corpus["multi"]) == ()


def test_the_stored_signature_is_the_pair_the_run_used(migrated, corpus) -> None:
    synchronize_location_resolutions(migrated)
    source = load_location_sources(migrated)[0]
    assert stored_signature(migrated, source.source_location_id) == (
        location_fingerprint(source.location_text),
        RESOLVER_VERSION,
    )


# --------------------------------------------------------------------------
# The profile side, and the verdict nobody stores
# --------------------------------------------------------------------------


def test_a_profile_restricted_to_morocco_targets_ma(migrated, moroccan_profile) -> None:
    target = resolve_profile_target(migrated, moroccan_profile)
    assert target.country_code == "MA"
    assert target.rule_id == RESTRICTED_COUNTRY_RULE


def test_a_profile_that_stated_no_mobility_targets_nothing(
    migrated, profile_id
) -> None:
    target = resolve_profile_target(migrated, profile_id)
    assert target.country_code is None
    assert target.rule_id == MOBILITY_ABSENT_RULE


def test_resolving_a_target_never_writes_the_derived_code_back(
    migrated, moroccan_profile
) -> None:
    """`profile_mobility` keeps the person's words: `["Maroc"]` stays `["Maroc"]`."""
    before = _mobility_rows(migrated)
    resolve_profile_target(migrated, moroccan_profile)
    assert _mobility_rows(migrated) == before
    stored = get_profile_mobility(migrated, moroccan_profile)
    assert stored.value.locations == ("Maroc",)
    assert "MA" not in before[0][3]


def test_no_verdict_is_written_anywhere_by_an_audit(
    migrated, corpus, moroccan_profile
) -> None:
    synchronize_location_resolutions(migrated)
    before = _resolution_rows(migrated)
    mobility = _mobility_rows(migrated)
    locations = _location_rows(migrated)
    tables = _tables(migrated)
    audit_geographic_targeting(migrated, moroccan_profile)
    assert _resolution_rows(migrated) == before
    assert _mobility_rows(migrated) == mobility
    assert _location_rows(migrated) == locations
    # No table appeared to hold the answer, and none of Phase 7A.1's tables is
    # named for one: `0024` adds exactly the projection.
    assert _tables(migrated) == tables
    assert PROJECTION_TABLE in tables
    assert not {name for name in tables if "verdict" in name}


def test_the_audit_partitions_every_posting_into_three_answers(
    migrated, corpus, moroccan_profile
) -> None:
    synchronize_location_resolutions(migrated)
    report = audit_geographic_targeting(migrated, moroccan_profile).as_dict()
    assert report["target_country_code"] == "MA"
    assert report["total_opportunities"] == 5
    assert report["source_location_rows"] == 4
    assert report["segments"] == 5
    assert report["resolved"] == 4
    assert report["unknown"] == 1
    assert report["resolved_target_country"] == 1
    assert report["resolved_other_country"] == 3
    # Casablanca matches; Paris and Doha/London are elsewhere; `Any Office` and
    # the posting with no location at all are questions, not refusals.
    assert report["match"] == 1
    assert report["out_of_target"] == 2
    assert report["unknown_target"] == 2
    assert report["match"] + report["out_of_target"] + report["unknown_target"] == 5


def test_an_unknown_target_makes_every_posting_unknown(
    migrated, corpus, profile_id
) -> None:
    synchronize_location_resolutions(migrated)
    report = audit_geographic_targeting(migrated, profile_id).as_dict()
    assert report["target_country_code"] == "UNKNOWN"
    assert report["unknown_target"] == 5
    assert report["match"] == 0
    assert report["out_of_target"] == 0


def test_the_stored_projection_answers_the_evaluator(
    migrated, corpus, moroccan_profile
) -> None:
    synchronize_location_resolutions(migrated)
    target = resolve_profile_target(migrated, moroccan_profile)
    verdicts = {
        name: evaluate_target(
            target.country_code, read_opportunity_resolutions(migrated, opportunity_id)
        ).verdict
        for name, opportunity_id in corpus.items()
    }
    assert verdicts == {
        "moroccan": TargetVerdict.MATCH,
        "french": TargetVerdict.OUT_OF_TARGET,
        "multi": TargetVerdict.OUT_OF_TARGET,
        "unplaceable": TargetVerdict.UNKNOWN,
        "silent": TargetVerdict.UNKNOWN,
    }


def test_a_posting_in_two_countries_matches_when_one_of_them_is_the_target(
    migrated, moroccan_profile
) -> None:
    opportunity_id = insert_opportunity(migrated, "Casablanca, Maroc; Paris, France")
    synchronize_opportunity_constraints(migrated)
    synchronize_location_resolutions(migrated)
    target = resolve_profile_target(migrated, moroccan_profile)
    resolutions = read_opportunity_resolutions(migrated, opportunity_id)
    assert [row.country_code for row in resolutions] == ["MA", "FR"]
    assert evaluate_target(target.country_code, resolutions).verdict is (
        TargetVerdict.MATCH
    )


def test_a_country_field_and_a_location_field_are_two_source_rows(
    migrated, moroccan_profile
) -> None:
    """`0012` stores `location` and `country` separately; both are read."""
    opportunity_id = insert_opportunity(migrated, MULTI_LOCATION, country="Morocco")
    synchronize_opportunity_constraints(migrated)
    synchronize_location_resolutions(migrated)
    resolutions = read_opportunity_resolutions(migrated, opportunity_id)
    assert [row.country_code for row in resolutions] == ["QA", "GB", "MA"]
    target = resolve_profile_target(migrated, moroccan_profile)
    assert evaluate_target(target.country_code, resolutions).verdict is (
        TargetVerdict.MATCH
    )
