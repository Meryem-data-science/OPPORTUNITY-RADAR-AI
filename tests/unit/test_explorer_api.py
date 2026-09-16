"""Phase 11.1C: unit coverage for the read-only Explorer Data & AI surface.

Each test builds a small disposable SQLite file holding only the columns the
Explorer reads, so every assertion is about behaviour the SQL really has: which
postings are in the universe, which filter matches what, the navigation order,
and that an unknown value is never turned into a known one. The migrated schema
and the read-only guarantees are proven separately, in the SQLite integration
test. The clock is held still, and every value is TEST ONLY.
"""

import ast
from datetime import datetime, timezone
import inspect
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from services.api import explorer as explorer_api
from services.api import main
from services.api.explorer import PUBLIC_EXPLORER_ERROR
from services.api.link_priority import SourceObservation, preferred_link, select_original_url

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
FINE_VERSION = "TEST-ONLY-fine-v0"
UNCLASSIFIED = object()

SCHEMA = """
CREATE TABLE opportunities (
    id INTEGER PRIMARY KEY, canonical_title TEXT NOT NULL, organization TEXT NOT NULL,
    opportunity_type TEXT, location TEXT, last_seen_at TEXT NOT NULL,
    source_url TEXT NOT NULL, application_url TEXT, canonical_url TEXT,
    status TEXT NOT NULL, is_active INTEGER NOT NULL,
    relevance_score REAL, eligibility_score REAL, match_score REAL,
    priority_score REAL, interview_potential_score REAL
);
CREATE TABLE opportunity_qualifications (
    opportunity_id INTEGER PRIMARY KEY, qualification TEXT NOT NULL,
    fine_primary_category TEXT, fine_secondary_categories_json TEXT,
    fine_category_evidence_json TEXT, fine_reasons_json TEXT, fine_classifier_version TEXT
);
CREATE TABLE opportunity_constraints (opportunity_id INTEGER PRIMARY KEY, opportunity_type TEXT);
CREATE TABLE opportunity_location_resolutions (
    id INTEGER PRIMARY KEY, opportunity_id INTEGER NOT NULL, source_location_id INTEGER NOT NULL,
    segment_position INTEGER NOT NULL, country_code TEXT, city_key TEXT,
    resolution_status TEXT NOT NULL
);
CREATE TABLE sources (id TEXT PRIMARY KEY, type TEXT NOT NULL);
CREATE TABLE opportunity_sources (
    id INTEGER PRIMARY KEY, opportunity_id INTEGER NOT NULL, source_id TEXT NOT NULL,
    source_url TEXT NOT NULL, application_url TEXT, canonical_url TEXT
);
"""


