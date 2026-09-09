"""The readiness preflight: what stops a cohort before anything is assessed.

The per-opportunity branches are exercised against a real SQLite database in
`tests/integration/test_recommendation_sqlite.py`; what is covered here are the
whole-cohort refusals, which short-circuit before any row is read.
"""

import ast
import pathlib
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.collector.matching import (
    MATCHING_ENGINE_VERSION,
    MATCHING_PERSISTENCE_VERSION,
    MATCHING_RULES_VERSION,
    MATCHING_SELECTION_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
    MatchingInputError,
    MatchingPersistenceAuditError,
    MatchingReadError,
)
from services.digital_twin.preferences.models import ExplicitProfileInputError
from services.eligibility.inputs import EligibilityInputError
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
    """Answers the one query the short-circuit branches reach, and holds a
    snapshot the way SQLite does.

    `in_transaction`, `BEGIN` and `rollback` are modelled rather than stubbed
    away, because the assembly's ownership contract is part of what these tests
    exercise: it opens a transaction when the caller has none and releases it on
    every return path.
    """

    def __init__(self, owner=(41,)):
        self.owner = owner
        self.in_transaction = False
        self.began = 0
        self.rolled_back = 0

    def execute(self, sql, parameters=()):
        if sql.strip().upper() == "BEGIN":
            assert not self.in_transaction, "nested transaction"
            self.in_transaction = True
            self.began += 1
            return SimpleNamespace(fetchone=lambda: None)
        if "FROM profiles" in sql:
            return SimpleNamespace(fetchone=lambda: self.owner)
        raise AssertionError(f"unexpected query: {sql}")

    def rollback(self):
        self.in_transaction = False
        self.rolled_back += 1

    def commit(self):  # pragma: no cover - the assembly must never call this
        raise AssertionError("the assembly must not commit")


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
    connection = Connection(owner=None)
    result = invoke(monkeypatch, connection=connection)
    assert result.status is RecommendationReadinessStatus.INCOMPLETE
    assert codes(result) == [RecommendationReadinessIssueCode.PROFILE_NOT_FOUND]
    # Opened once before the first read, released on this early return.
    assert (connection.began, connection.rolled_back) == (1, 1)
    assert connection.in_transaction is False
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
    connection = Connection()
    snapshot = run(SimpleNamespace(opportunity_id=1))
    result = invoke(
        monkeypatch, connection=connection, snapshot=snapshot, selected=(1, 2)
    )
    assert codes(result) == [RecommendationReadinessIssueCode.STALE_MATCHING_COHORT]
    assert result.records == ()
    assert (connection.began, connection.rolled_back) == (1, 1)


def test_a_caller_owned_transaction_is_left_exactly_as_it_was(monkeypatch):
    """The assembly reads within somebody else's snapshot and never ends it."""
    connection = Connection()
    connection.execute("BEGIN")
    result = invoke(monkeypatch, connection=connection)
    assert result.status is RecommendationReadinessStatus.READY or result.issues
    assert connection.in_transaction is True
    # One BEGIN, the caller's; no nested begin and no rollback of it.
    assert (connection.began, connection.rolled_back) == (1, 0)


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


def test_the_snapshot_transaction_is_not_permission_to_write():
    """Holding a transaction is how the reads stay coherent, nothing more.

    The byte-for-byte read-only integration test is the real proof; this is the
    cheap static one, and it also pins the deferred `BEGIN` — an `IMMEDIATE` or
    `EXCLUSIVE` one would take a write lock and stall the collectors this phase
    is supposed to run alongside.
    """
    source = pathlib.Path("services/recommendation/input_assembly.py").read_text(
        encoding="utf-8"
    )
    statements = re.findall(r'"""(?:.|\n)*?"""|"[^"\n]*"', source)
    sql = " ".join(
        item.upper()
        for item in statements
        if not item.startswith('"""')
    )
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ", "ALTER "):
        assert verb not in sql, f"assembly SQL contains {verb.strip()}"
    assert "BEGIN IMMEDIATE" not in sql and "BEGIN EXCLUSIVE" not in sql

    referenced = _referenced_names("services/recommendation/input_assembly.py")
    # Released, never committed: this function has nothing of its own to commit.
    assert "rollback" in referenced and "commit" not in referenced
    # And it never drives an upstream synchronization.
    for forbidden in (
        "sync_matching",
        "persist_qualifications",
        "synchronize_eligibility",
        "synchronize_location_resolutions",
    ):
        assert forbidden not in referenced


