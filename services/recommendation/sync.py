"""Controlled synchronization: readiness, computation and publication as one act.

Phases 9A and 9B.1/9B.2 each own one honest piece and deliberately stop at its
edge. `input_assembly` answers *may this profile be recommended right now, over
which cohort*; `engine` turns that cohort into a ranking without touching a
database; `persistence` stores a ranking it is handed; `read_model` and
`persistence_audit` read back what was stored. Nothing in any of them decides
*when* to recommend, and nothing writes the INCOMPLETE state the schema has
carried since 0026. This module is that missing step, and it is the only one
here that both reads freshness and writes.

The whole design is one sentence: **a published recommendation must describe a
world that actually existed**.

That is not rhetoric, it is the reason for the transaction contract below.
Readiness is a claim about upstream data at an instant — this Matching run, this
qualification projection, these location resolutions, this eligibility decision.
Between deciding that claim and publishing a recommendation built on it, any of
those can change. If assembly runs in one transaction and the write happens in
another, the recommendation that becomes *current* can describe a world that had
already stopped being true before it was stored, and no later audit could ever
detect it: every stored digest would agree with every other, because they were
all derived from the same, now-superseded, reading. The corruption would be
invisible and permanent.

So a persisted sync owns one `BEGIN IMMEDIATE` that opens **before the first
business read** and closes at the single `COMMIT`:

    BEGIN IMMEDIATE
      does this profile exist
      is the recommendation persistence schema actually there
      assemble_recommendation_inputs   <- all freshness, one snapshot
      READY: build the batch, read the source matching fingerprint, store it,
             point the state at it
      INCOMPLETE: point the state at nothing and record why
    COMMIT

`IMMEDIATE` rather than deferred is the point rather than a detail. A deferred
transaction takes no write lock until its first write, so another connection may
commit an upstream change *after* this one has read freshness and *before* it
writes — precisely the interleaving the transaction exists to exclude. It is
never `EXCLUSIVE`: reserving the database against readers is not needed to make
a publication atomic, and under WAL it would stop concurrent readers for no gain.

For the same reason a persisted sync refuses to run inside a transaction it did
not open. Borrowing one would mean the caller decides when — and whether — the
publication commits, and the guarantee above would become that caller's promise
to keep rather than this function's to make.

`persist=False` inverts every part of that and is strictly read-only: one
*deferred* snapshot when it owns one, a borrowed one when the caller has already
opened it, never a write, never a commit, and no persistence schema required at
all. It answers "what would a sync do right now" and it works on a `mode=ro`
connection.

What this module does **not** do, and must never be extended to do:

    it repairs no upstream phase
        no Matching resynchronization, no qualification reprojection, no
        geography resolution, no re-run of the eligibility engine. When the
        assembly says a projection is stale, the answer is INCOMPLETE and the
        name of the phase that owns the repair — not a repair performed from
        here, which would make this module a second writer of everybody
        else's truth.

    it computes no recommendation of its own
        no score, no disposition, no reason, no ranking, no weight, no
        threshold. `build_recommendation_batch` is called with the assembled
        inputs and its result is used exactly as returned.

    it invents no readiness
        INCOMPLETE is only ever the status `input_assembly` returned. An engine
        error, a persistence error or a SQLite failure is a failed
        synchronization — `RecommendationSyncError` — never a profile that
        "isn't ready". Relabelling a bug as readiness would publish a state that
        looks like ordinary staleness and hides a defect.

    it never rewrites history
        a run already stored is never updated, never deleted, and never
        re-pointed. READY appends or reuses; INCOMPLETE only moves the pointer
        to NULL. A profile that goes stale keeps every recommendation it ever
        made — they are what was recommended then, and that stays true.

There is no EMPTY state here. Matching has one, because "no opportunity was
selected" is a real and healthy outcome of a selection step. Recommendation has
no selection of its own: a cohort it cannot recommend over is INCOMPLETE, with
the assembly issue that says why.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .engine import build_recommendation_batch
from .input_assembly import (
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RecommendationInputAssemblyResult,
    RecommendationReadinessIssue,
    RecommendationReadinessStatus,
    assemble_recommendation_inputs,
)
from .models import RecommendationInputError
from .persistence import (
    RecommendationPersistenceError,
    _prepare,
    _set_recommendation_state_incomplete_in_transaction,
    _store_prepared_recommendation_batch_in_transaction,
)

__all__ = [
    "RecommendationSyncError",
    "RecommendationSyncResult",
    "sync_recommendations",
]

#: The migration that introduced the three tables a persisted sync writes. A
#: persisted sync verifies it rather than applying it: migrating a database is
#: an operator's decision, and a synchronization that quietly changed the schema
#: it found would be doing something nobody asked it to do.
_PERSISTENCE_MIGRATION = "0026"

_PERSISTENCE_TABLES = frozenset(
    {
        "recommendation_runs",
        "recommendation_assessments",
        "recommendation_profile_state",
    }
)

_SCHEMA_UNAVAILABLE = (
    "recommendation persistence schema is not ready; migrate the database"
    f" through {_PERSISTENCE_MIGRATION} before a persisted sync"
)


class RecommendationSyncError(RuntimeError):
    """Raised when a synchronization cannot be completed honestly.

    Not a readiness verdict and never a substitute for one. This says the
    orchestration itself failed — an unusable argument, an absent profile, a
    borrowed transaction, a missing schema, an assembly result that contradicts
    itself, a source run that cannot be read, a SQLite failure. INCOMPLETE is
    the only way a *profile* can be reported as not ready, and it comes from
    `input_assembly` alone.
    """


@dataclass(frozen=True)
class RecommendationSyncResult:
    """What one synchronization did, in the vocabulary the phase already has.

    The nullable fields are null for a reason rather than for convenience.
    `created`, `run_id` and `run_fingerprint` describe a *stored* run, so they
    are `None` whenever no run was stored — every INCOMPLETE, and every dry run.
    `batch_fingerprint` and `assessment_count` describe what the engine
    produced, so a READY dry run carries them in full: knowing what *would* be
    published is the whole purpose of asking without persisting.

    `persisted` says whether this call wrote, not whether it stored a run: an
    INCOMPLETE sync with `persist=True` really did write the state row, and
    saying otherwise would misreport a state change that a reader can see.
    """

    profile_id: int
    input_assembly_version: str
    state: RecommendationReadinessStatus
    persisted: bool
    source_matching_run_id: int | None
    assessment_count: int
    issues: tuple[RecommendationReadinessIssue, ...]
    created: bool | None
    run_id: int | None
    run_fingerprint: str | None
    batch_fingerprint: str | None


# --------------------------------------------------------------------------
# arguments and the world the sync is allowed to assume
# --------------------------------------------------------------------------


def _positive_int(value: object) -> bool:
    """`True` is an `int` and equals `1`; a profile whose id is 1 is not `True`."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_profile_id(profile_id: object) -> None:
    """The one check made before any transaction, because it needs no database."""
    if not _positive_int(profile_id):
        raise RecommendationSyncError("profile_id must be a positive integer")


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    """An absent profile is a failed sync, never an INCOMPLETE state row.

    `input_assembly` reports a missing profile as `PROFILE_NOT_FOUND`, which is
    the right answer for a read. It cannot be the right answer for a *write*:
    `recommendation_profile_state.profile_id` references `profiles(id)`, so
    there is no row to write it on. Publishing that particular readiness is
    impossible by construction, and the failure is raised instead of being
    attempted and refused by a foreign key.
    """
    try:
        found = connection.execute(
            "SELECT 1 FROM profiles WHERE id=?", (profile_id,)
        ).fetchone()
    except sqlite3.Error as error:
        raise RecommendationSyncError(
            f"cannot read profile {profile_id}"
        ) from error
    if found is None:
        raise RecommendationSyncError(f"profile {profile_id} does not exist")


