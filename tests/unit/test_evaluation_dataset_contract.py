"""The Phase 10.1 contract, checked without a database.

Everything here is about the *statement* a dataset makes: which values reach the
fingerprint, which deliberately do not, and which distinctions the record
serialization is required to preserve. The extraction itself is checked against
real SQLite in `tests/integration/test_evaluation_snapshot_sqlite.py`.

Every opportunity below is invented, and no file in this module opens the
operational database or any generated dataset.
"""

import ast
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from evaluation.dataset import (
    EVALUATION_CANONICAL_ORDER,
    EVALUATION_COHORT_CRITERIA,
    EVALUATION_COHORT_QUALIFICATIONS,
    EVALUATION_COHORT_VERSION,
    EVALUATION_DATASET_SCHEMA_VERSION,
    EvaluationCohortDefinition,
    EvaluationDatasetError,
    EvaluationMatchingRecord,
    EvaluationOpportunityRecord,
    EvaluationProfileContext,
    EvaluationQualificationRecord,
    EvaluationRecommendationRecord,
    EvaluationUpstreamProvenance,
    canonical_evaluation_content_payload,
    evaluation_content_fingerprint,
    evaluation_record_fingerprint,
    evaluation_record_payload,
    require_supported_schema_version,
)
from services.collector.matching.fingerprint import canonical_json

EVALUATION_PACKAGE = Path("evaluation/dataset")


def qualification(**overrides) -> EvaluationQualificationRecord:
    values = {
        "qualification": "CORE_TARGET",
        "primary_domain": "DATA_ENGINEERING",
        "opportunity_type": "PFE",
        "employment_type": "UNKNOWN",
        "listing_quality": "NORMAL_LISTING",
        "classifier_version": "qualification-rules-v2",
        "input_fingerprint": "a" * 64,
        "classified_at": "2026-01-01T00:00:00+00:00",
        "fine_primary_category": "DATA_ENGINEERING",
        "fine_secondary_categories": (),
        "fine_classifier_version": "fine-data-ai-rules-v2",
    }
    values.update(overrides)
    return EvaluationQualificationRecord(**values)


def record(opportunity_id: int = 1, **overrides) -> EvaluationOpportunityRecord:
    values = {
        "opportunity_id": opportunity_id,
        "canonical_title": "Data Engineering PFE",
        "organization": "Example Org",
        "opportunity_type": "PFE",
        "employment_type": None,
        "location": "Casablanca, Maroc",
        "country": "MA",
        "remote_type": "ONSITE",
        "source_url": "https://example.invalid/offers/1",
        "application_url": None,
        "canonical_url": "https://example.invalid/offers/1",
        "status": "active",
        "is_active": True,
        "published_at": None,
        "deadline": None,
        "discovered_at": "2026-01-01T00:00:00+00:00",
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-02T00:00:00+00:00",
        "absorbed_duplicate_ids": (),
        "sources": (),
        "qualification": qualification(),
        "geography_segments": (),
        "eligibility": None,
        "matching": None,
        "recommendation": None,
    }
    values.update(overrides)
    return EvaluationOpportunityRecord(**values)


COHORT = EvaluationCohortDefinition(
    version=EVALUATION_COHORT_VERSION,
    criteria=EVALUATION_COHORT_CRITERIA,
    ordering=EVALUATION_CANONICAL_ORDER,
    excluded_counts={"merged_duplicate": 1, "inactive": 2},
)

PROFILE_CONTEXT = EvaluationProfileContext(
    profile_id=1, user_id=1, fingerprint="9" * 64
)

UPSTREAM = EvaluationUpstreamProvenance(
    matching_status="READY",
    matching_run_id=7,
    matching_run_fingerprint="b" * 64,
    matching_engine_version="matching-engine-v1",
    matching_rules_version="matching-rules-v1",
    matching_selection_version="matching-selection-v1",
    recommendation_status="READY",
    recommendation_run_id=9,
    recommendation_run_fingerprint="c" * 64,
    recommendation_engine_version="recommendation-engine-v1",
    recommendation_rules_version="recommendation-rules-v1",
)


def fingerprint(
    records, *, cohort=COHORT, upstream=UPSTREAM, profile_context=PROFILE_CONTEXT
) -> str:
    return evaluation_content_fingerprint(
        schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
        cohort=cohort,
        profile_context=profile_context,
        upstream=upstream,
        records=tuple(records),
    )