# --------------------------------------------------------------------------
# The explicit-profile error boundary, one named owner call at a time.
#
# Four Recommendation-side reads reach the strict Phase 3.4C projections, and
# an integration test can only ever prove the first one that fails. These
# monkeypatch each owner boundary in turn, so every adapter is shown to hold on
# its own rather than being covered by the one before it.
# --------------------------------------------------------------------------

PROFILE_BOUNDARIES = (
    "load_profile_matching_input",
    "resolve_profile_target",
    "get_profile_preferences",
    "load_profile_input",
)


def _profile_reads(monkeypatch, failing, error):
    """Let every profile boundary answer, except the one under test."""
    for name in PROFILE_BOUNDARIES:
        if name == failing:

            def raiser(*_args, _error=error, **_kwargs):
                raise _error

            monkeypatch.setattr(
                f"services.recommendation.input_assembly.{name}", raiser
            )
        elif name == "get_profile_preferences":
            # None is the honest "this profile stated nothing" answer.
            monkeypatch.setattr(
                f"services.recommendation.input_assembly.{name}", lambda *_a, **_k: None
            )
        else:
            monkeypatch.setattr(
                f"services.recommendation.input_assembly.{name}",
                lambda *_a, **_k: SimpleNamespace(),
            )


@pytest.mark.parametrize("boundary", PROFILE_BOUNDARIES)
def test_every_profile_boundary_reports_an_unreadable_projection(
    monkeypatch, boundary
):
    """`ExplicitProfileInputError` becomes INVALID_UPSTREAM_VALUE, everywhere."""
    connection = Connection()
    _profile_reads(
        monkeypatch, boundary, ExplicitProfileInputError("TEST ONLY not canonical")
    )

    result = invoke(monkeypatch, connection=connection, selected=())

    assert result.status is RecommendationReadinessStatus.INCOMPLETE
    assert result.records == ()
    assert codes(result) == [
        RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE
    ]
    assert "TEST ONLY not canonical" in result.issues[0].message
    # The transaction this call opened is still released on the new paths.
    assert connection.began == 1
    assert connection.rolled_back == 1
    assert connection.in_transaction is False


def test_the_mobility_boundary_names_the_projection_it_could_not_read(monkeypatch):
    _profile_reads(
        monkeypatch,
        "resolve_profile_target",
        ExplicitProfileInputError("mobility.locations must be a JSON array"),
    )

    result = invoke(monkeypatch, selected=())

    assert codes(result) == [RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE]
    assert "mobility" in result.issues[0].message


def test_the_preferences_boundary_names_the_projection_it_could_not_read(monkeypatch):
    _profile_reads(
        monkeypatch,
        "get_profile_preferences",
        ExplicitProfileInputError("preferences.preferred_domains must be a JSON array"),
    )

    result = invoke(monkeypatch, selected=())

    assert codes(result) == [RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE]
    assert "preferences" in result.issues[0].message


def test_an_eligibility_input_gap_keeps_its_own_distinct_readiness_code(monkeypatch):
    """The two exceptions are not collapsed: they answer different questions.

    `EligibilityInputError` says Phase 3.6 does not have enough information
    about this profile. `ExplicitProfileInputError` says the persisted
    projection cannot be decoded at all. Reporting the second as the first
    would blame the eligibility inputs for an upstream corruption.
    """
    _profile_reads(
        monkeypatch,
        "load_profile_input",
        EligibilityInputError("TEST ONLY profile has no eligibility inputs"),
    )

    result = invoke(monkeypatch, selected=())

    assert result.status is RecommendationReadinessStatus.INCOMPLETE
    assert result.records == ()
    assert codes(result) == [
        RecommendationReadinessIssueCode.ELIGIBILITY_INPUT_INCOMPLETE
    ]


