"""Phase 11.1C: `GET /api/explorer` over a disposable, fully migrated SQLite file.

Every database is built under a temporary directory and thrown away, and every
posting, source and link is invented. **The operational database is never
opened.** The schema is the project's own migrations, so each CHECK and foreign
key the real corpus obeys also holds here; only the minimal rows each test needs
are written, during setup, through an ordinary read-write connection.

The request under test then reads through the real read-only helper. The tests
prove it: the SQLite file and its row counts are byte-for-byte unchanged, no
side file appears, every statement issued is a read, `query_only` is confirmed
before the first query, and no classifier, resolver or extractor is reachable.
"""

from datetime import datetime
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

import services.api.explorer as explorer_api
from services.api.explorer import PUBLIC_EXPLORER_ERROR
from services.api.main import app
from services.collector.database.connection import connect_database, connect_readonly_database
from services.collector.database.migrations import apply_migrations
from tests.integration.test_opportunity_api_fine_classification_sqlite import _insert_qualification

FINE_VERSION = "TEST-ONLY-fine-v0"
FINGERPRINT = "a" * 64
FIXED_NOW = "2026-06-15T12:00:00+00:00"

WATCHED_TABLES = (
    "opportunities",
    "opportunity_qualifications",
    "opportunity_constraints",
    "opportunity_constraint_locations",
    "opportunity_location_resolutions",
    "opportunity_sources",
    "sources",
    "schema_migrations",
)

#: Entry points that classify, resolve, extract or synchronize. A GET that
#: reached any of them would fail.
COMPUTING_ENTRY_POINTS = (
    ("services.collector.qualification.classifier", "classify_opportunity"),
    ("services.collector.qualification.fine_classifier", "classify_fine_categories"),
    ("services.geography.resolver", "resolve_segment"),
    ("services.geography.repository", "store_location_resolutions"),
    ("services.collector.extractors.opportunity_constraints.extractor", "extract_opportunity_constraints"),
    ("services.recommendation.sync", "sync_recommendations"),
    ("services.collector.matching", "sync_matching"),
)


class Corpus:
    """Writes valid, migrated rows during setup only."""

    def __init__(self, path: Path):
        self.path = path
        self.connection = connect_database(path)
        apply_migrations(self.connection)
        self.connection.commit()

    def source(self, source_id, source_type="greenhouse"):
        self.connection.execute(
            "INSERT OR IGNORE INTO sources (id, type, status) VALUES (?, ?, 'active')",
            (source_id, source_type),
        )

    def posting(
        self,
        title,
        *,
        last_seen="2026-06-15T10:00:00+00:00",
        status="visible",
        qualification="CORE_TARGET",
        fine="DATA_SCIENCE",
        opportunity_type=None,
        constraints=True,
        locations=(),
        observations=(("board_a", "greenhouse", None),),
    ):
        opportunity_id = int(
            self.connection.execute(
                """INSERT INTO opportunities (canonical_title, organization, location,
                description, discovered_at, first_seen_at, last_seen_at, source_url, status)
                VALUES (?, 'TEST ONLY org', 'TEST ONLY place', 'TEST', ?, ?, ?, ?, ?)
                RETURNING id""",
                (title, last_seen, last_seen, last_seen,
                 f"https://fallback.example.invalid/{title}", status),
            ).fetchone()[0]
        )
        if qualification is not None:
            classified = {} if fine is None else {
                "fine_primary_category": fine,
                "fine_secondary_categories_json": "[]",
                "fine_category_evidence_json": "[]",
                "fine_reasons_json": json.dumps(["TEST ONLY"]),
                "fine_classifier_version": FINE_VERSION,
            }
            _insert_qualification(self.connection, opportunity_id, qualification=qualification, **classified)
        if constraints:
            self.connection.execute(
                """INSERT INTO opportunity_constraints (opportunity_id, opportunity_type,
                extractor_version, source_fingerprint, extracted_at)
                VALUES (?, ?, 'TEST-ONLY-extractor-v0', ?, ?)""",
                (opportunity_id, opportunity_type, FINGERPRINT, FIXED_NOW),
            )
        for position, (resolution_status, country, city) in enumerate(locations):
            location_id = int(
                self.connection.execute(
                    """INSERT INTO opportunity_constraint_locations (opportunity_id, position, location_text)
                    VALUES (?, ?, ?) RETURNING id""",
                    (opportunity_id, position, f"TEST ONLY segment {position}"),
                ).fetchone()[0]
            )
            self.connection.execute(
                """INSERT INTO opportunity_location_resolutions (opportunity_id, source_location_id,
                segment_position, raw_segment, country_code, city_key, resolution_status,
                resolution_rule_id, resolver_version, source_fingerprint, resolved_at)
                VALUES (?, ?, 0, ?, ?, ?, ?, 'TEST-ONLY-rule', 'TEST-ONLY-resolver-v0', ?, ?)""",
                (opportunity_id, location_id, f"TEST ONLY segment {position}", country, city,
                 resolution_status, FINGERPRINT, FIXED_NOW),
            )
        for source_id, source_type, application_url in observations:
            self.source(source_id, source_type)
            self.connection.execute(
                """INSERT INTO opportunity_sources (opportunity_id, source_id, source_url,
                application_url, discovered_at) VALUES (?, ?, ?, ?, ?)""",
                (opportunity_id, source_id, f"https://{source_id}.example.invalid/{title}",
                 application_url, last_seen),
            )
        self.connection.commit()
        return opportunity_id

    def close(self):
        self.connection.close()


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    path = tmp_path / "explorer.db"
    built = Corpus(path)
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.delenv("OPPORTUNITY_RADAR_PROFILE_ID", raising=False)
    monkeypatch.setattr(explorer_api, "_utc_now", lambda: datetime.fromisoformat(FIXED_NOW))
    yield built
    built.close()


