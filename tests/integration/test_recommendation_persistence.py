"""Phase 9B.1 persistence, on disposable SQLite databases these tests build.

Every database here is created under `tmp_path` and thrown away. The profile is
a `.invalid` address, every posting is invented, and **the operational database
is never opened**: nothing below reads or writes anything a person uses.

The batches are produced by Phase 9A's own engine over Phase 9A's own fixtures,
and the source matching run by Phase 4's own `store_matching_batch`, so what is
persisted here is the real product of the real upstream rather than a look-alike
row this file invented.

Two properties carry most of the weight:

    the ranking is stored, not re-derived — `rank_position` is 1-based and holds
    the order the engine produced, and persistence refuses a batch that is not
    in that order rather than quietly re-sorting it

    identity is operational, not content — a run is reused only when the profile,
    the source matching run, the postings, the digests **and their order** all
    match, and a stored row that no longer says so is refused, never adopted
"""

from dataclasses import replace
import json
import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import store_matching_batch
from services.collector.matching.fingerprint import canonical_json
from services.digital_twin.repository import ensure_user_profile
from services.recommendation import (
    RECOMMENDATION_ENGINE_VERSION,
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RECOMMENDATION_PERSISTENCE_VERSION,
    RECOMMENDATION_RULES_VERSION,
    RecommendationBatchResult,
    RecommendationPersistenceError,
    build_recommendation_assessment,
    canonical_recommendation_assessment_payload,
    canonical_recommendation_batch_payload,
    rank_recommendation_assessments,
    recommendation_assessment_fingerprint,
    recommendation_batch_fingerprint,
    recommendation_run_fingerprint,
    store_recommendation_batch,
)
from tests.integration.test_matching_persistence import (
    add_opportunity,
    make_batch as make_matching_batch,
)
from tests.unit.recommendation_fixtures import opportunity, recommendation_input

SELECTION_VERSION = "selection-test-v1"

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "recommendation.persistence@example.invalid"
TEST_ONLY_OTHER_EMAIL = "recommendation.persistence.other@example.invalid"


def assessment_for(profile_id: int, opportunity_id: int, **overrides):
    """One real 9A assessment, re-bound to the profile this database created.

    `profile_id` is an operational id and Phase 9A's assessment payload holds
    none, so re-binding it leaves the assessment fingerprint exactly as the
    engine computed it — which the assertion here states rather than assumes.
    """
    built = build_recommendation_assessment(
        recommendation_input(
            opp=opportunity(opportunity_id=opportunity_id), **overrides
        )
    )
    rebound = replace(built, profile_id=profile_id)
    assert rebound.assessment_fingerprint == recommendation_assessment_fingerprint(
        rebound
    )
    return rebound


def make_batch(profile_id: int, opportunity_ids, scores=None):
    """A ranked batch over these postings, ranked by Phase 9A's own primitive."""
    scores = scores or {}
    assessments = rank_recommendation_assessments(
        [
            assessment_for(
                profile_id,
                opportunity_id,
                **({"required": scores[opportunity_id]} if opportunity_id in scores else {}),
            )
            for opportunity_id in opportunity_ids
        ]
    )
    raw = RecommendationBatchResult(
        profile_id=profile_id,
        assessments=assessments,
        assessment_count=len(assessments),
    )
    return replace(raw, batch_fingerprint=recommendation_batch_fingerprint(raw))


def resealed(batch):
    """Re-digest a mutated batch so it is coherent *as a batch*.

    Some mutations below target a check that lives past the batch-fingerprint
    gate. Leaving the old digest in place would trip that gate first and the
    test would prove nothing about the check it names, so the content digest is
    recomputed and the deeper refusal is the one under test.
    """
    return replace(batch, batch_fingerprint=recommendation_batch_fingerprint(batch))


