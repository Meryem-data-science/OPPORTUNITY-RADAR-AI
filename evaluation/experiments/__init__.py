"""Phase 10.5: experiments over a frozen evaluation snapshot.

This package is an **offline layer over artefacts that are already frozen**:

    Phase 10.1 frozen dataset
        + Phase 10.2 human evidence
        + Phase 10.3 ranking metrics
        + Phase 10.4 BusinessMetricRun
            -> Phase 10.5 experiments

and never the reverse. Nothing here re-runs production: no Matching run, no
Recommendation run, no Qualification pass, no HTTP request, no SQLite write and
no change to a profile. Nothing in `services/` imports this package, and
`evaluation/__init__.py` deliberately does not re-export it — evaluation never
feeds production, and a root facade would be the first step towards something
importing it by accident.

    schema.py         the contract — versions, the five projections, the six
                      overlaps, the four ranking questions, the result shapes
                      and the validators that re-establish them. It opens
                      nothing and computes nothing.
    fingerprint.py    the identities — one per result, one per run, one for the
                      context — each with a canonical payload and a verifier
                      that recomputes the structure and the digest before
                      believing either.
    context.py        what one run has to work with, how it is established, and
                      the persistent binding a stored run keeps.
    projections.py    `compute_projection` — the one projection authority.
    overlaps.py       `compute_overlap` — the one overlap authority.
    ranking.py        `compute_ranking_experiment` — the adapter that asks Phase
                      10.3's four questions and owns no ranking formula.
    run.py            the atomic twelve-block run, and the two verifications.
    storage.py        one immutable content-addressed document per run.
    report.py         a derived, non-authoritative Markdown view.
    error_analysis.py a derived, non-persisted diagnostic view.

## The ideas worth remembering

**Two kinds of experiment, strictly apart.** Stage-selection experiments ask
which postings a stage *included* — a membership question, which has no
Precision@K because a set has no order. The observed ranking experiment asks how
good the frozen Recommendation ordering is, and it is the only place the three
ranking metrics appear. `opportunity_id ASC` is a serialization; `match_quality
DESC` is not a ranking this package can state; and a posting the Recommendation
run never covered is **not in the ranking** rather than last in it.

**Five projections, independent, never a funnel.** Each is a projection of the
same frozen cohort and none composes with another. The only comparisons the
contract can express are membership, non-membership, cohort share and overlap —
there is no field, status or reason code anywhere in it that names a conversion,
a drop-off, a retention or an improvement caused by anything.

**UNKNOWN is not FALSE, and absence is not contradiction.** A posting nobody
could place is *included* by `GEO_NOT_EXPLICITLY_OUT_OF_TARGET`; a posting the
classifier never read is `UNCLASSIFIED` and *included* by
`DATA_AI_NOT_EXPLICITLY_OUT_OF_SCOPE`. The excluded set of each is exactly what
the pipeline made an explicit negative statement about. But evidence that
*contradicts itself* — a segment RESOLVED to no country, a ranking whose members
are not the observed recommendation set, a projection naming a posting the
cohort does not hold — is a hard error and never an `N_A`.

**`N_A` is a result that is present.** A run holds twelve blocks whatever
happens, and an `N_A` block counts towards them. It states a reason and
fabricates nothing: no empty membership, no zero count, no `0/0` reported as
`0.0`.

**A dataclass is not a proof token.** `ExperimentRunContext` is public and
anybody can construct one, so every public computation re-verifies it on every
call — which re-verifies the bound Phase 10.4 run in full, over the very records
in hand. A builder exists for convenience, never as a credential.

**Phase 10.5 owns no formula.** The four ranking numbers come from
`evaluation.metrics`' own `precision_at_k`, `recall_at_k` and `ndcg_at_k`, with
Phase 10.3's `effective_k`, Phase 10.3's gates and Phase 10.3's support blocks,
stored exactly as returned. The geography and Data/AI readings come from
`evaluation.frozen_facts`, the module Phase 10.4's own rates read — so the two
phases cannot come to disagree about which postings are out of target or out of
scope, and the D42 cross-checks that hold them against each other are real
checks rather than tautologies.

**A run is all twelve blocks or no run at all.** A hard error anywhere leaves
nothing: no partial run, no stored failed run, no selected subset, no optional
projection. And there is no global status, no computed/`N_A` tally and no overall
score — a run is twelve separate statements about one cohort, and a single figure
summarising them would be a number nobody could re-derive.

**The verifiers recompute; they never trust a declared digest.** Every child
fingerprint is recomputed before the parent's, every overlap's partitions are
re-derived from the run's own projections, and the full verifier recomputes all
twelve blocks over the frozen artefacts. A forged membership with an impeccable
digest is exactly what that exists to catch. There is no `strict=False`, no
`repair=True` and no `allow_partial` anywhere.

**Reports and error analyses are derived, not authoritative.** They full-verify
first, they invent no baseline ranking, no global score, no causal claim and no
extra metric, and they are not stored, not fingerprinted and not content
addressed.
"""

