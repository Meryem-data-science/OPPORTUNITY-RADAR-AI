"""Safety contract for confirmed duplicate merge planning and mutation."""

import sqlite3

import pytest

from services.collector.database.migrations import apply_migrations
from services.collector.deduplication.merges import (
    FILL_ONLY_FIELDS, MERGED_DUPLICATE_STATUS, MergeError, apply_merge,
    preflight_merge, preflight_rollback, rollback_merge,
)


@pytest.fixture
def db(tmp_path):
    connection = sqlite3.connect(tmp_path / "merge.db")
    connection.execute("PRAGMA foreign_keys=ON")
    apply_migrations(connection)
    connection.executemany("INSERT INTO sources(id,type,status) VALUES (?,'test','active')", [("a",), ("b",), ("c",)])
    opportunities = [
        (1, "Canonical title", "Canonical org", None, "canonical description", "https://a/1"),
        (2, "Merged title", "Merged org", "Casablanca", "merged description", "https://b/2"),
        (3, "Third title", "Third org", "Rabat", None, "https://c/3"),
    ]
    connection.executemany("""INSERT INTO opportunities
      (id,canonical_title,organization,location,description,discovered_at,first_seen_at,last_seen_at,source_url,status)
      VALUES (?,?,?,?,?,'2026-01-01','2026-01-01','2026-01-01',?,'visible')""", opportunities)
    connection.executemany("""INSERT INTO opportunity_sources
      (id,opportunity_id,source_id,source_url,application_url,canonical_url,discovered_at)
      VALUES (?,?,?,?,?,?,'2026-01-01')""", [
        (10,1,"a","https://a/1","https://a/apply","https://a/canonical"),
        (20,2,"b","https://b/2","https://b/apply","https://b/canonical"),
        (30,3,"c","https://c/3",None,None),
    ])
    connection.commit()
    yield connection
    connection.close()


def decision(db, a=1, b=2, status="CONFIRMED_DUPLICATE"):
    db.execute("""INSERT INTO deduplication_decisions
      (opportunity_a_id,opportunity_b_id,status,audit_classification,title_similarity,
       organization_similarity,title_normalized_exact,organization_normalized_exact,
       location_signal,shared_source_url,shared_application_url,shared_canonical_url,
       reasons_json,first_detected_at,last_detected_at)
      VALUES (?,?,?,'STRONG_CANDIDATE',1,1,1,1,'MATCH',0,0,0,'[]','2026-01-01','2026-01-01')""", (a,b,status))
    db.commit()


@pytest.mark.parametrize("status", ["POSSIBLE_DUPLICATE", "NOT_DUPLICATE"])
def test_only_confirmed_decision_is_mergeable(db, status):
    decision(db, status=status)
    plan = preflight_merge(db, 1, 2, 1)
    assert not plan.merge_allowed and plan.error_code == "DECISION_NOT_CONFIRMED"


def test_missing_decision_and_canonical_outside_pair_are_refused(db):
    assert preflight_merge(db, 1, 2, 1).error_code == "DECISION_NOT_FOUND"
    decision(db)
    assert preflight_merge(db, 1, 2, 3).error_code == "CANONICAL_OUTSIDE_PAIR"