# --------------------------------------------------------------------------
# the cohort contract, stated rather than executed
# --------------------------------------------------------------------------


def test_the_cohort_does_not_filter_on_the_classifiers_verdict():
    """Every qualification state is admitted, `OUT_OF_SCOPE` included.

    `qualification` is a *prediction*, not a human truth. A real Data/AI
    opportunity the classifier judged `OUT_OF_SCOPE` is a false negative, and
    filtering on that verdict would delete exactly the rows Phase 10 exists to
    find. `evaluation-cohort-v2` therefore selects on collection — active and
    not a duplicate — and on nothing a model decided.
    """
    assert set(EVALUATION_COHORT_QUALIFICATIONS) == {
        "CORE_TARGET",
        "ADJACENT_TARGET",
        "OUT_OF_SCOPE",
        "UNCERTAIN",
    }
    assert EVALUATION_COHORT_CRITERIA == (
        "opportunities.is_active = 1",
        "opportunities.status != 'merged_duplicate'",
    )
    assert not any(
        "qualification" in criterion for criterion in EVALUATION_COHORT_CRITERIA
    )


def test_an_unread_posting_has_no_qualification_rather_than_a_verdict():
    """`None`, and never `OUT_OF_SCOPE`, `UNCERTAIN`, `{}` or `False`."""
    unread = record(qualification=None)
    payload = evaluation_record_payload(unread)
    assert payload["qualification"] is None
    assert payload["qualification"] != {}
    # And it is a distinct statement from every verdict the classifier can make.
    for verdict in ("CORE_TARGET", "ADJACENT_TARGET", "OUT_OF_SCOPE", "UNCERTAIN"):
        assert evaluation_record_fingerprint(unread) != (
            evaluation_record_fingerprint(
                record(qualification=qualification(qualification=verdict))
            )
        )


# --------------------------------------------------------------------------
# serialization: what a record is allowed to lose
# --------------------------------------------------------------------------


def test_absent_fine_classification_is_null_and_not_an_empty_list():
    """`0025`'s two meanings of NULL survive serialization."""
    never_ran = record(
        qualification=qualification(
            fine_primary_category=None,
            fine_secondary_categories=None,
            fine_classifier_version=None,
        )
    )
    ran_without_category = record(
        qualification=qualification(
            fine_primary_category=None,
            fine_secondary_categories=(),
            fine_classifier_version="fine-data-ai-rules-v2",
        )
    )
    assert (
        evaluation_record_payload(never_ran)["qualification"][
            "fine_secondary_categories"
        ]
        is None
    )
    assert (
        evaluation_record_payload(ran_without_category)["qualification"][
            "fine_secondary_categories"
        ]
        == []
    )
    assert evaluation_record_fingerprint(never_ran) != evaluation_record_fingerprint(
        ran_without_category
    )


def test_absent_downstream_signals_serialize_as_null():
    """No recommendation is `null`, never a zero-scored rejection."""
    payload = evaluation_record_payload(record())
    assert payload["eligibility"] is None
    assert payload["matching"] is None
    assert payload["recommendation"] is None


def test_unmeasured_match_quality_stays_null_beside_zero_coverage():
    """Phase 4's contract: no evidence means no score, never a score of zero."""
    payload = evaluation_record_payload(
        record(
            matching=EvaluationMatchingRecord(
                lane="UNCERTAIN",
                match_quality=None,
                evidence_coverage=0.0,
                assessment_fingerprint="d" * 64,
            )
        )
    )
    assert payload["matching"]["match_quality"] is None
    assert payload["matching"]["evidence_coverage"] == 0.0