from .context import (
    ExperimentRunContext,
    RankingEvaluationInputs,
    build_experiment_run_context,
    experiment_run_context_binding,
    verify_experiment_run_context,
)
from .error_analysis import (
    ErrorAnalysis,
    FalseNegativeCandidate,
    FalsePositiveCandidate,
    GeoDiagnostics,
    JudgedRankingInversion,
    RankingDiagnostics,
    SkillDiagnostics,
    SourceDiagnostics,
    build_error_analysis,
)
from .fingerprint import (
    EXPERIMENT_PLACEHOLDER_FINGERPRINT,
    canonical_experiment_run_context_payload,
    canonical_experiment_run_payload,
    canonical_overlap_result_payload,
    canonical_projection_result_payload,
    canonical_ranking_experiment_result_payload,
    experiment_run_context_fingerprint,
    experiment_run_fingerprint,
    overlap_result_fingerprint,
    projection_result_fingerprint,
    ranking_experiment_result_fingerprint,
    verify_experiment_run_context_fingerprint,
    verify_experiment_run_fingerprint,
    verify_overlap_against_projections,
    verify_overlap_result_fingerprint,
    verify_projection_result_fingerprint,
    verify_ranking_experiment_result_fingerprint,
)
from .overlaps import compute_overlap
from .projections import compute_projection
from .ranking import compute_ranking_experiment
from .report import (
    ExperimentReport,
    build_experiment_report,
    render_experiment_report_markdown,
)
from .run import (
    build_experiment_run,
    verify_experiment_run,
    verify_experiment_run_structure,
)
from .schema import (
    EXPERIMENT_CONTRACT_VERSION,
    EXPERIMENT_RANKING_SOURCE,
    EXPERIMENT_RANKING_UNIVERSE_KIND,
    EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION,
    EXPERIMENT_RUN_SCHEMA_VERSION,
    OVERLAP_PAIR_ORDER,
    OVERLAP_PAIR_PROJECTIONS,
    OVERLAP_RESULT_SCHEMA_VERSION,
    PROJECTION_ORDER,
    PROJECTION_RESULT_SCHEMA_VERSION,
    RANKING_EXPERIMENT_QUESTIONS,
    RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION,
    SUPPORTED_EXPERIMENT_CONTRACT_VERSIONS,
    SUPPORTED_EXPERIMENT_RUN_SCHEMA_VERSIONS,
    CohortProjection,
    DirectionalRate,
    DirectionalRateName,
    ExperimentArgumentError,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRun,
    ExperimentRunContextBinding,
    ExperimentRunProvenance,
    ExperimentsError,
    OverlapPair,
    OverlapResult,
    OverlapStatus,
    OverlapUnavailableReason,
    ProjectionResult,
    ProjectionStatus,
    ProjectionUnavailableReason,
    RankingExperimentResult,
    RankingExperimentStatus,
    RankingExperimentUnavailableReason,
    RankingMetricEntry,
    canonical_experiment_opportunity_ids,
    directional_rate_payload,
    experiment_run_context_binding_payload,
    experiment_run_fingerprint_payload,
    experiment_run_payload,
    experiment_run_provenance_payload,
    overlap_pair_projections,
    overlap_result_payload,
    projection_result_payload,
    ranking_experiment_result_payload,
    ranking_metric_entry_payload,
    require_supported_experiment_contract_version,
    require_supported_experiment_run_schema_version,
    validate_cohort_size,
    validate_experiment_count,
    validate_experiment_fingerprint,
    validate_experiment_opportunity_id,
    validate_experiment_run_structure,
    validate_overlap_result_structure,
    validate_projection_result_structure,
    validate_ranking_experiment_result_structure,
)
from .storage import (
    DEFAULT_EXPERIMENT_RUN_ROOT,
    ExperimentRunStorageResult,
    ExperimentRunWriteStatus,
    read_experiment_run,
    write_experiment_run,
)

