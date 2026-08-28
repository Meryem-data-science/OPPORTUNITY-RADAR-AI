"""Official-link priority for GET /api/opportunities, on a disposable SQLite database."""

from fastapi.testclient import TestClient

from services.api.main import app
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.opportunities import persist_opportunities
from services.collector.deduplication.merges import apply_merge
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig

GREENHOUSE_APPLY = "https://boards.example.invalid/acme/jobs/1/apply"
GREENHOUSE_SOURCE = "https://boards.example.invalid/acme/jobs/1"
LINKEDIN_APPLY = "https://www.linkedin.invalid/jobs/view/1/apply"
LINKEDIN_SOURCE = "https://www.linkedin.invalid/jobs/view/1"


def _greenhouse_source(source_id: str = "test_greenhouse") -> SourceConfig:
    return SourceConfig(
        id=source_id,
        type="greenhouse",
        enabled=True,
        organization="TEST ONLY organization",
        board_token="test-only",
        category="jobs",
        country="US",
        frequency_minutes=60,
        status="active",
    )


def _linkedin_source(source_id: str = "test_linkedin") -> SourceConfig:
    return SourceConfig(
        id=source_id,
        type="gmail_linkedin_alert",
        enabled=True,
        category="jobs",
        country="FR",
        frequency_minutes=60,
        status="active",
        gmail_query="TEST ONLY query",
        gmail_message_limit=10,
    )


def _candidate(source_id: str, suffix: str, source_url: str, application_url: str | None):
    return OpportunityCandidate(
        source_id=source_id,
        source_external_id=f"TEST-ONLY-{suffix}",
        canonical_title="TEST ONLY data engineer",
        organization="TEST ONLY organization",
        location="TEST ONLY location",
        description=f"TEST ONLY description {suffix}",
        published_at=None,
        source_url=source_url,
        application_url=application_url,
        canonical_url=None,
    )


def _database(path):
    connection = connect_database(path)
    apply_migrations(connection)
    return connection


def _confirm_and_merge(connection, first_id: int, second_id: int, canonical_id: int) -> None:
    """Record the human CONFIRMED_DUPLICATE decision and apply the real merge."""
    first, second = min(first_id, second_id), max(first_id, second_id)
    connection.execute(
        """INSERT INTO deduplication_decisions
           (opportunity_a_id, opportunity_b_id, status, audit_classification,
            title_similarity, organization_similarity, title_normalized_exact,
            organization_normalized_exact, location_signal, shared_source_url,
            shared_application_url, shared_canonical_url, reasons_json,
            first_detected_at, last_detected_at)
           VALUES (?, ?, 'CONFIRMED_DUPLICATE', 'STRONG_CANDIDATE', 1, 1, 1, 1,
                   'MATCH', 0, 0, 0, '[]', '2026-01-01', '2026-01-01')""",
        (first, second),
    )
    connection.commit()
    apply_merge(connection, first, second, canonical_id)


def _observations(path) -> list[tuple]:
    connection = connect_database(path)
    try:
        return connection.execute(
            """SELECT id, opportunity_id, source_id, source_url, application_url,
                      canonical_url, discovered_at
               FROM opportunity_sources ORDER BY id"""
        ).fetchall()
    finally:
        connection.close()


def _get(monkeypatch, path, query: str = "?limit=100") -> dict:
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    response = TestClient(app).get(f"/api/opportunities{query}")
    assert response.status_code == 200
    return response.json()


def test_linkedin_only_keeps_the_linkedin_link(tmp_path, monkeypatch) -> None:
    path = tmp_path / "linkedin-only.db"
    connection = _database(path)
    try:
        linkedin = _linkedin_source()
        persist_opportunities(
            connection,
            linkedin,
            [_candidate(linkedin.id, "li", LINKEDIN_SOURCE, LINKEDIN_APPLY)],
        )
    finally:
        connection.close()

    payload = _get(monkeypatch, path)

    assert [item["original_url"] for item in payload["items"]] == [LINKEDIN_APPLY]


def test_greenhouse_only_uses_the_greenhouse_link(tmp_path, monkeypatch) -> None:
    path = tmp_path / "greenhouse-only.db"
    connection = _database(path)
    try:
        greenhouse = _greenhouse_source()
        persist_opportunities(
            connection,
            greenhouse,
            [_candidate(greenhouse.id, "gh", GREENHOUSE_SOURCE, GREENHOUSE_APPLY)],
        )
    finally:
        connection.close()

    payload = _get(monkeypatch, path)

    assert [item["original_url"] for item in payload["items"]] == [GREENHOUSE_APPLY]


