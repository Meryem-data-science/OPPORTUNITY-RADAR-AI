from dataclasses import replace
from datetime import date
import hashlib
import pathlib

from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.database.migrations import apply_migrations
from services.collector.matching import (
    MATCHING_SELECTION_VERSION,
    MatchingBatchResult,
    matching_assessment_fingerprint,
    matching_batch_fingerprint,
    store_matching_batch,
)
from services.collector.qualification.persistence import persist_qualifications
from services.digital_twin.repository import ensure_user_profile
from services.eligibility import ELIGIBILITY_ENGINE_VERSION
from services.priority.dry_run import run_priority_dry_run
from services.priority.input_assembly import PriorityReadinessStatus
from tests.unit.test_matching_engine import assessment


def _batch(profile_id, opportunity_id):
    original = assessment()
    raw = replace(
        original,
        profile_id=profile_id,
        opportunity_id=opportunity_id,
        semantic=replace(
            original.semantic, corpus_fingerprint="a" * 64, model_fingerprint="b" * 64
        ),
        assessment_fingerprint="",
    )
    item = replace(raw, assessment_fingerprint=matching_assessment_fingerprint(raw))
    batch = MatchingBatchResult((item,), "a" * 64, "b" * 64, 1)
    return replace(batch, batch_fingerprint=matching_batch_fingerprint(batch))


def test_real_file_dry_run_is_read_only_and_resolves_profile_owner(tmp_path):
    path = tmp_path / "priority.db"
    connection = connect_database(path)
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO users(id,email) VALUES (41,'dummy@example.invalid')"
    )
    connection.commit()
    identity = ensure_user_profile(connection, "owner@example.invalid")
    opportunity_id = connection.execute(
        """INSERT INTO opportunities
        (canonical_title,organization,published_at,deadline,discovered_at,first_seen_at,last_seen_at,source_url,status)
        VALUES ('Data Engineer Internship','Org',NULL,NULL,'2099-01-01','2099-01-01','2099-01-01','https://example.invalid/real','new') RETURNING id"""
    ).fetchone()[0]
    connection.commit()
    persist_qualifications(connection)
    connection.execute(
        """INSERT INTO opportunity_eligibilities
        (user_id,opportunity_id,status,engine_version,input_fingerprint,satisfied_count,
         violated_count,unknown_count,not_applicable_count,not_evaluated_count,
         blocking_unknown_count,evaluated_at) VALUES (?,?,?,?,?,0,0,1,0,0,1,?)""",
        (
            identity.user_id,
            opportunity_id,
            "UNKNOWN",
            ELIGIBILITY_ENGINE_VERSION,
            "e" * 64,
            "2026-09-01",
        ),
    )
    connection.commit()
    stored = store_matching_batch(
        connection,
        identity.profile_id,
        _batch(identity.profile_id, opportunity_id),
        selection_version=MATCHING_SELECTION_VERSION,
    )
    connection.close()
    assert identity.profile_id != identity.user_id
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with connect_readonly_database(path) as readonly:
        report = run_priority_dry_run(readonly, identity.profile_id, date(2026, 9, 2))
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert report.status is PriorityReadinessStatus.READY
    assert (
        report.user_id == identity.user_id and report.matching_run_id == stored.run_id
    )
    assert report.assessment_count == 1
    assert dict(report.eligibility_counts)["UNKNOWN"] == 1
    assert dict(report.freshness_counts) == {"available": 0, "missing": 1}
    assert report.lines[0].source_url == "https://example.invalid/real"
    assert before == after


def test_phase_5_1b_modules_have_no_sql_write_statements():
    for name in ("input_assembly.py", "dry_run.py", "dry_run_cli.py"):
        source = (pathlib.Path("services/priority") / name).read_text().upper()
        assert not any(
            token in source
            for token in (
                "INSERT ",
                "UPDATE ",
                "DELETE ",
                "REPLACE ",
                "CREATE TABLE",
                "ALTER TABLE",
                "DROP TABLE",
            )
        )
