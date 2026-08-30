"""Migration 0006 and the user/profile root, on disposable SQLite databases."""

import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
)
from services.collector.database.opportunities import persist_opportunities
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig
from services.digital_twin.identity import InvalidEmailError
from services.digital_twin.repository import (
    UserProfileError,
    ensure_user_profile,
    get_user_profile_by_email,
)

# TEST ONLY addresses; `.invalid` never resolves and no real identity is used.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_EMAIL_VARIANT = "  Student@Example.invalid  "
TEST_ONLY_OTHER_EMAIL = "other.student@example.invalid"

PHASE_TWO_VERSIONS = ("0001", "0002", "0003", "0004", "0005")


def _phase_two_migrations():
    """Only the migrations that existed before this slice."""
    return [
        migration
        for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY)
        if migration.version in PHASE_TWO_VERSIONS
    ]


def _apply_phase_two(connection, tmp_path) -> list[str]:
    directory = tmp_path / "phase2-migrations"
    directory.mkdir()
    for migration in _phase_two_migrations():
        (directory / migration.path.name).write_text(
            migration.path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return apply_migrations(connection, directory)


def _seed_phase_two_opportunity(connection) -> None:
    source = SourceConfig(
        id="test_greenhouse",
        type="greenhouse",
        enabled=True,
        organization="TEST ONLY organization",
        board_token="test-only",
        category="jobs",
        country="US",
        frequency_minutes=60,
        status="active",
    )
    persist_opportunities(
        connection,
        source,
        [
            OpportunityCandidate(
                source_id=source.id,
                source_external_id="TEST-ONLY-1",
                canonical_title="TEST ONLY role",
                organization="TEST ONLY organization",
                location="TEST ONLY location",
                description="TEST ONLY description",
                published_at=None,
                source_url="https://boards.example.invalid/acme/jobs/1",
                application_url="https://boards.example.invalid/acme/jobs/1/apply",
                canonical_url=None,
            )
        ],
    )


def _tables(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _counts(connection) -> tuple[int, int]:
    users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    profiles = connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    return int(users), int(profiles)


def test_0006_upgrades_an_existing_phase_two_database_without_losing_data(tmp_path):
    connection = connect_database(tmp_path / "upgrade.db")
    try:
        assert _apply_phase_two(connection, tmp_path) == list(PHASE_TWO_VERSIONS)
        _seed_phase_two_opportunity(connection)
        before = connection.execute(
            "SELECT id, canonical_title, source_url FROM opportunities ORDER BY id"
        ).fetchall()
        observations_before = connection.execute(
            "SELECT id, opportunity_id, source_id FROM opportunity_sources ORDER BY id"
        ).fetchall()

        assert apply_migrations(connection) == [
            "0006", "0007", "0008", "0009", "0010", "0011", "0012", "0013",
            "0014",
        ]

        assert {"users", "profiles"} <= _tables(connection)
        assert (
            connection.execute(
                "SELECT id, canonical_title, source_url FROM opportunities ORDER BY id"
            ).fetchall()
            == before
        )
        assert (
            connection.execute(
                "SELECT id, opportunity_id, source_id FROM opportunity_sources ORDER BY id"
            ).fetchall()
            == observations_before
        )
        assert len(before) == 1
    finally:
        connection.close()


def test_0006_on_a_fresh_database_creates_no_identity_row(tmp_path):
    connection = connect_database(tmp_path / "fresh.db")
    try:
        assert "0006" in apply_migrations(connection)

        assert {"users", "profiles"} <= _tables(connection)
        assert _counts(connection) == (0, 0)
    finally:
        connection.close()


def test_reapplying_migrations_is_idempotent_and_records_0006_once(tmp_path):
    connection = connect_database(tmp_path / "idempotent.db")
    try:
        first = apply_migrations(connection)

        assert apply_migrations(connection) == []
        assert apply_migrations(connection) == []
        recorded = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = '0006'"
        ).fetchall()

        assert first[-1] == "0014"
        assert recorded == [("0006",)]
        assert _counts(connection) == (0, 0)
    finally:
        connection.close()


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "profile.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def test_first_call_creates_exactly_one_user_and_one_profile(migrated):
    result = ensure_user_profile(migrated, TEST_ONLY_EMAIL)

    assert result.created is True
    assert result.user_id > 0
    assert result.profile_id > 0
    assert result.profile.user_id == result.user_id
    assert result.user.email == TEST_ONLY_EMAIL
    assert _counts(migrated) == (1, 1)


def test_second_call_returns_the_same_ids_and_creates_nothing(migrated):
    first = ensure_user_profile(migrated, TEST_ONLY_EMAIL)

    second = ensure_user_profile(migrated, TEST_ONLY_EMAIL)

    assert (second.user_id, second.profile_id) == (first.user_id, first.profile_id)
    assert second.created is False
    assert _counts(migrated) == (1, 1)


def test_case_and_spacing_variants_resolve_to_the_same_user(migrated):
    first = ensure_user_profile(migrated, TEST_ONLY_EMAIL)

    variant = ensure_user_profile(migrated, TEST_ONLY_EMAIL_VARIANT)
    other = ensure_user_profile(migrated, TEST_ONLY_OTHER_EMAIL)

    assert (variant.user_id, variant.profile_id) == (first.user_id, first.profile_id)
    assert other.user_id != first.user_id
    assert _counts(migrated) == (2, 2)


def test_a_manifestly_invalid_address_writes_nothing(migrated):
    with pytest.raises(InvalidEmailError):
        ensure_user_profile(migrated, "not-an-address")

    assert _counts(migrated) == (0, 0)


def test_one_user_can_only_own_one_profile(migrated):
    result = ensure_user_profile(migrated, TEST_ONLY_EMAIL)

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("INSERT INTO profiles (user_id) VALUES (?)", (result.user_id,))

    assert _counts(migrated) == (1, 1)


def test_foreign_key_refuses_a_profile_without_its_user(migrated):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("INSERT INTO profiles (user_id) VALUES (?)", (4242,))

    assert _counts(migrated) == (0, 0)


def test_a_failure_before_the_profile_rolls_the_user_back(migrated):
    def explode() -> None:
        raise RuntimeError("TEST ONLY failure while creating the profile")

    with pytest.raises(RuntimeError):
        ensure_user_profile(migrated, TEST_ONLY_EMAIL, after_user=explode)

    assert _counts(migrated) == (0, 0)
    assert get_user_profile_by_email(migrated, TEST_ONLY_EMAIL) is None


def test_reading_an_existing_and_a_missing_profile(migrated):
    created = ensure_user_profile(migrated, TEST_ONLY_EMAIL)

    found = get_user_profile_by_email(migrated, TEST_ONLY_EMAIL_VARIANT)

    assert found is not None
    assert (found.user_id, found.profile_id) == (created.user_id, created.profile_id)
    assert found.created is False
    assert get_user_profile_by_email(migrated, TEST_ONLY_OTHER_EMAIL) is None


def test_reading_refuses_to_invent_a_profile_for_a_user_without_one(migrated):
    result = ensure_user_profile(migrated, TEST_ONLY_EMAIL)
    migrated.execute("DELETE FROM profiles WHERE id = ?", (result.profile_id,))

    with pytest.raises(UserProfileError):
        get_user_profile_by_email(migrated, TEST_ONLY_EMAIL)
