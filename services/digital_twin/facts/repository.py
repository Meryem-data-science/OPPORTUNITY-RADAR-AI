"""Transactional persistence for profile facts and the evidence behind them.

The style is the one `services/digital_twin/repository.py` already uses: plain
SQLite, no ORM, no second connection factory, no second migration runner, and
one explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` around every write that
touches more than one row. A failure rolls the whole operation back, so a fact
never exists without the evidence that justified it.

Two rules are enforced here rather than trusted to callers:

* every mutation is scoped by `profile_id`, so a fact id from one profile can
  never move a fact belonging to another;
* the only reading advertised as verified, `list_verified_profile_facts`,
  filters on `status = 'ACCEPTED'` in SQL. A proposal, a rejection or a
  correction cannot leak into it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from services.digital_twin.facts.models import (
    ALLOWED_TRANSITIONS,
    FactProvenance,
    FactSourceType,
    FactStatus,
    ProfileFact,
    ProfileFactType,
    ProvenanceInput,
    decode_page_numbers,
)

_FACT_COLUMNS = (
    "id, profile_id, fact_type, value, normalized_value, status, "
    "replaced_by_fact_id, created_at, updated_at, decided_at"
)

_PROVENANCE_COLUMNS = (
    "id, fact_id, source_type, provenance_key, source_locator, cv_sha256, "
    "parser_version, extractor_version, candidate_fingerprint, rule_id, "
    "page_numbers, section_type, section_index, created_at"
)


class ProfileFactError(RuntimeError):
    """Raised when profile facts cannot be read or written safely."""


class ProfileFactNotFoundError(ProfileFactError):
    """Raised when the requested fact does not exist *for this profile*.

    A fact that exists under another profile is reported as missing on purpose:
    answering anything else would confirm the existence of another person's row.
    """


class InvalidFactTransitionError(ProfileFactError):
    """Raised when a decision would move a fact somewhere the cycle forbids."""


class AmbiguousFactEvidenceError(ProfileFactError):
    """Raised when one piece of evidence is attached to several facts of a profile.

    `UNIQUE (fact_id, provenance_key)` stops the same proof from being recorded
    twice *for one fact*; it cannot stop two facts of the same profile from
    each claiming it. That state is a business integrity failure, and the only
    honest answer to it is to say so: picking one of the two would silently
    decide which reading of the CV counts.
    """


class ConflictingFactEvidenceError(ProfileFactError):
    """Raised when known evidence already justifies another reading of a profile.

    A piece of evidence identifies one reading of one document, so neither the
    type it supports nor the fact it belongs to can change between two runs. If
    either appears to have changed, something upstream is wrong: a second fact
    must not be invented for it, and it must not be silently moved onto a fact
    a caller happens to name.
    """


@dataclass(frozen=True)
class FactCorrection:
    """The outcome of one correction: the old fact and the one that replaced it."""

    #: The previous fact, now `CORRECTED`. Its `value` is untouched.
    corrected: ProfileFact
    #: The new fact, `ACCEPTED` because a correction is an explicit human
    #: statement, carrying its own `USER_INPUT` provenance.
    replacement: ProfileFact


def _fact_from_row(row: tuple) -> ProfileFact:
    return ProfileFact(
        id=int(row[0]),
        profile_id=int(row[1]),
        fact_type=str(row[2]),
        value=str(row[3]),
        normalized_value=None if row[4] is None else str(row[4]),
        status=FactStatus(str(row[5])),
        replaced_by_fact_id=None if row[6] is None else int(row[6]),
        created_at=str(row[7]),
        updated_at=str(row[8]),
        decided_at=None if row[9] is None else str(row[9]),
    )


def _provenance_from_row(row: tuple) -> FactProvenance:
    return FactProvenance(
        id=int(row[0]),
        fact_id=int(row[1]),
        source_type=FactSourceType(str(row[2])),
        provenance_key=str(row[3]),
        source_locator=None if row[4] is None else str(row[4]),
        cv_sha256=None if row[5] is None else str(row[5]),
        parser_version=None if row[6] is None else str(row[6]),
        extractor_version=None if row[7] is None else str(row[7]),
        candidate_fingerprint=None if row[8] is None else str(row[8]),
        rule_id=None if row[9] is None else str(row[9]),
        page_numbers=decode_page_numbers(None if row[10] is None else str(row[10])),
        section_type=None if row[11] is None else str(row[11]),
        section_index=None if row[12] is None else int(row[12]),
        created_at=str(row[13]),
    )


def _fact_type_value(fact_type: ProfileFactType | str) -> str:
    if isinstance(fact_type, ProfileFactType):
        return fact_type.value
    text = str(fact_type)
    if text.strip() == "" or text != text.strip():
        raise ProfileFactError("fact_type is empty or untrimmed")
    return text


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    row = connection.execute(
        "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if row is None:
        raise ProfileFactNotFoundError("profile does not exist")


def _select_fact(
    connection: sqlite3.Connection, profile_id: int, fact_id: int
) -> ProfileFact | None:
    row = connection.execute(
        f"SELECT {_FACT_COLUMNS} FROM profile_facts WHERE id = ? AND profile_id = ?",
        (fact_id, profile_id),
    ).fetchone()
    return None if row is None else _fact_from_row(row)


def _require_fact(
    connection: sqlite3.Connection, profile_id: int, fact_id: int
) -> ProfileFact:
    fact = _select_fact(connection, profile_id, fact_id)
    if fact is None:
        raise ProfileFactNotFoundError(
            f"fact {fact_id} does not belong to profile {profile_id}"
        )
    return fact


def _check_transition(fact: ProfileFact, target: FactStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[fact.status]:
        raise InvalidFactTransitionError(
            f"a {fact.status.value} fact cannot become {target.value}"
        )


def _insert_provenance(
    connection: sqlite3.Connection, fact_id: int, provenance: ProvenanceInput
) -> FactProvenance:
    row = connection.execute(
        f"""INSERT INTO profile_fact_provenance (
                fact_id, source_type, provenance_key, source_locator, cv_sha256,
                parser_version, extractor_version, candidate_fingerprint,
                rule_id, page_numbers, section_type, section_index
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING {_PROVENANCE_COLUMNS}""",
        (
            fact_id,
            provenance.source_type.value,
            provenance.resolved_provenance_key(),
            provenance.source_locator,
            provenance.cv_sha256,
            provenance.parser_version,
            provenance.extractor_version,
            provenance.candidate_fingerprint,
            provenance.rule_id,
            provenance.encoded_page_numbers,
            provenance.section_type,
            provenance.section_index,
        ),
    ).fetchone()
    if row is None:
        raise ProfileFactError("provenance insert returned no row")
    return _provenance_from_row(row)


def _select_provenance(
    connection: sqlite3.Connection, fact_id: int, provenance_key: str
) -> FactProvenance | None:
    """The evidence this fact already holds under that key, or None."""
    row = connection.execute(
        f"SELECT {_PROVENANCE_COLUMNS} FROM profile_fact_provenance "
        "WHERE fact_id = ? AND provenance_key = ?",
        (fact_id, provenance_key),
    ).fetchone()
    return None if row is None else _provenance_from_row(row)


def _insert_fact(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    fact_type: str,
    value: str,
    normalized_value: str | None,
    status: FactStatus,
) -> ProfileFact:
    decided_at = "NULL" if status is FactStatus.PROPOSED else "CURRENT_TIMESTAMP"
    row = connection.execute(
        f"""INSERT INTO profile_facts (
                profile_id, fact_type, value, normalized_value, status, decided_at
            ) VALUES (?, ?, ?, ?, ?, {decided_at})
            RETURNING {_FACT_COLUMNS}""",
        (profile_id, fact_type, value, normalized_value, status.value),
    ).fetchone()
    if row is None:
        raise ProfileFactError("fact insert returned no row")
    return _fact_from_row(row)


def propose_profile_fact(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    fact_type: ProfileFactType | str,
    value: str,
    provenance: ProvenanceInput,
    normalized_value: str | None = None,
    after_fact: Callable[[], None] | None = None,
) -> ProfileFact:
    """Record one claim as `PROPOSED`, together with the evidence for it.

    The fact and its first provenance are written in one transaction: a fact
    with no evidence would be an assertion nobody could check, so a provenance
    the schema refuses leaves no fact behind either.

    `after_fact` is a test seam invoked once the fact row exists and before the
    provenance is written; production callers leave it unset.
    """
    if not isinstance(provenance, ProvenanceInput):
        raise ProfileFactError("provenance must be a ProvenanceInput")
    if value is None or str(value).strip() == "":
        raise ProfileFactError("value must not be empty")
    stored_type = _fact_type_value(fact_type)
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_profile(connection, profile_id)
        fact = _insert_fact(
            connection,
            profile_id=profile_id,
            fact_type=stored_type,
            value=value,
            normalized_value=normalized_value,
            status=FactStatus.PROPOSED,
        )
        if after_fact is not None:
            after_fact()
        _insert_provenance(connection, fact.id, provenance)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return fact


@dataclass(frozen=True)
class FactProposal:
    """The outcome of one idempotent import: the fact, and whether it is new."""

    #: The fact this evidence justifies, whatever its status has become since.
    fact: ProfileFact
    #: True only when this call created it. A second call with the same proof
    #: reports False and creates nothing.
    created: bool


def _select_facts_by_evidence(
    connection: sqlite3.Connection, profile_id: int, provenance_key: str
) -> tuple[ProfileFact, ...]:
    """Every fact of this profile that this exact proof already justifies.

    The join is what scopes the lookup: `provenance_key` is unique per fact,
    not per profile and not per database, so one key may legitimately appear
    under another profile and must not be visible from here.
    """
    rows = connection.execute(
        f"""SELECT {', '.join('f.' + column for column in _FACT_COLUMNS.split(', '))}
              FROM profile_facts AS f
              JOIN profile_fact_provenance AS p ON p.fact_id = f.id
             WHERE f.profile_id = ? AND p.provenance_key = ?
             ORDER BY f.id""",
        (profile_id, provenance_key),
    ).fetchall()
    return tuple(_fact_from_row(row) for row in rows)


def ensure_profile_fact_proposal(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    fact_type: ProfileFactType | str,
    value: str,
    provenance: ProvenanceInput,
    normalized_value: str | None = None,
    after_lookup: Callable[[], None] | None = None,
) -> FactProposal:
    """Record this claim as `PROPOSED`, unless this exact proof is already known.

    Identity here is the **evidence**, not the text: two facts are the same
    import when the same `provenance_key` resolves for both. That key is a pure
    function of the source, the document digest, the parser and extractor
    versions, the candidate fingerprint, the rule and the place in the
    document, so re-reading one CV proposes nothing new, while the same value
    read from a *different* CV is a different proof and a fact of its own. No
    consolidation across CV versions happens here, and none is implied.

    Idempotence deliberately ignores status. A fact whose proof is already
    recorded is returned as it stands — `PROPOSED`, `ACCEPTED`, `REJECTED` or
    `CORRECTED` — because a decision a human already took must survive the next
    import. A rejected reading that came back as a fresh proposal would ask the
    person to refuse it again, and an accepted one would be duplicated.

    The whole operation is one `BEGIN IMMEDIATE` transaction scoped by
    `profile_id`, so a run interrupted between two candidates leaves every
    candidate it did import complete, with its evidence, and re-importing
    resumes without duplicating any of them.

    `after_lookup` is a test seam invoked once the lookup is done and before
    anything is written; production callers leave it unset.
    """
    if not isinstance(provenance, ProvenanceInput):
        raise ProfileFactError("provenance must be a ProvenanceInput")
    if value is None or str(value).strip() == "":
        raise ProfileFactError("value must not be empty")
    stored_type = _fact_type_value(fact_type)
    provenance_key = provenance.resolved_provenance_key()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_profile(connection, profile_id)
        known = _select_facts_by_evidence(connection, profile_id, provenance_key)
        if after_lookup is not None:
            after_lookup()
        if len(known) > 1:
            raise AmbiguousFactEvidenceError(
                f"{len(known)} facts of profile {profile_id} share one proof"
            )
        if known:
            existing = known[0]
            if existing.fact_type != stored_type:
                raise ConflictingFactEvidenceError(
                    f"that proof already justifies a {existing.fact_type} fact, "
                    f"not a {stored_type} one"
                )
            connection.execute("COMMIT")
            return FactProposal(fact=existing, created=False)
        fact = _insert_fact(
            connection,
            profile_id=profile_id,
            fact_type=stored_type,
            value=value,
            normalized_value=normalized_value,
            status=FactStatus.PROPOSED,
        )
        _insert_provenance(connection, fact.id, provenance)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return FactProposal(fact=fact, created=True)


def add_profile_fact_provenance(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    fact_id: int,
    provenance: ProvenanceInput,
) -> FactProvenance:
    """Attach one more piece of evidence to an existing fact of this profile.

    Evidence accumulates; it never replaces what is already recorded, and the
    same proof cannot be attached twice — `UNIQUE (fact_id, provenance_key)`
    refuses it rather than letting a duplicate look like corroboration.
    """
    if not isinstance(provenance, ProvenanceInput):
        raise ProfileFactError("provenance must be a ProvenanceInput")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_fact(connection, profile_id, fact_id)
        recorded = _insert_provenance(connection, fact_id, provenance)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return recorded


@dataclass(frozen=True)
class FactProvenanceAttachment:
    """The outcome of one idempotent attachment: the evidence, and whether it is new."""

    #: The evidence this fact holds under that key, whether this call wrote it
    #: or found it already recorded.
    provenance: FactProvenance
    #: True only when this call inserted it. A second call with the same proof
    #: reports False and writes nothing.
    created: bool


def ensure_profile_fact_provenance(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    fact_id: int,
    provenance: ProvenanceInput,
    after_lookup: Callable[[], None] | None = None,
) -> FactProvenanceAttachment:
    """Attach this evidence to this fact, unless it is already attached to it.

    `add_profile_fact_provenance` appends and lets the schema refuse a
    duplicate; this is the no-op form of the same operation, for a caller that
    re-runs. Identity is the resolved `provenance_key`, exactly as it is for
    `ensure_profile_fact_proposal`: the same proof presented twice for the same
    fact resolves to the same key, so a second call reports `created=False` and
    writes nothing at all — no second row, no `updated_at` touched anywhere, no
    `IntegrityError` for the caller to interpret.

    Three states are refused rather than guessed at:

    * the fact does not belong to this profile — `ProfileFactNotFoundError`;
    * that proof already justifies several facts of this profile —
      `AmbiguousFactEvidenceError`, because the database is already
      inconsistent and picking one of them would decide which reading counts;
    * that proof already justifies a *different* fact of this profile —
      `ConflictingFactEvidenceError`. One piece of evidence identifies one
      reading, so moving it onto another fact would silently rewrite what the
      document was read as saying.

    The whole operation is one `BEGIN IMMEDIATE` transaction scoped by
    `profile_id`. Nothing about the fact itself is read as part of the
    decision: evidence accumulates on a `PROPOSED`, `ACCEPTED`, `CORRECTED` or
    `REJECTED` fact alike, and attaching it decides nothing and re-opens
    nothing — a status is moved by the decision calls of this module, never by
    a piece of evidence arriving.

    `after_lookup` is a test seam invoked once the lookup is done and before
    anything is written; production callers leave it unset.
    """
    if not isinstance(provenance, ProvenanceInput):
        raise ProfileFactError("provenance must be a ProvenanceInput")
    provenance_key = provenance.resolved_provenance_key()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_fact(connection, profile_id, fact_id)
        known = _select_facts_by_evidence(connection, profile_id, provenance_key)
        if after_lookup is not None:
            after_lookup()
        if len(known) > 1:
            raise AmbiguousFactEvidenceError(
                f"{len(known)} facts of profile {profile_id} share one proof"
            )
        if known and known[0].id != fact_id:
            raise ConflictingFactEvidenceError(
                f"that proof already justifies fact {known[0].id} of profile "
                f"{profile_id}, not fact {fact_id}"
            )
        recorded = _select_provenance(connection, fact_id, provenance_key)
        if recorded is not None:
            connection.execute("COMMIT")
            return FactProvenanceAttachment(provenance=recorded, created=False)
        attached = _insert_provenance(connection, fact_id, provenance)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return FactProvenanceAttachment(provenance=attached, created=True)


def get_profile_fact(
    connection: sqlite3.Connection, profile_id: int, fact_id: int
) -> ProfileFact | None:
    """Read one fact of this profile, or None. Never another profile's fact."""
    return _select_fact(connection, profile_id, fact_id)


def list_profile_facts(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    fact_type: ProfileFactType | str | None = None,
    statuses: tuple[FactStatus, ...] | None = None,
) -> tuple[ProfileFact, ...]:
    """Every fact of this profile, whatever its status, oldest id first.

    This is the review and audit reading: it deliberately shows proposals,
    rejections and corrections. It is **not** the verified reading — use
    `list_verified_profile_facts` for that.
    """
    clauses = ["profile_id = ?"]
    parameters: list[object] = [profile_id]
    if fact_type is not None:
        clauses.append("fact_type = ?")
        parameters.append(_fact_type_value(fact_type))
    if statuses is not None:
        if not statuses:
            return ()
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        parameters.extend(status.value for status in statuses)
    rows = connection.execute(
        f"SELECT {_FACT_COLUMNS} FROM profile_facts "
        f"WHERE {' AND '.join(clauses)} ORDER BY id",
        tuple(parameters),
    ).fetchall()
    return tuple(_fact_from_row(row) for row in rows)


def list_profile_facts_by_evidence(
    connection: sqlite3.Connection, profile_id: int, provenance_key: str
) -> tuple[ProfileFact, ...]:
    """Every fact of this profile that this exact proof justifies, oldest first.

    Normally none or one. Two or more is the inconsistency
    `AmbiguousFactEvidenceError` names, and this reading is how a caller sees
    it rather than being handed one of the two.
    """
    return _select_facts_by_evidence(connection, profile_id, provenance_key)


def list_profile_facts_by_cv_evidence(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    cv_sha256: str,
    parser_version: str,
    extractor_version: str,
) -> tuple[ProfileFact, ...]:
    """Every fact of this profile read from that document by those versions.

    The filter is the whole point: one document digest, one parser version and
    one extractor version identify a single *reading campaign* of a CV, and a
    later campaign over the same file is a different set of rows even where the
    values are identical. `source_type = 'CV'` is written into the statement
    rather than passed in, so no caller can widen it to `USER_INPUT` and read a
    person's own corrections back as if a document had produced them.

    Status takes no part: a fact read by that campaign belongs to it whether a
    human has since accepted, corrected or rejected it. `DISTINCT` is what
    keeps a fact carrying several proofs of one campaign from being listed
    twice.
    """
    columns = ", ".join("f." + column for column in _FACT_COLUMNS.split(", "))
    rows = connection.execute(
        f"""SELECT DISTINCT {columns}
              FROM profile_facts AS f
              JOIN profile_fact_provenance AS p ON p.fact_id = f.id
             WHERE f.profile_id = ?
               AND p.source_type = 'CV'
               AND p.cv_sha256 = ?
               AND p.parser_version = ?
               AND p.extractor_version = ?
             ORDER BY f.id""",
        (profile_id, cv_sha256, parser_version, extractor_version),
    ).fetchall()
    return tuple(_fact_from_row(row) for row in rows)


def list_verified_profile_facts(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    fact_type: ProfileFactType | str | None = None,
) -> tuple[ProfileFact, ...]:
    """Only the `ACCEPTED` facts of this profile, oldest id first.

    This is the one reading later phases are meant to build on. A `PROPOSED`
    fact was decided by nobody, a `REJECTED` one was refused, and a `CORRECTED`
    one has been superseded: none of the three is knowledge about the person,
    so none of them appears here. The filter is applied in SQL, not by the
    caller, so there is no way to forget it.
    """
    return list_profile_facts(
        connection, profile_id, fact_type=fact_type, statuses=(FactStatus.ACCEPTED,)
    )


def list_profile_fact_provenance(
    connection: sqlite3.Connection, profile_id: int, fact_id: int
) -> tuple[FactProvenance, ...]:
    """Every piece of evidence recorded for one fact of this profile."""
    _require_fact(connection, profile_id, fact_id)
    rows = connection.execute(
        f"SELECT {_PROVENANCE_COLUMNS} FROM profile_fact_provenance "
        "WHERE fact_id = ? ORDER BY id",
        (fact_id,),
    ).fetchall()
    return tuple(_provenance_from_row(row) for row in rows)


def _decide(
    connection: sqlite3.Connection,
    profile_id: int,
    fact_id: int,
    target: FactStatus,
) -> ProfileFact:
    connection.execute("BEGIN IMMEDIATE")
    try:
        fact = _require_fact(connection, profile_id, fact_id)
        _check_transition(fact, target)
        if fact.status is target:
            # Deciding again what was already decided changes nothing, and
            # must not restamp the moment the real decision was taken.
            connection.execute("COMMIT")
            return fact
        row = connection.execute(
            f"""UPDATE profile_facts
                   SET status = ?,
                       decided_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ? AND profile_id = ? AND status = ?
             RETURNING {_FACT_COLUMNS}""",
            (target.value, fact_id, profile_id, fact.status.value),
        ).fetchone()
        if row is None:
            raise ProfileFactError("the fact changed while it was being decided")
        decided = _fact_from_row(row)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return decided


def accept_profile_fact(
    connection: sqlite3.Connection, profile_id: int, fact_id: int
) -> ProfileFact:
    """Confirm a fact of this profile. Accepting an accepted fact is a no-op.

    Acceptance is the only thing that makes a fact verified, and it is the only
    thing this call does: no value is rewritten and no evidence is added.
    """
    return _decide(connection, profile_id, fact_id, FactStatus.ACCEPTED)


def reject_profile_fact(
    connection: sqlite3.Connection, profile_id: int, fact_id: int
) -> ProfileFact:
    """Refuse a fact of this profile. Rejecting a rejected fact is a no-op.

    The row is kept: a refusal is a decision worth reading later, and deleting
    it would lose the fact that the claim was ever made.
    """
    return _decide(connection, profile_id, fact_id, FactStatus.REJECTED)


def correct_profile_fact(
    connection: sqlite3.Connection,
    profile_id: int,
    fact_id: int,
    *,
    value: str,
    normalized_value: str | None = None,
    provenance: ProvenanceInput | None = None,
    after_replacement: Callable[[], None] | None = None,
) -> FactCorrection:
    """Replace a fact's value without ever overwriting it.

    One transaction creates the new fact, gives it its `USER_INPUT` provenance,
    then marks the old one `CORRECTED` and points it at its replacement. The
    old row keeps its own `value`, so a chain of corrections keeps every value
    that was ever proposed, and no `UPDATE ... SET value = ...` exists anywhere
    in this package.

    The replacement is `ACCEPTED`: a person typing the right value is an
    explicit human validation, not another proposal to review. Its `fact_type`
    is the corrected fact's own — correcting a value never silently reclassifies
    what the fact is about.

    `after_replacement` is a test seam invoked once the replacement and its
    provenance exist and before the old fact is marked; production callers
    leave it unset.
    """
    if value is None or str(value).strip() == "":
        raise ProfileFactError("a correction must carry a value")
    if provenance is None:
        provenance = ProvenanceInput(source_type=FactSourceType.USER_INPUT)
    if not isinstance(provenance, ProvenanceInput):
        raise ProfileFactError("provenance must be a ProvenanceInput")
    if provenance.source_type is not FactSourceType.USER_INPUT:
        raise ProfileFactError("a correction is USER_INPUT evidence")
    connection.execute("BEGIN IMMEDIATE")
    try:
        original = _require_fact(connection, profile_id, fact_id)
        _check_transition(original, FactStatus.CORRECTED)
        replacement = _insert_fact(
            connection,
            profile_id=profile_id,
            fact_type=original.fact_type,
            value=value,
            normalized_value=normalized_value,
            status=FactStatus.ACCEPTED,
        )
        _insert_provenance(connection, replacement.id, provenance)
        if after_replacement is not None:
            after_replacement()
        row = connection.execute(
            f"""UPDATE profile_facts
                   SET status = ?,
                       replaced_by_fact_id = ?,
                       decided_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ? AND profile_id = ? AND status = ?
             RETURNING {_FACT_COLUMNS}""",
            (
                FactStatus.CORRECTED.value,
                replacement.id,
                fact_id,
                profile_id,
                original.status.value,
            ),
        ).fetchone()
        if row is None:
            raise ProfileFactError("the fact changed while it was being corrected")
        corrected = _fact_from_row(row)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return FactCorrection(corrected=corrected, replacement=replacement)
