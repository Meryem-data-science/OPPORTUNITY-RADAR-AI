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
dataset see what the pipeline dropped?** Seven postings go in, five come out,
and every qualification verdict, the absence of a verdict, both exclusions and
all three false-negative shapes are represented exactly once.
"""

import hashlib
import json
import sqlite3

import pytest

from evaluation.dataset import (
    EVALUATION_DATASET_SCHEMA_VERSION,
    WRITE_STATUS_CREATED,
    WRITE_STATUS_UNCHANGED,
    EvaluationDatasetError,
    build_evaluation_dataset,
    evaluation_manifest_payload,
    evaluation_record_payload,
    profile_context_fingerprint,
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
POSTING_TEXT = "  Nous recherchons un·e stagiaire PFE.\n\nProfil:\t Python, SQL.  "

#: The seven postings. Ids are explicit so the assertions below can name them.
#:
#:   1  CORE_TARGET, fully processed          -> in, recommended
#:   2  UNCERTAIN, never matched              -> in, invisible to Matching and
#:                                               everything after it
#:   3  ADJACENT_TARGET, matched, not ranked  -> in, dropped between Matching
#:                                               and Recommendation
#:   4  OUT_OF_SCOPE                          -> in: the classifier's verdict is
#:                                               a prediction, not a truth, and
#:                                               may itself be the false negative
#:   5  CORE_TARGET but inactive              -> excluded: collection stopped
#:                                               seeing it
#:   6  CORE_TARGET but merged_duplicate      -> excluded: tombstone, absorbed
#:                                               by 1 and traceable from there
#:   7  active with no qualification row      -> in, `qualification is None`:
#:                                               the classifier never read it
COHORT_IDS = (1, 2, 3, 4, 7)


def _insert_opportunity(
    connection: sqlite3.Connection,
    opportunity_id: int,
    *,
    title: str,
    description: str | None = None,
    status: str = "active",
    is_active: int = 1,
) -> None:
    connection.execute(
        """INSERT INTO opportunities
               (id, canonical_title, organization, opportunity_type, location,
                country, remote_type, description, source_url, canonical_url,
                status, is_active, published_at, discovered_at, first_seen_at,
                last_seen_at)
           VALUES (?, ?, 'Example Org', 'PFE', 'Casablanca, Maroc', 'MA',
                   'ONSITE', ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
        (
            opportunity_id,
            title,
            description,
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


def _state_preferences(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    preferred_domains: str,
) -> None:
    """State one profile's opportunity preferences, through the real tables.

    Written as an accepted `profile_facts` row plus its `profile_preferences`
    projection, which is the shape Phase 3.4 leaves behind — so what the
    profile-context loaders read here is what they read in production.
    """
    fact_id = connection.execute(
        """INSERT INTO profile_facts
               (profile_id, fact_type, value, status, created_at, updated_at,
                decided_at)
           VALUES (?, 'opportunity_preferences', 'stated', 'ACCEPTED', ?, ?, ?)
           RETURNING id""",
        (profile_id, WHEN, WHEN, WHEN),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO profile_preferences
               (profile_id, fact_id, opportunity_types_json, work_modes_json,
                preferred_domains_json, convention_status,
                visa_sponsorship_required, constraints_json, input_version)
           VALUES (?, ?, '["PFE"]', '["ON_SITE"]', ?, 'AVAILABLE', 'NO', '[]',
                   'profile-preferences-v1')""",
        (profile_id, fact_id, preferred_domains),
    )


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
        _insert_opportunity(
            connection,
            1,
            title="Data Engineering PFE",
            # Awkward on purpose: leading and trailing spaces, newlines, a tab
            # and a non-ASCII character, so a test can prove nothing cleans it.
            description=POSTING_TEXT,
        )
        # A posting whose advertisement was never captured: NULL, not "".
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
        _state_preferences(
            connection, identity.profile_id, preferred_domains='["DATA_ENGINEERING"]'
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
    """Five postings in, and only one of them was ever recommended.

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


def test_every_qualification_verdict_is_in_the_cohort(dataset):
    """CORE_TARGET, ADJACENT_TARGET, UNCERTAIN, OUT_OF_SCOPE, and none at all.

    `OUT_OF_SCOPE` is the one that matters: it is the classifier saying "this is
    not Data/AI", and if that judgement is wrong the posting is a false negative
    that only a human can catch. Filtering it out would make the classifier
    unfalsifiable.
    """
    verdicts = {
        record.opportunity_id: (
            None if record.qualification is None
            else record.qualification.qualification
        )
        for record in dataset.records
    }
    assert verdicts == {
        1: "CORE_TARGET",
        2: "UNCERTAIN",
        3: "ADJACENT_TARGET",
        4: "OUT_OF_SCOPE",
        7: None,
    }


def test_an_unread_posting_is_included_with_no_qualification_at_all(dataset):
    """`None`, and never a verdict the classifier did not make."""
    unread = next(record for record in dataset.records if record.opportunity_id == 7)
    assert unread.qualification is None
    assert evaluation_record_payload(unread)["qualification"] is None


def test_the_cohort_excludes_only_what_collection_decided(
    operational_database, dataset
):
    """Inactive and merged-duplicate, and nothing a model decided."""
    path, _ = operational_database
    with connect_readonly_database(path) as connection:
        assert select_evaluation_cohort_ids(connection) == COHORT_IDS
    assert dataset.manifest.cohort.excluded_counts == {
        "merged_duplicate": 1,
        "inactive": 1,
    }
    excluded = {5, 6}
    assert excluded & {record.opportunity_id for record in dataset.records} == set()
    # The two exclusions and the cohort partition the whole table.
    with connect_readonly_database(path) as connection:
        total = connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    assert total == len(COHORT_IDS) + sum(
        dataset.manifest.cohort.excluded_counts.values()
    )


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


def test_the_excluded_duplicate_stays_traceable_from_its_canonical(dataset):
    """A tombstone is out of the records, and its merge is not lost with it."""
    by_id = {record.opportunity_id: record for record in dataset.records}
    assert 6 not in by_id
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
    digests = []
    for index, generated_at in enumerate((WHEN, "2027-06-15T12:34:56+00:00")):
        with connect_readonly_database(path) as connection:
            dataset = build_evaluation_dataset(
                connection,
                profile_id=identity.profile_id,
                database_path=path,
                generated_at=generated_at,
            )
        # A separate root each time, so this checks the *rendering* rather than
        # the freeze — the freeze has its own tests below.
        paths = write_evaluation_dataset(dataset, tmp_path / f"datasets{index}")
        digests.append(_digest(paths.records))
    assert digests[0] == digests[1]


# --------------------------------------------------------------------------
# a frozen dataset is frozen
# --------------------------------------------------------------------------


def test_the_first_write_creates_the_dataset(dataset, tmp_path):
    root = tmp_path / "datasets"
    paths = write_evaluation_dataset(dataset, root)
    assert paths.status == WRITE_STATUS_CREATED
    assert paths.frozen_generated_at == dataset.manifest.generated_at
    assert paths.directory.name == dataset.manifest.dataset_id
    assert paths.manifest.is_file() and paths.records.is_file()


def test_a_second_write_of_the_same_dataset_changes_nothing(
    operational_database, tmp_path
):
    """The property the word "frozen" is doing all the work for.

    The second extraction is a different *run* — a later clock, a git commit
    where there was none — and it resolves to the same `dataset_id` because the
    data did not move. Rewriting the directory would replace `generated_at`,
    which is outside the fingerprint precisely because it may differ, and a
    Phase 10.2 label attached to that id would then describe a snapshot that had
    since been rewritten underneath it.
    """
    path, identity = operational_database
    root = tmp_path / "datasets"
    first = None
    for generated_at, commit in (
        (WHEN, None),
        ("2027-06-15T12:34:56+00:00", "a" * 40),
    ):
        with connect_readonly_database(path) as connection:
            built = build_evaluation_dataset(
                connection,
                profile_id=identity.profile_id,
                database_path=path,
                generated_at=generated_at,
                git_commit=commit,
            )
        paths = write_evaluation_dataset(built, root)
        if first is None:
            first = (paths, _digest(paths.manifest), _digest(paths.records))
            assert paths.status == WRITE_STATUS_CREATED
            continue
        assert paths.status == WRITE_STATUS_UNCHANGED
        # Nothing on disk moved, and the caller is told when it was really
        # frozen rather than when this run happened to re-derive it.
        assert paths.frozen_generated_at == WHEN
        assert built.manifest.generated_at != WHEN
        assert _digest(paths.manifest) == first[1]
        assert _digest(paths.records) == first[2]
    stored = json.loads(first[0].manifest.read_text(encoding="utf-8"))
    assert stored["generated_at"] == WHEN
    assert stored["git_commit"] is None
    # Exactly one directory: the freeze is idempotent, not accumulative.
    assert [entry.name for entry in root.iterdir()] == [
        first[0].directory.name
    ]


def test_a_dataset_id_naming_other_content_is_refused(dataset, tmp_path):
    """Same id, different content: report it, never overwrite it."""
    root = tmp_path / "datasets"
    paths = write_evaluation_dataset(dataset, root)
    stored = json.loads(paths.manifest.read_text(encoding="utf-8"))
    stored["content_fingerprint"] = "f" * 64
    paths.manifest.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(EvaluationDatasetError, match="refusing to overwrite"):
        write_evaluation_dataset(dataset, root)


def test_a_dataset_whose_records_were_altered_is_refused(dataset, tmp_path):
    """The manifest agreeing is not enough; the bytes have to agree too."""
    root = tmp_path / "datasets"
    paths = write_evaluation_dataset(dataset, root)
    paths.records.write_text("{}\n", encoding="utf-8")
    with pytest.raises(EvaluationDatasetError, match="different records"):
        write_evaluation_dataset(dataset, root)


def test_a_half_written_dataset_directory_is_refused(dataset, tmp_path):
    """An interrupted freeze is reported, not silently completed."""
    root = tmp_path / "datasets"
    paths = write_evaluation_dataset(dataset, root)
    paths.manifest.unlink()
    with pytest.raises(EvaluationDatasetError, match="incomplete"):
        write_evaluation_dataset(dataset, root)


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
        "OUT_OF_SCOPE": 1,
        "UNCERTAIN": 1,
        # Reported explicitly: the classifier has never read this posting.
        "null": 1,
    }
    assert report["write_status"] == WRITE_STATUS_CREATED
    # Two of the three cohort members were never ranked, and the summary says so
    # rather than reporting a cohort of one.
    assert report["distribution"]["recommendation_disposition"] == {
        "RECOMMENDED": 1,
        "null": 4,
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


# --------------------------------------------------------------------------
# the dataset is bound to the person it was built for
# --------------------------------------------------------------------------


def test_the_manifest_binds_the_dataset_to_the_profile_it_was_built_for(
    operational_database, dataset
):
    """Identity plus a digest of what the profile declared — and no copy of it."""
    path, identity = operational_database
    context = dataset.manifest.profile_context
    assert context.profile_id == identity.profile_id
    assert context.user_id == identity.user_id
    assert len(context.fingerprint) == 64
    with connect_readonly_database(path) as connection:
        assert context.fingerprint == profile_context_fingerprint(
            connection, identity.profile_id
        )
    # The digest identifies the profile state; it does not republish it.
    manifest = json.dumps(evaluation_manifest_payload(dataset.manifest))
    assert "tester@example.invalid" not in manifest
    assert "DATA_ENGINEERING" not in manifest


def test_two_profiles_with_no_downstream_runs_do_not_collide(operational_database):
    """The exact collision a personalised dataset must never allow.

    Both profiles have no Matching and no Recommendation run, so every record of
    both datasets carries `null` in all three personalised blocks and the two
    record sets are byte-identical. Only the profile binding separates them.
    """
    path, identity = operational_database
    connection = connect_database(path)
    try:
        # Strip the runs, so neither profile has any downstream signal at all.
        connection.execute("DELETE FROM recommendation_profile_state")
        connection.execute("DELETE FROM recommendation_assessments")
        connection.execute("DELETE FROM recommendation_runs")
        connection.execute("DELETE FROM matching_profile_state")
        connection.execute("DELETE FROM matching_assessments")
        connection.execute("DELETE FROM matching_runs")
        connection.execute("DELETE FROM opportunity_eligibilities")
        # `ensure_user_profile` opens its own transaction, so this one is closed
        # before it is called rather than nested inside it.
        connection.commit()
        second = ensure_user_profile(connection, "other@example.invalid")
        _state_preferences(
            connection, second.profile_id, preferred_domains='["DATA_ENGINEERING"]'
        )
        connection.commit()
    finally:
        connection.close()

    built = {}
    for profile_id in (identity.profile_id, second.profile_id):
        with connect_readonly_database(path) as read_only:
            built[profile_id] = build_evaluation_dataset(
                read_only,
                profile_id=profile_id,
                database_path=path,
                generated_at=WHEN,
            )
    first, other = built[identity.profile_id], built[second.profile_id]
    # The records really are identical — that is what makes the case dangerous.
    assert first.records == other.records
    assert all(record.recommendation is None for record in first.records)
    assert all(record.matching is None for record in first.records)
    # ...and the datasets are still distinct.
    assert (
        first.manifest.content_fingerprint != other.manifest.content_fingerprint
    )
    assert first.manifest.dataset_id != other.manifest.dataset_id


def test_a_changed_preference_moves_the_dataset_fingerprint(operational_database):
    """An edited profile produces a new dataset over unchanged postings."""
    path, identity = operational_database
    with connect_readonly_database(path) as read_only:
        before = build_evaluation_dataset(
            read_only,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at=WHEN,
        )
    connection = connect_database(path)
    try:
        connection.execute(
            """UPDATE profile_preferences
                  SET preferred_domains_json = '["MACHINE_LEARNING_AI"]'
                WHERE profile_id = ?""",
            (identity.profile_id,),
        )
        connection.commit()
    finally:
        connection.close()
    with connect_readonly_database(path) as read_only:
        after = build_evaluation_dataset(
            read_only,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at=WHEN,
        )
    assert after.records == before.records
    assert (
        after.manifest.profile_context.fingerprint
        != before.manifest.profile_context.fingerprint
    )
    assert (
        after.manifest.content_fingerprint != before.manifest.content_fingerprint
    )
    assert after.manifest.dataset_id != before.manifest.dataset_id


# --------------------------------------------------------------------------
# the frozen evidence a human will judge
# --------------------------------------------------------------------------


def test_the_posting_text_travels_with_the_record(dataset, tmp_path):
    """Byte for byte, from `opportunities.description` to the JSONL line.

    Phase 10.2 must be able to judge from the frozen artefact alone — not by
    re-reading the live database, and not by following a URL that may have
    changed or gone. So the check goes all the way to the file.
    """
    by_id = {record.opportunity_id: record for record in dataset.records}
    assert by_id[1].description == POSTING_TEXT
    paths = write_evaluation_dataset(dataset, tmp_path / "datasets")
    lines = {
        json.loads(line)["opportunity_id"]: json.loads(line)
        for line in paths.records.read_text(encoding="utf-8").splitlines()
    }
    assert lines[1]["description"] == POSTING_TEXT
    # Untrimmed, unnormalised, unsummarised, untruncated.
    assert lines[1]["description"].startswith("  ")
    assert lines[1]["description"].endswith("  ")
    assert "\t" in lines[1]["description"]


def test_a_posting_with_no_text_reads_as_null(dataset, tmp_path):
    """SQL NULL becomes JSON `null`, never `""`."""
    by_id = {record.opportunity_id: record for record in dataset.records}
    assert by_id[2].description is None
    paths = write_evaluation_dataset(dataset, tmp_path / "datasets")
    lines = {
        json.loads(line)["opportunity_id"]: json.loads(line)
        for line in paths.records.read_text(encoding="utf-8").splitlines()
    }
    assert lines[2]["description"] is None


def test_an_edited_description_moves_the_dataset_fingerprint(operational_database):
    """Re-written evidence is a different dataset, even at the same URL."""
    path, identity = operational_database
    with connect_readonly_database(path) as read_only:
        before = build_evaluation_dataset(
            read_only,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at=WHEN,
        )
    connection = connect_database(path)
    try:
        connection.execute(
            "UPDATE opportunities SET description = ? WHERE id = 1",
            ("A different advertisement entirely.",),
        )
        connection.commit()
    finally:
        connection.close()
    with connect_readonly_database(path) as read_only:
        after = build_evaluation_dataset(
            read_only,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at=WHEN,
        )
    assert [record.opportunity_id for record in after.records] == list(COHORT_IDS)
    assert (
        after.manifest.content_fingerprint != before.manifest.content_fingerprint
    )
    assert after.manifest.dataset_id != before.manifest.dataset_id


# --------------------------------------------------------------------------
# an edited manifest is never accepted as unchanged
# --------------------------------------------------------------------------


def _tamper(paths, mutate) -> None:
    """Edit a frozen manifest in place, leaving its `content_fingerprint` alone.

    That is the whole point of these cases: the stored digest is a claim the
    manifest makes about itself, and an edited file can keep making it.
    """
    stored = json.loads(paths.manifest.read_text(encoding="utf-8"))
    mutate(stored)
    paths.manifest.write_text(json.dumps(stored), encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "mutate", "message"),
    [
        (
            "dataset_id",
            lambda stored: stored.__setitem__("dataset_id", "evaluation-dataset-v3-0"),
            "dataset_id",
        ),
        (
            "schema_version",
            lambda stored: stored.__setitem__(
                "schema_version", "evaluation-dataset-v2"
            ),
            "do not match",
        ),
        (
            "record_count",
            lambda stored: stored.__setitem__("record_count", 99),
            "record_count",
        ),
        (
            "profile_context",
            lambda stored: stored["profile_context"].__setitem__("profile_id", 999),
            "do not match",
        ),
        (
            "profile_context fingerprint",
            lambda stored: stored["profile_context"].__setitem__(
                "fingerprint", "c" * 64
            ),
            "do not match",
        ),
        (
            "cohort version",
            lambda stored: stored["cohort"].__setitem__(
                "version", "evaluation-cohort-v1"
            ),
            "do not match",
        ),
        (
            "cohort excluded_counts",
            lambda stored: stored["cohort"]["excluded_counts"].__setitem__(
                "inactive", 999
            ),
            "do not match",
        ),
        (
            "upstream run fingerprint",
            lambda stored: stored["upstream"].__setitem__(
                "recommendation_run_fingerprint", "d" * 64
            ),
            "do not match",
        ),
        (
            "upstream engine version",
            lambda stored: stored["upstream"].__setitem__(
                "matching_engine_version", "matching-engine-v99"
            ),
            "do not match",
        ),
        (
            "upstream status",
            lambda stored: stored["upstream"].__setitem__(
                "recommendation_status", "NOT_SYNCED"
            ),
            "do not match",
        ),
    ],
)
def test_an_edited_manifest_is_refused_however_it_was_edited(
    dataset, tmp_path, name, mutate, message
):
    """Every fingerprinted field, plus the two the digest deliberately omits.

    None of these edits touches `content_fingerprint`, so a writer that trusted
    the stored digest would report UNCHANGED and hand Phase 10.2 a manifest
    describing something else.
    """
    root = tmp_path / "datasets"
    paths = write_evaluation_dataset(dataset, root)
    _tamper(paths, mutate)
    with pytest.raises(EvaluationDatasetError, match=message):
        write_evaluation_dataset(dataset, root)


def test_metadata_outside_the_fingerprint_may_differ_without_a_rewrite(
    operational_database, tmp_path
):
    """The other half of the contract: execution metadata is free to move.

    A second extraction from a copy of the database, at another moment, from
    another checkout, is the same snapshot. It must report UNCHANGED and leave
    the frozen manifest exactly as it was — otherwise every re-run would be an
    error, and the freeze would be unusable rather than strict.
    """
    path, identity = operational_database
    root = tmp_path / "datasets"
    with connect_readonly_database(path) as connection:
        first = build_evaluation_dataset(
            connection,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at=WHEN,
        )
    paths = write_evaluation_dataset(first, root)
    frozen = paths.manifest.read_text(encoding="utf-8")

    # The same database at another path: a different `database_path`, a
    # different file digest is impossible for a copy, so the WAL flags and the
    # path are what move — along with the clock and the commit.
    copy = tmp_path / "copy.db"
    copy.write_bytes(path.read_bytes())
    with connect_readonly_database(copy) as connection:
        second = build_evaluation_dataset(
            connection,
            profile_id=identity.profile_id,
            database_path=copy,
            generated_at="2027-06-15T12:34:56+00:00",
            git_commit="a" * 40,
        )
    assert second.manifest.source.database_path != (
        first.manifest.source.database_path
    )
    assert second.manifest.content_fingerprint == first.manifest.content_fingerprint

    again = write_evaluation_dataset(second, root)
    assert again.status == WRITE_STATUS_UNCHANGED
    assert again.frozen_generated_at == WHEN
    assert paths.manifest.read_text(encoding="utf-8") == frozen


def test_a_renumbered_upstream_run_is_still_the_same_frozen_dataset(
    operational_database, tmp_path
):
    """Row identity is metadata: re-persisting an identical run rewrites nothing."""
    path, identity = operational_database
    root = tmp_path / "datasets"
    with connect_readonly_database(path) as connection:
        first = build_evaluation_dataset(
            connection,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at=WHEN,
        )
    paths = write_evaluation_dataset(first, root)
    frozen = paths.manifest.read_text(encoding="utf-8")

    connection = connect_database(path)
    try:
        # Same run, same fingerprints, new autoincrement ids.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE matching_runs SET id = 500 WHERE id = ?",
                           (first.manifest.upstream.matching_run_id,))
        connection.execute("UPDATE matching_assessments SET run_id = 500")
        connection.execute("UPDATE matching_profile_state SET current_run_id = 500")
        connection.execute(
            "UPDATE recommendation_runs SET id = 600, source_matching_run_id = 500"
        )
        connection.execute("UPDATE recommendation_assessments SET run_id = 600")
        connection.execute(
            "UPDATE recommendation_profile_state SET current_run_id = 600"
        )
        connection.commit()
    finally:
        connection.close()

    with connect_readonly_database(path) as read_only:
        second = build_evaluation_dataset(
            read_only,
            profile_id=identity.profile_id,
            database_path=path,
            generated_at="2027-06-15T12:34:56+00:00",
        )
    assert second.manifest.upstream.matching_run_id == 500
    assert second.manifest.content_fingerprint == first.manifest.content_fingerprint
    again = write_evaluation_dataset(second, root)
    assert again.status == WRITE_STATUS_UNCHANGED
    assert paths.manifest.read_text(encoding="utf-8") == frozen
