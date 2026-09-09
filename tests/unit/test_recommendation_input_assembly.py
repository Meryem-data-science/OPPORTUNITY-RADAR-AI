"""The readiness preflight: what stops a cohort before anything is assessed.

The per-opportunity branches are exercised against a real SQLite database in
`tests/integration/test_recommendation_sqlite.py`; what is covered here are the
whole-cohort refusals, which short-circuit before any row is read.
"""

import ast
import pathlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.collector.matching import (
    MATCHING_ENGINE_VERSION,
    MATCHING_PERSISTENCE_VERSION,
    MATCHING_RULES_VERSION,
    MATCHING_SELECTION_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
    MatchingPersistenceAuditError,
    MatchingReadError,
)
from services.recommendation import (
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RecommendationReadinessIssueCode,
    RecommendationReadinessStatus,
    assemble_recommendation_inputs,
    current_semantic_binding_fingerprint,
    current_semantic_corpus_fingerprint,
)
from tests.unit.recommendation_fixtures import opportunity

PROFILE_ID = 7


class Connection:
    """Answers only the one query the short-circuit branches actually reach."""

    def __init__(self, owner=(41,)):
        self.owner = owner

    def execute(self, sql, parameters=()):
        if "FROM profiles" in sql:
            return SimpleNamespace(fetchone=lambda: self.owner)
        raise AssertionError(f"unexpected query: {sql}")


def run(*assessments, status="READY", selected=(1,), **overrides):
    values = dict(
        run_id=9,
        persistence_version=MATCHING_PERSISTENCE_VERSION,
        selection_version=MATCHING_SELECTION_VERSION,
        matching_engine_version=MATCHING_ENGINE_VERSION,
        matching_rules_version=MATCHING_RULES_VERSION,
        semantic_percentile_version=SEMANTIC_PERCENTILE_VERSION,
        assessments=assessments,
    )
    values.update(overrides)
    current = SimpleNamespace(**values) if status == "READY" else None
    return SimpleNamespace(status=status, current_run=current)


def audit(run_id=9, ok=True, issues=(), profile_issues=(), current_run_id=9):
    """A Phase 4 persistence audit report, reduced to what the preflight reads."""
    audited = SimpleNamespace(run_id=run_id, ok=ok, issues=tuple(issues))
    return SimpleNamespace(
        current_run_id=current_run_id,
        runs=(audited,),
        issues=tuple(issues) + tuple(profile_issues),
    )


def issue(code, run_id=9):
    return SimpleNamespace(code=code, run_id=run_id)


def invoke(monkeypatch, *, connection=None, snapshot=None, selected=(1,), report=None):
    monkeypatch.setattr(
        "services.recommendation.input_assembly.read_current_matching",
        lambda *_: snapshot if snapshot is not None else run(),
    )
    monkeypatch.setattr(
        "services.recommendation.input_assembly.select_matching_opportunity_ids",
        lambda *_: selected,
    )
    monkeypatch.setattr(
        "services.recommendation.input_assembly.audit_matching_profile_history",
        lambda *_: audit() if report is None else report,
    )
    return assemble_recommendation_inputs(connection or Connection(), PROFILE_ID)


def codes(result):
    return [issue.code for issue in result.issues]


def test_an_unknown_profile_is_incomplete_and_yields_nothing(monkeypatch):
    result = invoke(monkeypatch, connection=Connection(owner=None))
    assert result.status is RecommendationReadinessStatus.INCOMPLETE
    assert codes(result) == [RecommendationReadinessIssueCode.PROFILE_NOT_FOUND]
    assert result.records == ()
    assert result.assembly_version == RECOMMENDATION_INPUT_ASSEMBLY_VERSION
    assert result.user_id is None


@pytest.mark.parametrize("owner", [(0,), (None,), ("41",), (True,)])
def test_an_invalid_profile_owner_is_refused(monkeypatch, owner):
    result = invoke(monkeypatch, connection=Connection(owner=owner))
    assert codes(result) == [RecommendationReadinessIssueCode.PROFILE_OWNER_INVALID]


def test_an_unreadable_matching_snapshot_is_reported_not_recomputed(monkeypatch):
    def explode(*_):
        raise MatchingReadError("assessment count mismatch")

    monkeypatch.setattr(
        "services.recommendation.input_assembly.read_current_matching", explode
    )
    monkeypatch.setattr(
        "services.recommendation.input_assembly.select_matching_opportunity_ids",
        lambda *_: (1,),
    )
    result = assemble_recommendation_inputs(Connection(), PROFILE_ID)
    assert codes(result) == [RecommendationReadinessIssueCode.MATCHING_NOT_READY]
    assert "assessment count mismatch" in result.issues[0].message


