"""Phase 10.1 extraction, against the project's own schema on disposable SQLite.

Every database here is created under `tmp_path` from the real migrations in
`migrations/`, and thrown away. **The operational database is never opened**:
nothing in this module reads or writes anything a person uses, every posting is
invented, every URL is `.invalid`, and the profile is a `.invalid` address.

Nothing is faked out either. A hand-rolled mini-schema would let the extraction
agree with tables this file invented rather than with the ones it will actually
be pointed at, so the schema is the project's, up to `0026`, with its CHECK
constraints intact — which is what makes the fixture's `UNCERTAIN` qualification,
`UNKNOWN` eligibility and unscored matching lane real states rather than
plausible-looking strings.

The fixture is deliberately small and is built around one question: **can this
dataset see what the pipeline dropped?** Seven postings go in, three come out,
and each of the four exclusions and each of the two false-negative shapes is
represented exactly once.
"""

import hashlib
import json
import sqlite3

import pytest

from evaluation.dataset import (
    EVALUATION_DATASET_SCHEMA_VERSION,
    build_evaluation_dataset,
    evaluation_record_payload,
    select_evaluation_cohort_ids,
    write_evaluation_dataset,
)
from evaluation.dataset.cli import main as evaluation_cli
from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.database.migrations import apply_migrations
from services.digital_twin.repository import ensure_user_profile

FINGERPRINT = "0" * 64
WHEN = "2026-01-01T00:00:00+00:00"

#: The seven postings. Ids are explicit so the assertions below can name them.
#:
#:   1  CORE_TARGET, fully processed          -> in the cohort, recommended
#:   2  UNCERTAIN, never matched              -> in the cohort, invisible to
#:                                               Matching and everything after it
#:   3  ADJACENT_TARGET, matched, not ranked  -> in the cohort, dropped between
#:                                               Matching and Recommendation
#:   4  OUT_OF_SCOPE                          -> excluded: asserted negative
#:   5  CORE_TARGET but inactive              -> excluded
#:   6  CORE_TARGET but merged_duplicate      -> excluded, absorbed by 1
#:   7  active with no qualification row      -> excluded, and counted
COHORT_IDS = (1, 2, 3)


def _insert_opportunity(
    connection: sqlite3.Connection,
    opportunity_id: int,
    *,
    title: str,
    status: str = "active",
    is_active: int = 1,
) -> None:
    connection.execute(
        """INSERT INTO opportunities
               (id, canonical_title, organization, opportunity_type, location,
                country, remote_type, source_url, canonical_url, status,
                is_active, published_at, discovered_at, first_seen_at,
                last_seen_at)
           VALUES (?, ?, 'Example Org', 'PFE', 'Casablanca, Maroc', 'MA',
                   'ONSITE', ?, ?, ?, ?, NULL, ?, ?, ?)""",
        (
            opportunity_id,
            title,
            f"https://example.invalid/offers/{opportunity_id}",
            f"https://example.invalid/offers/{opportunity_id}",
            status,
            is_active,
            WHEN,
            WHEN,
            WHEN,
        ),
    )
    connection.execute(
        """INSERT INTO opportunity_sources
               (opportunity_id, source_id, source_url, discovered_at)
           VALUES (?, 'example-source', ?, ?)""",
        (
            opportunity_id,
            f"https://example.invalid/offers/{opportunity_id}",
            WHEN,
        ),
    )


