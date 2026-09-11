"""Phase 10.3: offline ranking metrics for a frozen evaluation run.

This package decides **whether** a ranking metric may be reported, and then
computes it. The two are separate jobs in separate modules, and the separation
is the design rather than an accident of how it was built:

    schema.py       the contract — objects, versions, statuses, reason codes,
                    and the frozen definitions (relevance threshold, NDCG gain,
                    positional discount). It computes nothing.
    run.py          builders and verifiers — universes, rankings, label
                    coverage, evaluation runs, and the context verification
                    every decision rests on.
    availability.py the gates — given this universe, this ranking and these
                    judgements, is the metric knowable at all? They verify the
                    whole context and decide; they compute no score.
    formulas.py     the three metrics — Precision@K, Recall@K, NDCG@K — each of
                    which asks its own gate first and computes only what the
                    gate authorised.

There is still no baseline, no ablation, no business KPI and no error analysis.

    frozen Phase 10.1 dataset
        -> evaluation universe + frozen ranking projection   (run.py)
        -> Phase 10.2 label coverage                         (run.py)
        -> a bound, digested evaluation run                  (schema.py)
        -> AVAILABLE / N_A gate decisions                    (availability.py)
        -> COMPUTED / N_A metric results                     (formulas.py)

and never the reverse. Nothing in `services/` imports this package; nothing here
opens SQLite, writes a label, or touches Recommendation, Matching,
Qualification, Geography or Eligibility. A metric run is decided from a frozen
directory and a label file, on a machine that has those two things and nothing
else.

## The five ideas worth remembering

**This package evaluates frozen outputs, offline.** The ranking under
evaluation is the `recommendation` block Phase 10.1 already froze on each
record, read as a deterministic projection of a snapshot. The live engine is
never queried, never re-run and never told this package exists.

**The evaluation universe is not the labelled subset.** A universe is declared
first — the whole frozen cohort, or a pool fixed in advance — and the labels are
measured against it. Define it as "the ids that happen to have labels" and every
coverage is 100%, every denominator is known and every metric is available,
which is exactly how incomplete evidence turns into a benchmark claim.

**UNJUDGED is not 0.** An opportunity nobody judged has no label row in Phase
10.2 and no grade here. `LabelCoverage.grade` raises for one; there is no
default, no `Optional`, and no fallback that reads an unanswered question as a
negative answer.

**NDCG needs more than a judged top K.** Precision asks only about what was
shown, so a fully judged top K is enough for it. NDCG is a ratio against the
ideal ordering, and the ideal ordering is drawn from the best grades anywhere in
the comparison universe: an unjudged tail cannot lower the DCG but does lower
the IDCG this build could construct, and a ratio with an understated denominator
is an overstated score. So NDCG's safe rule is a fully judged universe, and
Recall's is the same universe for a different reason — without it the total
number of relevant items is a lower bound presented as a total. When that fully
judged universe turns out to have an ideal ordering worth nothing at the
cut-off, NDCG is `N_A / ZERO_IDEAL_DCG` rather than a division by zero; that is
a different fact from Recall's `NO_RELEVANT_ITEMS`, because relevance is binary
at `grade >= 2` while `gain(1)` is 1.

**Calibration evidence is not an independent benchmark.** The existing
`human-relevance-calibration-v0` round — twelve judgements, AI-assisted with
final human validation — is legitimate diagnostic evidence and says so in its
own recorded provenance. `EvidenceClass` makes that a declaration bound into the
run fingerprint rather than an inference from a label count, and
`require_evidence_class` fails closed: no label protocol in this build can
establish `INDEPENDENT_BENCHMARK`, and the known calibration labelset is refused
by name. Nothing here migrates, converts, duplicates or rewrites a v0 label, and
no v1 label is written: a real benchmark needs a separately stored labelset that
does not exist yet.

**Verification has an explicit trust boundary.** `verify_*_structure` checks
what an artefact can establish about itself — contract versions, structure,
recomputed digests, internal agreement. It cannot check that an opportunity id
names a real posting, because a SHA-256 identifies content it does not hold. The
dataset-bound verifiers — `verify_evaluation_run(run, dataset)`,
`verify_label_coverage(coverage, dataset)` — take the verified frozen dataset
and ask it: every universe and ranking id is in the cohort, and the calibration
lot is redrawn by Phase 10.2's own selector. `verify_metric_run_context` runs all of
it over a context bundle — the dataset, the run and the coverage — and **every
availability gate calls it first**. A `MetricRunContext` is a public dataclass,
so holding one proves nothing and nothing is inferred from it; the verification
is unavoidable because the gate performs it, not because the argument is hard to
construct. The labelset-match state the gates use is that verifier's return
value, derived from two verified artefacts rather than set by a caller.

**Nothing declares its own identity.** A universe, a ranking and a run each
carry a fingerprint field, and every one of them is recomputed before it is
believed — together with the *structure* it claims, because a digest taken over
an invalid artefact is a valid digest. A label coverage is held to the same
rule: it holds exactly the effective judgements of one verified calibration lot,
its labelset digest is recomputed from those judgements through Phase 10.2's own
`labelset_fingerprint`, and a run can only be bound to a coverage that survived
that. There is no builder path that accepts a bare fingerprint string with no
labels behind it.

## What a result may say

`COMPUTED` or `N_A`, with a stable reason code from a closed vocabulary and a
support block — `k_requested`, `k_effective`, `judged_count`, `universe_size`
and, for a computed metric, its numerator and denominator. An unavailable
metric is never a silently approximated number and never a zero.
"""