def _preflight_persistence_schema(connection: sqlite3.Connection) -> None:
    """Verify — never apply — the schema a persisted sync is about to write.

    Checked inside the sync's own transaction and before any business read, so a
    database that cannot receive the result is refused before assembly does the
    work, rather than after the computation is complete and only the write can
    fail.
    """
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (_PERSISTENCE_MIGRATION,),
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
                " ('recommendation_runs','recommendation_assessments',"
                "'recommendation_profile_state')"
            ).fetchall()
        }
    except sqlite3.Error as error:
        raise RecommendationSyncError(_SCHEMA_UNAVAILABLE) from error
    if migrated is None or tables != set(_PERSISTENCE_TABLES):
        raise RecommendationSyncError(_SCHEMA_UNAVAILABLE)


def _assemble(
    connection: sqlite3.Connection, profile_id: int
) -> RecommendationInputAssemblyResult:
    """Run the real assembly inside whatever snapshot this sync established.

    `assemble_recommendation_inputs` already owns its transaction contract: it
    borrows a transaction that exists and opens a deferred one only when none
    does. A sync always has one open by the time it calls this, so the freshness
    reads join the sync's snapshot instead of taking a second one beside it —
    which is exactly the property that makes the published result describe one
    world. That contract is used here, never modified.
    """
    try:
        assembly = assemble_recommendation_inputs(connection, profile_id)
    except sqlite3.Error as error:
        raise RecommendationSyncError(
            f"cannot assemble recommendation inputs for profile {profile_id}"
        ) from error
    _require_coherent_assembly(assembly, profile_id)
    return assembly