@pytest.mark.parametrize("status", ["NOT_SYNCED", "EMPTY"])
def test_a_matching_state_that_is_not_ready_stops_the_cohort(monkeypatch, status):
    result = invoke(monkeypatch, snapshot=run(status=status))
    assert codes(result) == [RecommendationReadinessIssueCode.MATCHING_NOT_READY]
    assert result.user_id == 41 and result.matching_run_id is None


@pytest.mark.parametrize(
    "field",
    [
        "persistence_version",
        "selection_version",
        "matching_engine_version",
        "matching_rules_version",
        "semantic_percentile_version",
    ],
)
def test_any_stale_matching_version_stops_the_cohort(monkeypatch, field):
    result = invoke(monkeypatch, snapshot=run(**{field: "something-else-v9"}))
    assert codes(result) == [RecommendationReadinessIssueCode.MATCHING_VERSION_STALE]
    assert field in result.issues[0].message
    assert result.matching_run_id == 9


def test_a_cohort_that_drifted_from_the_current_selection_is_refused(monkeypatch):
    snapshot = run(SimpleNamespace(opportunity_id=1))
    result = invoke(monkeypatch, snapshot=snapshot, selected=(1, 2))
    assert codes(result) == [RecommendationReadinessIssueCode.STALE_MATCHING_COHORT]
    assert result.records == ()


def test_a_current_run_failing_its_own_persistence_audit_stops_the_cohort(
    monkeypatch,
):
    result = invoke(
        monkeypatch,
        report=audit(ok=False, issues=[issue("ASSESSMENT_FINGERPRINT_MISMATCH")]),
    )
    assert codes(result) == [
        RecommendationReadinessIssueCode.MATCHING_PERSISTENCE_INVALID
    ]
    assert "ASSESSMENT_FINGERPRINT_MISMATCH" in result.issues[0].message
    assert result.records == ()


def test_a_corrupt_historical_run_does_not_stop_a_healthy_current_one(monkeypatch):
    """An old run that rotted is a real problem, and not this cohort's."""
    healthy = audit()
    healthy.runs = (
        *healthy.runs,
        SimpleNamespace(run_id=4, ok=False, issues=(issue("RUN_FINGERPRINT", 4),)),
    )
    healthy.issues = (issue("RUN_FINGERPRINT", 4),)
    result = invoke(monkeypatch, report=healthy, snapshot=run(status="EMPTY"))
    # The EMPTY state stops it for its own reason, never for run 4's corruption.
    assert codes(result) == [RecommendationReadinessIssueCode.MATCHING_NOT_READY]


def test_a_profile_state_pointing_at_another_run_stops_the_cohort(monkeypatch):
    result = invoke(monkeypatch, report=audit(current_run_id=41))
    assert codes(result) == [
        RecommendationReadinessIssueCode.MATCHING_PERSISTENCE_INVALID
    ]


def test_a_profile_level_audit_issue_stops_the_cohort(monkeypatch):
    result = invoke(
        monkeypatch,
        report=audit(profile_issues=[issue("STATE_RUN_MISMATCH", None)]),
    )
    assert codes(result) == [
        RecommendationReadinessIssueCode.MATCHING_PERSISTENCE_INVALID
    ]
    assert "STATE_RUN_MISMATCH" in result.issues[0].message


def test_an_unauditable_matching_persistence_stops_the_cohort(monkeypatch):
    def explode(*_):
        raise MatchingPersistenceAuditError("matching persistence query failed")

    monkeypatch.setattr(
        "services.recommendation.input_assembly.read_current_matching",
        lambda *_: run(),
    )
    monkeypatch.setattr(
        "services.recommendation.input_assembly.select_matching_opportunity_ids",
        lambda *_: (1,),
    )
    monkeypatch.setattr(
        "services.recommendation.input_assembly.audit_matching_profile_history", explode
    )
    result = assemble_recommendation_inputs(Connection(), PROFILE_ID)
    assert codes(result) == [
        RecommendationReadinessIssueCode.MATCHING_PERSISTENCE_INVALID
    ]


# --------------------------------------------------------------------------
# the two cohort-wide freshness proofs, as pure functions
# --------------------------------------------------------------------------


def test_the_corpus_fingerprint_sees_content_and_not_the_cohort_membership():
    """Identical ids over edited documents must not fingerprint identically."""
    cohort = (opportunity(opportunity_id=1), opportunity(opportunity_id=2))
    edited = (
        opportunity(opportunity_id=1),
        replace(opportunity(opportunity_id=2), description="something else entirely"),
    )
    assert [item.opportunity_id for item in cohort] == [
        item.opportunity_id for item in edited
    ]
    assert current_semantic_corpus_fingerprint(
        cohort
    ) != current_semantic_corpus_fingerprint(edited)


def test_the_corpus_fingerprint_ignores_the_order_the_cohort_was_read_in():
    cohort = (opportunity(opportunity_id=1), opportunity(opportunity_id=2))
    assert current_semantic_corpus_fingerprint(
        cohort
    ) == current_semantic_corpus_fingerprint(tuple(reversed(cohort)))


