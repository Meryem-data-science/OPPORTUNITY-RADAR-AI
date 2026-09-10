"""Phase 9 — the personalized recommendation engine, and its storage layer.

Phase 8 answers *what is this opportunity*. This package answers the other half:

    among the Data & AI opportunities that are relevant at all, which ones fit
    **this** profile, its CV-derived facts and its declared preferences?

It is an additive layer and it owns no new truth. Everything it reads was
established upstream and is read exactly as persisted:

    Phase 4 matching snapshot   required-skill fit, semantic percentile, coarse
                                domain alignment, opportunity-type signal
    Phase 8 fine classification which Data/AI sub-domain, read, never recomputed
    Phase 7 geography           the posting's stored location resolutions, and
                                the country the profile's mobility restricts to
    Phase 3.6 eligibility       the stored decision for this user and posting
    Phase 3 Digital Twin        the declared preferences, through Matching's own
                                read-only loader

and it produces one score, one route, one deterministic ranking and a set of
machine-readable reasons. There is no LLM anywhere in it, no embedding, no
similarity of its own, no generated sentence and no invented confidence.

One freshness question stays open, narrowly and on purpose. Opportunity-side
semantic drift **is** now detected, read-only, in both of its forms: content
drift — a posting whose title or description moved while its id stayed in the
cohort — by the corpus digest Matching computes before fitting, and
identity-binding drift — two postings exchanging their documents, which leaves
that digest untouched — by the ordered `(opportunity_id, document_hash)`
binding digest beside it. Profile-side semantic-document drift is **not**:
Matching v1 persists no standalone fingerprint of the profile's semantic
document, and refitting TF-IDF merely to discover one is not something this phase
may do. That one gap is documented rather than papered over, and closing it
belongs with Matching, not here.

What 9A deliberately does **not** do: **the engine** persists nothing, reads
nothing it was not handed, and depends on no clock.

Phase 9B.1 adds a storage layer beside it, and the boundary between the two is
the point of the split:

    engine 9A       pure, deterministic, read-only, id-free in its digests
    persistence 9B  additive, append-only, and the only part that writes

`persistence.py` receives a batch the engine already produced and never re-opens
it: no score, no disposition, no reason and no ranking is recomputed there. What
it adds is an **operational identity** — `recommendation_run_fingerprint`, in
`persistence_fingerprint.py` — that carries the profile, the source matching run
and the ranked opportunity ids that `recommendation_batch_fingerprint`
deliberately excludes. The content digest of 9A is untouched and must stay that
way; the two answer different questions and neither may stand in for the other.

Phase 9B.2a adds `read_model.py`: the strict, read-only view of what 9B.1 wrote.
It hands back a stored run with its ranking in the persisted order, the profile's
history, and the profile's current state — NOT_SYNCED, READY or INCOMPLETE — and
refuses a structure it cannot read safely rather than repairing it. It verifies
only what a safe read needs: shapes, the ranking's contiguity, the column-level
business contract and the *form* of every digest it exposes — and it verifies
them on the history listing exactly as strictly as on a full run. Each public
read is several queries and is taken as one deferred-read-transaction snapshot,
so no caller ever sees a projection blended from two database states.

Still absent, and belonging to later sub-phases: the persistence audit that
recomputes the three recommendation digests over a whole history (9B.2b), and the
freshness → compute → persist orchestration that writes the INCOMPLETE state
(9B.3) the read model above already knows how to report. This package still
exposes no HTTP route and changes no page.

**Matching v1's scoring contract is untouched**, which is what makes the
recommendation a controlled evolution of that baseline rather than a second
layer of bonuses stacked on top of `match_quality`: the same 50/30/20 weights,
the same `MATCHING_ENGINE_VERSION`, `MATCHING_RULES_VERSION`,
`SEMANTIC_PERCENTILE_VERSION`, `MATCHING_SELECTION_VERSION` and
`MATCHING_PERSISTENCE_VERSION`, the same TF-IDF configuration, and the same
percentile and scoring behaviour.

Matching did gain one **additive provenance** field, and it is not honest to
call it unchanged. `SEMANTIC_BINDING_VERSION = "semantic-binding-v1"` and its
`semantic_binding_fingerprint` are carried on a batch, stored inside the
existing `matching_runs.batch_payload_json` envelope, and protected by the run
fingerprint. No schema migration, no new column, no new table, and no value any
score depends on: it exists so this package can prove each semantic document is
still attached to the same posting, and it changes no Matching score and no
recommendation score. Runs persisted before it stay readable and auditable.

    models.py          the vocabulary: versions, enums, immutable values
    fine_domain.py     the fine -> canonical-family bridge, and the fit
    engine.py          the score, the disposition, the ranking, pure
    fingerprint.py     canonical payloads and their SHA-256 digests
    input_assembly.py  read-only assembly out of SQLite, and its readiness

    persistence_fingerprint.py  the operational identity of a stored run
    persistence.py              atomic, append-only, idempotent storage
    read_model.py               strict, read-only views of what was stored
"""