def _require_coherent_assembly(
    assembly: RecommendationInputAssemblyResult, profile_id: int
) -> None:
    """Refuse to publish a readiness verdict that contradicts itself.

    Not a second freshness opinion — none of the upstream rules are re-decided
    here — but a check that the *shape* of what came back can honestly be
    published. A READY carrying issues, an INCOMPLETE carrying records, a READY
    without a source Matching run: each would become a stored state asserting
    something the assembly did not actually establish.

    Such a result is a defect in this package, so it is raised rather than
    softened into INCOMPLETE. Publishing INCOMPLETE for it would record ordinary
    staleness where there was a bug, and the bug would never be seen again.
    """
    if assembly.profile_id != profile_id:
        raise RecommendationSyncError(
            "input assembly answered about another profile"
        )
    if assembly.assembly_version != RECOMMENDATION_INPUT_ASSEMBLY_VERSION:
        raise RecommendationSyncError(
            f"unexpected input assembly version: {assembly.assembly_version!r}"
        )
    if assembly.status is RecommendationReadinessStatus.READY:
        if assembly.issues:
            raise RecommendationSyncError("a READY assembly carries readiness issues")
        if not assembly.records:
            raise RecommendationSyncError("a READY assembly carries no input record")
        if not _positive_int(assembly.matching_run_id):
            raise RecommendationSyncError(
                "a READY assembly names no source matching run"
            )
        return
    if assembly.status is not RecommendationReadinessStatus.INCOMPLETE:
        raise RecommendationSyncError(
            f"unknown input assembly status: {assembly.status!r}"
        )
    if assembly.records:
        raise RecommendationSyncError("an INCOMPLETE assembly carries input records")
    if not assembly.issues:
        raise RecommendationSyncError("an INCOMPLETE assembly carries no issue")


def _source_matching_fingerprint(
    connection: sqlite3.Connection, profile_id: int, matching_run_id: int
) -> str:
    """The stored fingerprint of exactly the Matching run assembly used.

    Read, never recomputed, and never re-selected: the profile's *current*
    Matching run is not consulted here. Assembly already decided which run this
    recommendation is being produced over, and asking the state again could
    silently attach the batch to a different run than the one whose evidence it
    was built from.

    Together with `matching_run_id` this pair is part of the recommendation's
    operational identity, which is why the same batch content over a different
    Matching snapshot is a different run rather than the same one.
    """
    try:
        row = connection.execute(
            "SELECT profile_id,run_fingerprint FROM matching_runs WHERE id=?",
            (matching_run_id,),
        ).fetchone()
    except sqlite3.Error as error:
        raise RecommendationSyncError(
            f"cannot read source matching run {matching_run_id}"
        ) from error
    if row is None:
        raise RecommendationSyncError(
            f"source matching run {matching_run_id} does not exist"
        )
    if row[0] != profile_id:
        raise RecommendationSyncError(
            f"source matching run {matching_run_id} belongs to another profile"
        )
    fingerprint = row[1]
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise RecommendationSyncError(
            f"source matching run {matching_run_id} carries an unusable fingerprint"
        )
    return fingerprint


# --------------------------------------------------------------------------
# the two publications
# --------------------------------------------------------------------------


def _incomplete_result(
    assembly: RecommendationInputAssemblyResult, *, persisted: bool
) -> RecommendationSyncResult:
    """No run was computed, so nothing about a run is reported."""
    return RecommendationSyncResult(
        profile_id=assembly.profile_id,
        input_assembly_version=assembly.assembly_version,
        state=RecommendationReadinessStatus.INCOMPLETE,
        persisted=persisted,
        source_matching_run_id=assembly.matching_run_id,
        assessment_count=0,
        issues=assembly.issues,
        created=None,
        run_id=None,
        run_fingerprint=None,
        batch_fingerprint=None,
    )


def _build_batch(assembly: RecommendationInputAssemblyResult):
    """Hand the assembled inputs to the pure engine, in the order they arrived.

    No pre-sorting: the engine ranks, and a sort here would either be ignored or
    — worse — quietly become part of the ranking. No filtering, no weighting, no
    second look at any score. The batch that comes back is used exactly as it
    is, including the `batch_fingerprint` the engine sealed it with.
    """
    inputs = tuple(record.recommendation_input for record in assembly.records)
    try:
        return build_recommendation_batch(assembly.profile_id, inputs)
    except RecommendationInputError as error:
        # The engine refusing inputs the assembly called READY is a defect in
        # this package, not a profile that turned out not to be ready.
        raise RecommendationSyncError(
            f"the recommendation engine refused the assembled cohort for profile"
            f" {assembly.profile_id}"
        ) from error