def expected_run_fingerprint(batch, matching_run_id, matching_fingerprint):
    return recommendation_run_fingerprint(
        profile_id=batch.profile_id,
        source_matching_run_id=matching_run_id,
        source_matching_run_fingerprint=matching_fingerprint,
        input_assembly_version=RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        recommendation_engine_version=batch.recommendation_engine_version,
        recommendation_rules_version=batch.recommendation_rules_version,
        batch_fingerprint=batch.batch_fingerprint,
        ranked_assessments=tuple(
            (item.opportunity_id, item.assessment_fingerprint)
            for item in batch.assessments
        ),
    )


@pytest.fixture
def database(tmp_path):
    connection = connect_database(tmp_path / "recommendation-persistence.db")
    apply_migrations(connection)
    yield connection
    connection.close()


class Fixture:
    """One profile, three postings and the matching run they were matched by."""

    def __init__(self, connection, profile_id, opportunity_ids, matching):
        self.connection = connection
        self.profile_id = profile_id
        self.opportunity_ids = opportunity_ids
        self.matching_run_id = matching.run_id
        self.matching_run_fingerprint = matching.run_fingerprint

    def batch(self, opportunity_ids=None, scores=None):
        return make_batch(
            self.profile_id, opportunity_ids or self.opportunity_ids, scores
        )

    def store(self, batch=None, **overrides):
        values = dict(
            source_matching_run_id=self.matching_run_id,
            source_matching_run_fingerprint=self.matching_run_fingerprint,
        )
        values.update(overrides)
        return store_recommendation_batch(
            self.connection, self.profile_id, batch or self.batch(), **values
        )


@pytest.fixture
def ready(database):
    profile_id = ensure_user_profile(database, TEST_ONLY_EMAIL).profile_id
    ids = tuple(add_opportunity(database, suffix) for suffix in ("a", "b", "c"))
    database.commit()
    matching = store_matching_batch(
        database,
        profile_id,
        make_matching_batch(profile_id, ids),
        selection_version=SELECTION_VERSION,
    )
    return Fixture(database, profile_id, ids, matching)


def stored_assessments(connection, run_id):
    return connection.execute(
        "SELECT rank_position,opportunity_id,disposition,recommendation_score,"
        "evidence_coverage,assessment_fingerprint,assessment_payload_json"
        " FROM recommendation_assessments WHERE run_id=? ORDER BY rank_position",
        (run_id,),
    ).fetchall()


def test_a_valid_batch_is_stored_whole_with_its_ranking_and_its_digests(ready):
    batch = ready.batch(scores={ready.opportunity_ids[0]: 0.1})
    result = ready.store(batch)

    assert result.created is True
    assert result.run_fingerprint == expected_run_fingerprint(
        batch, ready.matching_run_id, ready.matching_run_fingerprint
    )
    run = ready.connection.execute(
        "SELECT profile_id,source_matching_run_id,persistence_version,"
        "input_assembly_version,recommendation_engine_version,"
        "recommendation_rules_version,source_matching_run_fingerprint,"
        "batch_fingerprint,run_fingerprint,assessment_count,batch_payload_json"
        " FROM recommendation_runs WHERE id=?",
        (result.run_id,),
    ).fetchone()
    assert run == (
        ready.profile_id,
        ready.matching_run_id,
        RECOMMENDATION_PERSISTENCE_VERSION,
        RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        RECOMMENDATION_ENGINE_VERSION,
        RECOMMENDATION_RULES_VERSION,
        ready.matching_run_fingerprint,
        batch.batch_fingerprint,
        result.run_fingerprint,
        3,
        canonical_json(canonical_recommendation_batch_payload(batch)),
    )
    # The stored envelope is canonical JSON: re-serializing what was read back
    # reproduces it byte for byte.
    assert canonical_json(json.loads(run[10])) == run[10]

    assert stored_assessments(ready.connection, result.run_id) == [
        (
            position,
            item.opportunity_id,
            item.disposition.value,
            item.recommendation_score,
            item.recommendation_evidence_coverage,
            item.assessment_fingerprint,
            canonical_json(canonical_recommendation_assessment_payload(item)),
        )
        for position, item in enumerate(batch.assessments, start=1)
    ]