from services.recommendation.engine import (
    build_domain_component,
    build_eligibility_evidence,
    build_geography_signal,
    build_recommendation_assessment,
    build_recommendation_batch,
    build_skill_evidence,
    eligibility_signal_status,
    rank_recommendation_assessments,
    recommendation_disposition,
)
from services.recommendation.fine_domain import (
    FINE_DOMAIN_BRIDGE,
    FINE_DOMAIN_BRIDGE_VERSION,
    UNBRIDGED_FINE_CATEGORIES,
    FineDomainAvailability,
    FineDomainFit,
    FineDomainReason,
    build_fine_domain_fit,
    preferred_rank_score,
)
from services.recommendation.fingerprint import (
    canonical_recommendation_assessment_payload,
    canonical_recommendation_batch_payload,
    recommendation_assessment_fingerprint,
    recommendation_batch_fingerprint,
)
from services.recommendation.input_assembly import (
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    current_location_signature,
    current_semantic_binding_fingerprint,
    current_semantic_corpus_fingerprint,
    RecommendationInputAssemblyResult,
    RecommendationInputRecord,
    RecommendationOpportunityContext,
    RecommendationReadinessIssue,
    RecommendationReadinessIssueCode,
    RecommendationReadinessStatus,
    assemble_recommendation_inputs,
)
from services.recommendation.models import (
    DISPOSITION_PRECEDENCE,
    DISPOSITION_RANK,
    DOMAIN_WEIGHT,
    RECOMMENDATION_ENGINE_VERSION,
    RECOMMENDATION_RULES_VERSION,
    REQUIRED_SKILL_WEIGHT,
    SEMANTIC_WEIGHT,
    CONFIRMED_GAP_CODES,
    STRENGTH_CODES,
    UNKNOWN_CODES,
    ComponentStatus,
    DomainComponent,
    DomainFitSource,
    EligibilitySignal,
    EligibilitySignalStatus,
    GeographySignal,
    GeographyState,
    OpportunityTypeSignal,
    RecommendationAssessment,
    RecommendationBatchResult,
    RecommendationDisposition,
    RecommendationEligibilityEvidence,
    RecommendationEligibilityInput,
    RecommendationFineClassification,
    RecommendationGeographyInput,
    RecommendationInput,
    RecommendationInputError,
    RecommendationMatchingSnapshot,
    RecommendationReasonCode,
    RecommendationSkillEvidence,
    RequiredSkillComponent,
    SemanticComponent,
    WorkModeSignal,
)
from services.recommendation.persistence import (
    RecommendationPersistenceError,
    RecommendationStoreResult,
    store_recommendation_batch,
)
from services.recommendation.persistence_fingerprint import (
    RECOMMENDATION_PERSISTENCE_VERSION,
    canonical_recommendation_run_payload,
    recommendation_run_fingerprint,
)
from services.recommendation.read_model import (
    RecommendationAssessmentReadModel,
    RecommendationProfileReadModel,
    RecommendationReadError,
    RecommendationRunReadModel,
    RecommendationRunSummary,
    list_recommendation_runs,
    read_current_recommendation,
    read_recommendation_run,
)

__all__ = [
    "CONFIRMED_GAP_CODES",
    "DISPOSITION_PRECEDENCE",
    "DISPOSITION_RANK",
    "DOMAIN_WEIGHT",
    "FINE_DOMAIN_BRIDGE",
    "FINE_DOMAIN_BRIDGE_VERSION",
    "RECOMMENDATION_ENGINE_VERSION",
    "RECOMMENDATION_INPUT_ASSEMBLY_VERSION",
    "RECOMMENDATION_PERSISTENCE_VERSION",
    "RECOMMENDATION_RULES_VERSION",
    "REQUIRED_SKILL_WEIGHT",
    "SEMANTIC_WEIGHT",
    "STRENGTH_CODES",
    "UNBRIDGED_FINE_CATEGORIES",
    "UNKNOWN_CODES",
    "ComponentStatus",
    "DomainComponent",
    "DomainFitSource",
    "EligibilitySignal",
    "EligibilitySignalStatus",
    "FineDomainAvailability",
    "FineDomainFit",
    "FineDomainReason",
    "GeographySignal",
    "GeographyState",
    "OpportunityTypeSignal",
    "RecommendationAssessment",
    "RecommendationAssessmentReadModel",
    "RecommendationBatchResult",
    "RecommendationDisposition",
    "RecommendationEligibilityEvidence",
    "RecommendationEligibilityInput",
    "RecommendationFineClassification",
    "RecommendationGeographyInput",
    "RecommendationInput",
    "RecommendationInputAssemblyResult",
    "RecommendationInputError",
    "RecommendationInputRecord",
    "RecommendationMatchingSnapshot",
    "RecommendationOpportunityContext",
    "RecommendationPersistenceError",
    "RecommendationProfileReadModel",
    "RecommendationReadError",
    "RecommendationReadinessIssue",
    "RecommendationReadinessIssueCode",
    "RecommendationReadinessStatus",
    "RecommendationReasonCode",
    "RecommendationRunReadModel",
    "RecommendationRunSummary",
    "RecommendationSkillEvidence",
    "RecommendationStoreResult",
    "RequiredSkillComponent",
    "SemanticComponent",
    "WorkModeSignal",
    "assemble_recommendation_inputs",
    "build_domain_component",
    "build_eligibility_evidence",
    "build_fine_domain_fit",
    "build_geography_signal",
    "build_recommendation_assessment",
    "build_recommendation_batch",
    "build_skill_evidence",
    "canonical_recommendation_assessment_payload",
    "canonical_recommendation_batch_payload",
    "canonical_recommendation_run_payload",
    "current_location_signature",
    "current_semantic_binding_fingerprint",
    "current_semantic_corpus_fingerprint",
    "eligibility_signal_status",
    "list_recommendation_runs",
    "preferred_rank_score",
    "rank_recommendation_assessments",
    "read_current_recommendation",
    "read_recommendation_run",
    "recommendation_assessment_fingerprint",
    "recommendation_batch_fingerprint",
    "recommendation_disposition",
    "recommendation_run_fingerprint",
    "store_recommendation_batch",
]