def test_the_written_payload_is_the_digested_payload():
    """One serialization, so the file and the fingerprint cannot disagree."""
    item = record()
    assert evaluation_record_fingerprint(item) == hashlib.sha256(
        canonical_json(evaluation_record_payload(item)).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------
# the fingerprint: what moves it, and what must never move it
# --------------------------------------------------------------------------


def test_the_same_content_yields_the_same_fingerprint():
    assert fingerprint([record(1), record(2)]) == fingerprint(
        [record(1), record(2)]
    )


def test_a_business_change_moves_the_fingerprint():
    """Any field of any record is in the digest domain."""
    baseline = fingerprint([record(1), record(2)])
    assert fingerprint([replace(record(1), canonical_title="Other"), record(2)]) != (
        baseline
    )
    assert fingerprint(
        [
            record(1),
            replace(
                record(2),
                recommendation=EvaluationRecommendationRecord(
                    rank_position=1,
                    disposition="RECOMMENDED",
                    recommendation_score=0.5,
                    evidence_coverage=1.0,
                    assessment_fingerprint="e" * 64,
                ),
            ),
        ]
    ) != baseline


def test_a_changed_order_moves_the_fingerprint():
    """The canonical order is part of the statement, so it is digested."""
    assert fingerprint([record(1), record(2)]) != fingerprint(
        [record(2), record(1)]
    )


def test_a_changed_cohort_rule_moves_the_fingerprint():
    """Two datasets taken under different universes are not comparable."""
    widened = replace(COHORT, version="evaluation-cohort-v3")
    assert fingerprint([record(1)], cohort=widened) != fingerprint([record(1)])


def test_a_changed_upstream_run_fingerprint_moves_the_dataset_fingerprint():
    moved = replace(UPSTREAM, recommendation_run_fingerprint="f" * 64)
    assert fingerprint([record(1)], upstream=moved) != fingerprint([record(1)])


def test_volatile_and_identity_metadata_are_outside_the_digest_domain():
    """No clock, no path, no row id — the digest describes data, not a run."""
    payload = canonical_evaluation_content_payload(
        schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
        cohort=COHORT,
        profile_context=PROFILE_CONTEXT,
        upstream=UPSTREAM,
        records=(record(1),),
    )
    serialized = canonical_json(payload)
    for absent in (
        "generated_at",
        "dataset_id",
        "git_commit",
        "database_path",
        "database_sha256",
        "applied_migrations",
        "matching_run_id",
        "recommendation_run_id",
        "record_count",
    ):
        assert absent not in serialized
    # ...and the four things that must be in it, are.
    for present in ("excluded_counts", "profile_context", "cohort", "records"):
        assert present in serialized


def test_a_renumbered_upstream_run_does_not_move_the_fingerprint():
    """Re-persisting an identical run under a new row id changes nothing."""
    renumbered = replace(UPSTREAM, matching_run_id=4242, recommendation_run_id=99)
    assert fingerprint([record(1)], upstream=renumbered) == fingerprint([record(1)])


def test_a_changed_excluded_count_moves_the_fingerprint():
    """Two selections that refused different rows are not the same selection.

    `excluded_counts` is part of the cohort statement the manifest records, so
    it is inside the digest: one `dataset_id` must never name two different
    selection states.
    """
    elsewhere = replace(COHORT, excluded_counts={"merged_duplicate": 99})
    assert fingerprint([record(1)], cohort=elsewhere) != fingerprint([record(1)])


# --------------------------------------------------------------------------
# the dataset is bound to the person it was built for
# --------------------------------------------------------------------------


def test_two_profiles_cannot_collide_when_no_downstream_run_exists():
    """The case a personalised dataset must never get wrong.

    A profile with no Matching and no Recommendation run produces records whose
    downstream blocks are all `null` — which is to say, records identical to
    those of any other such profile over the same postings. Without the profile
    binding both would fingerprint the same, land in one directory, and a human
    label attached there would name no one.
    """
    empty_upstream = replace(
        UPSTREAM,
        matching_status="NOT_SYNCED",
        matching_run_id=None,
        matching_run_fingerprint=None,
        matching_engine_version=None,
        matching_rules_version=None,
        matching_selection_version=None,
        recommendation_status="NOT_SYNCED",
        recommendation_run_id=None,
        recommendation_run_fingerprint=None,
        recommendation_engine_version=None,
        recommendation_rules_version=None,
    )
    records = [record(1), record(2)]
    first = fingerprint(
        records,
        upstream=empty_upstream,
        profile_context=EvaluationProfileContext(1, 1, "9" * 64),
    )
    second = fingerprint(
        records,
        upstream=empty_upstream,
        profile_context=EvaluationProfileContext(2, 2, "9" * 64),
    )
    assert first != second


def test_a_changed_profile_context_moves_the_fingerprint():
    """An edited preference produces a new dataset even over unchanged postings."""
    moved = replace(PROFILE_CONTEXT, fingerprint="8" * 64)
    assert fingerprint([record(1)], profile_context=moved) != fingerprint(
        [record(1)]
    )


def test_indentation_and_key_order_cannot_move_the_fingerprint():
    """The digest is taken over a structure, never over the bytes of a file."""
    item = record(1, sources=(), geography_segments=())
    payload = evaluation_record_payload(item)
    reindented = json.loads(json.dumps(payload, indent=4))
    reversed_keys = {key: reindented[key] for key in reversed(list(reindented))}
    expected = evaluation_record_fingerprint(item)
    for variant in (reindented, reversed_keys):
        assert (
            hashlib.sha256(canonical_json(variant).encode("utf-8")).hexdigest()
            == expected
        )


# --------------------------------------------------------------------------
# refusing a snapshot this build cannot read
# --------------------------------------------------------------------------


def test_an_unknown_schema_version_is_refused_at_the_boundary():
    require_supported_schema_version(
        {"schema_version": EVALUATION_DATASET_SCHEMA_VERSION}
    )
    for payload in ({}, {"schema_version": "evaluation-dataset-v1"}):
        with pytest.raises(EvaluationDatasetError, match="schema version"):
            require_supported_schema_version(payload)


# --------------------------------------------------------------------------
# the architectural boundary, read off the source
# --------------------------------------------------------------------------


#: A string constant is treated as SQL when it opens with a statement keyword.
_SQL_OPENING = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|BEGIN|COMMIT|ROLLBACK|CREATE"
    r"|DROP|ALTER|PRAGMA|VACUUM|ATTACH|REINDEX)\b",
    re.IGNORECASE,
)