def _publish_ready(
    connection: sqlite3.Connection, assembly: RecommendationInputAssemblyResult
) -> RecommendationSyncResult:
    """Compute and store the recommendation, inside the sync's own transaction."""
    batch = _build_batch(assembly)
    matching_run_id = int(assembly.matching_run_id)
    fingerprint = _source_matching_fingerprint(
        connection, assembly.profile_id, matching_run_id
    )
    try:
        prepared, batch_payload, run_fingerprint = _prepare(
            assembly.profile_id,
            batch,
            matching_run_id,
            fingerprint,
            assembly.assembly_version,
        )
        stored = _store_prepared_recommendation_batch_in_transaction(
            connection,
            assembly.profile_id,
            batch,
            source_matching_run_id=matching_run_id,
            source_matching_run_fingerprint=fingerprint,
            input_assembly_version=assembly.assembly_version,
            prepared=prepared,
            batch_payload=batch_payload,
            run_fingerprint=run_fingerprint,
        )
    except RecommendationPersistenceError as error:
        raise RecommendationSyncError(
            f"cannot store the recommendation for profile {assembly.profile_id}"
        ) from error
    except sqlite3.Error as error:
        raise RecommendationSyncError(
            f"cannot store the recommendation for profile {assembly.profile_id}"
        ) from error
    return RecommendationSyncResult(
        profile_id=assembly.profile_id,
        input_assembly_version=assembly.assembly_version,
        state=RecommendationReadinessStatus.READY,
        persisted=True,
        source_matching_run_id=matching_run_id,
        assessment_count=batch.assessment_count,
        issues=(),
        created=stored.created,
        run_id=stored.run_id,
        run_fingerprint=stored.run_fingerprint,
        batch_fingerprint=batch.batch_fingerprint,
    )


def _publish_incomplete(
    connection: sqlite3.Connection, assembly: RecommendationInputAssemblyResult
) -> RecommendationSyncResult:
    """Point the state at nothing, say why, and leave every stored run alone."""
    try:
        _set_recommendation_state_incomplete_in_transaction(
            connection,
            assembly.profile_id,
            issues=assembly.issues,
            input_assembly_version=assembly.assembly_version,
        )
    except RecommendationPersistenceError as error:
        raise RecommendationSyncError(
            f"cannot publish the INCOMPLETE state for profile"
            f" {assembly.profile_id}"
        ) from error
    except sqlite3.Error as error:
        raise RecommendationSyncError(
            f"cannot publish the INCOMPLETE state for profile"
            f" {assembly.profile_id}"
        ) from error
    return _incomplete_result(assembly, persisted=True)


def _synchronize(
    connection: sqlite3.Connection, profile_id: int, *, persist: bool
) -> RecommendationSyncResult:
    """The business flow, with a transaction already established around it."""
    _require_profile(connection, profile_id)
    if persist:
        _preflight_persistence_schema(connection)
    assembly = _assemble(connection, profile_id)
    if assembly.status is RecommendationReadinessStatus.INCOMPLETE:
        return (
            _publish_incomplete(connection, assembly)
            if persist
            else _incomplete_result(assembly, persisted=False)
        )
    if persist:
        return _publish_ready(connection, assembly)
    batch = _build_batch(assembly)
    return RecommendationSyncResult(
        profile_id=assembly.profile_id,
        input_assembly_version=assembly.assembly_version,
        state=RecommendationReadinessStatus.READY,
        persisted=False,
        source_matching_run_id=int(assembly.matching_run_id),
        assessment_count=batch.assessment_count,
        issues=(),
        created=None,
        run_id=None,
        run_fingerprint=None,
        batch_fingerprint=batch.batch_fingerprint,
    )


# --------------------------------------------------------------------------
# transaction ownership
# --------------------------------------------------------------------------


