"""Phase 10.1: the evaluation boundary, and the frozen real dataset behind it.

This package reads the operational database and the signals the pipeline has
already persisted, and freezes them into a versioned, reproducible snapshot that
later Phase 10 slices measure against. The dependency runs one way only:

    operational SQLite / existing pipeline outputs
        -> evaluation snapshot (here)
            -> human labelling, offline metrics, baselines, error analysis

It sits inside the repository's existing evaluation layer rather than beside
the production services, and that is the point: `evaluation/` is already the
non-production side of the arrow, and `test_production_never_depends_on_the_
evaluation_layer` already refuses any import of it from `services/`. Phase 10.1
inherits that guarantee instead of restating it. Nothing here writes to the
operational database — the extraction opens a `mode=ro` connection and runs
`SELECT`s — and nothing here reads a human label: none exists in this slice, and
the contract does not mention one.

What this slice does **not** contain, deliberately: no labels, no labelling UI,
no Precision@K, no NDCG, no recall, no business metric, no baseline, no
experiment runner, no error analysis, no scoring of any kind, and no change to
Geo, Qualification, Eligibility, Matching or Recommendation. Those are later
slices, and each of them starts from a dataset produced here.
"""

from .cohort import (
    EVALUATION_COHORT_QUALIFICATIONS,
    count_excluded_opportunities,
    select_evaluation_cohort_ids,
)
from .profile_context import (
    PROFILE_CONTEXT_VERSION,
    canonical_profile_context_payload,
    profile_context_fingerprint,
    read_profile_context,
)
from .fingerprint import (
    canonical_evaluation_content_payload,
    evaluation_content_fingerprint,
    evaluation_record_fingerprint,
)
from .schema import (
    EVALUATION_CANONICAL_ORDER,
    EVALUATION_COHORT_CRITERIA,
    EVALUATION_COHORT_VERSION,
    EVALUATION_DATASET_SCHEMA_VERSION,
    EvaluationCohortDefinition,
    EvaluationDataset,
    EvaluationDatasetError,
    EvaluationDatasetManifest,
    EvaluationEligibilityRecord,
    EvaluationGeographySegmentRecord,
    EvaluationMatchingRecord,
    EvaluationOpportunityRecord,
    EvaluationProfileContext,
    EvaluationQualificationRecord,
    EvaluationRecommendationRecord,
    EvaluationSourceIdentity,
    EvaluationSourceRecord,
    EvaluationUpstreamProvenance,
    evaluation_manifest_payload,
    evaluation_record_payload,
    require_supported_schema_version,
)
from .snapshot import build_evaluation_dataset, resolve_git_commit
from .storage import (
    DEFAULT_EVALUATION_DATASET_ROOT,
    WRITE_STATUS_CREATED,
    WRITE_STATUS_UNCHANGED,
    EvaluationDatasetPaths,
    write_evaluation_dataset,
)

__all__ = [
    "DEFAULT_EVALUATION_DATASET_ROOT",
    "EVALUATION_CANONICAL_ORDER",
    "EVALUATION_COHORT_CRITERIA",
    "EVALUATION_COHORT_QUALIFICATIONS",
    "EVALUATION_COHORT_VERSION",
    "EVALUATION_DATASET_SCHEMA_VERSION",
    "PROFILE_CONTEXT_VERSION",
    "WRITE_STATUS_CREATED",
    "WRITE_STATUS_UNCHANGED",
    "EvaluationCohortDefinition",
    "EvaluationDataset",
    "EvaluationDatasetError",
    "EvaluationDatasetManifest",
    "EvaluationDatasetPaths",
    "EvaluationEligibilityRecord",
    "EvaluationGeographySegmentRecord",
    "EvaluationMatchingRecord",
    "EvaluationOpportunityRecord",
    "EvaluationProfileContext",
    "EvaluationQualificationRecord",
    "EvaluationRecommendationRecord",
    "EvaluationSourceIdentity",
    "EvaluationSourceRecord",
    "EvaluationUpstreamProvenance",
    "build_evaluation_dataset",
    "canonical_evaluation_content_payload",
    "canonical_profile_context_payload",
    "count_excluded_opportunities",
    "evaluation_content_fingerprint",
    "evaluation_manifest_payload",
    "evaluation_record_fingerprint",
    "evaluation_record_payload",
    "profile_context_fingerprint",
    "read_profile_context",
    "require_supported_schema_version",
    "resolve_git_commit",
    "select_evaluation_cohort_ids",
    "write_evaluation_dataset",
]