def test_the_corpus_fingerprint_is_a_sha256_and_repeats_exactly():
    cohort = (opportunity(opportunity_id=1),)
    first = current_semantic_corpus_fingerprint(cohort)
    assert first == current_semantic_corpus_fingerprint(cohort)
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def _referenced_names(module: str) -> set[str]:
    """Every identifier the module's **code** uses, prose excluded.

    Parsed rather than grepped on purpose: these files explain in their
    docstrings exactly which functions they refuse to call, and a substring
    search cannot tell an explanation from a call.
    """
    tree = ast.parse(pathlib.Path(module).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
    return names


def test_the_assembly_never_calls_a_tfidf_entry_point():
    """A static counterpart to the monkeypatched integration test.

    The corpus proof is a canonical document digest. Nothing in this package may
    fit a vectorizer, score a similarity, or reach for sklearn at all.
    """
    for name in ("input_assembly", "engine", "models", "fine_domain", "fingerprint"):
        referenced = _referenced_names(f"services/recommendation/{name}.py")
        for forbidden in (
            "fit_tfidf_corpus",
            "score_profile_against_tfidf_corpus",
            "score_matching_input_semantic_similarity",
            "fit_transform",
            "TfidfVectorizer",
        ):
            assert forbidden not in referenced, f"{name}.py uses {forbidden}"
        assert not any(item.startswith("sklearn") for item in referenced), name


def test_the_phase_8_provenance_check_reuses_persistence_and_runs_no_classifier():
    """The check must mirror `persist_qualifications`, not restate it.

    Phase 8 leaves a row alone exactly when its stored triple equals this one, so
    the assembly imports that function and those two constants rather than
    hashing the same five fields a second way.
    """
    referenced = _referenced_names("services/recommendation/input_assembly.py")
    assert {
        "services.collector.qualification.persistence",
        "input_fingerprint",
        "CLASSIFIER_VERSION",
        "FINE_CLASSIFIER_VERSION",
    } <= referenced
    # Read-only: neither classifier, nor the persistence that writes their rows.
    for forbidden in (
        "persist_qualifications",
        "persist_configured_qualifications",
        "classify_opportunity",
        "classify_fine_categories",
    ):
        assert forbidden not in referenced


def test_the_provenance_check_compares_exactly_the_persisted_triple():
    """The three columns compared are the three `0025` reconciles on."""
    source = pathlib.Path("services/recommendation/input_assembly.py").read_text(
        encoding="utf-8"
    )
    query = source[source.index("SELECT input_fingerprint, classifier_version") :]
    assert "fine_classifier_version" in query.split("FROM")[0]
    assert "opportunity_qualifications" in query.split("WHERE")[0]


def test_the_binding_fingerprint_sees_a_swap_the_corpus_digest_cannot():
    """Why Recommendation checks two digests rather than one."""
    cohort = (
        opportunity(opportunity_id=1),
        replace(opportunity(opportunity_id=2), description="something else entirely"),
    )
    swapped = (
        replace(opportunity(opportunity_id=1), description="something else entirely"),
        replace(
            opportunity(opportunity_id=2),
            canonical_title=cohort[0].canonical_title,
            description=cohort[0].description,
        ),
    )
    # Same ids, same multiset of documents...
    assert [item.opportunity_id for item in cohort] == [
        item.opportunity_id for item in swapped
    ]
    assert current_semantic_corpus_fingerprint(
        cohort
    ) == current_semantic_corpus_fingerprint(swapped)
    # ...and a different assignment of documents to postings.
    assert current_semantic_binding_fingerprint(
        cohort
    ) != current_semantic_binding_fingerprint(swapped)


def test_the_binding_fingerprint_ignores_the_order_the_cohort_was_read_in():
    cohort = (opportunity(opportunity_id=1), opportunity(opportunity_id=2))
    assert current_semantic_binding_fingerprint(
        cohort
    ) == current_semantic_binding_fingerprint(tuple(reversed(cohort)))


def test_the_binding_fingerprint_is_a_sha256_and_repeats_exactly():
    cohort = (opportunity(opportunity_id=1),)
    first = current_semantic_binding_fingerprint(cohort)
    assert first == current_semantic_binding_fingerprint(cohort)
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def test_the_binding_helper_delegates_to_the_matching_owned_primitive():
    """One hash format, owned by Matching, used by both sides of the check."""
    referenced = _referenced_names("services/recommendation/input_assembly.py")
    assert {"semantic_binding_fingerprint", "build_opportunity_semantic_document"} <= (
        referenced
    )
    assert "SEMANTIC_BINDING_VERSION" in referenced
    # No second digest implementation inside Recommendation.
    for forbidden in ("sha256", "hashlib", "canonical_semantic_binding_payload"):
        assert forbidden not in referenced