def _insert_qualification(
    connection: sqlite3.Connection,
    opportunity_id: int,
    qualification: str,
    *,
    fine: bool = True,
) -> None:
    """One current qualification row, with or without its fine half.

    `fine=False` is the state migration `0025` describes as "migrated, never
    fine-classified": all five fine columns NULL. It is in the fixture so the
    extraction is checked against a real absent upstream rather than only
    against a present one.
    """
    connection.execute(
        """INSERT INTO opportunity_qualifications
               (opportunity_id, qualification, primary_domain, opportunity_type,
                employment_type, listing_quality, matched_domains_json,
                matched_title_signals_json, matched_description_signals_json,
                matched_exclusion_signals_json, reasons_json, classifier_version,
                input_fingerprint, classified_at, fine_primary_category,
                fine_secondary_categories_json, fine_category_evidence_json,
                fine_reasons_json, fine_classifier_version)
           VALUES (?, ?, ?, 'PFE', 'UNKNOWN', 'NORMAL_LISTING', '[]', '[]', '[]',
                   '[]', '[]', 'qualification-rules-v2', ?, ?, ?, ?, ?, ?, ?)""",
        (
            opportunity_id,
            qualification,
            "DATA_ENGINEERING" if qualification != "OUT_OF_SCOPE" else "NON_TARGET",
            FINGERPRINT,
            WHEN,
            "DATA_ENGINEERING" if fine else None,
            "[]" if fine else None,
            "[]" if fine else None,
            '["fine rule"]' if fine else None,
            "fine-data-ai-rules-v2" if fine else None,
        ),
    )


def _insert_location(
    connection: sqlite3.Connection,
    opportunity_id: int,
    *,
    raw_segment: str,
    resolution_status: str,
    country_code: str | None,
) -> None:
    connection.execute(
        """INSERT INTO opportunity_constraints
               (opportunity_id, extractor_version, source_fingerprint, extracted_at)
           VALUES (?, 'opportunity-constraints-v1', ?, ?)""",
        (opportunity_id, FINGERPRINT, WHEN),
    )
    location_id = connection.execute(
        """INSERT INTO opportunity_constraint_locations
               (opportunity_id, position, location_text)
           VALUES (?, 0, ?) RETURNING id""",
        (opportunity_id, raw_segment),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO opportunity_location_resolutions
               (opportunity_id, source_location_id, segment_position, raw_segment,
                country_code, city_key, resolution_status, resolution_rule_id,
                resolver_version, source_fingerprint, resolved_at)
           VALUES (?, ?, 0, ?, ?, NULL, ?, 'rule-1', 'geographic-v1', ?, ?)""",
        (
            opportunity_id,
            location_id,
            raw_segment,
            country_code,
            resolution_status,
            FINGERPRINT,
            WHEN,
        ),
    )


def _insert_eligibility(
    connection: sqlite3.Connection,
    user_id: int,
    opportunity_id: int,
    status: str,
    *,
    violated: int = 0,
    blocking_unknown: int = 0,
) -> None:
    connection.execute(
        """INSERT INTO opportunity_eligibilities
               (user_id, opportunity_id, status, engine_version, input_fingerprint,
                satisfied_count, violated_count, unknown_count,
                not_applicable_count, not_evaluated_count, blocking_unknown_count,
                evaluated_at)
           VALUES (?, ?, ?, 'eligibility-engine-v1', ?, 1, ?, ?, 0, 0, ?, ?)""",
        (
            user_id,
            opportunity_id,
            status,
            FINGERPRINT,
            violated,
            blocking_unknown,
            blocking_unknown,
            WHEN,
        ),
    )


def _insert_matching_run(connection: sqlite3.Connection, profile_id: int) -> int:
    run_id = connection.execute(
        """INSERT INTO matching_runs
               (profile_id, persistence_version, selection_version,
                matching_engine_version, matching_rules_version,
                semantic_percentile_version, corpus_fingerprint,
                tfidf_model_fingerprint, batch_fingerprint, run_fingerprint,
                assessment_count, batch_payload_json)
           VALUES (?, 'matching-persistence-v1', 'matching-selection-v1',
                   'matching-engine-v1', 'matching-rules-v1',
                   'semantic-percentile-v1', ?, ?, ?, ?, 2, '{}')
           RETURNING id""",
        (profile_id, FINGERPRINT, FINGERPRINT, "1" * 64, "2" * 64),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO matching_assessments
               (run_id, opportunity_id, lane, match_quality, evidence_coverage,
                assessment_fingerprint, assessment_payload_json)
           VALUES (?, 1, 'PRIMARY', 0.8, 1.0, ?, '{}')""",
        (run_id, "3" * 64),
    )
    # Assessed, and measured against nothing: `evidence_coverage = 0.0` with a
    # NULL quality is Phase 4's own "no evidence means no score".
    connection.execute(
        """INSERT INTO matching_assessments
               (run_id, opportunity_id, lane, match_quality, evidence_coverage,
                assessment_fingerprint, assessment_payload_json)
           VALUES (?, 3, 'UNCERTAIN', NULL, 0.0, ?, '{}')""",
        (run_id, "4" * 64),
    )
    connection.execute(
        """INSERT INTO matching_profile_state
               (profile_id, state, current_run_id, persistence_version,
                selection_version)
           VALUES (?, 'READY', ?, 'matching-persistence-v1',
                   'matching-selection-v1')""",
        (profile_id, run_id),
    )
    return run_id


