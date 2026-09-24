"""Applying a reviewed CV replacement: all of it, in one transaction.

An activation is the one moment where a staged review stops being a proposal
and becomes the person's profile. Everything it touches has to move together:
the facts a person accepted, the readings they refused, the corrections they
typed, the facts they removed, which document is the active CV — and the
statement that the persisted Recommendation no longer describes this profile. A
database where half of that happened would be a Digital Twin whose facts and
whose active CV disagree, with nothing to say which of the two is right.

What this step does **not** do: advance anything. It writes no watermark, so
every phase is left visibly behind the revision it has just created. That is
the whole mechanism — `services/profile_revision/watermark.py` turns "behind"
into "not presentable as current" for the readers, and each phase's own owner
advances its watermark only when it has actually recomputed and published. The
activation's one job here is to create the new revision and say so.

So there is one `BEGIN IMMEDIATE`, opened **before the first business read**,
and one `COMMIT`. `IMMEDIATE` rather than deferred is the point rather than a
detail: a deferred transaction takes no write lock until its first write, so
another connection could cancel the attempt, decide a fact or edit the review
between the checks below and the writes they authorise. That is precisely the
interleaving this function exists to exclude.

What it refuses, and in this order, before writing anything:

1. a transaction the caller already opened — the guarantee above is this
   function's to make, not its caller's to remember;
2. an attempt that is not this profile's, is cancelled, or was already
   activated;
3. a review token the caller did not supply, or that is not the one the review
   was declared ready with;
4. a review that is no longer complete or no longer current — the same check
   `mark_ready_to_activate` makes, through the same shared validator;
5. a token that no longer matches the decisions actually stored;
6. a baseline document that is no longer the profile's active CV;
7. a target document that is already active, or that is historical —
   reactivating an old CV is a different question, and it is refused by name
   rather than by an `UPDATE` that happens to match no row.

What it never does: reach for an owner that opens its own transaction, invent a
business rule the review did not decide, write a preference, or put a CV value
in an error message or a log.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from services.digital_twin.cv.replacement.manifest import read_manifest
from services.digital_twin.cv.replacement.models import (
    BLOCKED_DIFFERENCES,
    CvDocument,
    CvDocumentNotFoundError,
    CvStagingError,
    DecisionRole,
    DifferenceKind,
    DocumentLifecycle,
    ExtractionNotFoundError,
    PlanEntry,
    Replacement,
    ReplacementLifecycle,
    ReplacementNotFoundError,
    ReviewDecision,
    StagedDecision,
)
from services.digital_twin.cv.replacement.repository import (
    REPLACEMENT_COLUMNS,
    get_cv_document,
    get_extraction,
    get_replacement,
    list_baseline_cv_facts,
    list_staged_decisions,
    read_staged_correction,
    replacement_from_row,
)
from services.digital_twin.cv.replacement.review_digest import compute_review_digest
from services.digital_twin.cv.replacement.staging import (
    require_complete_current_review,
)
from services.digital_twin.facts.models import (
    FactSourceType,
    FactStatus,
    ProvenanceInput,
    decode_page_numbers,
)
from services.digital_twin.facts.repository import (
    correct_profile_fact_in_transaction,
    decide_profile_facts_in_transaction,
    ensure_profile_fact_proposal_in_transaction,
    ensure_profile_fact_provenance_in_transaction,
    retire_profile_fact_in_transaction,
)
from services.recommendation.input_assembly import (
    PROFILE_CV_ACTIVATION_PENDING_SYNC_MESSAGE,
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RecommendationReadinessIssue,
    RecommendationReadinessIssueCode,
)
from services.recommendation.persistence import (
    set_recommendation_state_incomplete_in_transaction,
)

__all__ = [
    "ActivationResult",
    "ActiveCvChangedError",
    "AlreadyActiveDocumentError",
    "AmbiguousBaselineReadingError",
    "HistoricalDocumentReactivationError",
    "ReviewChangedError",
    "activate_cv_replacement",
]

#: What a reader is told once an activation has landed: the persisted
#: recommendation describes a profile that no longer exists, and nothing
#: downstream has caught up yet. Written by the activation itself, in its own
#: transaction, so there is no instant in which the Recommendation is still
#: served as current for a CV it was not computed from.
#:
#: The wording lives beside the readiness code it accompanies, in
#: `services/recommendation/input_assembly.py`, because the recommendation
#: reader derives the same issue when it finds a stored READY that a later
#: activation overtook. Two places saying it in two ways would be two answers.
PENDING_SYNC_MESSAGE = PROFILE_CV_ACTIVATION_PENDING_SYNC_MESSAGE


class ReviewChangedError(CvStagingError):
    """The review is not the one the caller confirmed.

    Either no token was supplied, or the token does not match what the review
    was declared ready with, or the decisions have moved since. Applying it
    anyway would apply answers the person never confirmed.
    """


class ActiveCvChangedError(CvStagingError):
    """The active CV is no longer the one this attempt was opened against."""


class HistoricalDocumentReactivationError(CvStagingError):
    """The target document was active once and was replaced.

    Bringing it back is a different question from replacing a CV: it would also
    have to decide the fate of everything accepted since. Nobody has designed
    that review, so this refuses by name rather than guessing.
    """


class AlreadyActiveDocumentError(CvStagingError):
    """The target document is already this profile's active CV."""


