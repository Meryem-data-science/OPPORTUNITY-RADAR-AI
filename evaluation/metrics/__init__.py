"""Phase 10.3a: the offline metric run contract, and the availability gates.

This package decides **whether** a ranking metric may be reported for a frozen
evaluation run. It does not compute one. There is no Precision@K, no Recall@K,
no DCG, no IDCG, no NDCG, no baseline, no ablation, no business KPI and no error
analysis in it, and that absence is the deliverable: the point of this slice is
to make the later formulas difficult to misuse.

    frozen Phase 10.1 dataset
        -> evaluation universe + frozen ranking projection   (run.py)
        -> Phase 10.2 label coverage                         (run.py)
        -> a bound, digested evaluation run                  (schema.py)
        -> COMPUTED / N_A availability decisions             (availability.py)

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
number of relevant items is a lower bound presented as a total.

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

## What a result may say

`COMPUTED` or `N_A`, with a stable reason code from a closed vocabulary and a
support block — `k_requested`, `k_effective`, `judged_count`, `universe_size`
and, when a formula eventually exists, its numerator and denominator. An
unavailable metric is never a silently approximated number and never a zero.
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
    assert_ranking_within_universe,
    build_evaluation_ranking,
    build_evaluation_run,
    build_evaluation_universe,
    build_label_coverage,
    ranking_from_frozen_dataset,
    verify_evaluation_run,
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
    MetricStatus,
    MetricSupport,
    MetricUnavailableReason,
    RankingSource,
    UnjudgedOpportunityError,
    evaluation_ranking_payload,
    evaluation_run_payload,
    evaluation_universe_payload,
    is_relevant_grade,
    metric_result_payload,
    metric_support_payload,
    require_evidence_class,
    require_supported_metric_contract_version,
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
    "MetricStatus",
    "MetricSupport",
    "MetricUnavailableReason",
    "RankingSource",
    "UnjudgedOpportunityError",
    "assert_ranking_within_universe",
    "build_evaluation_ranking",
    "build_evaluation_run",
    "build_evaluation_universe",
    "build_label_coverage",
    "canonical_evaluation_ranking_payload",
    "canonical_evaluation_run_payload",
    "canonical_evaluation_universe_payload",
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
    "ndcg_at_k_availability",
    "precision_at_k_availability",
    "ranking_from_frozen_dataset",
    "recall_at_k_availability",
    "relevant_count_in_universe",
    "require_evidence_class",
    "require_supported_metric_contract_version",
    "top_k_judged_coverage",
    "universe_judged_coverage",
    "validate_fingerprint",
    "validate_k",
    "validate_opportunity_id",
    "validate_rank_position",
    "verify_evaluation_ranking_fingerprint",
    "verify_evaluation_run",
    "verify_evaluation_run_fingerprint",
    "verify_evaluation_universe_fingerprint",
]
