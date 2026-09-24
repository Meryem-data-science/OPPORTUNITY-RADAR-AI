"""Paging the opportunity listing, against a migrated temporary SQLite database.

Every opportunity here is invented and marked TEST ONLY, and every database is
created under `tmp_path` and thrown away. The operational database is never
opened and nothing is collected.

The property under test is one sentence: walking the listing page by page
returns every opportunity exactly once, in one stable order, whatever the page
size — including when many opportunities share a `last_seen_at` to the
microsecond, which is what one collection run actually produces.
"""

from fastapi.testclient import TestClient

from services.api.main import app
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.opportunities import persist_opportunities
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig

SOURCE = SourceConfig(
    "test_pagination", "greenhouse", True, "TEST ONLY organization", "test-only",
    "jobs", None, 60, "active",
)

#: One instant for every opportunity, which is the interesting case: the second
#: ordering key is the only thing that makes the walk stable.
SHARED_INSTANT = "2026-01-01T00:00:00+00:00"


def candidate(suffix: str) -> OpportunityCandidate:
    return OpportunityCandidate(
        source_id=SOURCE.id,
        source_external_id=f"TEST-ONLY-{suffix}",
        canonical_title=f"TEST ONLY role {suffix}",
        organization="TEST ONLY organization",
        location="TEST ONLY location",
        description=f"TEST ONLY description {suffix}",
        published_at=None,
        source_url=f"https://example.invalid/source/{suffix}",
        application_url=None,
        canonical_url=f"https://example.invalid/canonical/{suffix}",
    )


def seeded(tmp_path, monkeypatch, count: int, *, shared_instant: bool = True):
    """`count` visible, active opportunities, and the API pointed at them."""
    path = tmp_path / "pagination.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        stamps = iter(
            [SHARED_INSTANT] * count
            if shared_instant
            else [f"2026-01-{number + 1:02d}T00:00:00+00:00" for number in range(count)]
        )
        outcome = persist_opportunities(
            connection,
            SOURCE,
            [candidate(f"{number:03d}") for number in range(count)],
            clock=lambda: next(stamps),
        )
        assert outcome.created == count
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return path


def page(client, *, limit: int, offset: int | None = None) -> dict:
    query = f"/api/opportunities?limit={limit}"
    if offset is not None:
        query += f"&offset={offset}"
    response = client.get(query)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------- the default page


def test_the_listing_without_an_offset_is_exactly_what_it_always_was(
    tmp_path, monkeypatch
):
    """The historical contract: no offset means the first page."""
    seeded(tmp_path, monkeypatch, 5)
    client = TestClient(app)

    without = page(client, limit=2)
    with_zero = page(client, limit=2, offset=0)

    assert without == with_zero
    assert without["returned"] == 2
    assert without["total"] == 5


def test_total_counts_the_whole_listing_not_the_page(tmp_path, monkeypatch):
    seeded(tmp_path, monkeypatch, 7)
    client = TestClient(app)

    for offset in (0, 3, 6):
        answer = page(client, limit=3, offset=offset)
        assert answer["total"] == 7
        assert answer["returned"] == len(answer["items"])


# -------------------------------------------------------- walking the whole


def test_walking_page_by_page_returns_every_opportunity_exactly_once(
    tmp_path, monkeypatch
):
    """The property the second ordering key exists for.

    All twelve share one `last_seen_at`, so without a unique tiebreaker SQLite
    could order them differently between two queries and the walk could repeat
    one row while silently dropping another.
    """
    seeded(tmp_path, monkeypatch, 12)
    client = TestClient(app)

    seen: list[int] = []
    offset = 0
    while True:
        answer = page(client, limit=5, offset=offset)
        if not answer["items"]:
            break
        seen.extend(item["id"] for item in answer["items"])
        offset += len(answer["items"])

    assert len(seen) == 12
    assert len(set(seen)) == 12  # no duplicate
    # And the walk is the same order the single big page gives.
    whole = page(client, limit=100)
    assert seen == [item["id"] for item in whole["items"]]


def test_consecutive_pages_never_overlap(tmp_path, monkeypatch):
    seeded(tmp_path, monkeypatch, 9)
    client = TestClient(app)

    first = {item["id"] for item in page(client, limit=4, offset=0)["items"]}
    second = {item["id"] for item in page(client, limit=4, offset=4)["items"]}
    third = {item["id"] for item in page(client, limit=4, offset=8)["items"]}

    assert len(first) == 4 and len(second) == 4 and len(third) == 1
    assert first.isdisjoint(second)
    assert second.isdisjoint(third)
    assert first.isdisjoint(third)


def test_a_page_size_of_one_still_walks_the_whole_listing(tmp_path, monkeypatch):
    seeded(tmp_path, monkeypatch, 6)
    client = TestClient(app)

    seen = [page(client, limit=1, offset=offset)["items"][0]["id"] for offset in range(6)]

    assert len(set(seen)) == 6
    assert seen == [item["id"] for item in page(client, limit=100)["items"]]


def test_distinct_timestamps_are_ordered_newest_first(tmp_path, monkeypatch):
    """The ordering itself is unchanged: newest `last_seen_at` first."""
    seeded(tmp_path, monkeypatch, 4, shared_instant=False)
    client = TestClient(app)

    stamps = [item["last_seen_at"] for item in page(client, limit=100)["items"]]

    assert stamps == sorted(stamps, reverse=True)


# ------------------------------------------------------------ the two edges


def test_an_offset_past_the_end_is_an_empty_page_and_not_an_error(
    tmp_path, monkeypatch
):
    seeded(tmp_path, monkeypatch, 3)
    client = TestClient(app)

    answer = page(client, limit=10, offset=99)

    assert answer["items"] == []
    assert answer["returned"] == 0
    # `total` still describes the listing, so a caller knows why it is empty.
    assert answer["total"] == 3


def test_an_empty_listing_pages_without_complaining(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))

    answer = page(TestClient(app), limit=20, offset=0)

    assert answer == {"items": [], "returned": 0, "total": 0}


# ------------------------------------------------------- the bounds are real


def test_the_limit_ceiling_of_one_hundred_is_unchanged(tmp_path, monkeypatch):
    """Paging is how a caller reads more than 100, not a bigger single answer."""
    seeded(tmp_path, monkeypatch, 3)
    client = TestClient(app)

    assert client.get("/api/opportunities?limit=100").status_code == 200
    assert client.get("/api/opportunities?limit=101").status_code == 422
    assert client.get("/api/opportunities?limit=0").status_code == 422
    assert client.get("/api/opportunities?limit=-1").status_code == 422


def test_a_malformed_offset_is_refused_before_it_reaches_sql(tmp_path, monkeypatch):
    seeded(tmp_path, monkeypatch, 3)
    client = TestClient(app)

    for raw in ("-1", "abc", "1.5", ""):
        assert client.get(f"/api/opportunities?limit=5&offset={raw}").status_code == 422


def test_paging_writes_nothing(tmp_path, monkeypatch):
    path = seeded(tmp_path, monkeypatch, 5)
    client = TestClient(app)

    connection = connect_database(path)
    try:
        before = connection.execute(
            "SELECT * FROM opportunities ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    for offset in (0, 2, 4, 99):
        page(client, limit=2, offset=offset)

    connection = connect_database(path)
    try:
        after = connection.execute("SELECT * FROM opportunities ORDER BY id").fetchall()
    finally:
        connection.close()
    assert after == before
