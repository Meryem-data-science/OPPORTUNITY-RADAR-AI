"""Inert staging for replacing a profile's CV with a different document.

Phase 11.3A-R4C-B1a. Everything in this package prepares and reviews; nothing
in it activates. Preparing, reviewing, cancelling and restarting leave
`profile_facts`, `profile_fact_provenance` and every preference, availability
and mobility row exactly as they were — there is no code path here that writes
to any of them, and a test reads the SQL to keep it that way.

The public surface is small on purpose:

    repository   documents, extraction campaigns, attempts, stored decisions
    manifest     the ordered, chained record of what a document was read as
    planning     what a replacement would change, computed read-only
    staging      recording a human's answers, and holding them

Activation, downstream invalidation and any API or screen are deliberately
absent, and adding one here would defeat the point of this slice.
"""

from services.digital_twin.cv.replacement.models import (
    ActiveCvDocumentError,
    ContradictoryReviewError,
    CvDocument,
    CvDocumentNotFoundError,
    CvExtraction,
    CvStagingError,
    DecisionNotPermittedError,
    DecisionRole,
    DifferenceKind,
    DocumentLifecycle,
    DocumentOrigin,
    EffectiveReplacementState,
    ExtractionNotFoundError,
    ManifestEntry,
    ManifestIntegrityError,
    ManifestState,
    ManifestVerification,
    OpenReplacementExistsError,
    PlanEntry,
    Replacement,
    ReplacementAlreadyActivatedError,
    ReplacementClosedError,
    ReplacementLifecycle,
    ReplacementNotFoundError,
    ReplacementPlan,
    ReviewDecision,
    ReviewIncompleteError,
    StagedDecision,
    TerminalFactError,
)

__all__ = [
    "ActiveCvDocumentError",
    "ContradictoryReviewError",
    "CvDocument",
    "CvDocumentNotFoundError",
    "CvExtraction",
    "CvStagingError",
    "DecisionNotPermittedError",
    "DecisionRole",
    "DifferenceKind",
    "DocumentLifecycle",
    "DocumentOrigin",
    "EffectiveReplacementState",
    "ExtractionNotFoundError",
    "ManifestEntry",
    "ManifestIntegrityError",
    "ManifestState",
    "ManifestVerification",
    "OpenReplacementExistsError",
    "PlanEntry",
    "Replacement",
    "ReplacementAlreadyActivatedError",
    "ReplacementClosedError",
    "ReplacementLifecycle",
    "ReplacementNotFoundError",
    "ReplacementPlan",
    "ReviewDecision",
    "ReviewIncompleteError",
    "StagedDecision",
    "TerminalFactError",
]