def test_plan_and_apply_preserve_canonical_and_move_exact_source_row(db):
    decision(db)
    before = db.total_changes
    plan = preflight_merge(db, 1, 2, 1)
    assert db.total_changes == before
    assert plan.merge_allowed and plan.source_moves_count == 1
    assert plan.sources_to_move[0]["id"] == 20
    assert plan.fields_to_fill == ("location",)
    result = apply_merge(db, 1, 2, 1, clock=lambda: "2026-02-01")
    assert result["result"] == "APPLIED"
    assert db.execute("SELECT opportunity_id,source_url,application_url,canonical_url,discovered_at FROM opportunity_sources WHERE id=20").fetchone() == (1,"https://b/2","https://b/apply","https://b/canonical","2026-01-01")
    assert db.execute("SELECT canonical_title,organization,location,description,source_url,is_active FROM opportunities WHERE id=1").fetchone() == ("Canonical title","Canonical org","Casablanca","canonical description","https://a/1",1)
    assert db.execute("SELECT status,is_active FROM opportunities WHERE id=2").fetchone() == (MERGED_DUPLICATE_STATUS,0)
    assert db.execute("SELECT opportunity_source_id FROM deduplication_merge_source_moves").fetchone() == (20,)
    assert preflight_merge(db, 1, 2, 1).error_code == "ALREADY_APPLIED"
    assert preflight_merge(db, 1, 2, 2).error_code == "CANONICAL_DIRECTION_CONFLICT"


def test_exact_observation_conflict_refuses_merge(db):
    decision(db)
    db.execute("UPDATE opportunity_sources SET source_id='a',source_url='https://a/1' WHERE id=20")
    db.commit()
    plan = preflight_merge(db, 1, 2, 1)
    assert not plan.merge_allowed and plan.error_code == "OBSERVATION_CONFLICT"
    with pytest.raises(MergeError, match="OBSERVATION_CONFLICT"):
        apply_merge(db, 1, 2, 1)
    assert db.execute("SELECT opportunity_id FROM opportunity_sources WHERE id=20").fetchone() == (2,)


def test_merge_failure_is_atomic(db):
    decision(db)
    def fail():
        raise RuntimeError("injected")
    with pytest.raises(RuntimeError, match="injected"):
        apply_merge(db, 1, 2, 1, after_move=fail)
    assert db.execute("SELECT opportunity_id FROM opportunity_sources WHERE id=20").fetchone() == (2,)
    assert db.execute("SELECT COUNT(*) FROM deduplication_merges").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM deduplication_merge_source_moves").fetchone() == (0,)


def test_rollback_restores_sources_fill_and_tombstone_but_not_decision(db):
    decision(db)
    merge_id = apply_merge(db,1,2,1)["merge_id"]
    plan = preflight_rollback(db, merge_id)
    assert plan.rollback_allowed and plan.fields_to_restore == ("location",)
    rollback_merge(db, merge_id, clock=lambda: "2026-03-01")
    assert db.execute("SELECT opportunity_id FROM opportunity_sources WHERE id=20").fetchone() == (2,)
    assert db.execute("SELECT location FROM opportunities WHERE id=1").fetchone() == (None,)
    assert db.execute("SELECT status,is_active FROM opportunities WHERE id=2").fetchone() == ("visible",1)
    assert db.execute("SELECT status FROM deduplication_merges WHERE id=?",(merge_id,)).fetchone() == ("ROLLED_BACK",)
    assert db.execute("SELECT status FROM deduplication_decisions").fetchone() == ("CONFIRMED_DUPLICATE",)
    assert db.execute("SELECT COUNT(*) FROM deduplication_merge_source_moves").fetchone() == (1,)
    assert apply_merge(db,1,2,1)["result"] == "APPLIED"


def test_rollback_data_drift_and_lifo_are_refused(db):
    decision(db)
    first = apply_merge(db,1,2,1)["merge_id"]
    db.execute("UPDATE opportunities SET location='Rabat' WHERE id=1")
    assert preflight_rollback(db, first).error_code == "ROLLBACK_DATA_DRIFT"
    db.execute("UPDATE opportunities SET location='Casablanca' WHERE id=1")
    decision(db,1,3)
    second = apply_merge(db,1,3,1)["merge_id"]
    assert preflight_rollback(db, first).error_code == "ROLLBACK_NOT_LIFO"
    assert preflight_rollback(db, second).rollback_allowed


def test_fill_only_field_contract_is_explicit():
    assert FILL_ONLY_FIELDS == ("opportunity_type","employment_type","location","country","remote_type","description","published_at","deadline","application_url","canonical_url")