def _insert_recommendation_run(
    connection: sqlite3.Connection, profile_id: int, matching_run_id: int
) -> int:
    """One ranked posting out of the two the matching run assessed.

    Posting 3 is deliberately left out: it is the false negative between
    Matching and Recommendation, and it is invisible from the recommendation
    output alone.
    """
    run_id = connection.execute(
        """INSERT INTO recommendation_runs
               (profile_id, source_matching_run_id, persistence_version,
                input_assembly_version, recommendation_engine_version,
                recommendation_rules_version, source_matching_run_fingerprint,
                batch_fingerprint, run_fingerprint, assessment_count,
                batch_payload_json)
           VALUES (?, ?, 'recommendation-persistence-v1',
                   'recommendation-input-assembly-v1', 'recommendation-engine-v1',
                   'recommendation-rules-v1', ?, ?, ?, 1, '{}')
           RETURNING id""",
        (profile_id, matching_run_id, "2" * 64, "5" * 64, "6" * 64),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO recommendation_assessments
               (run_id, opportunity_id, rank_position, disposition,
                recommendation_score, evidence_coverage, assessment_fingerprint,
                assessment_payload_json)
           VALUES (?, 1, 1, 'RECOMMENDED', 0.9, 1.0, ?, '{}')""",
        (run_id, "7" * 64),
    )
    connection.execute(
        """INSERT INTO recommendation_profile_state
               (profile_id, state, current_run_id, persistence_version,
                input_assembly_version, readiness_issues_json)
           VALUES (?, 'READY', ?, 'recommendation-persistence-v1',
                   'recommendation-input-assembly-v1', '[]')""",
        (profile_id, run_id),
    )
    return run_id


@pytest.fixture()
def operational_database(tmp_path):
    """A disposable database holding the seven-posting fixture."""
    path = tmp_path / "radar.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection)
        identity = ensure_user_profile(connection, "tester@example.invalid")
        connection.execute(
            """INSERT INTO sources (id, type, status)
               VALUES ('example-source', 'board', 'active')"""
        )
        _insert_opportunity(connection, 1, title="Data Engineering PFE")
        _insert_opportunity(connection, 2, title="Ambiguous Data Role")
        _insert_opportunity(connection, 3, title="Analytics Internship")
        _insert_opportunity(connection, 4, title="Warehouse Operative")
        _insert_opportunity(connection, 5, title="Expired Data PFE", is_active=0)
        _insert_opportunity(
            connection, 6, title="Duplicate Data PFE",
            status="merged_duplicate", is_active=0,
        )
        _insert_opportunity(connection, 7, title="Unclassified Posting")
        _insert_qualification(connection, 1, "CORE_TARGET")
        _insert_qualification(connection, 2, "UNCERTAIN")
        _insert_qualification(connection, 3, "ADJACENT_TARGET", fine=False)
        _insert_qualification(connection, 4, "OUT_OF_SCOPE")
        _insert_qualification(connection, 5, "CORE_TARGET")
        _insert_qualification(connection, 6, "CORE_TARGET")
        _insert_location(
            connection, 1, raw_segment="Casablanca",
            resolution_status="RESOLVED", country_code="MA",
        )
        # No country any closed rule recognises: the resolver's UNKNOWN, which
        # must not be read anywhere as "somewhere else".
        _insert_location(
            connection, 2, raw_segment="Quelque part",
            resolution_status="UNKNOWN", country_code=None,
        )
        _insert_eligibility(connection, identity.user_id, 1, "ELIGIBLE")
        _insert_eligibility(
            connection, identity.user_id, 2, "UNKNOWN", blocking_unknown=1
        )
        decision_id = connection.execute(
            """INSERT INTO deduplication_decisions
                   (opportunity_a_id, opportunity_b_id, status,
                    audit_classification, title_similarity,
                    organization_similarity, title_normalized_exact,
                    organization_normalized_exact, location_signal,
                    shared_source_url, shared_application_url,
                    shared_canonical_url, reasons_json, first_detected_at,
                    last_detected_at, reviewed_at)
               VALUES (1, 6, 'CONFIRMED_DUPLICATE', 'STRONG_CANDIDATE', 1.0, 1.0,
                       1, 1, 'SAME', 0, 0, 0, '[]', ?, ?, ?) RETURNING id""",
            (WHEN, WHEN, WHEN),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO deduplication_merges
                   (decision_id, canonical_opportunity_id, merged_opportunity_id,
                    status, canonical_before_json, canonical_after_json,
                    merged_before_json, fields_filled_json, applied_at)
               VALUES (?, 1, 6, 'APPLIED', '{}', '{}', '{}', '[]', ?)""",
            (decision_id, WHEN),
        )
        matching_run_id = _insert_matching_run(connection, identity.profile_id)
        _insert_recommendation_run(
            connection, identity.profile_id, matching_run_id
        )
        connection.commit()
    finally:
        connection.close()
    return path, identity