def test_a_matching_input_error_keeps_reporting_an_invalid_upstream_value(monkeypatch):
    """The pre-existing mapping at the first boundary is unchanged."""
    _profile_reads(
        monkeypatch,
        "load_profile_matching_input",
        MatchingInputError("TEST ONLY profile 41 does not exist"),
    )

    result = invoke(monkeypatch, selected=())

    assert codes(result) == [RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE]


@pytest.mark.parametrize("boundary", PROFILE_BOUNDARIES)
def test_an_unexpected_failure_at_a_profile_boundary_still_escapes(
    monkeypatch, boundary
):
    """The catches stay narrow: a programming defect must not become readiness.

    An unreadable projection is a fact about the data. A `TypeError` is a fact
    about this code, and turning it into an INCOMPLETE would hide it behind a
    verdict that looks like ordinary upstream staleness.
    """
    connection = Connection()
    _profile_reads(monkeypatch, boundary, TypeError("TEST ONLY coding defect"))

    with pytest.raises(TypeError, match="TEST ONLY coding defect"):
        invoke(monkeypatch, connection=connection, selected=())

    # And the snapshot is still released, through the same outer `finally`.
    assert connection.in_transaction is False
    assert connection.rolled_back == 1


def test_an_invalid_projection_does_not_end_a_caller_owned_transaction(monkeypatch):
    connection = Connection()
    connection.execute("BEGIN")
    assert connection.in_transaction is True
    _profile_reads(
        monkeypatch,
        "resolve_profile_target",
        ExplicitProfileInputError("TEST ONLY not canonical"),
    )

    result = invoke(monkeypatch, connection=connection, selected=())

    assert codes(result) == [RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE]
    assert connection.in_transaction is True
    assert connection.began == 1  # the caller's, not a nested one
    assert connection.rolled_back == 0


def _assembly_tree() -> ast.AST:
    source = pathlib.Path("services/recommendation/input_assembly.py").read_text()
    return ast.parse(source)


def _handler_types(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"<bare>"}
    parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {ast.unparse(part) for part in parts}


def _guards_of(tree: ast.AST, call: str) -> list[set[str]]:
    """The exception types of every `try` whose body calls `call`."""
    guards = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        called = {
            ast.unparse(inner.func)
            for inner in ast.walk(ast.Module(body=node.body, type_ignores=[]))
            if isinstance(inner, ast.Call)
        }
        if call in called:
            guards.append(set().union(*(_handler_types(h) for h in node.handlers)))
    return guards


def test_each_profile_boundary_translates_named_owner_exceptions_only():
    """Narrow on purpose: a coding defect must still fail loudly.

    `ExplicitProfileInputError` happens to subclass `ValueError`, so catching
    `ValueError` here would have worked and would also have swallowed every
    unrelated value bug in the same call. Each boundary names its owners.
    """
    tree = _assembly_tree()
    expected = {
        "load_profile_matching_input": {
            "MatchingInputError",
            "ExplicitProfileInputError",
        },
        "resolve_profile_target": {"ExplicitProfileInputError"},
        "get_profile_preferences": {"ExplicitProfileInputError"},
        "load_profile_input": {"ExplicitProfileInputError", "EligibilityInputError"},
    }
    for call, types in expected.items():
        guards = _guards_of(tree, call)
        assert guards, f"{call} is not inside a try block"
        assert set().union(*guards) == types, call


def test_no_broad_exception_class_guards_a_profile_boundary():
    """Nothing added for this boundary widens into a catch-all."""
    tree = _assembly_tree()
    for call in PROFILE_BOUNDARIES:
        for guard in _guards_of(tree, call):
            assert not guard & {
                "<bare>",
                "BaseException",
                "Exception",
                "ValueError",
                "RuntimeError",
                "sqlite3.Error",
                "AssertionError",
            }, call


def test_no_catch_all_exists_anywhere_in_the_assembly():
    """The two pre-existing `(ValueError, TypeError)` decode guards are not
    catch-alls and are unchanged; nothing in this module catches `Exception`."""
    tree = _assembly_tree()
    caught: set[str] = set()
    for handler in ast.walk(tree):
        if isinstance(handler, ast.ExceptHandler):
            caught |= _handler_types(handler)

    assert "ExplicitProfileInputError" in caught
    assert not caught & {"<bare>", "Exception", "BaseException", "RuntimeError"}