#: The frozen public surface. The computation authorities are the eleven
#: functions D52 names; everything else here is the contract they speak in — the
#: dataclasses a stored document parses back into, the closed enums, the
#: canonical payloads a test asserts on, and the validators.
#:
#: What is deliberately **not** here: every registry, every resolver and every
#: sealer. There is no `make_projection_result`, no `make_overlap_result` and no
#: `seal_result(value=...)`, because a caller who could reach one would have a
#: second way to publish a result — one that had not re-verified its context and
#: had not gone through the closed registry. `_partitions_against` is the one
#: shared derivation and it is exported only as `partitions_against`'s verifier
#: uses it, under `verify_overlap_against_projections`.
__all__ = [
    "DEFAULT_EXPERIMENT_RUN_ROOT",
    "EXPERIMENT_CONTRACT_VERSION",
    "EXPERIMENT_PLACEHOLDER_FINGERPRINT",
    "EXPERIMENT_RANKING_SOURCE",
    "EXPERIMENT_RANKING_UNIVERSE_KIND",
    "EXPERIMENT_RUN_CONTEXT_SCHEMA_VERSION",
    "EXPERIMENT_RUN_SCHEMA_VERSION",
    "OVERLAP_PAIR_ORDER",
    "OVERLAP_PAIR_PROJECTIONS",
    "OVERLAP_RESULT_SCHEMA_VERSION",
    "PROJECTION_ORDER",
    "PROJECTION_RESULT_SCHEMA_VERSION",
    "RANKING_EXPERIMENT_QUESTIONS",
    "RANKING_EXPERIMENT_RESULT_SCHEMA_VERSION",
    "SUPPORTED_EXPERIMENT_CONTRACT_VERSIONS",
    "SUPPORTED_EXPERIMENT_RUN_SCHEMA_VERSIONS",
    "CohortProjection",
    "DirectionalRate",
    "DirectionalRateName",
    "ErrorAnalysis",
    "ExperimentArgumentError",
    "ExperimentBindingError",
    "ExperimentContractError",
    "ExperimentReport",
    "ExperimentRun",
    "ExperimentRunContext",
    "ExperimentRunContextBinding",
    "ExperimentRunProvenance",
    "ExperimentRunStorageResult",
    "ExperimentRunWriteStatus",
    "ExperimentsError",
    "FalseNegativeCandidate",
    "FalsePositiveCandidate",
    "GeoDiagnostics",
    "JudgedRankingInversion",
    "OverlapPair",
    "OverlapResult",
    "OverlapStatus",
    "OverlapUnavailableReason",
    "ProjectionResult",
    "ProjectionStatus",
    "ProjectionUnavailableReason",
    "RankingDiagnostics",
    "RankingEvaluationInputs",
    "RankingExperimentResult",
    "RankingExperimentStatus",
    "RankingExperimentUnavailableReason",
    "RankingMetricEntry",
    "SkillDiagnostics",
    "SourceDiagnostics",
    "build_error_analysis",
    "build_experiment_report",
    "build_experiment_run",
    "build_experiment_run_context",
    "canonical_experiment_opportunity_ids",
    "canonical_experiment_run_context_payload",
    "canonical_experiment_run_payload",
    "canonical_overlap_result_payload",
    "canonical_projection_result_payload",
    "canonical_ranking_experiment_result_payload",
    "compute_overlap",
    "compute_projection",
    "compute_ranking_experiment",
    "directional_rate_payload",
    "experiment_run_context_binding",
    "experiment_run_context_binding_payload",
    "experiment_run_context_fingerprint",
    "experiment_run_fingerprint",
    "experiment_run_fingerprint_payload",
    "experiment_run_payload",
    "experiment_run_provenance_payload",
    "overlap_pair_projections",
    "overlap_result_fingerprint",
    "overlap_result_payload",
    "projection_result_fingerprint",
    "projection_result_payload",
    "ranking_experiment_result_fingerprint",
    "ranking_experiment_result_payload",
    "ranking_metric_entry_payload",
    "read_experiment_run",
    "render_experiment_report_markdown",
    "require_supported_experiment_contract_version",
    "require_supported_experiment_run_schema_version",
    "validate_cohort_size",
    "validate_experiment_count",
    "validate_experiment_fingerprint",
    "validate_experiment_opportunity_id",
    "validate_experiment_run_structure",
    "validate_overlap_result_structure",
    "validate_projection_result_structure",
    "validate_ranking_experiment_result_structure",
    "verify_experiment_run",
    "verify_experiment_run_context",
    "verify_experiment_run_context_fingerprint",
    "verify_experiment_run_fingerprint",
    "verify_experiment_run_structure",
    "verify_overlap_against_projections",
    "verify_overlap_result_fingerprint",
    "verify_projection_result_fingerprint",
    "verify_ranking_experiment_result_fingerprint",
    "write_experiment_run",
]