@pytest.fixture()
def dataset(operational_database):
    """The dataset the fixture produces, read through a `mode=ro` connection."""
    path, identity = operational_database
    with connect_readonly_database(path) as connection:
        return build_evaluation_dataset(
            connection,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at=WHEN,
        )


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump(path) -> str:
    with connect_readonly_database(path) as connection:
        return "\n".join(connection.iterdump())


# --------------------------------------------------------------------------
# the cohort
# --------------------------------------------------------------------------


def test_the_cohort_is_the_upstream_universe_not_the_recommendations(dataset):
    """Three postings in, and only one of them was ever recommended.

    This is the property the whole slice exists for. A dataset built from the
    current recommendation run would hold posting 1 alone; posting 3 was dropped
    between Matching and Recommendation and posting 2 never reached Matching at
    all, so both classes of false negative would be invisible by construction.
    """
    assert [record.opportunity_id for record in dataset.records] == list(COHORT_IDS)
    recommended = [
        record.opportunity_id
        for record in dataset.records
        if record.recommendation is not None
    ]
    assert recommended == [1]
    by_id = {record.opportunity_id: record for record in dataset.records}
    # Assessed by Matching, never ranked: dropped downstream of selection.
    assert by_id[3].matching is not None
    assert by_id[3].recommendation is None
    # Never assessed at all: dropped at the qualification gate.
    assert by_id[2].matching is None
    assert by_id[2].recommendation is None


def test_the_cohort_excludes_what_the_contract_says_it_excludes(
    operational_database, dataset
):
    path, _ = operational_database
    with connect_readonly_database(path) as connection:
        assert select_evaluation_cohort_ids(connection) == COHORT_IDS
    assert dataset.manifest.cohort.excluded_counts == {
        "merged_duplicate": 1,
        "inactive": 1,
        "out_of_scope": 1,
        "unclassified": 1,
    }


def test_an_unclassified_posting_is_excluded_but_never_silently(dataset):
    """A stale qualification upstream is a number in the manifest, not a shrug."""
    assert 7 not in {record.opportunity_id for record in dataset.records}
    assert dataset.manifest.cohort.excluded_counts["unclassified"] == 1


# --------------------------------------------------------------------------
# UNKNOWN survives the extraction
# --------------------------------------------------------------------------


def test_every_unknown_state_reaches_the_dataset_unchanged(dataset):
    """Four different UNKNOWNs, none of them rewritten into a negative."""
    by_id = {record.opportunity_id: record for record in dataset.records}
    assert by_id[2].qualification.qualification == "UNCERTAIN"
    assert by_id[2].eligibility.status == "UNKNOWN"
    assert by_id[2].eligibility.violated_count == 0
    segment = by_id[2].geography_segments[0]
    assert segment.status == "UNKNOWN"
    assert segment.country_code is None
    assert by_id[3].matching.lane == "UNCERTAIN"
    assert by_id[3].matching.match_quality is None
    assert by_id[3].matching.evidence_coverage == 0.0


