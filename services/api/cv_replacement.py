"""HTTP surface for the CV replacement review, over the B1b owners.

This module translates. It reads a request, resolves the profile from the
server's own configuration, calls exactly one domain owner, and turns what comes
back — a snapshot or a refusal — into an answer. It decides nothing: which
answers a plan entry allows, whether a review is complete, whether a token still
describes the stored decisions, whether an activation may proceed and what it
writes are all settled in `services/digital_twin/cv/replacement/`, and are
settled there only.

**No transaction is opened here.** `open_replacement`, `record_review_decision`,
`mark_ready_to_activate`, `cancel_replacement` and `activate_cv_replacement`
each own a `BEGIN IMMEDIATE` and refuse a borrowed one. Wrapping any of them in
an API transaction would not add safety; it would break them.

**What this surface may show, and what it may never say.** A review is the one
place a person has to see their own CV readings, so `display_value` carries
them — to this profile's own browser, over the same localhost surface every
other page uses. That licence is exactly as wide as it needs to be and no
wider: a reading never reaches a log, an error message, a repr, or any response
built for a machine rather than for the person reviewing. A staged correction is
narrower still — the domain keeps it unreadable from outside, and this surface
does not widen that: a correction is reported as `has_staged_value`, never as
text. Nothing derived that could identify a reading — `provenance_key`,
`state_digest`, `reading_digest`, `normalized_value` — is published either.

What this phase does **not** do: accept an upload, read a PDF, create an
extraction, or synchronize any downstream phase after an activation. Activation
publishes the Recommendation as INCOMPLETE and leaves every watermark where it
was, exactly as B1b-C intends; re-synchronizing is an operator's act.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)
from services.collector.logging_config import get_logger
from services.digital_twin.cv.replacement.activation import (
    ActiveCvChangedError,
    AlreadyActiveDocumentError,
    AmbiguousBaselineReadingError,
    HistoricalDocumentReactivationError,
    ReviewChangedError,
    activate_cv_replacement,
)
from services.digital_twin.cv.replacement.manifest import read_manifest
from services.digital_twin.cv.replacement.models import (
    CvDocumentNotFoundError,
    CvStagingError,
    DecisionRole,
    ExtractionNotFoundError,
    ExtractionMismatchError,
    ManifestIntegrityError,
    OpenReplacementExistsError,
    PlanEntry,
    Replacement,
    ReplacementAlreadyActivatedError,
    ReplacementClosedError,
    ReplacementNotFoundError,
    ReviewDecision,
    StagedDecision,
)
from services.digital_twin.cv.replacement.planning import compute_replacement_plan
from services.digital_twin.cv.replacement.repository import (
    cancel_replacement,
    get_cv_document,
    get_extraction,
    get_open_replacement,
    get_replacement,
    list_baseline_cv_facts,
    list_staged_decisions,
    open_replacement,
)
from services.digital_twin.cv.replacement.staging import (
    mark_ready_to_activate,
    record_review_decision,
    review_progress,
)
from services.digital_twin.facts.repository import ProfileFactError

LOGGER = get_logger("services.api.cv_replacement")

#: Fixed sentences. Nothing that came out of the database, and nothing that came
#: in from the caller, is ever interpolated into any of them.
PUBLIC_CV_REPLACEMENT_ERROR = "CV replacement review is temporarily unavailable."
PUBLIC_CV_REPLACEMENT_REQUEST_ERROR = "CV replacement request is invalid."
PUBLIC_REPLACEMENT_NOT_FOUND = "CV replacement review not found."
PUBLIC_EXTRACTION_NOT_FOUND = "CV extraction not found."

#: The migration this workflow needs, and the objects 0027 and 0028 actually
#: create that it touches. Read off the two files rather than assumed: the five
#: staging tables, the watermark table an activation's downstream contract
#: depends on, and the four columns 0028 adds by `ALTER TABLE` — which
#: `sqlite_master` alone would not reveal, because the tables they belong to
#: already existed.
REQUIRED_MIGRATION_VERSION = "0028"
_REQUIRED_TABLES: tuple[str, ...] = (
    "profile_cv_documents",
    "profile_cv_extractions",
    "profile_cv_candidates",
    "profile_cv_replacements",
    "profile_cv_replacement_decisions",
    "profile_downstream_sync_watermark",
)
_REQUIRED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("profile_cv_replacements", "ready_review_digest"),
    ("profile_cv_replacements", "activation_revision"),
    ("profile_cv_replacements", "activated_at"),
    ("profile_facts", "retired_at"),
)

#: A correction is typed by a person into a review field. The bound is explicit
#: so that an absurd body is refused by length rather than by whatever the
#: database happens to accept, and it is generous enough for the longest
#: experience or education line a CV realistically holds.
MAX_STAGED_VALUE_LENGTH = 4000

_INCOMING_DECISIONS = ("ACCEPT", "REJECT", "CORRECT", "SKIP_BLOCKED")
_EXISTING_DECISIONS = ("KEEP", "RETIRE")


class CvReplacementApiError(RuntimeError):
    """The surface cannot serve the request at all. Answered 503."""


class CvReplacementApiRequestError(RuntimeError):
    """The request is malformed. Answered 400."""


class CvReplacementApiNotFoundError(RuntimeError):
    """This profile has no such replacement or extraction. Answered 404."""


class CvReplacementApiConflictError(RuntimeError):
    """The domain refused on business grounds. Answered 409."""


# --------------------------------------------------------------- responses


class ReplacementResponse(BaseModel):
    """The attempt itself. Identifiers, states and a token — never a reading."""

    replacement_id: int
    extraction_id: int
    baseline_document_id: int | None
    lifecycle: str
    effective_state: str
    ready_review_digest: str | None
    activation_revision: int | None
    activated_at: str | None


class ReviewProgressResponse(BaseModel):
    """How much of the plan is answered, and how much of it has gone stale."""

    plan_entries: int
    answered: int
    unanswered: int
    decisions_outside_plan: int
    stale_decisions: int
    counts_by_difference: dict[str, int]


class ReviewEntryResponse(BaseModel):
    """One thing the review must answer, as the person needs to see it.

    `display_value` is the reading itself, and it is the only field here that
    is. `has_staged_value` says a correction was typed without saying what it
    says, which is the contract the domain already keeps.
    """

    role: str
    difference: str
    candidate_id: int | None
    fact_id: int | None
    fact_type: str
    fact_status: str | None
    #: The reading itself, serialized to the browser as normal and kept out of
    #: `repr` — which is what a traceback, a logging call and a failed
    #: assertion all print without anybody deciding to. The same protection
    #: `ManifestEntry` and `BaselineFact` already give the values they hold.
    display_value: str = Field(repr=False)
    decision: str | None
    has_staged_value: bool


class ReviewSnapshotResponse(BaseModel):
    """One coherent picture of an open review, or the absence of one."""

    replacement: ReplacementResponse | None
    progress: ReviewProgressResponse | None = None
    entries: tuple[ReviewEntryResponse, ...] = ()


class ActivationResponse(BaseModel):
    """What one activation did. Counters and identifiers, no CV value at all."""

    replacement_id: int
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
    effective_state: str


class CancelResponse(BaseModel):
    replacement_id: int
    effective_state: str


# ------------------------------------------------------- configuration, db


def _profile_id() -> int:
    """The one profile this deployment serves. Never taken from a request."""
    raw = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID")
    try:
        value = int(raw) if raw is not None else 0
    except ValueError as error:
        raise CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR) from error
    if raw is None or value <= 0 or raw.strip() != str(value):
        raise CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR)
    return value


def _database_path() -> Path:
    """An existing SQLite file, or a refusal. Never a path that gets created."""
    settings = load_settings()
    if (
        settings.database_backend is not DatabaseBackend.SQLITE
        or settings.sqlite_database_path is None
    ):
        raise CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR)
    path = Path(settings.sqlite_database_path)
    if not path.is_file():
        # `connect_database` would happily create an empty database here, and
        # the review would then be served against a file nobody migrated.
        raise CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR)
    return path


def preflight(connection: sqlite3.Connection) -> None:
    """Refuse a database this workflow cannot be run against.

    The recorded migration, the tables and the four columns `ALTER TABLE` added
    are all checked, before any owner is called and therefore before anything
    could be written. A schema that is half applied fails with one fixed
    sentence here rather than with "no such column" from inside a transaction
    that has already changed something.
    """
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (REQUIRED_MIGRATION_VERSION,),
        ).fetchone()
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns: set[tuple[str, str]] = set()
        for table in {name for name, _ in _REQUIRED_COLUMNS}:
            if table not in present:
                continue
            for row in connection.execute(f"PRAGMA table_info({table})"):
                columns.add((table, str(row[1])))
    except sqlite3.Error as error:
        raise CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR) from error
    if (
        migrated is None
        or not set(_REQUIRED_TABLES) <= present
        or not set(_REQUIRED_COLUMNS) <= columns
    ):
        raise CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR)


def _connect_read() -> sqlite3.Connection:
    connection = connect_readonly_database(_database_path())
    try:
        connection.execute("PRAGMA query_only = ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if enabled is None or enabled[0] != 1:
            raise CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR)
        preflight(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _connect_write() -> sqlite3.Connection:
    connection = connect_database(_database_path())
    try:
        preflight(connection)
    except Exception:
        connection.close()
        raise
    return connection


# ------------------------------------------------------------- body parsing


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CvReplacementApiRequestError(PUBLIC_CV_REPLACEMENT_REQUEST_ERROR)
    return value


def _identifier(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CvReplacementApiRequestError(f"{field} must be a positive integer")
    return value


def parse_open_request(body: Any) -> int:
    """Read the extraction this review is about, and nothing else.

    A body carrying `profile_id` is refused rather than ignored: resolving the
    owner on the server is the whole point, and dropping the field silently
    would hide an attempt to choose one.
    """
    payload = _mapping(body)
    if set(payload) != {"extraction_id"}:
        raise CvReplacementApiRequestError(PUBLIC_CV_REPLACEMENT_REQUEST_ERROR)
    return _identifier(payload.get("extraction_id"), "extraction_id")


def parse_decision_request(
    body: Any,
) -> tuple[int | None, int | None, ReviewDecision, str | None]:
    """Read one answer: exactly one target, one decision, one optional value.

    Everything this checks is about the *shape* of the request. Whether the
    answer is allowed — the role it belongs to, the difference it faces, a
    terminal fact, a protected fact, a stale target — is decided by
    `record_review_decision`, and is not second-guessed here.

    `staged_normalized_value` is not accepted from a client at all: a normal
    form is computed from a value, never declared alongside it.
    """
    payload = _mapping(body)
    allowed = {"candidate_id", "fact_id", "decision", "staged_value"}
    if not set(payload) <= allowed or "decision" not in payload:
        raise CvReplacementApiRequestError(PUBLIC_CV_REPLACEMENT_REQUEST_ERROR)
    has_candidate = "candidate_id" in payload
    has_fact = "fact_id" in payload
    if has_candidate == has_fact:
        raise CvReplacementApiRequestError(
            "a decision names a candidate or an existing fact, not both"
        )
    candidate_id = (
        _identifier(payload["candidate_id"], "candidate_id") if has_candidate else None
    )
    fact_id = _identifier(payload["fact_id"], "fact_id") if has_fact else None

    raw_decision = payload.get("decision")
    if not isinstance(raw_decision, str):
        raise CvReplacementApiRequestError(PUBLIC_CV_REPLACEMENT_REQUEST_ERROR)
    permitted = _INCOMING_DECISIONS if has_candidate else _EXISTING_DECISIONS
    if raw_decision not in permitted:
        raise CvReplacementApiRequestError(
            f"decision must be one of: {', '.join(permitted)}"
        )
    decision = ReviewDecision(raw_decision)

    staged_value = None
    if decision is ReviewDecision.CORRECT:
        if "staged_value" not in payload:
            raise CvReplacementApiRequestError(
                "a correction stages the value it proposes"
            )
        raw_value = payload["staged_value"]
        if not isinstance(raw_value, str):
            raise CvReplacementApiRequestError(PUBLIC_CV_REPLACEMENT_REQUEST_ERROR)
        if not raw_value.strip():
            raise CvReplacementApiRequestError(
                "a correction stages the value it proposes"
            )
        if len(raw_value) > MAX_STAGED_VALUE_LENGTH:
            # Reported by length only. The value itself never appears here.
            raise CvReplacementApiRequestError(
                f"staged_value must be at most {MAX_STAGED_VALUE_LENGTH} characters"
            )
        # Kept exactly as typed: trimming would silently change the correction
        # the person is about to see stored under their own name.
        staged_value = raw_value
    elif "staged_value" in payload:
        raise CvReplacementApiRequestError("only a correction stages a value")

    return candidate_id, fact_id, decision, staged_value


def parse_activate_request(body: Any) -> str:
    """Read the review token the person confirmed, and nothing else."""
    payload = _mapping(body)
    if set(payload) != {"review_digest"}:
        raise CvReplacementApiRequestError(PUBLIC_CV_REPLACEMENT_REQUEST_ERROR)
    digest = payload.get("review_digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise CvReplacementApiRequestError(
            "review_digest must be 64 lowercase hexadecimal characters"
        )
    return digest


# ------------------------------------------------------------- snapshotting


@contextmanager
def _read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    """Hold one SQLite read transaction across a whole logical snapshot.

    A snapshot is several SELECTs — the attempt, the plan, the stored answers,
    the progress, the manifest, the baseline facts — and Python's `sqlite3`
    opens a transaction only for DML, never for a SELECT. Without a boundary
    each statement is its own implicit read transaction, so another connection
    may commit in between and the answer returned would mix two database
    states: a plan and a `ready_review_digest` from before a decision changed,
    beside the decisions from after it. That is not merely stale, it is a
    review that never existed.

    A plain `BEGIN` is a **deferred** transaction: it takes no lock until the
    first read, and from that read onwards every statement sees one snapshot.
    Never `BEGIN IMMEDIATE` — that reserves the write lock a reader must not
    take, and it would fail outright on a `mode=ro` connection.

    It is opened **after** a write owner has returned, never around one: the
    owners hold their own `BEGIN IMMEDIATE` and refuse a borrowed transaction.
    So this composes the picture of what they have already committed.

    It always ends with `ROLLBACK`: a read transaction has nothing to persist,
    and `ROLLBACK` is the one ending that cannot write even by accident.
    """
    if connection.in_transaction:
        # Borrowed, not owned: begin nothing, end nothing.
        yield
        return
    try:
        connection.execute("BEGIN")
    except sqlite3.Error as error:
        raise CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR) from error
    try:
        yield
    except BaseException:
        # The failure inside the body is the report the caller needs; a failure
        # while releasing the snapshot must not replace it.
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as error:
        raise CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR) from error


def _replacement_response(replacement: Replacement) -> ReplacementResponse:
    return ReplacementResponse(
        replacement_id=replacement.id,
        extraction_id=replacement.extraction_id,
        baseline_document_id=replacement.baseline_document_id,
        lifecycle=replacement.lifecycle.value,
        effective_state=replacement.effective_state.value,
        ready_review_digest=replacement.ready_review_digest,
        activation_revision=replacement.activation_revision,
        activated_at=replacement.activated_at,
    )


def _display_values(
    connection: sqlite3.Connection, *, profile_id: int, replacement: Replacement
) -> tuple[dict[int, str], dict[int, str]]:
    """The readings to show, keyed by candidate id and by fact id.

    Both sides come from the owners that already read them — the manifest for
    the incoming document, `list_baseline_cv_facts` for the active one — so no
    statement here decides what a reading is.
    """
    incoming = {
        entry.id: entry.value
        for entry in read_manifest(
            connection, profile_id=profile_id, extraction_id=replacement.extraction_id
        )
        if entry.id is not None
    }
    existing: dict[int, str] = {}
    if replacement.baseline_document_id is not None:
        document = get_cv_document(
            connection,
            profile_id=profile_id,
            document_id=replacement.baseline_document_id,
        )
        if document is not None:
            existing = {
                fact.fact_id: fact.value
                for fact in list_baseline_cv_facts(
                    connection,
                    profile_id=profile_id,
                    content_sha256=document.content_sha256,
                )
            }
    return incoming, existing


def _entry_response(
    entry: PlanEntry,
    decision: StagedDecision | None,
    incoming: dict[int, str],
    existing: dict[int, str],
) -> ReviewEntryResponse:
    if entry.role is DecisionRole.INCOMING:
        display = incoming.get(entry.candidate_id or 0, "")
    else:
        display = existing.get(entry.fact_id or 0, "")
    return ReviewEntryResponse(
        role=entry.role.value,
        difference=entry.difference.value,
        candidate_id=entry.candidate_id,
        fact_id=entry.fact_id,
        fact_type=entry.fact_type,
        fact_status=entry.fact_status,
        display_value=display,
        decision=None if decision is None else decision.decision.value,
        has_staged_value=False if decision is None else decision.has_staged_value,
    )


def _snapshot(
    connection: sqlite3.Connection, *, profile_id: int, replacement: Replacement
) -> ReviewSnapshotResponse:
    """One coherent picture: the attempt, its progress and every entry.

    Everything below is read inside one snapshot, so the plan, the answers, the
    progress and the readings all describe the same instant. The attempt itself
    is re-read there too rather than trusted from the argument: it may have been
    fetched before the snapshot opened, and a `ready_review_digest` from before
    a decision changed, sitting beside the decisions from after it, is exactly
    the mixture this exists to prevent.
    """
    with _read_snapshot(connection):
        current = get_replacement(
            connection, profile_id=profile_id, replacement_id=replacement.id
        )
        if current is None:
            raise CvReplacementApiNotFoundError(PUBLIC_REPLACEMENT_NOT_FOUND)
        replacement = current
        plan = compute_replacement_plan(
            connection, profile_id=profile_id, replacement_id=replacement.id
        )
        decisions = list_staged_decisions(
            connection, profile_id=profile_id, replacement_id=replacement.id
        )
        answered = {
            (decision.candidate_id, decision.fact_id): decision
            for decision in decisions
        }
        progress = review_progress(
            connection, profile_id=profile_id, replacement_id=replacement.id
        )
        incoming, existing = _display_values(
            connection, profile_id=profile_id, replacement=replacement
        )
    return ReviewSnapshotResponse(
        replacement=_replacement_response(replacement),
        progress=ReviewProgressResponse(
            plan_entries=int(progress["plan_entries"]),
            answered=int(progress["answered"]),
            unanswered=int(progress["unanswered"]),
            decisions_outside_plan=int(progress["decisions_outside_plan"]),
            stale_decisions=int(progress["stale_decisions"]),
            counts_by_difference=dict(progress["counts_by_difference"]),
        ),
        entries=tuple(
            _entry_response(
                entry,
                answered.get((entry.candidate_id, entry.fact_id)),
                incoming,
                existing,
            )
            for entry in plan.entries
        ),
    )


def _reload_snapshot(
    connection: sqlite3.Connection, *, profile_id: int, replacement_id: int
) -> ReviewSnapshotResponse:
    with _read_snapshot(connection):
        replacement = get_replacement(
            connection, profile_id=profile_id, replacement_id=replacement_id
        )
        if replacement is None:
            raise CvReplacementApiNotFoundError(PUBLIC_REPLACEMENT_NOT_FOUND)
        # Inside the snapshot already: `_snapshot` borrows it and ends nothing.
        return _snapshot(
            connection, profile_id=profile_id, replacement=replacement
        )


# ------------------------------------------------------------- translation


#: Domain refusals that mean "this profile does not have that".
_NOT_FOUND = (ReplacementNotFoundError, CvDocumentNotFoundError)
#: Domain refusals that mean "the request contradicts the stored review".
_CONFLICT = (
    OpenReplacementExistsError,
    ReplacementClosedError,
    ReplacementAlreadyActivatedError,
    ManifestIntegrityError,
    ExtractionMismatchError,
    ReviewChangedError,
    ActiveCvChangedError,
    HistoricalDocumentReactivationError,
    AlreadyActiveDocumentError,
    AmbiguousBaselineReadingError,
)


def _translate(error: Exception, operation: str) -> Exception:
    """Turn one domain refusal into the answer this surface owes the caller.

    The domain's own messages are allowed through for conflicts because they
    are built from canonical names, ids and counts — the packages that raise
    them keep readings out of their errors on purpose, and tests hold them to
    it. Anything unrecognised becomes the fixed sentence, because an unforeseen
    message is exactly the one nobody has checked.
    """
    if isinstance(error, ExtractionNotFoundError):
        return CvReplacementApiNotFoundError(PUBLIC_EXTRACTION_NOT_FOUND)
    if isinstance(error, _NOT_FOUND):
        return CvReplacementApiNotFoundError(PUBLIC_REPLACEMENT_NOT_FOUND)
    if isinstance(error, _CONFLICT):
        return CvReplacementApiConflictError(str(error))
    if isinstance(error, (CvStagingError, ProfileFactError)):
        # Everything the review itself refuses: a decision the plan forbids, a
        # terminal fact, an incomplete, stale or contradictory review, a fact
        # this document may not retire.
        return CvReplacementApiConflictError(str(error))
    LOGGER.error(
        "CV replacement request failed.",
        extra={
            "event": f"cv_replacement_{operation}_failed",
            "operation": operation,
            "error_type": type(error).__name__,
        },
    )
    return CvReplacementApiError(PUBLIC_CV_REPLACEMENT_ERROR)


_API_ERRORS = (
    CvReplacementApiError,
    CvReplacementApiRequestError,
    CvReplacementApiNotFoundError,
    CvReplacementApiConflictError,
)


def _run(operation: str, action: Any, *, write: bool) -> Any:
    connection = None
    try:
        profile_id = _profile_id()
        connection = _connect_write() if write else _connect_read()
        return action(connection, profile_id)
    except _API_ERRORS:
        raise
    except Exception as error:
        raise _translate(error, operation) from None
    finally:
        if connection is not None:
            connection.close()


# ----------------------------------------------------------------- surfaces


def read_current_review_surface() -> ReviewSnapshotResponse:
    """The open review of the configured profile, or the absence of one."""

    def action(connection: sqlite3.Connection, profile_id: int):
        with _read_snapshot(connection):
            replacement = get_open_replacement(connection, profile_id)
            if replacement is None:
                # Not an error, and nothing is created to make one exist.
                return ReviewSnapshotResponse(replacement=None)
            return _snapshot(
                connection, profile_id=profile_id, replacement=replacement
            )

    return _run("read_current", action, write=False)


def open_review_surface(body: Any) -> ReviewSnapshotResponse:
    """Open a review over an extraction this profile already holds."""
    extraction_id = parse_open_request(body)

    def action(connection: sqlite3.Connection, profile_id: int):
        extraction = get_extraction(
            connection, profile_id=profile_id, extraction_id=extraction_id
        )
        if extraction is None:
            raise CvReplacementApiNotFoundError(PUBLIC_EXTRACTION_NOT_FOUND)
        replacement = open_replacement(
            connection, profile_id=profile_id, extraction_id=extraction_id
        )
        # The owner has committed and released its lock; the snapshot below
        # opens a read transaction of its own, never around the owner.
        return _reload_snapshot(
            connection, profile_id=profile_id, replacement_id=replacement.id
        )

    return _run("open", action, write=True)


def record_decision_surface(replacement_id: int, body: Any) -> ReviewSnapshotResponse:
    """Record or replace one human answer, through the owner that validates it."""
    candidate_id, fact_id, decision, staged_value = parse_decision_request(body)

    def action(connection: sqlite3.Connection, profile_id: int):
        record_review_decision(
            connection,
            profile_id=profile_id,
            replacement_id=replacement_id,
            decision=decision,
            candidate_id=candidate_id,
            fact_id=fact_id,
            staged_value=staged_value,
        )
        return _reload_snapshot(
            connection, profile_id=profile_id, replacement_id=replacement_id
        )

    return _run("decision", action, write=True)


def mark_ready_surface(replacement_id: int) -> ReviewSnapshotResponse:
    """Declare the review ready, and hand back the token it was sealed with."""

    def action(connection: sqlite3.Connection, profile_id: int):
        mark_ready_to_activate(
            connection, profile_id=profile_id, replacement_id=replacement_id
        )
        return _reload_snapshot(
            connection, profile_id=profile_id, replacement_id=replacement_id
        )

    return _run("ready", action, write=True)


def activate_surface(replacement_id: int, body: Any) -> ActivationResponse:
    """Apply the reviewed replacement, against the token the person confirmed.

    The digest is taken from the request and passed through untouched. Reading
    the stored one instead would turn the confirmation into a formality: the
    point of the token is that it describes the review the person actually
    looked at, so a review that moved since must fail here rather than be
    applied.
    """
    review_digest = parse_activate_request(body)

    def action(connection: sqlite3.Connection, profile_id: int):
        result = activate_cv_replacement(
            connection,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=review_digest,
        )
        return ActivationResponse(
            replacement_id=result.replacement.id,
            activated=result.activated,
            activation_revision=result.activation_revision,
            previous_document_id=result.previous_document_id,
            document_id=result.document_id,
            facts_accepted=result.facts_accepted,
            facts_rejected=result.facts_rejected,
            facts_corrected=result.facts_corrected,
            facts_retired=result.facts_retired,
            evidence_attached=result.evidence_attached,
            decisions_skipped=result.decisions_skipped,
            effective_state=result.replacement.effective_state.value,
        )

    return _run("activate", action, write=True)


def cancel_review_surface(replacement_id: int) -> CancelResponse:
    """Abandon an open review. Its decisions stay stored, as the domain says."""

    def action(connection: sqlite3.Connection, profile_id: int):
        replacement = cancel_replacement(
            connection, profile_id=profile_id, replacement_id=replacement_id
        )
        return CancelResponse(
            replacement_id=replacement.id,
            effective_state=replacement.effective_state.value,
        )

    return _run("cancel", action, write=True)