class AmbiguousBaselineReadingError(CvStagingError):
    """Two baseline facts hold one reading, so 'the' fact cannot be named.

    Attaching the new document's evidence to one of them would silently decide
    which of two accepted readings counts.
    """


@dataclass(frozen=True)
class ActivationResult:
    """What one activation did, in counters and canonical names."""

    replacement: Replacement
    #: True when this call performed the activation. False means it was already
    #: activated and this call wrote nothing at all.
    activated: bool
    activation_revision: int
    previous_document_id: int | None
    document_id: int
    facts_accepted: int
    facts_rejected: int
    facts_corrected: int
    facts_retired: int
    evidence_attached: int
    decisions_skipped: int

    def summary(self) -> dict[str, object]:
        """Counters, ids and canonical names. Never a reading, never a value."""
        return {
            "replacement_id": self.replacement.id,
            "activated": self.activated,
            "activation_revision": self.activation_revision,
            "previous_document_id": self.previous_document_id,
            "document_id": self.document_id,
            "facts_accepted": self.facts_accepted,
            "facts_rejected": self.facts_rejected,
            "facts_corrected": self.facts_corrected,
            "facts_retired": self.facts_retired,
            "evidence_attached": self.evidence_attached,
            "decisions_skipped": self.decisions_skipped,
        }


def _provenance_for_manifest_entry(entry, *, content_sha256: str, extraction) -> ProvenanceInput:
    """Rebuild the evidence this reading would carry as a fact.

    Field for field from the manifest row and the campaign it belongs to, which
    together recorded exactly what the candidate's own provenance said. The
    resulting key is therefore the one `ensure_profile_fact_proposal` keys
    idempotence on — the same one the manifest stored — so re-running an
    activation finds the fact it created rather than creating a second one.
    """
    return ProvenanceInput(
        source_type=FactSourceType.CV,
        source_locator=None,
        cv_sha256=content_sha256,
        parser_version=extraction.parser_version,
        extractor_version=extraction.extractor_version,
        candidate_fingerprint=entry.candidate_fingerprint,
        rule_id=entry.rule_id,
        page_numbers=decode_page_numbers(entry.page_numbers),
        section_type=entry.section_type,
        section_index=entry.section_index,
    )


def _baseline_fact_for_reading(baseline, fact_type: str, value: str) -> int:
    """The one baseline fact holding this exact reading, or a refusal.

    Byte for byte on `(fact_type, value)`, the rule the plan itself uses. Two
    matches is an integrity problem the database already has, and choosing one
    of them would decide which reading counts.
    """
    matches = [
        fact
        for fact in baseline
        if fact.fact_type == fact_type and fact.value == value
    ]
    if len(matches) > 1:
        raise AmbiguousBaselineReadingError(
            f"{len(matches)} baseline facts hold one {fact_type} reading of the "
            f"active document"
        )
    if not matches:
        raise CvStagingError(
            f"no baseline {fact_type} fact holds the reading this decision calls "
            f"unchanged"
        )
    return matches[0].fact_id


def _require_document(
    connection: sqlite3.Connection, *, profile_id: int, document_id: int
) -> CvDocument:
    document = get_cv_document(
        connection, profile_id=profile_id, document_id=document_id
    )
    if document is None:
        raise CvDocumentNotFoundError(
            f"document {document_id} does not belong to profile {profile_id}"
        )
    return document