def explore(query=""):
    response = TestClient(app).get(f"/api/explorer{query}")
    assert response.status_code == 200, response.text
    return response.json()


def ids(body):
    return [item["opportunity_id"] for item in body["items"]]


def fingerprint(path: Path):
    """The file bytes, the side files and every watched table's row count."""
    connection = connect_readonly_database(path)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in WATCHED_TABLES
        }
    finally:
        connection.close()
    sides = sorted(p.name for p in path.parent.iterdir() if p.name != path.name)
    return hashlib.sha256(path.read_bytes()).hexdigest(), sides, counts


def build_mixed(corpus):
    """A corpus exercising every multi-valued and unknown shape at once."""
    made = {}
    made["ma_casa_multi"] = corpus.posting(
        "ma-casa-multi", last_seen="2026-06-15T11:00:00+00:00", opportunity_type="INTERNSHIP",
        fine="DATA_ENGINEERING",
        locations=[("RESOLVED", "MA", "casablanca"), ("RESOLVED", "MA", "casablanca"), ("UNKNOWN", None, None)],
        observations=[("alert_z", "gmail_linkedin_alert", None), ("board_a", "greenhouse", None),
                      ("board_a", "greenhouse", "https://board_a.example.invalid/apply")],
    )
    made["split_rows"] = corpus.posting(
        "split-rows", last_seen="2026-06-15T11:00:00+00:00",
        locations=[("RESOLVED", "MA", None), ("RESOLVED", "GB", "casablanca")],
        observations=[("board_b", "greenhouse", None)],
    )
    made["unknown_everything"] = corpus.posting(
        "unknown-everything", last_seen="2026-06-14T09:00:00+00:00", fine=None, constraints=False,
        observations=(),
    )
    made["ambiguous"] = corpus.posting(
        "ambiguous", last_seen="2026-06-01T09:00:00+00:00", qualification="ADJACENT_TARGET", fine="OTHER",
        opportunity_type="JUNIOR_ROLE", locations=[("AMBIGUOUS", None, None)],
        observations=[("alert_z", "gmail_linkedin_alert", None)],
    )
    made["outside"] = corpus.posting(
        "outside", qualification="OUT_OF_SCOPE", fine=None, opportunity_type="PFE",
        locations=[("RESOLVED", "DE", "berlin")], observations=[("board_outside", "greenhouse", None)],
    )
    made["hidden"] = corpus.posting("hidden", status="hidden", locations=[("RESOLVED", "US", None)])
    return made


def test_the_read_only_path_confirms_query_only_and_issues_only_reads(corpus, monkeypatch):
    made = build_mixed(corpus)
    statements = []
    states = []
    real = connect_readonly_database

    class Recording:
        def __init__(self, connection):
            self.connection = connection

        @property
        def in_transaction(self):
            return self.connection.in_transaction

        def execute(self, statement, *parameters):
            statements.append(" ".join(statement.split()))
            if statement.lstrip().upper().startswith("SELECT"):
                states.append(self.connection.execute("PRAGMA query_only").fetchone()[0])
            return self.connection.execute(statement, *parameters)

        def close(self):
            self.connection.close()

    monkeypatch.setattr(explorer_api, "connect_readonly_database", lambda path: Recording(real(path)))
    body = explore("?country=MA&source=board_a")
    assert ids(body) == [made["ma_casa_multi"]]
    assert statements[:3] == ["PRAGMA query_only = ON", "PRAGMA query_only", "BEGIN"]
    assert statements[-1] == "ROLLBACK"
    assert all(s.startswith(("SELECT", "PRAGMA query_only", "BEGIN", "ROLLBACK")) for s in statements)
    assert states and set(states) == {1}