def test_an_absent_upstream_reads_as_absent_rather_than_as_a_value(dataset):
    """Nothing is invented where the pipeline produced nothing."""
    by_id = {record.opportunity_id: record for record in dataset.records}
    # Never fine-classified: `0025` case A, all five columns NULL.
    assert by_id[3].qualification.fine_classifier_version is None
    assert by_id[3].qualification.fine_primary_category is None
    assert by_id[3].qualification.fine_secondary_categories is None
    # Classified, and matched no secondary category: `[]`, not NULL.
    assert by_id[1].qualification.fine_secondary_categories == ()
    # No eligibility decision was ever stored for posting 3.
    assert by_id[3].eligibility is None
    # No location was ever resolved for posting 3.
    assert by_id[3].geography_segments == ()
    assert by_id[1].published_at is None
    assert by_id[1].application_url is None


def test_the_canonical_posting_reports_what_it_absorbed(dataset):
    by_id = {record.opportunity_id: record for record in dataset.records}
    assert by_id[1].absorbed_duplicate_ids == (6,)
    assert by_id[2].absorbed_duplicate_ids == ()


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def test_two_extractions_over_one_database_agree_completely(operational_database):
    """Same records, same order, same fingerprint, same dataset id."""
    path, identity = operational_database
    built = []
    for generated_at in (WHEN, "2027-06-15T12:34:56+00:00"):
        with connect_readonly_database(path) as connection:
            built.append(
                build_evaluation_dataset(
                    connection,
                    profile_id=identity.profile_id,
                    database_path=path,
                    generated_at=generated_at,
                    git_commit=None if generated_at == WHEN else "a" * 40,
                )
            )
    first, second = built
    assert first.records == second.records
    assert (
        first.manifest.content_fingerprint == second.manifest.content_fingerprint
    )
    assert first.manifest.dataset_id == second.manifest.dataset_id
    # ...and the volatile metadata that differed is genuinely in the manifest,
    # so the equality above is a statement about the digest, not about two
    # identical manifests.
    assert first.manifest.generated_at != second.manifest.generated_at
    assert first.manifest.git_commit != second.manifest.git_commit


def test_a_business_change_moves_the_dataset_fingerprint(operational_database):
    path, identity = operational_database
    before = None
    for title in ("Data Engineering PFE", "Data Engineering PFE (updated)"):
        connection = connect_database(path)
        try:
            connection.execute(
                "UPDATE opportunities SET canonical_title = ? WHERE id = 1",
                (title,),
            )
            connection.commit()
        finally:
            connection.close()
        with connect_readonly_database(path) as read_only:
            dataset = build_evaluation_dataset(
                read_only,
                profile_id=identity.profile_id,
                database_path=path,
                generated_at=WHEN,
            )
        if before is None:
            before = dataset.manifest.content_fingerprint
        else:
            assert dataset.manifest.content_fingerprint != before
            assert dataset.manifest.dataset_id != (
                f"{EVALUATION_DATASET_SCHEMA_VERSION}-{before[:16]}"
            )


def test_the_written_files_are_byte_identical_across_extractions(
    operational_database, tmp_path
):
    """Reproducibility a reader can check with `diff`, not only with a digest."""
    path, identity = operational_database
    root = tmp_path / "datasets"
    digests = []
    for generated_at in (WHEN, "2027-06-15T12:34:56+00:00"):
        with connect_readonly_database(path) as connection:
            dataset = build_evaluation_dataset(
                connection,
                profile_id=identity.profile_id,
                database_path=path,
                generated_at=generated_at,
            )
        paths = write_evaluation_dataset(dataset, root)
        digests.append(_digest(paths.records))
    assert digests[0] == digests[1]
    # The manifest is the one file that legitimately differs, and only in the
    # metadata that is outside the fingerprint.
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    assert manifest["generated_at"] == "2027-06-15T12:34:56+00:00"
    assert manifest["content_fingerprint"] == dataset.manifest.content_fingerprint
    assert manifest["record_count"] == len(COHORT_IDS)