from .availability import (
    effective_k,
    judged_coverage_of,
    judged_count_in_universe,
    ndcg_at_k_availability,
    precision_at_k_availability,
    recall_at_k_availability,
    relevant_count_in_universe,
    top_k_judged_coverage,
    universe_judged_coverage,
)
from .formulas import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from .fingerprint import (
    canonical_evaluation_ranking_payload,
    canonical_evaluation_run_payload,
    canonical_evaluation_universe_payload,
    evaluation_ranking_fingerprint,
    evaluation_run_fingerprint,
    evaluation_universe_fingerprint,
    verify_evaluation_ranking_fingerprint,
    verify_evaluation_run_fingerprint,
    verify_evaluation_universe_fingerprint,
)
from .run import (
    assert_ranking_against_dataset,
    assert_ranking_within_universe,
    assert_universe_against_dataset,
    build_evaluation_ranking,
    build_evaluation_run,
    build_evaluation_universe,
    build_label_coverage,
    build_metric_run_context,
    ranking_from_frozen_dataset,
    verify_evaluation_run,
    verify_evaluation_run_structure,
    verify_label_coverage,
    verify_label_coverage_structure,
    verify_metric_run_context,
)
from .schema import (
    BINARY_RELEVANCE_GRADE_NAME,
    EVALUATION_RANKING_VERSION,
    EVALUATION_RUN_SCHEMA_VERSION,
    EVALUATION_UNIVERSE_VERSION,
    INDEPENDENT_BENCHMARK_PROTOCOL_VERSIONS,
    METRIC_CONTRACT_VERSION,
    RELEVANT_GRADE_THRESHOLD,
    SUPPORTED_METRIC_CONTRACT_VERSIONS,
    EvaluationBindingError,
    EvaluationMetricsError,
    EvaluationRanking,
    EvaluationRankingEntry,
    EvaluationRunContract,
    EvaluationRunProvenance,
    EvaluationUniverse,
    EvaluationUniverseKind,
    EvidenceClass,
    EvidenceClassError,
    JudgedCoverage,
    LabelCoverage,
    MetricArgumentError,
    MetricAvailability,
    MetricAvailabilityDecision,
    MetricContractError,
    MetricName,
    MetricResult,
    MetricRunContext,
    MetricStatus,
    MetricSupport,
    MetricUnavailableReason,
    RankingSource,
    UnjudgedOpportunityError,
    canonical_opportunity_ids,
    evaluation_ranking_payload,
    evaluation_run_payload,
    evaluation_universe_payload,
    is_relevant_grade,
    metric_result_payload,
    metric_support_payload,
    rank_discount,
    relevance_gain,
    require_evidence_class,
    require_supported_metric_contract_version,
    validate_declared_size,
    validate_evaluation_ranking_structure,
    validate_evaluation_universe_structure,
    validate_fingerprint,
    validate_k,
    validate_opportunity_id,
    validate_rank_position,
)

__all__ = [
    "BINARY_RELEVANCE_GRADE_NAME",
    "EVALUATION_RANKING_VERSION",
    "EVALUATION_RUN_SCHEMA_VERSION",
    "EVALUATION_UNIVERSE_VERSION",
    "INDEPENDENT_BENCHMARK_PROTOCOL_VERSIONS",
    "METRIC_CONTRACT_VERSION",
    "RELEVANT_GRADE_THRESHOLD",
    "SUPPORTED_METRIC_CONTRACT_VERSIONS",
    "EvaluationBindingError",
    "EvaluationMetricsError",
    "EvaluationRanking",
    "EvaluationRankingEntry",
    "EvaluationRunContract",
    "EvaluationRunProvenance",
    "EvaluationUniverse",
    "EvaluationUniverseKind",
    "EvidenceClass",
    "EvidenceClassError",
    "JudgedCoverage",
    "LabelCoverage",
    "MetricArgumentError",
    "MetricAvailability",
    "MetricAvailabilityDecision",
    "MetricContractError",
    "MetricName",
    "MetricResult",
    "MetricRunContext",
    "MetricStatus",
    "MetricSupport",
    "MetricUnavailableReason",
    "RankingSource",
    "UnjudgedOpportunityError",
    "assert_ranking_against_dataset",
    "assert_ranking_within_universe",
    "assert_universe_against_dataset",
    "build_evaluation_ranking",
    "build_evaluation_run",
    "build_evaluation_universe",
    "build_label_coverage",
    "build_metric_run_context",
    "canonical_evaluation_ranking_payload",
    "canonical_evaluation_run_payload",
    "canonical_evaluation_universe_payload",
    "canonical_opportunity_ids",
    "effective_k",
    "evaluation_ranking_fingerprint",
    "evaluation_ranking_payload",
    "evaluation_run_fingerprint",
    "evaluation_run_payload",
    "evaluation_universe_fingerprint",
    "evaluation_universe_payload",
    "is_relevant_grade",
    "judged_coverage_of",
    "judged_count_in_universe",
    "metric_result_payload",
    "metric_support_payload",
    "ndcg_at_k",
    "ndcg_at_k_availability",
    "precision_at_k",
    "precision_at_k_availability",
    "rank_discount",
    "ranking_from_frozen_dataset",
    "recall_at_k",
    "recall_at_k_availability",
    "relevance_gain",
    "relevant_count_in_universe",
    "require_evidence_class",
    "require_supported_metric_contract_version",
    "top_k_judged_coverage",
    "universe_judged_coverage",
    "validate_declared_size",
    "validate_evaluation_ranking_structure",
    "validate_evaluation_universe_structure",
    "validate_fingerprint",
    "validate_k",
    "validate_opportunity_id",
    "validate_rank_position",
    "verify_evaluation_ranking_fingerprint",
    "verify_evaluation_run",
    "verify_evaluation_run_fingerprint",
    "verify_evaluation_run_structure",
    "verify_evaluation_universe_fingerprint",
    "verify_label_coverage",
    "verify_label_coverage_structure",
    "verify_metric_run_context",
]