def test_the_stored_rank_positions_are_1_to_n_in_the_engines_own_order(ready):
    # The first posting is scored low on purpose, so ranking by opportunity id
    # and ranking by the engine's key are visibly different orders.
    batch = ready.batch(scores={ready.opportunity_ids[0]: 0.05})
    result = ready.store(batch)

    rows = stored_assessments(ready.connection, result.run_id)
    assert [row[0] for row in rows] == [1, 2, 3]
    assert [row[1] for row in rows] == [
        item.opportunity_id for item in batch.assessments
    ]
    assert [row[1] for row in rows] != sorted(ready.opportunity_ids)


def test_a_stored_batch_leaves_the_profile_ready_and_pointing_at_the_run(ready):
    result = ready.store()

    assert ready.connection.execute(
        "SELECT profile_id,state,current_run_id,persistence_version,"
        "input_assembly_version,readiness_issues_json"
        " FROM recommendation_profile_state WHERE profile_id=?",
        (ready.profile_id,),
    ).fetchone() == (
        ready.profile_id,
        "READY",
        result.run_id,
        RECOMMENDATION_PERSISTENCE_VERSION,
        RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        "[]",
    )


def test_a_migrated_database_stays_consistent_after_a_store(ready):
    ready.store()
    ready.connection.commit()

    assert ready.connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert ready.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_storing_the_same_batch_twice_reuses_the_run_and_creates_nothing(ready):
    first = ready.store()
    second = ready.store()

    assert second.run_id == first.run_id
    assert second.run_fingerprint == first.run_fingerprint
    assert first.created is True and second.created is False
    assert (
        ready.connection.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0]
        == 1
    )
    assert (
        ready.connection.execute(
            "SELECT COUNT(*) FROM recommendation_assessments"
        ).fetchone()[0]
        == 3
    )
    assert ready.connection.execute(
        "SELECT state,current_run_id FROM recommendation_profile_state"
    ).fetchone() == ("READY", first.run_id)


def test_a_different_operational_identity_appends_a_second_run(ready):
    first = ready.store()
    other_ids = tuple(add_opportunity(ready.connection, suffix) for suffix in ("d", "e"))
    ready.connection.commit()
    other_matching = store_matching_batch(
        ready.connection,
        ready.profile_id,
        make_matching_batch(ready.profile_id, other_ids),
        selection_version=SELECTION_VERSION,
    )

    second = ready.store(
        source_matching_run_id=other_matching.run_id,
        source_matching_run_fingerprint=other_matching.run_fingerprint,
    )

    assert second.created is True and second.run_id != first.run_id
    assert second.run_fingerprint != first.run_fingerprint
    assert (
        ready.connection.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0]
        == 2
    )
    # Append-only: the first run and its ranking are untouched, and only the
    # single mutable pointer moved.
    assert len(stored_assessments(ready.connection, first.run_id)) == 3
    assert ready.connection.execute(
        "SELECT current_run_id FROM recommendation_profile_state"
    ).fetchone() == (second.run_id,)


def test_the_same_content_on_other_postings_is_a_different_run(ready):
    """The content digest agrees; the operational identity must not."""
    first = ready.store(ready.batch(ready.opportunity_ids[:2]))
    second = ready.store(ready.batch(ready.opportunity_ids[1:]))

    assert (
        ready.connection.execute(
            "SELECT batch_fingerprint FROM recommendation_runs ORDER BY id"
        ).fetchall()
        == [(ready.batch(ready.opportunity_ids[:2]).batch_fingerprint,)] * 2
    )
    assert second.run_id != first.run_id
    assert second.run_fingerprint != first.run_fingerprint