def test_the_helper_connection_itself_refuses_writes(corpus):
    corpus.posting("one")
    connection = connect_readonly_database(corpus.path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE opportunities SET status = 'hidden'")
    finally:
        connection.close()


def test_a_full_filtered_read_changes_nothing_on_disk(corpus):
    build_mixed(corpus)
    corpus.close()
    before = fingerprint(corpus.path)
    for query in ("", "?country=MA&city=casablanca", "?opportunity_type=INTERNSHIP", "?domain=OTHER",
                  "?source=alert_z", "?freshness=7d", "?limit=1&offset=2"):
        explore(query)
    assert fingerprint(corpus.path) == before


def test_no_computing_entry_point_is_reached(corpus, monkeypatch):
    build_mixed(corpus)

    def tripwire(*_args, **_kwargs):
        raise AssertionError("the Explorer GET reached a computing entry point")

    for module_name, attribute in COMPUTING_ENTRY_POINTS:
        monkeypatch.setattr(importlib.import_module(module_name), attribute, tripwire)
    assert explore("?country=MA&domain=DATA_ENGINEERING&freshness=30d")["total"] == 1


def test_pagination_is_deterministic_and_total_is_correct(corpus):
    made = [corpus.posting(f"p{index}", last_seen=f"2026-06-1{index % 3}T10:00:00+00:00") for index in range(7)]
    expected = sorted(
        made,
        key=lambda opportunity_id: (f"2026-06-1{made.index(opportunity_id) % 3}T10:00:00+00:00", opportunity_id),
        reverse=True,
    )
    pages = [explore(f"?limit=3&offset={offset}") for offset in (0, 3, 6)]
    assert [page["total"] for page in pages] == [7, 7, 7]
    assert sum((ids(page) for page in pages), []) == expected
    assert [ids(explore(f"?limit=3&offset={offset}")) for offset in (0, 3, 6)] == [ids(p) for p in pages]


def test_multi_valued_joins_never_duplicate_and_collapse_per_source(corpus):
    made = build_mixed(corpus)
    body = explore("?limit=50")
    assert len(ids(body)) == len(set(ids(body))) == body["total"] == 4
    item = {entry["opportunity_id"]: entry for entry in body["items"]}[made["ma_casa_multi"]]
    assert item["sources"] == [
        {"source_id": "alert_z", "source_type": "gmail_linkedin_alert"},
        {"source_id": "board_a", "source_type": "greenhouse"},
    ]
    assert item["resolved_locations"] == [{"country_code": "MA", "city_key": "casablanca"}]
    assert item["has_unresolved_location"] is True
    # The earliest official observation carrying a usable link wins; its
    # application URL is absent, so its source URL is the link.
    assert item["original_url"] == "https://board_a.example.invalid/ma-casa-multi"
    for query in ("?source=board_a", "?country=MA&city=casablanca"):
        filtered = explore(query)
        assert ids(filtered) == [made["ma_casa_multi"]] and filtered["total"] == 1


def test_country_and_city_must_come_from_the_same_resolution_row(corpus):
    made = build_mixed(corpus)
    assert ids(explore("?country=MA&city=casablanca")) == [made["ma_casa_multi"]]
    assert ids(explore("?country=GB&city=casablanca")) == [made["split_rows"]]
    assert sorted(ids(explore("?city=casablanca"))) == sorted([made["ma_casa_multi"], made["split_rows"]])
    assert sorted(ids(explore("?country=MA"))) == sorted([made["ma_casa_multi"], made["split_rows"]])


def test_null_values_remain_unknown(corpus):
    made = build_mixed(corpus)
    items = {entry["opportunity_id"]: entry for entry in explore()["items"]}
    unknown = items[made["unknown_everything"]]
    assert unknown["fine_primary_category"] is None
    assert unknown["opportunity_type"] is None
    assert unknown["resolved_locations"] == [] and unknown["has_unresolved_location"] is True
    assert unknown["sources"] == []
    assert unknown["original_url"] == "https://fallback.example.invalid/unknown-everything"
    ambiguous = items[made["ambiguous"]]
    assert ambiguous["fine_primary_category"] == "OTHER"
    assert ambiguous["resolved_locations"] == [] and ambiguous["has_unresolved_location"] is True
    filters = explore("?domain=OTHER&offset=9")["available_filters"]
    assert filters == {
        "countries": ["GB", "MA"],
        "cities": [
            {"country_code": "GB", "city_key": "casablanca"},
            {"country_code": "MA", "city_key": "casablanca"},
        ],
        "opportunity_types": ["INTERNSHIP", "JUNIOR_ROLE"],
        "domains": ["DATA_ENGINEERING", "DATA_SCIENCE", "OTHER"],
        "sources": [
            {"source_id": "alert_z", "source_type": "gmail_linkedin_alert"},
            {"source_id": "board_a", "source_type": "greenhouse"},
            {"source_id": "board_b", "source_type": "greenhouse"},
        ],
    }


def test_errors_are_stable_and_never_create_a_database(corpus, monkeypatch, tmp_path):
    corpus.posting("one")
    missing = tmp_path / "absent" / "explorer.db"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(missing))
    response = TestClient(app).get("/api/explorer")
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_EXPLORER_ERROR}
    assert str(missing) not in response.text and "absent" not in response.text
    assert not missing.parent.exists()
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(corpus.path))
    assert TestClient(app).get("/api/explorer?freshness=1y").status_code == 422
    assert explore()["total"] == 1
