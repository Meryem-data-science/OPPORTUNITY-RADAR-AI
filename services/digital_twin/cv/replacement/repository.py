"""Reading and writing the staging tables, and nothing beyond them.

Three rules are enforced here rather than trusted to callers, in the style
`services/digital_twin/skills/repository.py` already uses:

* every statement is scoped by `profile_id`, and every reference the schema
  carries is a composite `(id, profile_id)` foreign key, so one profile's
  staging can never be built out of another profile's rows — SQLite refuses it
  before any Python check runs;
* nothing in this module writes to `profile_facts` or `profile_fact_provenance`.
  There is no `INSERT`, `UPDATE` or `DELETE` against either table anywhere
  below, and a test reads the SQL to keep it so;
* a corrupt manifest is never rewritten and never reused. It is marked, it
  stays readable, and a fresh campaign is recorded beside it under the next
  `attempt_no`.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

from services.digital_twin.cv.candidates.models import StructuredCvExtraction
from services.digital_twin.cv.replacement.manifest import (
    chain_digest_of,
    validate_extraction_for_document,
    verify_manifest,
    write_extraction_with_manifest,
)
from services.digital_twin.cv.replacement.models import (
    ActiveCvDocumentError,
    CvDocument,
    CvDocumentNotFoundError,
    CvExtraction,
    CvStagingError,
    DecisionRole,
    DifferenceKind,
    DocumentLifecycle,
    DocumentOrigin,
    ExtractionMismatchError,
    ManifestState,
    ManifestVerification,
    OPEN_REPLACEMENT_LIFECYCLES,
    OpenReplacementExistsError,
    Replacement,
    ReplacementLifecycle,
    ReplacementNotFoundError,
    ReviewDecision,
    StagedDecision,
)

__all__ = [
    "ExtractionOutcome",
    "BaselineFact",
    "cancel_replacement",
    "declare_active_cv_document",
    "ensure_cv_document",
    "ensure_extraction_with_manifest",
    "fact_evidence_digest",
    "get_active_cv_document",
    "get_cv_document",
    "get_extraction",
    "get_replacement",
    "list_baseline_cv_facts",
    "list_fact_source_types",
    "list_staged_decisions",
    "open_replacement",
    "set_replacement_lifecycle",
]

_DOCUMENT_COLUMNS = (
    "id, profile_id, content_sha256, origin, byte_size, page_count, lifecycle"
)
_EXTRACTION_COLUMNS = (
    "id, document_id, profile_id, parser_version, extractor_version, attempt_no, "
    "candidate_count, manifest_chain_digest, manifest_state"
)
_REPLACEMENT_COLUMNS = (
    "id, profile_id, extraction_id, baseline_document_id, lifecycle"
)
_DECISION_COLUMNS = (
    "id, replacement_id, profile_id, role, candidate_id, fact_id, difference, "
    "decision, staged_value IS NOT NULL, fact_status_at_decision, "
    "review_state_digest"
)


def _document_from_row(row: tuple) -> CvDocument:
    return CvDocument(
        id=row[0],
        profile_id=row[1],
        content_sha256=row[2],
        origin=DocumentOrigin(row[3]),
        byte_size=row[4],
        page_count=row[5],
        lifecycle=DocumentLifecycle(row[6]),
    )


def _extraction_from_row(row: tuple) -> CvExtraction:
    return CvExtraction(
        id=row[0],
        document_id=row[1],
        profile_id=row[2],
        parser_version=row[3],
        extractor_version=row[4],
        attempt_no=row[5],
        candidate_count=row[6],
        manifest_chain_digest=row[7],
        manifest_state=ManifestState(row[8]),
    )


def _replacement_from_row(row: tuple) -> Replacement:
    return Replacement(
        id=row[0],
        profile_id=row[1],
        extraction_id=row[2],
        baseline_document_id=row[3],
        lifecycle=ReplacementLifecycle(row[4]),
    )


def _decision_from_row(row: tuple) -> StagedDecision:
    return StagedDecision(
        id=row[0],
        replacement_id=row[1],
        profile_id=row[2],
        role=DecisionRole(row[3]),
        candidate_id=row[4],
        fact_id=row[5],
        difference=DifferenceKind(row[6]),
        decision=ReviewDecision(row[7]),
        has_staged_value=bool(row[8]),
        fact_status_at_decision=row[9],
        state_digest=row[10],
    )


def _require_no_transaction(connection: sqlite3.Connection, what: str) -> None:
    if connection.in_transaction:
        raise sqlite3.ProgrammingError(f"{what} owns its transaction and borrows none")


def _digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise CvStagingError("a content digest is 64 hexadecimal characters")
    if any(character not in "0123456789abcdef" for character in value):
        raise CvStagingError("a content digest is 64 hexadecimal characters")
    return value


# ---------------------------------------------------------------- documents


def get_cv_document(
    connection: sqlite3.Connection, *, profile_id: int, document_id: int
) -> CvDocument | None:
    row = connection.execute(
        f"SELECT {_DOCUMENT_COLUMNS} FROM profile_cv_documents "
        "WHERE id = ? AND profile_id = ?",
        (document_id, profile_id),
    ).fetchone()
    return None if row is None else _document_from_row(row)


def get_active_cv_document(
    connection: sqlite3.Connection, profile_id: int
) -> CvDocument | None:
    """This profile's active CV, or `None` when it has never declared one."""
    row = connection.execute(
        f"SELECT {_DOCUMENT_COLUMNS} FROM profile_cv_documents "
        "WHERE profile_id = ? AND lifecycle = 'ACTIVE'",
        (profile_id,),
    ).fetchone()
    return None if row is None else _document_from_row(row)