def test_a_ranking_the_engine_would_not_have_produced_is_refused(ready):
    batch = ready.batch(scores={ready.opportunity_ids[0]: 0.05})
    shuffled = replace(batch, assessments=tuple(reversed(batch.assessments)))
    rebuilt = replace(
        shuffled, batch_fingerprint=recommendation_batch_fingerprint(shuffled)
    )

    with pytest.raises(RecommendationPersistenceError, match="ranking order"):
        ready.store(rebuilt)
    assert (
        ready.connection.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("label", "match", "mutate"),
    [
        (
            "a falsified batch fingerprint",
            "batch fingerprint",
            lambda batch: replace(batch, batch_fingerprint="f" * 64),
        ),
        (
            "a malformed batch fingerprint",
            "malformed batch fingerprint",
            lambda batch: replace(batch, batch_fingerprint="not-a-digest"),
        ),
        (
            "a miscounted batch",
            "assessment_count",
            lambda batch: replace(batch, assessment_count=99),
        ),
        (
            "an empty batch",
            "non-empty",
            lambda batch: replace(batch, assessments=(), assessment_count=0),
        ),
        (
            "an unexpected engine version",
            "batch versions",
            lambda batch: replace(batch, recommendation_engine_version="engine-v9"),
        ),
        (
            "an unexpected rules version",
            "batch versions",
            lambda batch: replace(batch, recommendation_rules_version="rules-v9"),
        ),
        (
            "a duplicate posting",
            "duplicate opportunity",
            lambda batch: resealed(
                replace(
                    batch,
                    assessments=(batch.assessments[0], batch.assessments[0]),
                    assessment_count=2,
                )
            ),
        ),
        (
            "a falsified assessment fingerprint",
            "assessment fingerprint",
            lambda batch: resealed(
                replace(
                    batch,
                    assessments=(
                        replace(batch.assessments[0], assessment_fingerprint="e" * 64),
                    )
                    + batch.assessments[1:],
                )
            ),
        ),
        (
            "a score without evidence",
            "score availability",
            lambda batch: replace(
                batch,
                assessments=(
                    replace(
                        batch.assessments[0],
                        recommendation_evidence_coverage=0.0,
                        recommendation_score=0.5,
                    ),
                )
                + batch.assessments[1:],
            ),
        ),
        (
            "evidence without a score",
            "score availability",
            lambda batch: replace(
                batch,
                assessments=(replace(batch.assessments[0], recommendation_score=None),)
                + batch.assessments[1:],
            ),
        ),
        (
            "a score outside the unit interval",
            "invalid recommendation score",
            lambda batch: replace(
                batch,
                assessments=(replace(batch.assessments[0], recommendation_score=1.5),)
                + batch.assessments[1:],
            ),
        ),
        (
            "coverage outside the unit interval",
            "invalid recommendation coverage",
            lambda batch: replace(
                batch,
                assessments=(
                    replace(
                        batch.assessments[0], recommendation_evidence_coverage=2.0
                    ),
                )
                + batch.assessments[1:],
            ),
        ),
        (
            "an invalid disposition",
            "invalid assessment disposition",
            lambda batch: replace(
                batch,
                assessments=(replace(batch.assessments[0], disposition="RECOMMENDED"),)
                + batch.assessments[1:],
            ),
        ),
        (
            "an assessment belonging to someone else",
            "operational ID mismatch",
            lambda batch: replace(
                batch,
                assessments=(replace(batch.assessments[0], profile_id=999),)
                + batch.assessments[1:],
            ),
        ),
        (
            "an assessment with no posting",
            "operational ID mismatch",
            lambda batch: replace(
                batch,
                assessments=(replace(batch.assessments[0], opportunity_id=0),)
                + batch.assessments[1:],
            ),
        ),
        (
            "unexpected assessment versions",
            "assessment versions",
            lambda batch: replace(
                batch,
                assessments=(
                    replace(
                        batch.assessments[0], recommendation_engine_version="engine-v9"
                    ),
                )
                + batch.assessments[1:],
            ),
        ),
    ],
)
def test_a_batch_that_contradicts_itself_is_refused_before_any_write(
    ready, label, match, mutate
):
    with pytest.raises(RecommendationPersistenceError, match=match):
        ready.store(mutate(ready.batch()))
    for table in ("recommendation_runs", "recommendation_assessments"):
        assert (
            ready.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        )