#: The only statements a read-only extraction is allowed to contain. `BEGIN` is
#: the deferred read snapshot and `ROLLBACK` is how it is released; both are
#: checked in full below, so `BEGIN IMMEDIATE` — which reserves the write lock —
#: is not quietly admitted by the prefix.
_ALLOWED_STATEMENTS = ("SELECT", "BEGIN", "ROLLBACK")


def _sql_constants(path: Path) -> list[str]:
    """Every SQL string this module contains, docstrings deliberately excluded.

    Read off the syntax tree rather than off the text: the prose explaining why
    a statement is absent must not be mistaken for the statement itself. Every
    statement the package runs is written as a literal somewhere in it — the
    `query()` helper only forwards one — so enumerating the literals enumerates
    the statements.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and _SQL_OPENING.match(node.value)
    ]


def test_the_package_never_writes_to_the_operational_database():
    """No DML anywhere in `evaluation/dataset`, checked rather than promised.

    Phase 10.1 reads the operational database and adds nothing to it: no INSERT,
    no UPDATE, no DELETE, no migration and no table of its own. That guarantee
    is enforced twice — by SQLite, because the CLI opens a `mode=ro` URI, and
    here, because a write statement cannot even be written in this package
    without failing the suite.
    """
    statements = [
        (path, statement)
        for path in sorted(EVALUATION_PACKAGE.glob("*.py"))
        for statement in _sql_constants(path)
    ]
    assert statements, "no SQL was found to check"
    for path, statement in statements:
        stripped = statement.strip()
        assert stripped.upper().startswith(_ALLOWED_STATEMENTS), (
            f"{path} contains {stripped.splitlines()[0]!r}"
        )
        assert stripped.upper() != "BEGIN IMMEDIATE", path


def test_nothing_in_the_package_depends_on_a_later_phase_10_slice():
    """No human label, no metric, no baseline — none exists in this slice.

    Phase 10.1 is the upstream end of the evaluation chain. If a module here
    ever imported a label store or a metric, the dependency would have been
    inverted and every measurement downstream would be reading its own output.
    """
    for path in sorted(EVALUATION_PACKAGE.glob("*.py")):
        body = path.read_text(encoding="utf-8")
        imports = [
            line for line in body.splitlines() if line.startswith(("import ", "from "))
        ]
        joined = "\n".join(imports).lower()
        for forbidden in ("label", "metric", "baseline", "experiment", "ndcg"):
            assert forbidden not in joined, f"{path} imports {forbidden}"
