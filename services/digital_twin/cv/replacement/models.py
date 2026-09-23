"""The vocabulary of an inert CV replacement: documents, manifests, decisions.

Nothing in this module reaches a database, and nothing in it decides anything.
It names the states the staging schema already constrains, so that the Python
side and `migrations/0027_cv_staging.sql` cannot drift apart without a test
noticing.

Every dataclass here that can hold CV text carries a `summary()` that does not.
That is the rule for this whole package: counters, canonical types, ordinals,
digests and ids are printable; `value`, `normalized_value` and `staged_value`
are not, and no flag turns that off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class CvStagingError(RuntimeError):
    """Base error for everything this package refuses to do."""


class CvDocumentNotFoundError(CvStagingError):
    """No such document for this profile. Never "some other profile's"."""


class ActiveCvDocumentError(CvStagingError):
    """The profile already has an active CV, or the one named is not active."""


class ManifestIntegrityError(CvStagingError):
    """The persisted manifest is not the one the extraction produced."""


class ExtractionNotFoundError(CvStagingError):
    """No such extraction campaign for this profile."""


class ReplacementNotFoundError(CvStagingError):
    """No such replacement attempt for this profile."""


class ReplacementClosedError(CvStagingError):
    """The attempt is closed; it answers no further decision.

    Cancelled, or activated. Both are terminal, for different reasons: a
    cancelled attempt was abandoned, an activated one was applied.
    """


class ReplacementAlreadyActivatedError(ReplacementClosedError):
    """The attempt was activated, and an activation is a historical event.

    Its review is closed, its decisions are the ones that were applied, and it
    can be neither re-opened, re-answered nor cancelled. Migration 0028 enforces
    the same thing with triggers; this is that refusal said earlier and by name.
    """


class OpenReplacementExistsError(CvStagingError):
    """One review at a time: a second open attempt would split the answers."""


class DecisionNotPermittedError(CvStagingError):
    """The decision does not fit what the plan says about its target."""


class TerminalFactError(DecisionNotPermittedError):
    """The reading resolves to a REJECTED or CORRECTED fact.

    Reopening one would turn a decision somebody already took into a fresh
    proposal. That needs a workflow of its own, which nobody has designed, so
    this package refuses rather than guessing which way the person meant it.
    """


class ReviewIncompleteError(CvStagingError):
    """Something in the review is still unanswered, or the plan moved."""


class ContradictoryReviewError(ReviewIncompleteError):
    """The review accepts a reading from the new CV and retires it at once.

    The same `(fact_type, value)` reaches the review twice — as a candidate of
    the new document and as the baseline fact that already holds it — and both
    answers are legal on their own. Together they are not: nothing can be both
    confirmed by the incoming CV and no longer part of the profile.

    A review in this state is incomplete in the only sense that matters: the
    person has not yet said what they want. So this refuses, names both targets
    and changes nothing — it never silently prefers one of the two answers.
    """


class StaleReviewDecisionError(ReviewIncompleteError):
    """A decision was taken against a state the database no longer holds.

    A fact that changed status, or that gained evidence no CV produced, makes
    the answer somebody gave about it an answer to a different question. The
    review fails closed and the affected entry has to be looked at again.
    """


class ExtractionMismatchError(CvStagingError):
    """The extraction does not describe the document it was submitted for.

    Raised *before* anything is written, and deliberately distinct from
    `ManifestIntegrityError`: a caller handing over inconsistent input says
    nothing about the campaign already stored, which stays exactly as valid as
    it was.
    """


class DocumentOrigin(StrEnum):
    #: This project read the bytes and hashed them.
    UPLOADED = "UPLOADED"
    #: An operator stated which document the existing facts came from, without
    #: the file itself being available any more.
    LEGACY_DECLARED = "LEGACY_DECLARED"


class DocumentLifecycle(StrEnum):
    KNOWN = "KNOWN"
    ACTIVE = "ACTIVE"
    HISTORICAL = "HISTORICAL"


class ManifestState(StrEnum):
    COMPLETE = "COMPLETE"
    #: Terminal. The rows stay readable and the campaign is never reused.
    CORRUPT = "CORRUPT"


class ReplacementLifecycle(StrEnum):
    PREPARED = "PREPARED"
    REVIEWING = "REVIEWING"
    #: A complete review exists. In B1a it activates nothing at all.
    READY_TO_ACTIVATE = "READY_TO_ACTIVATE"
    CANCELLED = "CANCELLED"


class EffectiveReplacementState(StrEnum):
    """Where an attempt actually stands, storage and activation read together.

    `profile_cv_replacements.lifecycle` still holds exactly the four values
    migration 0027 defined; 0028 added `activated_at` beside it rather than a
    fifth lifecycle value, because adding one to a SQLite `CHECK` would have
    meant rebuilding the table. So an activated attempt is stored as
    `READY_TO_ACTIVATE` with `activated_at` set, and **this** is what business
    code reads: `lifecycle` is a storage column, `effective_state` is the answer
    to "where is this attempt".
    """

    PREPARED = "PREPARED"
    REVIEWING = "REVIEWING"
    #: A complete, still-current review exists. Nothing has been applied.
    READY_TO_ACTIVATE = "READY_TO_ACTIVATE"
    CANCELLED = "CANCELLED"
    #: Terminal. The review was applied and cannot be reopened.
    ACTIVATED = "ACTIVATED"


OPEN_REPLACEMENT_LIFECYCLES: frozenset[ReplacementLifecycle] = frozenset(
    {
        ReplacementLifecycle.PREPARED,
        ReplacementLifecycle.REVIEWING,
        ReplacementLifecycle.READY_TO_ACTIVATE,
    }
)


class DecisionRole(StrEnum):
    #: A manifest candidate. It has no fact yet, and B1a creates none.
    INCOMING = "INCOMING"
    #: A fact this profile already holds.
    EXISTING = "EXISTING"


class DifferenceKind(StrEnum):
    #: The document proposes a reading the active CV did not produce.
    NEW = "NEW"
    #: The same `(fact_type, value)`, byte for byte, on both sides.
    UNCHANGED_STILL_SUPPORTED = "UNCHANGED_STILL_SUPPORTED"
    #: An active-CV fact no candidate of the new document reads. That is
    #: UNKNOWN — not evidence that the person lost anything.
    ABSENT_FROM_NEW_CV = "ABSENT_FROM_NEW_CV"
    #: The fact also rests on evidence no CV produced.
    INDEPENDENTLY_SUPPORTED = "INDEPENDENTLY_SUPPORTED"
    #: The fact is what a correction replaced an earlier reading with.
    USER_INPUT_REPLACEMENT = "USER_INPUT_REPLACEMENT"
    #: This exact proof already justifies a fact awaiting a decision.
    ALREADY_PROPOSED = "ALREADY_PROPOSED"
    #: This exact proof already justifies an accepted fact.
    ALREADY_ACCEPTED = "ALREADY_ACCEPTED"
    #: This exact proof already justifies a fact somebody refused.
    BLOCKED_TERMINAL_REJECTED = "BLOCKED_TERMINAL_REJECTED"
    #: This exact proof already justifies a fact a correction superseded.
    BLOCKED_TERMINAL_CORRECTED = "BLOCKED_TERMINAL_CORRECTED"


BLOCKED_DIFFERENCES: frozenset[DifferenceKind] = frozenset(
    {
        DifferenceKind.BLOCKED_TERMINAL_REJECTED,
        DifferenceKind.BLOCKED_TERMINAL_CORRECTED,
    }
)

#: Differences a replacement may never retire, whatever the screen offered.
PROTECTED_DIFFERENCES: frozenset[DifferenceKind] = frozenset(
    {DifferenceKind.INDEPENDENTLY_SUPPORTED, DifferenceKind.USER_INPUT_REPLACEMENT}
)


class ReviewDecision(StrEnum):
    UNDECIDED = "UNDECIDED"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    CORRECT = "CORRECT"
    #: The only answer a blocked reading takes.
    SKIP_BLOCKED = "SKIP_BLOCKED"
    KEEP = "KEEP"
    RETIRE = "RETIRE"


INCOMING_DECISIONS: frozenset[ReviewDecision] = frozenset(
    {
        ReviewDecision.UNDECIDED,
        ReviewDecision.ACCEPT,
        ReviewDecision.REJECT,
        ReviewDecision.CORRECT,
        ReviewDecision.SKIP_BLOCKED,
    }
)
EXISTING_DECISIONS: frozenset[ReviewDecision] = frozenset(
    {ReviewDecision.UNDECIDED, ReviewDecision.KEEP, ReviewDecision.RETIRE}
)


@dataclass(frozen=True)
class CvDocument:
    id: int
    profile_id: int
    content_sha256: str
    origin: DocumentOrigin
    byte_size: int | None
    page_count: int | None
    lifecycle: DocumentLifecycle

    def summary(self) -> dict[str, object]:
        """Safe to print: a digest is not a name and not a path."""
        return {
            "document_id": self.id,
            "content_sha256": self.content_sha256,
            "origin": self.origin.value,
            "lifecycle": self.lifecycle.value,
            "page_count": self.page_count,
        }


@dataclass(frozen=True)
class CvExtraction:
    id: int
    document_id: int
    profile_id: int
    parser_version: str
    extractor_version: str
    attempt_no: int
    candidate_count: int
    manifest_chain_digest: str
    manifest_state: ManifestState

    def summary(self) -> dict[str, object]:
        return {
            "extraction_id": self.id,
            "document_id": self.document_id,
            "parser_version": self.parser_version,
            "extractor_version": self.extractor_version,
            "attempt_no": self.attempt_no,
            "candidate_count": self.candidate_count,
            "manifest_chain_digest": self.manifest_chain_digest,
            "manifest_state": self.manifest_state.value,
        }


@dataclass(frozen=True)
class ManifestEntry:
    """One candidate as the manifest records it.

    `value` and `normalized_value` are CV text. They are here because a review
    has to show the person what the document said, and because a review that
    survives a restart cannot re-derive them from a PDF this project does not
    keep. They never appear in `summary()`, and `repr=False` keeps them out of
    the automatic `repr` too — which is what a traceback, a logging call and a
    failed assertion all print without anybody deciding to.
    """

    ordinal: int
    candidate_type: str
    fact_type: str
    candidate_fingerprint: str
    provenance_key: str
    value: str = field(repr=False)
    normalized_value: str | None = field(repr=False)
    rule_id: str
    page_numbers: str | None
    section_type: str | None
    section_index: int | None
    chain_digest: str
    #: None until the row has been read back from the database.
    id: int | None = None

    def summary(self) -> dict[str, object]:
        """No value, no normalized value, no raw text. Safe to log."""
        return {
            "candidate_id": self.id,
            "ordinal": self.ordinal,
            "candidate_type": self.candidate_type,
            "fact_type": self.fact_type,
            "rule_id": self.rule_id,
            "section_type": self.section_type,
            "section_index": self.section_index,
            "page_numbers": self.page_numbers,
            "candidate_fingerprint": self.candidate_fingerprint,
            "chain_digest": self.chain_digest,
        }


@dataclass(frozen=True)
class ManifestVerification:
    """What a re-read of a persisted manifest found."""

    extraction_id: int
    expected_count: int
    stored_count: int
    expected_chain_digest: str
    stored_chain_digest: str | None
    #: The first ordinal whose recomputed chain disagrees, or the first missing
    #: one. `None` when the manifest is exactly what was written.
    first_divergent_ordinal: int | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.reason is None

    def summary(self) -> dict[str, object]:
        return {
            "extraction_id": self.extraction_id,
            "ok": self.ok,
            "expected_count": self.expected_count,
            "stored_count": self.stored_count,
            "expected_chain_digest": self.expected_chain_digest,
            "stored_chain_digest": self.stored_chain_digest,
            "first_divergent_ordinal": self.first_divergent_ordinal,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Replacement:
    id: int
    profile_id: int
    extraction_id: int
    baseline_document_id: int | None
    #: The storage column, exactly as 0027 defines it. Business code reads
    #: `effective_state`; this stays because an audit needs to see what is
    #: actually written.
    lifecycle: ReplacementLifecycle
    #: The aggregate token of the review this attempt was declared ready with.
    #: `None` until a complete, current review is declared ready, and back to
    #: `None` the moment any decision changes.
    ready_review_digest: str | None = None
    #: Set together, by an activation, and never afterwards.
    activation_revision: int | None = None
    activated_at: str | None = None

    @property
    def is_activated(self) -> bool:
        return self.activated_at is not None

    @property
    def effective_state(self) -> EffectiveReplacementState:
        """Where this attempt stands. `ACTIVATED` outranks the stored lifecycle."""
        if self.activated_at is not None:
            return EffectiveReplacementState.ACTIVATED
        return EffectiveReplacementState(self.lifecycle.value)

    @property
    def is_open(self) -> bool:
        """True while the attempt can still be reviewed."""
        return (
            not self.is_activated
            and self.lifecycle in OPEN_REPLACEMENT_LIFECYCLES
        )

    def summary(self) -> dict[str, object]:
        return {
            "replacement_id": self.id,
            "extraction_id": self.extraction_id,
            "baseline_document_id": self.baseline_document_id,
            "lifecycle": self.lifecycle.value,
            "effective_state": self.effective_state.value,
            "ready_review_digest": self.ready_review_digest,
            "activation_revision": self.activation_revision,
            "activated_at": self.activated_at,
        }


@dataclass(frozen=True)
class PlanEntry:
    """One thing the review must answer, and why it is being asked."""

    role: DecisionRole
    difference: DifferenceKind
    #: Set for INCOMING entries only.
    candidate_id: int | None
    #: Set for EXISTING entries only.
    fact_id: int | None
    fact_type: str
    #: The status the fact holds right now, for EXISTING entries and for
    #: incoming readings whose proof already justifies one.
    fact_status: str | None
    #: Everything about this entry that an answer depends on, as one digest:
    #: the classification, the fact's status, whether it is protected, which
    #: evidence it rests on, and which manifest reading it is. A decision
    #: stores it, and readiness recomputes it — so a fact that changed status
    #: or gained independent evidence after somebody answered makes the review
    #: fail closed instead of applying an answer to a different question.
    state_digest: str = ""
    #: Which reading this entry is about — `(fact_type, value)` byte for byte,
    #: as a digest, never as text. It exists so that two entries about *the same
    #: reading*, one incoming and one existing, can be recognised as the same
    #: reading without the plan ever carrying a value. Nothing is keyed on it
    #: and no answer depends on it, so it stays out of `state_digest`.
    reading_digest: str = ""

    def summary(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "difference": self.difference.value,
            "candidate_id": self.candidate_id,
            "fact_id": self.fact_id,
            "fact_type": self.fact_type,
            "fact_status": self.fact_status,
            "state_digest": self.state_digest,
        }


@dataclass(frozen=True)
class ReplacementPlan:
    profile_id: int
    replacement_id: int
    extraction_id: int
    baseline_document_id: int | None
    entries: tuple[PlanEntry, ...]

    def counts_by_difference(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.difference.value] = counts.get(entry.difference.value, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def summary(self) -> dict[str, object]:
        """Counters and canonical names. Never a reading, never a value."""
        return {
            "profile_id": self.profile_id,
            "replacement_id": self.replacement_id,
            "extraction_id": self.extraction_id,
            "baseline_document_id": self.baseline_document_id,
            "entries": len(self.entries),
            "counts_by_difference": self.counts_by_difference(),
        }


@dataclass(frozen=True)
class StagedDecision:
    id: int
    replacement_id: int
    profile_id: int
    role: DecisionRole
    candidate_id: int | None
    fact_id: int | None
    difference: DifferenceKind
    decision: ReviewDecision
    has_staged_value: bool
    fact_status_at_decision: str | None
    #: The `PlanEntry.state_digest` this answer was given against.
    state_digest: str = ""

    def summary(self) -> dict[str, object]:
        """`has_staged_value` says a correction was typed, never what it says."""
        return {
            "decision_id": self.id,
            "replacement_id": self.replacement_id,
            "role": self.role.value,
            "candidate_id": self.candidate_id,
            "fact_id": self.fact_id,
            "difference": self.difference.value,
            "decision": self.decision.value,
            "has_staged_value": self.has_staged_value,
            "fact_status_at_decision": self.fact_status_at_decision,
            "state_digest": self.state_digest,
        }