@pytest.mark.parametrize(
    ("label", "match", "overrides"),
    [
        ("a non-positive matching run", "source_matching_run_id", {"source_matching_run_id": 0}),
        (
            "a boolean matching run",
            "source_matching_run_id",
            {"source_matching_run_id": True},
        ),
        (
            "a malformed matching fingerprint",
            "source matching run fingerprint",
            {"source_matching_run_fingerprint": "nope"},
        ),
        (
            "an empty assembly version",
            "input_assembly_version",
            {"input_assembly_version": "  "},
        ),
        (
            "a foreign assembly version",
            "unexpected input assembly version",
            {"input_assembly_version": "recommendation-input-assembly-v9"},
        ),
    ],
)
def test_malformed_provenance_arguments_are_refused(ready, label, match, overrides):
    with pytest.raises(RecommendationPersistenceError, match=match):
        ready.store(**overrides)


@pytest.mark.parametrize("profile_id", [0, -1, True, "11"])
def test_a_malformed_profile_id_is_refused(ready, profile_id):
    with pytest.raises(RecommendationPersistenceError, match="profile_id"):
        store_recommendation_batch(
            ready.connection,
            profile_id,
            ready.batch(),
            source_matching_run_id=ready.matching_run_id,
            source_matching_run_fingerprint=ready.matching_run_fingerprint,
        )


def test_a_batch_built_for_another_profile_is_refused(ready):
    other = ensure_user_profile(ready.connection, TEST_ONLY_OTHER_EMAIL).profile_id

    with pytest.raises(RecommendationPersistenceError, match="another profile"):
        ready.store(make_batch(other, ready.opportunity_ids))


def test_something_that_is_not_a_batch_is_refused(ready):
    with pytest.raises(RecommendationPersistenceError, match="RecommendationBatchResult"):
        ready.store(object())


def test_an_unknown_profile_is_refused(ready):
    missing = ready.profile_id + 5000

    with pytest.raises(RecommendationPersistenceError, match="does not exist"):
        store_recommendation_batch(
            ready.connection,
            missing,
            make_batch(missing, ready.opportunity_ids),
            source_matching_run_id=ready.matching_run_id,
            source_matching_run_fingerprint=ready.matching_run_fingerprint,
        )


def test_an_unknown_matching_run_is_refused(ready):
    with pytest.raises(RecommendationPersistenceError, match="matching run .* does not exist"):
        ready.store(source_matching_run_id=ready.matching_run_id + 500)


def test_another_profiles_matching_run_is_refused(ready):
    other = ensure_user_profile(ready.connection, TEST_ONLY_OTHER_EMAIL).profile_id
    other_ids = (add_opportunity(ready.connection, "f"),)
    ready.connection.commit()
    foreign = store_matching_batch(
        ready.connection,
        other,
        make_matching_batch(other, other_ids),
        selection_version=SELECTION_VERSION,
    )

    with pytest.raises(RecommendationPersistenceError, match="another profile"):
        ready.store(
            source_matching_run_id=foreign.run_id,
            source_matching_run_fingerprint=foreign.run_fingerprint,
        )


def test_a_matching_fingerprint_that_is_not_the_stored_one_is_refused(ready):
    with pytest.raises(RecommendationPersistenceError, match="does not match the stored run"):
        ready.store(source_matching_run_fingerprint="d" * 64)


def test_a_posting_that_does_not_exist_is_refused(ready):
    absent = max(ready.opportunity_ids) + 900

    with pytest.raises(RecommendationPersistenceError, match="do not exist"):
        ready.store(ready.batch((*ready.opportunity_ids, absent)))
    assert (
        ready.connection.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0]
        == 0
    )


def test_an_interruption_mid_insert_leaves_nothing_behind(ready):
    def fail(position):
        if position == 1:
            raise RuntimeError("test interruption")

    with pytest.raises(RuntimeError, match="test interruption"):
        ready.store(after_assessment_insert=fail)

    for table in (
        "recommendation_runs",
        "recommendation_assessments",
        "recommendation_profile_state",
    ):
        assert (
            ready.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        )
    # And the database is still usable afterwards.
    assert ready.store().created is True