def test_the_records_file_holds_one_canonical_record_per_line(
    dataset, tmp_path
):
    paths = write_evaluation_dataset(dataset, tmp_path / "datasets")
    lines = paths.records.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(dataset.records)
    assert [json.loads(line)["opportunity_id"] for line in lines] == list(COHORT_IDS)
    assert json.loads(lines[0]) == evaluation_record_payload(dataset.records[0])
    assert paths.directory.name == dataset.manifest.dataset_id


# --------------------------------------------------------------------------
# read-only, in fact and not only in intent
# --------------------------------------------------------------------------


def test_the_extraction_leaves_the_database_byte_for_byte_unchanged(
    operational_database, tmp_path
):
    """The strongest form of the claim: the file itself does not move."""
    path, identity = operational_database
    before_digest = _digest(path)
    before_dump = _dump(path)
    with connect_readonly_database(path) as connection:
        build_evaluation_dataset(
            connection,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at=WHEN,
        )
    assert _digest(path) == before_digest
    assert _dump(path) == before_dump
    assert not path.with_name(path.name + "-wal").exists()


def test_the_cli_writes_a_dataset_and_reports_it(
    operational_database, tmp_path, capsys
):
    path, identity = operational_database
    root = tmp_path / "datasets"
    exit_code = evaluation_cli(
        [
            "--database",
            str(path),
            "--profile-id",
            str(identity.profile_id),
            "--output-root",
            str(root),
        ]
    )
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["record_count"] == len(COHORT_IDS)
    assert report["written"] is True
    assert report["distribution"]["qualification"] == {
        "ADJACENT_TARGET": 1,
        "CORE_TARGET": 1,
        "UNCERTAIN": 1,
    }
    # Two of the three cohort members were never ranked, and the summary says so
    # rather than reporting a cohort of one.
    assert report["distribution"]["recommendation_disposition"] == {
        "RECOMMENDED": 1,
        "null": 2,
    }
    assert (root / report["dataset_id"] / "manifest.json").is_file()


def test_the_cli_dry_run_writes_nothing(operational_database, tmp_path, capsys):
    path, identity = operational_database
    root = tmp_path / "datasets"
    exit_code = evaluation_cli(
        [
            "--database",
            str(path),
            "--profile-id",
            str(identity.profile_id),
            "--output-root",
            str(root),
            "--dry-run",
        ]
    )
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["written"] is False
    assert not root.exists()


def test_an_unknown_profile_is_refused(operational_database, capsys):
    path, _ = operational_database
    assert evaluation_cli(["--database", str(path), "--profile-id", "999"]) == 2
    assert "does not exist" in json.loads(capsys.readouterr().out)["error"]


# --------------------------------------------------------------------------
# upstream states that are legitimate rather than failures
# --------------------------------------------------------------------------


def test_a_profile_with_no_recommendation_run_still_produces_a_dataset(
    operational_database
):
    """NOT_SYNCED is a smaller claim than "the engine rejected these"."""
    path, identity = operational_database
    connection = connect_database(path)
    try:
        connection.execute("DELETE FROM recommendation_profile_state")
        connection.execute("DELETE FROM recommendation_assessments")
        connection.execute("DELETE FROM recommendation_runs")
        connection.commit()
    finally:
        connection.close()
    with connect_readonly_database(path) as read_only:
        dataset = build_evaluation_dataset(
            read_only,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at=WHEN,
        )
    assert [record.opportunity_id for record in dataset.records] == list(COHORT_IDS)
    assert all(record.recommendation is None for record in dataset.records)
    assert dataset.manifest.upstream.recommendation_status == "NOT_SYNCED"
    assert dataset.manifest.upstream.recommendation_run_fingerprint is None
    # Matching is untouched and still reported, so the two are not confused.
    assert dataset.manifest.upstream.matching_status == "READY"


def test_the_manifest_identifies_its_source_without_copying_it(dataset, tmp_path):
    source = dataset.manifest.source
    assert source.database_sha256 == _digest(
        type(tmp_path)(source.database_path)
    )
    assert source.database_bytes > 0
    assert source.wal_present is False
    assert source.applied_migrations[0] == "0001"
    assert "0026" in source.applied_migrations