def test_linkedin_canonical_with_greenhouse_observation_prefers_greenhouse(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "linkedin-canonical.db"
    connection = _database(path)
    try:
        linkedin, greenhouse = _linkedin_source(), _greenhouse_source()
        persist_opportunities(
            connection,
            linkedin,
            [_candidate(linkedin.id, "li", LINKEDIN_SOURCE, LINKEDIN_APPLY)],
        )
        persist_opportunities(
            connection,
            greenhouse,
            [_candidate(greenhouse.id, "gh", GREENHOUSE_SOURCE, GREENHOUSE_APPLY)],
        )
        linkedin_id, greenhouse_id = [
            row[0] for row in connection.execute("SELECT id FROM opportunities ORDER BY id")
        ]
        _confirm_and_merge(connection, linkedin_id, greenhouse_id, linkedin_id)
        canonical = connection.execute(
            "SELECT id, source_url, application_url FROM opportunities WHERE is_active = 1"
        ).fetchall()
    finally:
        connection.close()

    before = _observations(path)
    payload = _get(monkeypatch, path)
    after = _observations(path)

    # The reviewer's canonical row stays the LinkedIn one and is not rewritten.
    assert canonical == [(linkedin_id, LINKEDIN_SOURCE, LINKEDIN_APPLY)]
    assert payload["returned"] == payload["total"] == 1
    assert payload["items"][0]["id"] == linkedin_id
    assert payload["items"][0]["source_url"] == LINKEDIN_SOURCE
    assert payload["items"][0]["application_url"] == LINKEDIN_APPLY
    assert payload["items"][0]["original_url"] == GREENHOUSE_APPLY
    assert before == after
    assert len(after) == 2


def test_greenhouse_canonical_with_linkedin_observation_stays_greenhouse(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "greenhouse-canonical.db"
    connection = _database(path)
    try:
        linkedin, greenhouse = _linkedin_source(), _greenhouse_source()
        persist_opportunities(
            connection,
            linkedin,
            [_candidate(linkedin.id, "li", LINKEDIN_SOURCE, LINKEDIN_APPLY)],
        )
        persist_opportunities(
            connection,
            greenhouse,
            [_candidate(greenhouse.id, "gh", GREENHOUSE_SOURCE, GREENHOUSE_APPLY)],
        )
        linkedin_id, greenhouse_id = [
            row[0] for row in connection.execute("SELECT id FROM opportunities ORDER BY id")
        ]
        _confirm_and_merge(connection, linkedin_id, greenhouse_id, greenhouse_id)
    finally:
        connection.close()

    payload = _get(monkeypatch, path)

    assert payload["returned"] == payload["total"] == 1
    assert payload["items"][0]["id"] == greenhouse_id
    assert payload["items"][0]["original_url"] == GREENHOUSE_APPLY


def test_several_official_observations_resolve_to_the_earliest_one(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "two-official.db"
    first_apply = "https://boards.example.invalid/acme/jobs/first/apply"
    second_apply = "https://boards.example.invalid/acme/jobs/second/apply"
    connection = _database(path)
    try:
        first_board = _greenhouse_source("test_greenhouse_first")
        second_board = _greenhouse_source("test_greenhouse_second")
        persist_opportunities(
            connection,
            first_board,
            [
                _candidate(
                    first_board.id,
                    "gh-1",
                    "https://boards.example.invalid/acme/jobs/first",
                    first_apply,
                )
            ],
        )
        persist_opportunities(
            connection,
            second_board,
            [
                _candidate(
                    second_board.id,
                    "gh-2",
                    "https://boards.example.invalid/acme/jobs/second",
                    second_apply,
                )
            ],
        )
        first_id, second_id = [
            row[0] for row in connection.execute("SELECT id FROM opportunities ORDER BY id")
        ]
        # The reviewer keeps the later observation as canonical on purpose.
        _confirm_and_merge(connection, first_id, second_id, second_id)
        observation_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM opportunity_sources WHERE opportunity_id = ? ORDER BY id",
                (second_id,),
            )
        ]
    finally:
        connection.close()

    payload = _get(monkeypatch, path)

    assert observation_ids == [1, 2]
    assert payload["returned"] == payload["total"] == 1
    assert payload["items"][0]["application_url"] == second_apply
    # Tie-break: smallest opportunity_sources.id among official observations.
    assert payload["items"][0]["original_url"] == first_apply


def test_link_selection_never_touches_opportunity_sources(tmp_path, monkeypatch) -> None:
    path = tmp_path / "read-only.db"
    connection = _database(path)
    try:
        linkedin, greenhouse = _linkedin_source(), _greenhouse_source()
        persist_opportunities(
            connection,
            linkedin,
            [_candidate(linkedin.id, "li", LINKEDIN_SOURCE, LINKEDIN_APPLY)],
        )
        persist_opportunities(
            connection,
            greenhouse,
            [_candidate(greenhouse.id, "gh", GREENHOUSE_SOURCE, GREENHOUSE_APPLY)],
        )
        linkedin_id, greenhouse_id = [
            row[0] for row in connection.execute("SELECT id FROM opportunities ORDER BY id")
        ]
        _confirm_and_merge(connection, linkedin_id, greenhouse_id, linkedin_id)
    finally:
        connection.close()

    before = _observations(path)
    _get(monkeypatch, path)
    _get(monkeypatch, path, "?limit=1")
    after = _observations(path)

    assert len(before) == 2
    assert before == after
    assert {row[2] for row in after} == {"test_linkedin", "test_greenhouse"}
    assert {row[1] for row in after} == {linkedin_id}


def test_order_limit_and_total_do_not_regress(tmp_path, monkeypatch) -> None:
    path = tmp_path / "listing.db"
    connection = _database(path)
    try:
        greenhouse = _greenhouse_source()
        stamps = iter(
            [
                "2026-01-01T00:00:00+00:00",
                "2026-01-03T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ]
        )
        persist_opportunities(
            connection,
            greenhouse,
            [
                _candidate(greenhouse.id, suffix, f"{GREENHOUSE_SOURCE}/{suffix}", None)
                for suffix in ("old", "new", "middle")
            ],
            clock=lambda: next(stamps),
        )
        connection.commit()
    finally:
        connection.close()

    payload = _get(monkeypatch, path)
    limited = _get(monkeypatch, path, "?limit=2")

    assert payload["returned"] == payload["total"] == 3
    assert [item["original_url"] for item in payload["items"]] == [
        f"{GREENHOUSE_SOURCE}/new",
        f"{GREENHOUSE_SOURCE}/middle",
        f"{GREENHOUSE_SOURCE}/old",
    ]
    assert limited["returned"] == 2
    assert limited["total"] == 3
    assert [item["id"] for item in limited["items"]] == [
        item["id"] for item in payload["items"][:2]
    ]