def _rollback_preserving_failure(connection: sqlite3.Connection) -> None:
    """Release a transaction while another failure is already on its way out.

    Only ever called with an exception in flight, and that is what makes the
    suppression correct: the failure the caller needs to see is the one that
    ended the synchronization, not a secondary error from the cleanup after it.
    Replacing the first with the second would hide the actual cause and report a
    rollback problem in its place.

    It is *not* the right cleanup for a call that succeeded — there the release
    is the last thing that can still go wrong, and swallowing it would let this
    module return a result while silently keeping a transaction it promised to
    end. `_release_snapshot` is that path.
    """
    if not connection.in_transaction:
        return
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _release_snapshot(connection: sqlite3.Connection, profile_id: int) -> None:
    """Release a snapshot this module owns, after the work itself succeeded.

    A dry run promises two things: it computes an answer, and it leaves the
    connection exactly as it found it. Both are part of the contract, so a
    release that fails is a failed dry run — not a successful one with a
    transaction quietly still open behind it. The result is therefore discarded
    and the SQLite failure is reported with its cause chained.

    Nothing is committed here, on any path. A dry run has nothing of its own to
    commit, and `ROLLBACK` also guarantees it can never commit an unrelated
    write that happened to be pending.
    """
    if not connection.in_transaction:
        # Nothing left to release, so there is nothing that can fail. Reached
        # only if something already ended the snapshot, which the read-only work
        # above never does.
        return
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as error:
        raise RecommendationSyncError(
            f"cannot release the recommendation dry-run snapshot for profile"
            f" {profile_id}"
        ) from error


def _persisted_sync(
    connection: sqlite3.Connection, profile_id: int
) -> RecommendationSyncResult:
    """One `BEGIN IMMEDIATE`, one `COMMIT`, and nothing in between that leaks."""
    if connection.in_transaction:
        raise RecommendationSyncError(
            "a persisted recommendation sync must own its transaction; commit or"
            " roll back the caller's transaction first"
        )
    try:
        # Before the first business read, and immediate rather than deferred: the
        # write lock is what stops another connection committing an upstream
        # change between the freshness decision and the publication built on it.
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as error:
        raise RecommendationSyncError(
            f"cannot open the synchronization transaction for profile {profile_id}"
        ) from error
    try:
        result = _synchronize(connection, profile_id, persist=True)
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        # Reached by a COMMIT that failed, and by nothing else: every business
        # read and write below is already translated where it happens.
        _rollback_preserving_failure(connection)
        raise RecommendationSyncError(
            f"cannot commit the synchronization of profile {profile_id}"
        ) from error
    except BaseException:
        # Including KeyboardInterrupt and SystemExit: the transaction must be
        # released whatever ends the call, but nothing here relabels an
        # unexpected exception as an ordinary synchronization outcome.
        _rollback_preserving_failure(connection)
        raise
    return result


def _dry_sync(
    connection: sqlite3.Connection, profile_id: int
) -> RecommendationSyncResult:
    """Read-only, and deferred: a dry run has nothing to reserve the database for.

    A transaction the caller already owns is borrowed and never finished —
    committing or rolling back someone else's work would end something this
    module cannot see. One opened here is always released, on every path, with
    `ROLLBACK`: there is nothing of this function's own to commit, and rolling
    back also guarantees it can never commit an unrelated pending write.

    How that release is reported depends on whether the work succeeded, and the
    two are not interchangeable. After a failure the release must not speak over
    the failure that caused it. After a success the release is the last thing
    that can still go wrong, and it is reported: returning a result while
    holding a transaction this function said it would end would leave the caller
    with a connection whose state contradicts what it was just told.
    """
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        try:
            connection.execute("BEGIN")
        except sqlite3.Error as error:
            raise RecommendationSyncError(
                f"cannot open a read snapshot for profile {profile_id}"
            ) from error
    try:
        result = _synchronize(connection, profile_id, persist=False)
    except BaseException:
        if owns_snapshot:
            _rollback_preserving_failure(connection)
        raise
    if owns_snapshot:
        _release_snapshot(connection, profile_id)
    return result


def sync_recommendations(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    persist: bool = True,
) -> RecommendationSyncResult:
    """Synchronize one profile's current recommendation, atomically and honestly.

    Returns READY with the run it stored, or INCOMPLETE with the readiness issues
    that stopped it. Both are ordinary outcomes and both are published; only a
    failure of the synchronization itself raises `RecommendationSyncError`.

    With `persist=True` this call owns one `BEGIN IMMEDIATE` covering the profile
    check, the schema check, the whole of input assembly, the computation, the
    source Matching read and the publication, and it refuses to run inside a
    transaction it did not open. With `persist=False` nothing is written: one
    deferred read snapshot, or the caller's own transaction borrowed and left
    exactly as it was found.

    Callers still serialize syncs for one profile. The transaction makes each
    synchronization atomic; it does not make two concurrent syncs of the same
    profile a sensible thing to ask for.
    """
    _require_profile_id(profile_id)
    if persist:
        return _persisted_sync(connection, profile_id)
    return _dry_sync(connection, profile_id)