class World:
    """A tiny persisted corpus; `add` writes one posting and what hangs off it."""

    def __init__(self, path: Path):
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA)
        self.location_id = 0

    def add(
        self,
        opportunity_id,
        *,
        last_seen="2026-06-15T10:00:00+00:00",
        status="visible",
        active=1,
        qualification="CORE_TARGET",
        fine="DATA_SCIENCE",
        constraints=True,
        opportunity_type=None,
        legacy_type=None,
        locations=(),
        sources=(("board_a", "greenhouse"),),
        location_text="TEST ONLY place",
        scores=(None, None, None, None, None),
    ):
        self.connection.execute(
            """INSERT INTO opportunities VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opportunity_id, f"Role {opportunity_id}", f"Org {opportunity_id}", legacy_type,
                location_text, last_seen, f"https://fallback.example.invalid/{opportunity_id}",
                status, active, *scores,
            ),
        )
        if qualification is not None:
            if fine is UNCLASSIFIED:
                fine_values = (None, None, None, None, None)
            else:
                fine_values = (fine, "[]", "[]", "[]", FINE_VERSION)
            self.connection.execute(
                "INSERT INTO opportunity_qualifications VALUES (?, ?, ?, ?, ?, ?, ?)",
                (opportunity_id, qualification, *fine_values),
            )
        if constraints:
            self.connection.execute(
                "INSERT INTO opportunity_constraints VALUES (?, ?)", (opportunity_id, opportunity_type)
            )
        for position, (resolution_status, country, city) in enumerate(locations):
            self.location_id += 1
            self.connection.execute(
                "INSERT INTO opportunity_location_resolutions VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                (opportunity_id, self.location_id, position, country, city, resolution_status),
            )
        for index, source in enumerate(sources):
            source_id, source_type = source[0], source[1]
            url = source[2] if len(source) > 2 else f"https://{source_id}.example.invalid/{opportunity_id}/{index}"
            self.connection.execute("INSERT OR IGNORE INTO sources VALUES (?, ?)", (source_id, source_type))
            self.connection.execute(
                "INSERT INTO opportunity_sources VALUES (NULL, ?, ?, ?, NULL, NULL)",
                (opportunity_id, source_id, url),
            )
        self.connection.commit()


@pytest.fixture
def world(monkeypatch, tmp_path):
    path = tmp_path / "explorer-unit.db"
    built = World(path)
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.delenv("OPPORTUNITY_RADAR_PROFILE_ID", raising=False)
    monkeypatch.setattr(explorer_api, "_utc_now", lambda: NOW)
    yield built
    built.connection.close()


def explore(query="", expected=200):
    response = TestClient(main.app).get(f"/api/explorer{query}")
    assert response.status_code == expected, response.text
    return response.json()


def ids(body):
    return [item["opportunity_id"] for item in body["items"]]


# --------------------------------------------------------------------------
# universe, pagination and order
# --------------------------------------------------------------------------


def test_the_base_population_is_visible_active_core_or_adjacent_only(world):
    world.add(1, qualification="CORE_TARGET")
    world.add(2, qualification="ADJACENT_TARGET", fine="OTHER")
    world.add(3, qualification="OUT_OF_SCOPE", fine=UNCLASSIFIED)
    world.add(4, qualification="UNCERTAIN", fine=UNCLASSIFIED)
    world.add(5, qualification=None)
    world.add(6, status="hidden")
    world.add(7, active=0)
    body = explore()
    assert sorted(ids(body)) == [1, 2]
    assert body["total"] == 2


def test_default_limit_is_24_and_offset_0(world):
    for opportunity_id in range(1, 31):
        world.add(opportunity_id)
    body = explore()
    assert body["limit"] == 24 and body["offset"] == 0
    assert body["returned"] == 24 and len(body["items"]) == 24
    assert body["total"] == 30


def test_limit_is_bounded_between_1_and_50(world):
    for opportunity_id in range(1, 56):
        world.add(opportunity_id)
    assert explore("?limit=50")["returned"] == 50
    explore("?limit=51", expected=422)
    explore("?limit=0", expected=422)


def test_offset_must_not_be_negative(world):
    world.add(1)
    explore("?offset=-1", expected=422)


@pytest.mark.parametrize(
    "query",
    [
        "?freshness=1h", "?freshness=30D", "?opportunity_type=CDI", "?opportunity_type=internship",
        "?domain=ROBOTICS", "?domain=UNKNOWN", "?country=Morocco", "?country=ma", "?city=", "?source=",
    ],
)
def test_invalid_closed_vocabulary_values_are_rejected_not_ignored(world, query):
    world.add(1)
    explore(query, expected=422)


def test_the_route_accepts_no_profile_and_ignores_a_client_profile_id(world):
    world.add(1)
    world.add(2)
    parameters = set(inspect.signature(main.get_explorer).parameters)
    assert parameters == {
        "country", "city", "opportunity_type", "domain", "source", "freshness", "limit", "offset"
    }
    assert explore("?profile_id=999") == explore()


def test_order_is_last_seen_desc_then_id_desc(world):
    world.add(1, last_seen="2026-06-14T09:00:00+00:00")
    world.add(2, last_seen="2026-06-15T09:00:00+00:00")
    world.add(3, last_seen="2026-06-15T09:00:00+00:00")
    world.add(4, last_seen="2026-06-13T09:00:00+00:00")
    world.add(5, last_seen="2026-06-15T11:00:00+00:00")
    assert ids(explore()) == [5, 3, 2, 1, 4]


def test_no_score_influences_the_order(world):
    world.add(1, last_seen="2026-06-15T11:00:00+00:00", scores=(0.0, 0.0, 0.0, 0.0, 0.0))
    world.add(2, last_seen="2026-06-15T10:00:00+00:00", scores=(1.0, 1.0, 1.0, 1.0, 1.0))
    world.add(3, last_seen="2026-06-15T09:00:00+00:00", scores=(0.5, 0.9, 0.1, 0.8, 0.2))
    assert ids(explore()) == [1, 2, 3]


def test_total_is_independent_of_pagination_and_pages_are_contiguous(world):
    for opportunity_id in range(1, 8):
        world.add(opportunity_id, last_seen=f"2026-06-1{opportunity_id % 5}T10:00:00+00:00")
    full = ids(explore("?limit=50"))
    pages = [explore(f"?limit=3&offset={offset}") for offset in (0, 3, 6, 9)]
    assert all(page["total"] == 7 for page in pages)
    assert [page["returned"] for page in pages] == [3, 3, 1, 0]
    assert sum((ids(page) for page in pages), []) == full


# --------------------------------------------------------------------------
# country and city
# --------------------------------------------------------------------------


def test_country_matches_only_a_resolved_persisted_country(world):
    world.add(1, locations=[("RESOLVED", "MA", None)])
    world.add(2, locations=[("RESOLVED", "FR", None)])
    world.add(3, locations=[("UNKNOWN", None, None)])
    world.add(4, locations=[("AMBIGUOUS", None, None)])
    world.add(5, locations=[], location_text="Casablanca, Morocco")
    body = explore("?country=MA")
    assert ids(body) == [1] and body["total"] == 1


def test_unknown_and_ambiguous_rows_never_match_even_beside_a_resolved_one(world):
    world.add(1, locations=[("UNKNOWN", None, None), ("RESOLVED", "GB", None)])
    world.add(2, locations=[("AMBIGUOUS", None, None), ("UNKNOWN", None, None)])
    assert ids(explore("?country=GB")) == [1]


def test_city_matches_only_an_explicitly_persisted_city_key(world):
    world.add(1, locations=[("RESOLVED", "MA", "casablanca")])
    world.add(2, locations=[("RESOLVED", "MA", None)], location_text="casablanca")
    world.add(3, locations=[("RESOLVED", "MA", "rabat")])
    assert ids(explore("?city=casablanca")) == [1]


def test_country_and_city_must_match_the_same_resolution_row(world):
    # 1: MA from one row, casablanca only on a GB row — must not match MA+casablanca.
    world.add(1, locations=[("RESOLVED", "MA", None), ("RESOLVED", "GB", "casablanca")])
    world.add(2, locations=[("RESOLVED", "MA", "casablanca")])
    body = explore("?country=MA&city=casablanca")
    assert ids(body) == [2] and body["total"] == 1


def test_duplicate_location_rows_do_not_duplicate_the_posting(world):
    world.add(
        1,
        locations=[
            ("RESOLVED", "MA", "casablanca"),
            ("RESOLVED", "MA", "casablanca"),
            ("RESOLVED", "MA", None),
        ],
    )
    body = explore("?country=MA&city=casablanca")
    assert ids(body) == [1] and body["total"] == 1
    item = explore()["items"][0]
    assert item["resolved_locations"] == [
        {"country_code": "MA", "city_key": "casablanca"},
        {"country_code": "MA", "city_key": None},
    ]


def test_resolved_locations_and_unresolved_flag(world):
    world.add(1, locations=[("RESOLVED", "MA", "rabat"), ("RESOLVED", "FR", None)])
    world.add(2, locations=[("RESOLVED", "MA", None), ("UNKNOWN", None, None)])
    world.add(3, locations=[("AMBIGUOUS", None, None)])
    world.add(4, locations=[])
    items = {item["opportunity_id"]: item for item in explore()["items"]}
    assert items[1]["resolved_locations"] == [
        {"country_code": "MA", "city_key": "rabat"},
        {"country_code": "FR", "city_key": None},
    ]
    assert items[1]["has_unresolved_location"] is False
    assert items[2]["resolved_locations"] == [{"country_code": "MA", "city_key": None}]
    assert items[2]["has_unresolved_location"] is True
    assert items[3]["resolved_locations"] == [] and items[3]["has_unresolved_location"] is True
    assert items[4]["resolved_locations"] == [] and items[4]["has_unresolved_location"] is True
    assert items[4]["raw_location"] == "TEST ONLY place"


# --------------------------------------------------------------------------
# opportunity type, domain, source, freshness
# --------------------------------------------------------------------------


def test_opportunity_type_uses_the_structured_constraints_value(world):
    world.add(1, opportunity_type="INTERNSHIP")
    world.add(2, opportunity_type="JUNIOR_ROLE")
    world.add(3, opportunity_type=None)
    body = explore("?opportunity_type=INTERNSHIP")
    assert ids(body) == [1]
    items = {item["opportunity_id"]: item for item in explore()["items"]}
    assert items[1]["opportunity_type"] == "INTERNSHIP"
    assert items[3]["opportunity_type"] is None


def test_the_legacy_opportunity_type_is_never_a_fallback(world):
    world.add(1, opportunity_type=None, legacy_type="INTERNSHIP")
    world.add(2, constraints=False, legacy_type="INTERNSHIP")
    assert explore("?opportunity_type=INTERNSHIP")["total"] == 0
    items = {item["opportunity_id"]: item for item in explore()["items"]}
    assert items[1]["opportunity_type"] is None and items[2]["opportunity_type"] is None


def test_domain_matches_the_exact_persisted_fine_category(world):
    world.add(1, fine="DATA_ENGINEERING")
    world.add(2, fine="DATA_SCIENCE")
    world.add(3, qualification="ADJACENT_TARGET", fine="OTHER")
    assert ids(explore("?domain=DATA_ENGINEERING")) == [1]


def test_other_is_a_real_category_and_distinct_from_null(world):
    world.add(1, qualification="ADJACENT_TARGET", fine="OTHER")
    world.add(2, fine=UNCLASSIFIED)
    assert ids(explore("?domain=OTHER")) == [1]
    items = {item["opportunity_id"]: item for item in explore()["items"]}
    assert items[1]["fine_primary_category"] == "OTHER"
    assert items[2]["fine_primary_category"] is None


def test_source_matches_an_exact_source_id(world):
    world.add(1, sources=[("board_a", "greenhouse")])
    world.add(2, sources=[("board_b", "greenhouse")])
    world.add(3, sources=[("board_ab", "greenhouse")])
    assert ids(explore("?source=board_a")) == [1]


def test_a_multi_source_posting_appears_once_with_one_entry_per_source_id(world):
    world.add(
        1,
        sources=[
            ("zeta_alert", "gmail_linkedin_alert"),
            ("alpha_board", "greenhouse"),
            ("alpha_board", "greenhouse"),
        ],
    )
    body = explore()
    assert ids(body) == [1] and body["total"] == 1
    assert body["items"][0]["sources"] == [
        {"source_id": "alpha_board", "source_type": "greenhouse"},
        {"source_id": "zeta_alert", "source_type": "gmail_linkedin_alert"},
    ]
    for source in ("alpha_board", "zeta_alert"):
        filtered = explore(f"?source={source}")
        assert ids(filtered) == [1] and filtered["total"] == 1


@pytest.mark.parametrize(
    ("window", "expected"),
    [("24h", [1, 2]), ("7d", [1, 2, 3]), ("30d", [1, 2, 3, 4])],
)
def test_freshness_windows_use_last_seen_at_against_the_request_clock(world, window, expected):
    world.add(1, last_seen="2026-06-15T11:59:00+00:00")
    world.add(2, last_seen="2026-06-14T12:30:00+00:00")
    world.add(3, last_seen="2026-06-10T12:00:00+00:00")
    world.add(4, last_seen="2026-05-20T12:00:00+00:00")
    world.add(5, last_seen="2026-04-01T12:00:00+00:00")
    world.add(6, last_seen="not a timestamp")
    assert sorted(ids(explore(f"?freshness={window}"))) == expected


def test_an_unparsable_timestamp_stays_in_the_unfiltered_listing(world):
    world.add(1, last_seen="not a timestamp")
    world.add(2)
    assert sorted(ids(explore())) == [1, 2]
    assert explore("?freshness=30d")["total"] == 1


def test_unknown_dimensions_do_not_remove_a_posting_while_their_filter_is_absent(world):
    world.add(1, fine=UNCLASSIFIED, constraints=False, locations=[], sources=())
    world.add(2, opportunity_type=None, locations=[("UNKNOWN", None, None)])
    body = explore()
    assert sorted(ids(body)) == [1, 2]
    item = {entry["opportunity_id"]: entry for entry in body["items"]}[1]
    assert item["fine_primary_category"] is None
    assert item["opportunity_type"] is None
    assert item["resolved_locations"] == [] and item["has_unresolved_location"] is True
    assert item["sources"] == []
    for query in ("?country=MA", "?city=casablanca", "?opportunity_type=INTERNSHIP",
                  "?domain=OTHER", "?source=board_a"):
        assert 1 not in ids(explore(query))


def test_filters_combine_as_a_conjunction(world):
    world.add(1, opportunity_type="INTERNSHIP", fine="DATA_ENGINEERING", locations=[("RESOLVED", "MA", None)])
    world.add(2, opportunity_type="INTERNSHIP", fine="DATA_SCIENCE", locations=[("RESOLVED", "MA", None)])
    world.add(3, opportunity_type="JUNIOR_ROLE", fine="DATA_ENGINEERING", locations=[("RESOLVED", "MA", None)])
    body = explore("?country=MA&opportunity_type=INTERNSHIP&domain=DATA_ENGINEERING&source=board_a&freshness=24h")
    assert ids(body) == [1] and body["total"] == 1


def test_the_original_url_uses_the_existing_link_priority(world):
    world.add(
        1,
        sources=[
            ("linkedin_alert", "gmail_linkedin_alert", "https://board.example.invalid/1"),
            ("company_ats", "greenhouse", "https://ats.example.invalid/1"),
        ],
    )
    world.add(2, sources=[("linkedin_alert", "gmail_linkedin_alert", "https://board.example.invalid/2")])
    world.add(3, sources=())
    items = {item["opportunity_id"]: item for item in explore()["items"]}
    expected_1 = select_original_url(
        [
            SourceObservation(1, "gmail_linkedin_alert", None, "https://board.example.invalid/1", None),
            SourceObservation(2, "greenhouse", None, "https://ats.example.invalid/1", None),
        ],
        fallback="https://fallback.example.invalid/1",
    )
    assert items[1]["original_url"] == expected_1 == "https://ats.example.invalid/1"
    assert items[2]["original_url"] == "https://fallback.example.invalid/2"
    assert items[3]["original_url"] == preferred_link(None, "https://fallback.example.invalid/3", None)


# --------------------------------------------------------------------------
# available filters
# --------------------------------------------------------------------------


def test_available_filters_describe_the_whole_base_not_the_page_or_active_filters(world):
    world.add(1, last_seen="2026-06-15T11:00:00+00:00", opportunity_type="INTERNSHIP",
              fine="DATA_ENGINEERING", locations=[("RESOLVED", "MA", "casablanca")],
              sources=[("board_a", "greenhouse")])
    world.add(2, last_seen="2026-06-01T11:00:00+00:00", opportunity_type="JUNIOR_ROLE",
              qualification="ADJACENT_TARGET", fine="OTHER",
              locations=[("RESOLVED", "GB", "london"), ("RESOLVED", "FR", None)],
              sources=[("alert_b", "gmail_linkedin_alert")])
    # Outside the Explorer base: its values must not become options.
    world.add(3, qualification="OUT_OF_SCOPE", fine=UNCLASSIFIED, opportunity_type="PFE",
              locations=[("RESOLVED", "DE", "berlin")], sources=[("outside", "greenhouse")])
    world.add(4, status="hidden", fine="NLP", locations=[("RESOLVED", "US", None)])
    expected = {
        "countries": ["FR", "GB", "MA"],
        "cities": [
            {"country_code": "GB", "city_key": "london"},
            {"country_code": "MA", "city_key": "casablanca"},
        ],
        "opportunity_types": ["INTERNSHIP", "JUNIOR_ROLE"],
        "domains": ["DATA_ENGINEERING", "OTHER"],
        "sources": [
            {"source_id": "alert_b", "source_type": "gmail_linkedin_alert"},
            {"source_id": "board_a", "source_type": "greenhouse"},
        ],
    }
    assert explore()["available_filters"] == expected
    assert explore("?limit=1")["available_filters"] == expected
    narrowed = explore("?country=MA&domain=DATA_ENGINEERING&freshness=24h&offset=5")
    assert narrowed["returned"] == 0
    assert narrowed["available_filters"] == expected


def test_filter_options_exclude_null_and_unresolved_values(world):
    world.add(1, fine=UNCLASSIFIED, opportunity_type=None,
              locations=[("UNKNOWN", None, None), ("AMBIGUOUS", None, None)], sources=())
    world.add(2, constraints=False, locations=[("RESOLVED", "MA", None)])
    filters = explore()["available_filters"]
    assert filters["countries"] == ["MA"]
    assert filters["cities"] == []
    assert filters["opportunity_types"] == []
    assert filters["domains"] == ["DATA_SCIENCE"]
    assert filters["sources"] == [{"source_id": "board_a", "source_type": "greenhouse"}]


def test_other_is_offered_as_a_domain_option(world):
    world.add(1, qualification="ADJACENT_TARGET", fine="OTHER")
    assert explore()["available_filters"]["domains"] == ["OTHER"]


# --------------------------------------------------------------------------
# failure contract and read-only discipline
# --------------------------------------------------------------------------


SECRET = "TEST-ONLY-secret-path-and-sql"


def assert_public_failure(response):
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_EXPLORER_ERROR}
    assert SECRET not in response.text


def test_a_database_failure_is_a_503_with_only_the_public_sentence(world, monkeypatch):
    def broken(_path):
        raise sqlite3.OperationalError(f"unable to open {SECRET}")

    monkeypatch.setattr(explorer_api, "connect_readonly_database", broken)
    assert_public_failure(TestClient(main.app).get("/api/explorer"))


def test_a_missing_database_is_refused_and_never_created(world, monkeypatch, tmp_path):
    missing = tmp_path / "nowhere" / "absent.db"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(missing))
    assert_public_failure(TestClient(main.app).get("/api/explorer"))
    assert not missing.exists() and not missing.parent.exists()


def test_an_incoherent_persisted_fine_row_refuses_the_surface(world):
    world.add(1)
    world.connection.execute(
        "UPDATE opportunity_qualifications SET fine_primary_category = NULL WHERE opportunity_id = 1"
    )
    world.connection.commit()
    assert_public_failure(TestClient(main.app).get("/api/explorer"))


def test_a_non_sqlite_backend_is_refused(world, monkeypatch):
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://test-only.example.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", SECRET)
    assert_public_failure(TestClient(main.app).get("/api/explorer"))


def test_a_connection_that_does_not_confirm_query_only_is_refused(world, monkeypatch):
    real = explorer_api.connect_readonly_database
    statements = []

    class Unconfirmed:
        def __init__(self, connection):
            self.connection = connection
            self.in_transaction = False

        def execute(self, statement, *parameters):
            statements.append(statement)
            if statement.strip() == "PRAGMA query_only":
                return self.connection.execute("SELECT 0")
            return self.connection.execute(statement, *parameters)

        def close(self):
            self.connection.close()

    monkeypatch.setattr(explorer_api, "connect_readonly_database", lambda path: Unconfirmed(real(path)))
    assert_public_failure(TestClient(main.app).get("/api/explorer"))
    assert statements == ["PRAGMA query_only = ON", "PRAGMA query_only"]


def module_tree():
    return ast.parse(Path(explorer_api.__file__).read_text(encoding="utf-8"))


def test_the_explorer_imports_no_computing_or_personalized_surface():
    imported = set()
    for node in ast.walk(module_tree()):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = (
        "services.recommendation", "services.api.recommendation",
        "services.collector.matching", "services.api.matching",
        "services.priority", "services.api.priority",
        "services.portfolio", "services.api.portfolio",
        "services.eligibility", "services.geography",
        "services.collector.extractors", "services.collector.qualification.fine_classifier",
        "services.collector.qualification.classifier",
    )
    assert not [name for name in imported if name.startswith(forbidden)], imported
    assert "services.collector.database.connection" in imported


def test_the_explorer_opens_sqlite_only_through_the_read_only_helper():
    source = Path(explorer_api.__file__).read_text(encoding="utf-8")
    assert "connect_readonly_database(" in source
    assert "connect_configured_database" not in source
    assert "connect_database(" not in source.replace("connect_readonly_database(", "")
    assert "sqlite3.connect" not in source


def test_no_statement_in_the_explorer_writes_or_ranks_by_a_score():
    literals = [
        node.value.upper()
        for node in ast.walk(module_tree())
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    sql = " ".join(literals)
    for keyword in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ", "CREATE ", "DROP ", "ALTER ", "COMMIT"):
        assert keyword not in sql
    for score in ("RELEVANCE_SCORE", "ELIGIBILITY_SCORE", "MATCH_SCORE", "PRIORITY_SCORE",
                  "INTERVIEW_POTENTIAL_SCORE", "RECOMMENDATION_SCORE", "PROFILE_ID"):
        assert score not in sql
