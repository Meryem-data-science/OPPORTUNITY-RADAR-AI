"""Post-merge persistence integration: secondary observations cannot flap canonical fields."""

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.opportunities import persist_opportunities
from services.collector.deduplication.merges import apply_merge
from tests.unit.test_opportunity_persistence import candidate, source


def test_secondary_refresh_updates_observation_not_canonical_and_primary_still_refreshes(tmp_path):
    db = connect_database(tmp_path / "integration.db")
    apply_migrations(db)
    source_a = source()
    source_b = source()
    # Persist two observations using distinct source configurations.
    persist_opportunities(db, source_a, [candidate()])
    from dataclasses import replace
    source_b = replace(source_a, id="secondary")
    secondary = candidate(source_id="secondary", source_url="https://secondary/2", canonical_title="Secondary title", organization="Secondary org", location="Secondary city", description="Secondary description", application_url="https://secondary/apply", canonical_url="https://secondary/canonical")
    persist_opportunities(db, source_b, [secondary])
    ids = [row[0] for row in db.execute("SELECT id FROM opportunities ORDER BY id")]
    db.execute("""INSERT INTO deduplication_decisions
      (opportunity_a_id,opportunity_b_id,status,audit_classification,title_similarity,organization_similarity,title_normalized_exact,organization_normalized_exact,location_signal,shared_source_url,shared_application_url,shared_canonical_url,reasons_json,first_detected_at,last_detected_at)
      VALUES (?,?,'CONFIRMED_DUPLICATE','STRONG_CANDIDATE',1,1,1,1,'MATCH',0,0,0,'[]','2026-01-01','2026-01-01')""", ids)
    db.commit()
    apply_merge(db, ids[0], ids[1], ids[0])
    secondary_refresh = replace(secondary, canonical_title="FLAP", organization="FLAP", location="FLAP", description="FLAP", application_url="https://secondary/new-apply")
    summary = persist_opportunities(db, source_b, [secondary_refresh], clock=lambda: "2026-04-01")
    assert (summary.created, summary.updated) == (0,1)
    assert db.execute("SELECT canonical_title,organization,location,description,last_seen_at,is_active FROM opportunities WHERE id=?",(ids[0],)).fetchone() == ("TEST ONLY Engineer","TEST ONLY Org","Test City","TEST ONLY description","2026-04-01",1)
    assert db.execute("SELECT application_url FROM opportunity_sources WHERE source_id='secondary'").fetchone() == ("https://secondary/new-apply",)
    primary_refresh = replace(candidate(), canonical_title="Primary updated", organization="Primary updated org", location="Primary city")
    summary = persist_opportunities(db, source_a, [primary_refresh], clock=lambda: "2026-05-01")
    assert (summary.created, summary.updated) == (0,1)
    assert db.execute("SELECT canonical_title,organization,location,last_seen_at FROM opportunities WHERE id=?",(ids[0],)).fetchone() == ("Primary updated","Primary updated org","Primary city","2026-05-01")
    assert db.execute("SELECT COUNT(*) FROM opportunities").fetchone() == (2,)
    db.close()
