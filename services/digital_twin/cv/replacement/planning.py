"""What a replacement would change, computed by reading and deciding nothing.

This module runs `SELECT`s and returns a plan. It writes no row anywhere, opens
no transaction of its own, and works on a `mode=ro` connection, so looking at
what a new CV would mean can never be the thing that changes something.

Two readings are the same reading when, and only when, the `fact_type` and the
`value` are equal **byte for byte** — the rule
`services/digital_twin/cv/reconciliation.py` already uses, and for the same
reason: a text differing by one character is a different reading, and only exact
equality rules out a changed reading slipping in under an old acceptance. There
is no normalization, no case folding, no whitespace collapsing, no substring
rule, no edit distance, no similarity, no embedding and no model here.

Equality decides nothing either. An unchanged reading still reaches the review
as something to answer: this module classifies, it never accepts.
"""

from __future__ import annotations

import hashlib
import sqlite3

from services.digital_twin.cv.replacement.manifest import read_manifest, verify_manifest
from services.digital_twin.cv.replacement.models import (
    CvDocumentNotFoundError,
    DecisionRole,
    DifferenceKind,
    ExtractionNotFoundError,
    ManifestIntegrityError,
    PlanEntry,
    ReplacementNotFoundError,
    ReplacementPlan,
)
from services.digital_twin.cv.replacement.repository import (
    fact_evidence_digest,
    get_cv_document,
    get_extraction,
    get_replacement,
    list_baseline_cv_facts,
)

__all__ = ["PLAN_STATE_VERSION", "compute_replacement_plan", "plan_entry_for"]

#: Named so that changing what an answer depends on is a visible change of
#: contract: every stored decision digest stops matching, and every open review
#: is asked again rather than being applied against a state nobody checked.
PLAN_STATE_VERSION = "cv-replacement-plan-state-v1"

#: What a fact's status means for a reading whose exact proof already justifies
#: it. `REJECTED` and `CORRECTED` are terminal in `ALLOWED_TRANSITIONS`, so no
#: owner can turn either back into a proposal; the plan says so out loud rather
#: than offering a decision that could not be applied.
_STATUS_TO_DIFFERENCE = {
    "PROPOSED": DifferenceKind.ALREADY_PROPOSED,
    "ACCEPTED": DifferenceKind.ALREADY_ACCEPTED,
    "REJECTED": DifferenceKind.BLOCKED_TERMINAL_REJECTED,
    "CORRECTED": DifferenceKind.BLOCKED_TERMINAL_CORRECTED,
}