def test_an_interruption_after_a_first_run_leaves_that_run_and_its_state_intact(ready):
    first = ready.store()
    other_ids = tuple(add_opportunity(ready.connection, suffix) for suffix in ("g", "h"))
    ready.connection.commit()
    other_matching = store_matching_batch(
        ready.connection,
        ready.profile_id,
        make_matching_batch(ready.profile_id, other_ids),
        selection_version=SELECTION_VERSION,
    )

    def fail(position):
        raise RuntimeError("test interruption")

    with pytest.raises(RuntimeError):
        ready.store(
            source_matching_run_id=other_matching.run_id,
            source_matching_run_fingerprint=other_matching.run_fingerprint,
            after_assessment_insert=fail,
        )

    assert ready.connection.execute(
        "SELECT id FROM recommendation_runs"
    ).fetchall() == [(first.run_id,)]
    assert len(stored_assessments(ready.connection, first.run_id)) == 3
    assert ready.connection.execute(
        "SELECT state,current_run_id FROM recommendation_profile_state"
    ).fetchone() == ("READY", first.run_id)


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        (
            "a rewritten assembly version",
            "UPDATE recommendation_runs SET input_assembly_version='tampered-v1' WHERE id=?",
        ),
        (
            "a rewritten source fingerprint",
            "UPDATE recommendation_runs SET source_matching_run_fingerprint=? WHERE id=?",
        ),
        (
            "a rewritten count",
            "UPDATE recommendation_runs SET assessment_count=99 WHERE id=?",
        ),
        (
            "a rewritten payload",
            "UPDATE recommendation_runs SET batch_payload_json='{\"a\":1}' WHERE id=?",
        ),
    ],
)
def test_an_existing_run_whose_metadata_moved_is_refused_not_adopted(
    ready, label, statement
):
    stored = ready.store()
    parameters = (
        ("c" * 64, stored.run_id) if statement.count("?") == 2 else (stored.run_id,)
    )
    ready.connection.execute(statement, parameters)
    ready.connection.commit()

    with pytest.raises(RecommendationPersistenceError, match="metadata is corrupt"):
        ready.store()


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        (
            "a rewritten disposition",
            "UPDATE recommendation_assessments SET disposition='KNOWN_BLOCKER' WHERE run_id=?",
        ),
        (
            "a rewritten score",
            "UPDATE recommendation_assessments SET recommendation_score=0.01 WHERE run_id=?",
        ),
        (
            "a rewritten assessment payload",
            "UPDATE recommendation_assessments SET assessment_payload_json='{}' WHERE run_id=?",
        ),
        (
            "a deleted assessment",
            "DELETE FROM recommendation_assessments WHERE run_id=? AND rank_position=3",
        ),
    ],
)
def test_an_existing_run_whose_assessments_moved_is_refused_not_adopted(
    ready, label, statement
):
    stored = ready.store()
    ready.connection.execute(statement, (stored.run_id,))
    ready.connection.commit()

    with pytest.raises(RecommendationPersistenceError, match="assessments are corrupt"):
        ready.store()


def test_a_reversed_stored_ranking_is_refused_not_adopted(ready):
    """The stored order is part of the identity, so a run whose ranking moved is
    not the run its fingerprint claims to be."""
    stored = ready.store()
    ready.connection.executescript(
        f"""UPDATE recommendation_assessments SET rank_position = 10 - rank_position
              WHERE run_id = {stored.run_id};"""
    )
    ready.connection.commit()

    with pytest.raises(RecommendationPersistenceError, match="assessments are corrupt"):
        ready.store()


def test_the_run_and_its_assessments_are_never_updated_in_place(ready):
    first = ready.store()
    before = stored_assessments(ready.connection, first.run_id)
    ready.connection.commit()

    ready.store()

    assert stored_assessments(ready.connection, first.run_id) == before
    with pytest.raises(sqlite3.IntegrityError):
        ready.connection.execute(
            "DELETE FROM matching_runs WHERE id=?", (ready.matching_run_id,)
        )