def ensure_cv_document(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    content_sha256: str,
    origin: DocumentOrigin = DocumentOrigin.UPLOADED,
    byte_size: int | None = None,
    page_count: int | None = None,
) -> CvDocument:
    """Record that this profile knows this document. Activates nothing.

    Idempotent on `(profile_id, content_sha256)`: the same document submitted
    twice is one row, whatever its lifecycle has become since.
    """
    _require_no_transaction(connection, "ensure_cv_document")
    digest = _digest(content_sha256)
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            f"SELECT {_DOCUMENT_COLUMNS} FROM profile_cv_documents "
            "WHERE profile_id = ? AND content_sha256 = ?",
            (profile_id, digest),
        ).fetchone()
        if existing is not None:
            connection.execute("COMMIT")
            return _document_from_row(existing)
        row = connection.execute(
            f"""INSERT INTO profile_cv_documents (
                    profile_id, content_sha256, origin, byte_size, page_count, lifecycle
                ) VALUES (?, ?, ?, ?, ?, 'KNOWN')
                RETURNING {_DOCUMENT_COLUMNS}""",
            (profile_id, digest, origin.value, byte_size, page_count),
        ).fetchone()
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    if row is None:
        raise CvStagingError("document insert returned no row")
    return _document_from_row(row)


def declare_active_cv_document(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    content_sha256: str,
    origin: DocumentOrigin = DocumentOrigin.LEGACY_DECLARED,
    byte_size: int | None = None,
    page_count: int | None = None,
) -> CvDocument:
    """State which document this profile's existing facts came from.

    Nothing is inferred. There is no majority digest, no "most recent campaign
    wins" and no scan that picks one: the caller names the document, and this
    records the statement. It reads no fact, changes no fact, retires no fact
    and touches no provenance — the existing Digital Twin is exactly as it was
    before and after.

    It refuses to run while another document is already active, because a
    profile with two active CVs has no answer to "what is the baseline".
    """
    _require_no_transaction(connection, "declare_active_cv_document")
    digest = _digest(content_sha256)
    connection.execute("BEGIN IMMEDIATE")
    try:
        active = connection.execute(
            f"SELECT {_DOCUMENT_COLUMNS} FROM profile_cv_documents "
            "WHERE profile_id = ? AND lifecycle = 'ACTIVE'",
            (profile_id,),
        ).fetchone()
        if active is not None:
            current = _document_from_row(active)
            connection.execute("ROLLBACK")
            if current.content_sha256 == digest:
                # Declaring the same document again states what is already
                # recorded, so it is a no-op rather than a failure.
                return current
            raise ActiveCvDocumentError(
                f"profile {profile_id} already has an active CV document"
            )
        existing = connection.execute(
            "SELECT id FROM profile_cv_documents WHERE profile_id = ? AND content_sha256 = ?",
            (profile_id, digest),
        ).fetchone()
        if existing is None:
            row = connection.execute(
                f"""INSERT INTO profile_cv_documents (
                        profile_id, content_sha256, origin, byte_size, page_count,
                        lifecycle, activated_at
                    ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', CURRENT_TIMESTAMP)
                    RETURNING {_DOCUMENT_COLUMNS}""",
                (profile_id, digest, origin.value, byte_size, page_count),
            ).fetchone()
        else:
            row = connection.execute(
                f"""UPDATE profile_cv_documents
                       SET lifecycle = 'ACTIVE', activated_at = CURRENT_TIMESTAMP
                     WHERE id = ? AND profile_id = ? AND lifecycle = 'KNOWN'
                 RETURNING {_DOCUMENT_COLUMNS}""",
                (existing[0], profile_id),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise ActiveCvDocumentError(
                    "only a KNOWN document can be declared active"
                )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    if row is None:
        raise CvStagingError("document activation returned no row")
    return _document_from_row(row)


# --------------------------------------------------------------- extractions


@dataclass(frozen=True)
class ExtractionOutcome:
    """What `ensure_extraction_with_manifest` did, and why."""

    extraction: CvExtraction
    #: True when this call wrote the campaign and its manifest.
    created: bool
    #: The verification of the campaign that was reused, when one was.
    reused_verification: ManifestVerification | None
    #: Set when a previous attempt was found corrupt and left in place.
    abandoned_attempt_no: int | None

    def summary(self) -> dict[str, object]:
        return {
            "created": self.created,
            "abandoned_attempt_no": self.abandoned_attempt_no,
            "reused_verification": (
                None
                if self.reused_verification is None
                else self.reused_verification.summary()
            ),
        } | self.extraction.summary()


def get_extraction(
    connection: sqlite3.Connection, *, profile_id: int, extraction_id: int
) -> CvExtraction | None:
    row = connection.execute(
        f"SELECT {_EXTRACTION_COLUMNS} FROM profile_cv_extractions "
        "WHERE id = ? AND profile_id = ?",
        (extraction_id, profile_id),
    ).fetchone()
    return None if row is None else _extraction_from_row(row)


def _latest_attempt(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    document_id: int,
    parser_version: str,
    extractor_version: str,
) -> CvExtraction | None:
    row = connection.execute(
        f"""SELECT {_EXTRACTION_COLUMNS} FROM profile_cv_extractions
             WHERE document_id = ? AND profile_id = ?
               AND parser_version = ? AND extractor_version = ?
             ORDER BY attempt_no DESC LIMIT 1""",
        (document_id, profile_id, parser_version, extractor_version),
    ).fetchone()
    return None if row is None else _extraction_from_row(row)


def _mark_manifest_corrupt(
    connection: sqlite3.Connection, *, profile_id: int, extraction_id: int
) -> None:
    """Mark, never delete and never rewrite. The rows stay auditable."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """UPDATE profile_cv_extractions
                  SET manifest_state = 'CORRUPT', corrupted_at = CURRENT_TIMESTAMP
                WHERE id = ? AND profile_id = ? AND manifest_state = 'COMPLETE'""",
            (extraction_id, profile_id),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def ensure_extraction_with_manifest(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    document_id: int,
    extraction: StructuredCvExtraction,
) -> ExtractionOutcome:
    """Reuse a verified campaign, or record a genuinely distinct new attempt.

    Re-uploading the same PDF after a cancellation is the ordinary case, and it
    must neither duplicate a manifest nor collide with the one already stored.
    So:

    * the latest attempt for this document and these versions is verified. If
      the chain still holds, it is reused exactly as it is — nothing is written,
      and the manifest keeps the identity the first reading gave it;
    * if it does not hold, it is marked `CORRUPT` and **left alone**. It is
      never repaired, never overwritten and never reused. A new attempt is
      recorded under the next `attempt_no`, which is why `attempt_no` is part of
      the uniqueness and the digests are not;
    * a campaign already marked `CORRUPT` is skipped the same way.

    Nothing of that happens before the input has been checked. The document has
    to belong to this profile, the extraction has to name that document, and
    every candidate has to carry the same digest and the same parser and
    extractor versions. A caller handing over an inconsistent extraction is a
    caller mistake and says nothing about what is stored, so it raises
    `ExtractionMismatchError` and **no** campaign is written or marked.
    """
    _require_no_transaction(connection, "ensure_extraction_with_manifest")
    document = get_cv_document(
        connection, profile_id=profile_id, document_id=document_id
    )
    if document is None:
        raise CvDocumentNotFoundError(
            f"document {document_id} does not belong to profile {profile_id}"
        )
    validate_extraction_for_document(
        extraction, content_sha256=document.content_sha256
    )
    supplied_digest = chain_digest_of(extraction)
    latest = _latest_attempt(
        connection,
        profile_id=profile_id,
        document_id=document_id,
        parser_version=extraction.parser_version,
        extractor_version=extraction.extractor_version,
    )
    abandoned: int | None = None
    if latest is not None and latest.manifest_state is ManifestState.COMPLETE:
        verification = verify_manifest(
            connection, profile_id=profile_id, extraction_id=latest.id
        )
        if verification.ok:
            if latest.manifest_chain_digest != supplied_digest:
                # Same document and same version labels, different readings.
                # The stored campaign is intact — it verifies — so it is not
                # corrupt and must not be marked as such; what disagrees is the
                # extraction handed in, and that is refused without writing.
                raise ExtractionMismatchError(
                    f"attempt {latest.attempt_no} of this document under "
                    f"{latest.parser_version}/{latest.extractor_version} holds a "
                    f"different reading than the extraction supplied"
                )
            return ExtractionOutcome(
                extraction=latest,
                created=False,
                reused_verification=verification,
                abandoned_attempt_no=None,
            )
        _mark_manifest_corrupt(
            connection, profile_id=profile_id, extraction_id=latest.id
        )
        abandoned = latest.attempt_no
    elif latest is not None:
        abandoned = latest.attempt_no
    stored = write_extraction_with_manifest(
        connection,
        profile_id=profile_id,
        document_id=document_id,
        extraction=extraction,
        attempt_no=1 if latest is None else latest.attempt_no + 1,
    )
    return ExtractionOutcome(
        extraction=stored,
        created=True,
        reused_verification=None,
        abandoned_attempt_no=abandoned,
    )


# -------------------------------------------------------------- replacements


def get_replacement(
    connection: sqlite3.Connection, *, profile_id: int, replacement_id: int
) -> Replacement | None:
    row = connection.execute(
        f"SELECT {_REPLACEMENT_COLUMNS} FROM profile_cv_replacements "
        "WHERE id = ? AND profile_id = ?",
        (replacement_id, profile_id),
    ).fetchone()
    return None if row is None else _replacement_from_row(row)


def get_open_replacement(
    connection: sqlite3.Connection, profile_id: int
) -> Replacement | None:
    row = connection.execute(
        f"""SELECT {_REPLACEMENT_COLUMNS} FROM profile_cv_replacements
             WHERE profile_id = ?
               AND lifecycle IN ('PREPARED', 'REVIEWING', 'READY_TO_ACTIVATE')""",
        (profile_id,),
    ).fetchone()
    return None if row is None else _replacement_from_row(row)


def open_replacement(
    connection: sqlite3.Connection, *, profile_id: int, extraction_id: int
) -> Replacement:
    """Open one review over a verified campaign. Writes no fact of any kind.

    The baseline is whatever document is active *now*; `None` is a legitimate
    baseline and means the profile has never declared one.
    """
    _require_no_transaction(connection, "open_replacement")
    connection.execute("BEGIN IMMEDIATE")
    try:
        extraction = connection.execute(
            f"SELECT {_EXTRACTION_COLUMNS} FROM profile_cv_extractions "
            "WHERE id = ? AND profile_id = ?",
            (extraction_id, profile_id),
        ).fetchone()
        if extraction is None:
            connection.execute("ROLLBACK")
            raise ReplacementNotFoundError(
                f"extraction {extraction_id} does not belong to profile {profile_id}"
            )
        if _extraction_from_row(extraction).manifest_state is not ManifestState.COMPLETE:
            connection.execute("ROLLBACK")
            raise CvStagingError(
                "a replacement cannot be opened over a corrupt manifest"
            )
        open_row = connection.execute(
            f"""SELECT {_REPLACEMENT_COLUMNS} FROM profile_cv_replacements
                 WHERE profile_id = ?
                   AND lifecycle IN ('PREPARED', 'REVIEWING', 'READY_TO_ACTIVATE')""",
            (profile_id,),
        ).fetchone()
        if open_row is not None:
            connection.execute("ROLLBACK")
            raise OpenReplacementExistsError(
                f"profile {profile_id} already has an open replacement"
            )
        active = connection.execute(
            "SELECT id FROM profile_cv_documents "
            "WHERE profile_id = ? AND lifecycle = 'ACTIVE'",
            (profile_id,),
        ).fetchone()
        row = connection.execute(
            f"""INSERT INTO profile_cv_replacements (
                    profile_id, extraction_id, baseline_document_id, lifecycle
                ) VALUES (?, ?, ?, 'PREPARED')
                RETURNING {_REPLACEMENT_COLUMNS}""",
            (profile_id, extraction_id, None if active is None else active[0]),
        ).fetchone()
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    if row is None:
        raise CvStagingError("replacement insert returned no row")
    return _replacement_from_row(row)


#: What this function may set. `READY_TO_ACTIVATE` is deliberately absent: it
#: is a statement that a complete, still-current review exists, and only the
#: review owner can make it — inside the transaction that checks it. A caller
#: able to set it here could declare an unanswered review ready.
_SETTABLE_LIFECYCLES: frozenset[ReplacementLifecycle] = frozenset(
    {ReplacementLifecycle.REVIEWING, ReplacementLifecycle.CANCELLED}
)


def set_replacement_lifecycle(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    replacement_id: int,
    target: ReplacementLifecycle,
) -> Replacement:
    """Move an open attempt to REVIEWING or CANCELLED, and to nothing else.

    `READY_TO_ACTIVATE` is refused here, and no statement in this module writes
    it. It is set only by
    `services.digital_twin.cv.replacement.staging.mark_ready_to_activate_in_transaction`,
    which verifies the review itself before writing, so the check and the claim
    cannot come apart whichever entry point a caller reaches for.
    """
    if target not in _SETTABLE_LIFECYCLES:
        raise CvStagingError(
            f"{target.value} is not a lifecycle this function may set"
        )
    _require_no_transaction(connection, "set_replacement_lifecycle")
    closing = target is ReplacementLifecycle.CANCELLED
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = connection.execute(
            f"SELECT {_REPLACEMENT_COLUMNS} FROM profile_cv_replacements "
            "WHERE id = ? AND profile_id = ?",
            (replacement_id, profile_id),
        ).fetchone()
        if current is None:
            connection.execute("ROLLBACK")
            raise ReplacementNotFoundError(
                f"replacement {replacement_id} does not belong to profile {profile_id}"
            )
        existing = _replacement_from_row(current)
        if existing.lifecycle not in OPEN_REPLACEMENT_LIFECYCLES:
            connection.execute("ROLLBACK")
            raise CvStagingError("a closed replacement does not move again")
        row = connection.execute(
            f"""UPDATE profile_cv_replacements
                   SET lifecycle = ?,
                       updated_at = CURRENT_TIMESTAMP,
                       closed_at = {"CURRENT_TIMESTAMP" if closing else "NULL"}
                 WHERE id = ? AND profile_id = ?
             RETURNING {_REPLACEMENT_COLUMNS}""",
            (target.value, replacement_id, profile_id),
        ).fetchone()
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    if row is None:
        raise CvStagingError("replacement update returned no row")
    return _replacement_from_row(row)


def cancel_replacement(
    connection: sqlite3.Connection, *, profile_id: int, replacement_id: int
) -> Replacement:
    """Abandon the review. The Digital Twin is untouched, so there is nothing
    to undo: no fact was created, no status moved and no evidence was attached.
    The decisions stay readable, because what somebody answered is worth
    keeping even once they changed their mind about the whole thing.
    """
    return set_replacement_lifecycle(
        connection,
        profile_id=profile_id,
        replacement_id=replacement_id,
        target=ReplacementLifecycle.CANCELLED,
    )


def list_staged_decisions(
    connection: sqlite3.Connection, *, profile_id: int, replacement_id: int
) -> tuple[StagedDecision, ...]:
    rows = connection.execute(
        f"""SELECT {_DECISION_COLUMNS} FROM profile_cv_replacement_decisions
             WHERE replacement_id = ? AND profile_id = ?
             ORDER BY id""",
        (replacement_id, profile_id),
    ).fetchall()
    return tuple(_decision_from_row(row) for row in rows)


# ------------------------------------------------------------ baseline facts


@dataclass(frozen=True)
class BaselineFact:
    """One fact the active CV document produced, as the plan needs to see it.

    `value` is CV text — the plan compares readings byte for byte, so it has to
    be here — and `repr=False` keeps it out of the automatic `repr` a traceback
    or a logging call would print.
    """

    fact_id: int
    fact_type: str
    value: str = field(repr=False)
    status: str
    #: True when a correction points at this fact as its replacement.
    is_user_input_replacement: bool
    #: True when at least one piece of its evidence is not a CV.
    has_non_cv_evidence: bool


def list_baseline_cv_facts(
    connection: sqlite3.Connection, *, profile_id: int, content_sha256: str
) -> tuple[BaselineFact, ...]:
    """The accepted facts this profile holds from that document.

    `source_type = 'CV'` is written into the statement rather than passed in, so
    no caller can widen it and read somebody's own corrections back as if a
    document had produced them. The two protection flags are computed here, in
    SQL, from the database as it stands — not from a label a screen stored
    earlier — because they are what decides whether a fact may ever be offered
    for retirement.
    """
    rows = connection.execute(
        """SELECT DISTINCT f.id, f.fact_type, f.value, f.status,
                  EXISTS (SELECT 1 FROM profile_facts AS r
                           WHERE r.profile_id = f.profile_id
                             AND r.replaced_by_fact_id = f.id),
                  EXISTS (SELECT 1 FROM profile_fact_provenance AS q
                           WHERE q.fact_id = f.id AND q.source_type <> 'CV')
             FROM profile_facts AS f
             JOIN profile_fact_provenance AS p ON p.fact_id = f.id
            WHERE f.profile_id = ?
              AND f.status = 'ACCEPTED'
              AND f.retired_at IS NULL
              AND p.source_type = 'CV'
              AND p.cv_sha256 = ?
            ORDER BY f.id""",
        (profile_id, content_sha256),
    ).fetchall()
    return tuple(
        BaselineFact(
            fact_id=row[0],
            fact_type=row[1],
            value=row[2],
            status=row[3],
            is_user_input_replacement=bool(row[4]),
            has_non_cv_evidence=bool(row[5]),
        )
        for row in rows
    )


def list_fact_source_types(
    connection: sqlite3.Connection, *, profile_id: int, fact_id: int
) -> tuple[str, ...]:
    """Every distinct evidence source recorded for one fact of this profile."""
    rows = connection.execute(
        """SELECT DISTINCT p.source_type
             FROM profile_fact_provenance AS p
             JOIN profile_facts AS f ON f.id = p.fact_id
            WHERE p.fact_id = ? AND f.profile_id = ?
            ORDER BY p.source_type""",
        (fact_id, profile_id),
    ).fetchall()
    return tuple(row[0] for row in rows)


def fact_evidence_digest(
    connection: sqlite3.Connection, *, profile_id: int, fact_id: int
) -> str:
    """A digest of every piece of evidence this fact rests on, right now.

    Sorted, so it is a function of the set and not of insertion order. It is
    what makes "this fact gained a GITHUB or USER_INPUT proof after you decided
    about it" detectable: the digest moves, and the review fails closed.

    The provenance keys are opaque identifiers, not readings, so a digest of
    them carries no CV text — and only the digest ever leaves this function.
    """
    rows = connection.execute(
        """SELECT p.source_type, p.provenance_key
             FROM profile_fact_provenance AS p
             JOIN profile_facts AS f ON f.id = p.fact_id
            WHERE p.fact_id = ? AND f.profile_id = ?""",
        (fact_id, profile_id),
    ).fetchall()
    payload = "\x1f".join(
        f"{source}\x1e{key}" for source, key in sorted(rows)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