def _state_digest(*parts: object) -> str:
    """One digest over everything an answer to this entry depended on.

    The parts are canonical strings — a role, a classification, a status, a
    protection flag, an evidence digest, a manifest chain digest. No reading,
    no value and no normalized value takes part, so the digest can be stored,
    compared, logged and printed.
    """
    payload = "\x1f".join([PLAN_STATE_VERSION, *(str(part) for part in parts)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fact_for_evidence(
    connection: sqlite3.Connection, *, profile_id: int, provenance_key: str
) -> tuple[int, str] | None:
    """The fact this exact proof already justifies for this profile, if any.

    Identity is the evidence, exactly as `ensure_profile_fact_proposal` defines
    it. It is **not** the replacement attempt: a second attempt over the same
    document, read by the same versions, produces the same proof, and treating
    it as a new one would propose one reading twice.
    """
    row = connection.execute(
        """SELECT f.id, f.status
             FROM profile_facts AS f
             JOIN profile_fact_provenance AS p ON p.fact_id = f.id
            WHERE f.profile_id = ? AND p.provenance_key = ?
            ORDER BY f.id""",
        (profile_id, provenance_key),
    ).fetchall()
    if not row:
        return None
    if len(row) > 1:
        # The database already holds one proof justifying several facts.
        # Choosing one of them would decide which reading counts.
        raise ManifestIntegrityError(
            f"{len(row)} facts of profile {profile_id} share one proof"
        )
    return int(row[0][0]), str(row[0][1])


def compute_replacement_plan(
    connection: sqlite3.Connection, *, profile_id: int, replacement_id: int
) -> ReplacementPlan:
    """Classify every incoming reading and every baseline fact. Read-only.

    The manifest is verified first. A plan built on a manifest that no longer
    reproduces its chain would be a plan about a document nobody can prove was
    read, so this raises instead of returning one.
    """
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
    verification = verify_manifest(
        connection, profile_id=profile_id, extraction_id=extraction.id
    )
    if not verification.ok:
        raise ManifestIntegrityError(
            f"the manifest of extraction {extraction.id} does not verify: "
            f"{verification.reason}"
        )

    baseline: tuple = ()
    if replacement.baseline_document_id is not None:
        document = get_cv_document(
            connection,
            profile_id=profile_id,
            document_id=replacement.baseline_document_id,
        )
        if document is None:
            raise CvDocumentNotFoundError(
                f"document {replacement.baseline_document_id} does not belong to "
                f"profile {profile_id}"
            )
        baseline = list_baseline_cv_facts(
            connection, profile_id=profile_id, content_sha256=document.content_sha256
        )

    manifest = read_manifest(
        connection, profile_id=profile_id, extraction_id=extraction.id
    )
    # Byte-for-byte, and nothing else. A reading present twice in the baseline
    # is kept as a set member once; the existing side below walks every fact.
    baseline_readings = {(fact.fact_type, fact.value) for fact in baseline}
    incoming_readings = {(entry.fact_type, entry.value) for entry in manifest}

    entries: list[PlanEntry] = []
    for entry in manifest:
        known = _fact_for_evidence(
            connection, profile_id=profile_id, provenance_key=entry.provenance_key
        )
        if known is not None:
            fact_id, status = known
            difference = _STATUS_TO_DIFFERENCE[status]
            evidence = fact_evidence_digest(
                connection, profile_id=profile_id, fact_id=fact_id
            )
        else:
            fact_id, status, evidence = None, None, ""
            difference = (
                DifferenceKind.UNCHANGED_STILL_SUPPORTED
                if (entry.fact_type, entry.value) in baseline_readings
                else DifferenceKind.NEW
            )
        entries.append(
            PlanEntry(
                role=DecisionRole.INCOMING,
                difference=difference,
                candidate_id=entry.id,
                fact_id=None,
                fact_type=entry.fact_type,
                fact_status=status,
                # The candidate's chain digest binds the reading, its position
                # and the document it came from, so an edited manifest row
                # makes every answer about it stale.
                state_digest=_state_digest(
                    DecisionRole.INCOMING.value,
                    entry.id,
                    entry.chain_digest,
                    entry.fact_type,
                    difference.value,
                    status or "",
                    evidence,
                ),
            )
        )

    for fact in baseline:
        # Protection first, and derived from the database rather than from what
        # a screen once called this row: a fact somebody stated themselves, or
        # that another source also supports, is not this document's to retire.
        if fact.is_user_input_replacement:
            difference = DifferenceKind.USER_INPUT_REPLACEMENT
        elif fact.has_non_cv_evidence:
            difference = DifferenceKind.INDEPENDENTLY_SUPPORTED
        elif (fact.fact_type, fact.value) in incoming_readings:
            difference = DifferenceKind.UNCHANGED_STILL_SUPPORTED
        else:
            # Absence from the new document is UNKNOWN. It is not evidence that
            # a skill, an experience or a qualification stopped being true.
            difference = DifferenceKind.ABSENT_FROM_NEW_CV
        entries.append(
            PlanEntry(
                role=DecisionRole.EXISTING,
                difference=difference,
                candidate_id=None,
                fact_id=fact.fact_id,
                fact_type=fact.fact_type,
                fact_status=fact.status,
                # Status, both protection flags and the evidence set itself. A
                # fact that gains a USER_INPUT or GITHUB proof after somebody
                # chose RETIRE moves this digest, and the review fails closed.
                state_digest=_state_digest(
                    DecisionRole.EXISTING.value,
                    fact.fact_id,
                    fact.fact_type,
                    difference.value,
                    fact.status,
                    int(fact.is_user_input_replacement),
                    int(fact.has_non_cv_evidence),
                    fact_evidence_digest(
                        connection, profile_id=profile_id, fact_id=fact.fact_id
                    ),
                ),
            )
        )

    return ReplacementPlan(
        profile_id=profile_id,
        replacement_id=replacement_id,
        extraction_id=extraction.id,
        baseline_document_id=replacement.baseline_document_id,
        entries=tuple(entries),
    )


def plan_entry_for(
    plan: ReplacementPlan, *, candidate_id: int | None, fact_id: int | None
) -> PlanEntry | None:
    """The one entry this decision is about, or `None` if the plan has none."""
    for entry in plan.entries:
        if candidate_id is not None and entry.candidate_id == candidate_id:
            return entry
        if fact_id is not None and entry.fact_id == fact_id:
            return entry
    return None