def _already_activated_result(replacement: Replacement, document_id: int, previous: int | None):
    return ActivationResult(
        replacement=replacement,
        activated=False,
        activation_revision=replacement.activation_revision or 0,
        previous_document_id=previous,
        document_id=document_id,
        facts_accepted=0,
        facts_rejected=0,
        facts_corrected=0,
        facts_retired=0,
        evidence_attached=0,
        decisions_skipped=0,
    )


def activate_cv_replacement(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    replacement_id: int,
    expected_review_digest: str,
    after_decisions=None,
    after_documents=None,
) -> ActivationResult:
    """Apply one reviewed replacement, whole, in one transaction.

    `expected_review_digest` is the token the caller confirmed. It is compared
    twice — against the token the review was declared ready with, and against
    the token the stored decisions produce right now — so a review that changed
    between the screen and this call fails rather than being applied.

    Re-running a finished activation is a no-op: it reports `activated=False`
    and writes nothing, the recommendation state included, so a legitimate
    synchronization performed in between is not undone.

    `after_decisions` and `after_documents` are test seams invoked inside the
    transaction, after those steps and before the ones that follow; production
    callers leave them unset.
    """
    if connection.in_transaction:
        raise CvStagingError(
            "activate_cv_replacement owns its transaction and borrows none"
        )
    if not isinstance(expected_review_digest, str) or len(
        expected_review_digest
    ) != 64 or any(
        character not in "0123456789abcdef" for character in expected_review_digest
    ):
        raise ReviewChangedError("a review token is 64 hexadecimal characters")

    # IMMEDIATE, and before the first business read: every check below has to
    # still hold when the writes it authorises land.
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = _activate(
            connection,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=expected_review_digest,
            after_decisions=after_decisions,
            after_documents=after_documents,
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return result


def _activate(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    replacement_id: int,
    expected_review_digest: str,
    after_decisions,
    after_documents,
) -> ActivationResult:
    """The business flow, with the transaction already established around it."""
    replacement = get_replacement(
        connection, profile_id=profile_id, replacement_id=replacement_id
    )
    if replacement is None:
        raise ReplacementNotFoundError(
            f"replacement {replacement_id} does not belong to profile {profile_id}"
        )
    extraction = get_extraction(
        connection, profile_id=profile_id, extraction_id=replacement.extraction_id
    )
    if extraction is None:
        raise ExtractionNotFoundError(
            f"extraction {replacement.extraction_id} does not belong to "
            f"profile {profile_id}"
        )

    if replacement.is_activated:
        # Already done. Saying so and writing nothing is what makes a retry
        # after an interruption safe.
        return _already_activated_result(
            replacement, extraction.document_id, replacement.baseline_document_id
        )
    if replacement.lifecycle is not ReplacementLifecycle.READY_TO_ACTIVATE:
        raise CvStagingError(
            f"replacement {replacement_id} is {replacement.lifecycle.value}; only a "
            f"review declared ready is applied"
        )

    if replacement.ready_review_digest is None:
        raise ReviewChangedError(
            f"replacement {replacement_id} carries no review token; declare the "
            f"review ready again"
        )
    if replacement.ready_review_digest != expected_review_digest:
        raise ReviewChangedError(
            f"the review of replacement {replacement_id} is not the one this "
            f"activation confirmed"
        )

    # The same check `mark_ready_to_activate` makes, through the same validator:
    # complete, and every answer still describing the database as it is now.
    plan = require_complete_current_review(
        connection, profile_id=profile_id, replacement_id=replacement_id
    )
    recomputed = compute_review_digest(
        connection,
        profile_id=profile_id,
        replacement_id=replacement_id,
        extraction_id=extraction.id,
    )
    if recomputed != replacement.ready_review_digest:
        raise ReviewChangedError(
            f"the decisions of replacement {replacement_id} have changed since "
            f"the review was declared ready"
        )

    # The baseline has to still be the active CV, and the target has to be a
    # document that was never active.
    active = connection.execute(
        "SELECT id FROM profile_cv_documents WHERE profile_id = ? AND lifecycle = 'ACTIVE'",
        (profile_id,),
    ).fetchone()
    active_id = None if active is None else int(active[0])
    if active_id != replacement.baseline_document_id:
        raise ActiveCvChangedError(
            f"replacement {replacement_id} was opened against document "
            f"{replacement.baseline_document_id}; the active CV is now {active_id}"
        )

    target = _require_document(
        connection, profile_id=profile_id, document_id=extraction.document_id
    )
    if target.lifecycle is DocumentLifecycle.ACTIVE:
        raise AlreadyActiveDocumentError(
            f"document {target.id} is already the active CV of profile {profile_id}"
        )
    if target.lifecycle is DocumentLifecycle.HISTORICAL:
        raise HistoricalDocumentReactivationError(
            f"document {target.id} was replaced; reactivating a historical CV is "
            f"not what a replacement does"
        )

    baseline = ()
    baseline_document = None
    if replacement.baseline_document_id is not None:
        baseline_document = _require_document(
            connection,
            profile_id=profile_id,
            document_id=replacement.baseline_document_id,
        )
        baseline = list_baseline_cv_facts(
            connection,
            profile_id=profile_id,
            content_sha256=baseline_document.content_sha256,
        )

    counters = _apply_decisions(
        connection,
        profile_id=profile_id,
        replacement_id=replacement_id,
        plan_entries=plan.entries,
        decisions=list_staged_decisions(
            connection, profile_id=profile_id, replacement_id=replacement_id
        ),
        manifest={
            entry.id: entry
            for entry in read_manifest(
                connection, profile_id=profile_id, extraction_id=extraction.id
            )
        },
        target=target,
        extraction=extraction,
        baseline=baseline,
        baseline_document=baseline_document,
    )
    if after_decisions is not None:
        after_decisions()

    # The documents switch together: the partial unique index makes two active
    # CVs impossible, so this order is the only one that can hold.
    if baseline_document is not None:
        connection.execute(
            """UPDATE profile_cv_documents
                  SET lifecycle = 'HISTORICAL', retired_at = CURRENT_TIMESTAMP
                WHERE id = ? AND profile_id = ? AND lifecycle = 'ACTIVE'""",
            (baseline_document.id, profile_id),
        )
    connection.execute(
        """UPDATE profile_cv_documents
              SET lifecycle = 'ACTIVE', activated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND profile_id = ? AND lifecycle = 'KNOWN'""",
        (target.id, profile_id),
    )
    if after_documents is not None:
        after_documents()

    revision = int(
        connection.execute(
            """SELECT COALESCE(MAX(activation_revision), 0) + 1
                 FROM profile_cv_replacements WHERE profile_id = ?""",
            (profile_id,),
        ).fetchone()[0]
    )
    # `lifecycle` stays READY_TO_ACTIVATE and `closed_at` stays NULL: 0027 ties
    # `closed_at` to CANCELLED and 0028 requires this lifecycle for an
    # activation, so `activated_at` is what makes the effective state ACTIVATED.
    row = connection.execute(
        f"""UPDATE profile_cv_replacements
               SET activation_revision = ?,
                   activated_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = ? AND profile_id = ?
               AND lifecycle = 'READY_TO_ACTIVATE'
               AND activated_at IS NULL
         RETURNING {REPLACEMENT_COLUMNS}""",
        (revision, replacement_id, profile_id),
    ).fetchone()
    if row is None:
        raise CvStagingError(
            f"replacement {replacement_id} stopped being ready while it was "
            f"being activated"
        )
    activated = replacement_from_row(row)

    # The persisted recommendation now describes a profile that no longer
    # exists. Saying so here, in this transaction, is what stops it from being
    # served as current between the commit and the first synchronization. The
    # stored runs are untouched: only the pointer that named one as current is.
    #
    # This protects the Recommendation, and only it. The other read models are
    # not gated by this step, and neither is a synchronization that starts after
    # this commit and reads a profile no phase has caught up with yet — both are
    # B1b-C's subject, not this one's.
    set_recommendation_state_incomplete_in_transaction(
        connection,
        profile_id,
        issues=(
            RecommendationReadinessIssue(
                code=RecommendationReadinessIssueCode.PROFILE_CV_ACTIVATION_PENDING_SYNC,
                message=PENDING_SYNC_MESSAGE,
            ),
        ),
        input_assembly_version=RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    )

    return ActivationResult(
        replacement=activated,
        activated=True,
        activation_revision=revision,
        previous_document_id=None if baseline_document is None else baseline_document.id,
        document_id=target.id,
        **counters,
    )


def _apply_decisions(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    replacement_id: int,
    plan_entries: tuple[PlanEntry, ...],
    decisions: tuple[StagedDecision, ...],
    manifest: dict,
    target: CvDocument,
    extraction,
    baseline,
    baseline_document,
) -> dict[str, int]:
    """Apply every answer, through the fact owners and through nothing else.

    The plan has already been recomputed and matched against the decisions by
    `require_complete_current_review`, so what is left here is to carry each
    answer out — never to re-decide what it means.
    """
    accepted: list[int] = []
    rejected: list[int] = []
    corrected = 0
    retired = 0
    attached = 0
    skipped = 0

    by_target = {
        (entry.candidate_id, entry.fact_id): entry for entry in plan_entries
    }
    for decision in decisions:
        entry = by_target[(decision.candidate_id, decision.fact_id)]

        if decision.decision in (ReviewDecision.KEEP, ReviewDecision.SKIP_BLOCKED):
            # KEEP writes nothing by definition, and a blocked reading resolves
            # to a fact somebody already refused or corrected.
            skipped += 1
            continue

        if entry.role is DecisionRole.EXISTING:
            if decision.decision is not ReviewDecision.RETIRE:
                raise CvStagingError(
                    f"{decision.decision.value} is not an answer about an existing fact"
                )
            if baseline_document is None:
                raise CvStagingError(
                    "a retirement needs the baseline document that supported the fact"
                )
            retire_profile_fact_in_transaction(
                connection,
                profile_id=profile_id,
                fact_id=decision.fact_id,
                baseline_content_sha256=baseline_document.content_sha256,
            )
            retired += 1
            continue

        # From here on the answer is about an incoming reading.
        if entry.difference in BLOCKED_DIFFERENCES:
            raise CvStagingError(
                f"a {entry.difference.value} reading is only ever skipped"
            )
        candidate = manifest[decision.candidate_id]
        provenance = _provenance_for_manifest_entry(
            candidate, content_sha256=target.content_sha256, extraction=extraction
        )

        if (
            decision.decision is ReviewDecision.ACCEPT
            and entry.difference is DifferenceKind.UNCHANGED_STILL_SUPPORTED
        ):
            # The reading the active CV already carries. Attaching the new
            # document's evidence to the fact that holds it is what keeps one
            # reading one fact; proposing it again would key idempotence on a
            # different proof and duplicate the Digital Twin at every
            # replacement.
            fact_id = _baseline_fact_for_reading(
                baseline, candidate.fact_type, candidate.value
            )
            outcome = ensure_profile_fact_provenance_in_transaction(
                connection,
                profile_id=profile_id,
                fact_id=fact_id,
                provenance=provenance,
            )
            attached += 1 if outcome.created else 0
            continue

        proposal = ensure_profile_fact_proposal_in_transaction(
            connection,
            profile_id=profile_id,
            fact_type=candidate.fact_type,
            value=candidate.value,
            provenance=provenance,
            normalized_value=candidate.normalized_value,
        )
        if decision.decision is ReviewDecision.ACCEPT:
            accepted.append(proposal.fact.id)
        elif decision.decision is ReviewDecision.REJECT:
            # Recorded as refused rather than left out: the claim was made, the
            # person refused it, and a later reading of the same document will
            # be told so instead of asking again.
            rejected.append(proposal.fact.id)
        elif decision.decision is ReviewDecision.CORRECT:
            value, normalized = read_staged_correction(
                connection, profile_id=profile_id, decision_id=decision.id
            )
            correct_profile_fact_in_transaction(
                connection,
                profile_id,
                proposal.fact.id,
                value=value,
                normalized_value=normalized,
            )
            corrected += 1
        else:  # pragma: no cover - the schema and the plan exclude the rest
            raise CvStagingError(
                f"{decision.decision.value} is not an answer about an incoming reading"
            )

    decide_profile_facts_in_transaction(
        connection, profile_id, accepted, FactStatus.ACCEPTED
    )
    decide_profile_facts_in_transaction(
        connection, profile_id, rejected, FactStatus.REJECTED
    )
    return {
        "facts_accepted": len(accepted),
        "facts_rejected": len(rejected),
        "facts_corrected": corrected,
        "facts_retired": retired,
        "evidence_attached": attached,
        "decisions_skipped": skipped,
    }
