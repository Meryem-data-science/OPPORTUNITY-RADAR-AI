"""Transactional projection of the four explicit-input facts of Phase 3.4C.

The style is the one `services/digital_twin/structured_profile/repository.py`
already uses: plain SQLite, no ORM, no second connection factory, no second
migration runner, and one explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`
around the whole operation. A failure anywhere rolls the projection back whole,
so a profile is never left holding half a reconciliation.

Four fact types are projected, by one reconciliation and into four tables:

    AVAILABILITY      → profile_availability
    MOBILITY          → profile_mobility
    PREFERENCE        → profile_preferences
    CAREER_OBJECTIVE  → profile_career_objectives

All four are **singletons**: a person has one availability, one mobility, one
set of preferences and one set of career objectives, and restating any of them
corrects the previous statement rather than adding a second one. That is why
`profile_id` is the primary key of each table.

Six rules are enforced here rather than trusted to callers:

* the only facts read are `status = 'ACCEPTED'` facts of those four types, each
  filtered by its own literal SQL statement below. A `PROPOSED`, `REJECTED` or
  `CORRECTED` fact, and an `ACCEPTED` fact of any other type, cannot reach a
  projected row, and no caller can widen the filter because no caller supplies
  it;
* **and the fact must carry `USER_INPUT` provenance**, checked by an `EXISTS`
  in the same statement. Acceptance alone is not enough here, unlike in Phases
  3.4A and 3.4B: those project what a document said about a past that
  happened, and a human accepting the reading is the whole question. These four
  are things a person says about what they want, so a `CV`, a `GITHUB` or an
  `OTHER_ACCEPTED_EVIDENCE` proof is not weaker evidence for them — it is the
  wrong kind of evidence entirely, and a fact resting only on it is somebody's
  document talking, not them;
* **two accepted facts of one singleton type is refused, out loud.** Picking
  the most recent one would silently decide which of somebody's two statements
  counts, and the newest row is not the truest one — it is merely the newest.
  The synchronization raises and rolls back, and the projection stays as it
  was until a human resolves the conflict through the facts lifecycle;
* nothing in this module writes to `profile_facts` or to
  `profile_fact_provenance`. There is no `INSERT`, `UPDATE` or `DELETE`
  against either table anywhere below, and a test reads the SQL to keep it so;
* nothing in this module touches the Phase 3.4A skill tables or the Phase
  3.4B1/3.4B2 structured tables. A preference is never inferred from a skill,
  an experience, a project, a diploma or a language, and none of those rows is
  read, written or counted here;
* every statement is scoped by `profile_id`, so one profile's rows can never be
  reconciled out of another profile's facts. `0011` makes that a database rule
  too, with a composite foreign key on `(fact_id, profile_id)`.

`synchronize_profile_preferences` is a reconciliation, not an append. It
computes what the accepted facts say now, and makes the projection equal to
that: a row the current facts would write identically is left exactly as it is,
timestamp included; what is missing is created; what has stopped being
justified is deleted; and a row the facts now decode differently — because the
statement was corrected, or because `EXPLICIT_PROFILE_INPUT_VERSION` moved — is
replaced whole rather than patched. Running it twice on unchanged facts writes
nothing and reports `changed=false`.

**No projection is ever created for a profile that stated nothing.** Zero
accepted facts means zero rows, and that absence is the entire representation
of UNKNOWN. There is no seeded row, no default row and no `UNKNOWN` row, so
nothing downstream can mistake "never said" for "said no".

Nothing here is an eligibility decision, a match or a score. No opportunity is
read, no offer constraint is stored, no location is compared to an offer's
location and no date is compared to an offer's date: this is the profile side
of Phases 3.5/3.6, and those phases are not implemented.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from services.digital_twin.facts.models import ProfileFactType
from services.digital_twin.preferences.codec import (
    canonical_json,
    decode_availability,
    decode_career_objectives,
    decode_mobility,
    decode_preferences,
)
from services.digital_twin.preferences.models import (
    EXPLICIT_PROFILE_INPUT_VERSION,
    AvailabilityPreference,
    CareerObjectives,
    MobilityPreference,
    OpportunityPreferences,
)


class ProfilePreferenceError(RuntimeError):
    """Raised when the explicit-input projection cannot be read or written safely."""


class ProfilePreferenceNotFoundError(ProfilePreferenceError):
    """Raised when the profile the projection was asked about does not exist."""


class AmbiguousExplicitInputError(ProfilePreferenceError):
    """Raised when a singleton domain holds more than one accepted fact.

    A person has one availability, not two. Two accepted `AVAILABILITY` facts
    means the lifecycle was bypassed somewhere — a correction that did not go
    through `correct_profile_fact`, or a second statement recorded directly —
    and the database is already inconsistent. The only honest answer is to say
    so: choosing between them would decide, on nobody's behalf, which of two
    things somebody said is the one that counts.
    """


#: The one reading this projection is built on, written out once per type.
#:
#: Three conditions, all of them literals inside the statement rather than
#: parameters, so widening any of them means editing this file — which a test
#: then catches:
#:
#: * the fact belongs to this profile;
#: * it is `ACCEPTED` and of that exact type;
#: * **and at least one of its provenance rows is `USER_INPUT`.**
#:
#: The third is what makes the contract a database rule rather than a habit.
#: These four domains are things a person states about themselves, so a CV, a
#: GitHub page or a derivation from other accepted evidence is not evidence for
#: them — not weak evidence, not evidence to be confirmed later: the wrong kind
#: entirely. A document can say somebody worked remotely; it cannot say they
#: want to. Without this clause an `ACCEPTED` `PREFERENCE` fact carrying only
#: `CV` provenance would be projected as if the person had stated it, and the
#: projection would attribute to them a preference nobody expressed.
#:
#: `EXISTS` rather than a join, so a fact carrying several `USER_INPUT` proofs
#: — one per correction, say — is returned once and not once per proof. A join
#: would need `DISTINCT` to say the same thing, and a `DISTINCT` that is load-
#: bearing is easy to drop by accident.
_USER_INPUT_EVIDENCE = (
    "AND EXISTS ("
    "SELECT 1 FROM profile_fact_provenance AS p "
    "WHERE p.fact_id = f.id AND p.source_type = 'USER_INPUT'"
    ") "
)


def _accepted_user_input_facts(fact_type: str) -> str:
    """The statement reading one domain's stated facts, oldest id first."""
    return (
        "SELECT f.id, f.value FROM profile_facts AS f "
        "WHERE f.profile_id = ? "
        f"AND f.fact_type = '{fact_type}' "
        "AND f.status = 'ACCEPTED' "
        f"{_USER_INPUT_EVIDENCE}"
        "ORDER BY f.id"
    )


_ACCEPTED_AVAILABILITY_FACTS = _accepted_user_input_facts("AVAILABILITY")
_ACCEPTED_MOBILITY_FACTS = _accepted_user_input_facts("MOBILITY")
_ACCEPTED_PREFERENCE_FACTS = _accepted_user_input_facts("PREFERENCE")
_ACCEPTED_CAREER_OBJECTIVE_FACTS = _accepted_user_input_facts("CAREER_OBJECTIVE")


def _availability_columns(value: AvailabilityPreference) -> tuple[object, ...]:
    return (value.status.value, value.available_from)


def _mobility_columns(value: MobilityPreference) -> tuple[object, ...]:
    return (value.scope.value, canonical_json(list(value.locations)))


def _preference_columns(value: OpportunityPreferences) -> tuple[object, ...]:
    return (
        canonical_json([member.value for member in value.opportunity_types]),
        canonical_json([member.value for member in value.work_modes]),
        canonical_json(list(value.preferred_domains)),
        value.convention_status.value,
        value.visa_sponsorship_required.value,
        canonical_json(list(value.constraints)),
    )


def _career_objective_columns(value: CareerObjectives) -> tuple[object, ...]:
    return (canonical_json(list(value.objectives)),)


@dataclass(frozen=True)
class _Projection:
    """One singleton fact type, the table it lands in, and how it is decoded.

    It holds no policy of its own: the SQL is a literal written above, the
    fields are the table's own columns, `decode` is the strict codec function
    and `columns` turns one decoded statement into the tuple the row stores.
    """

    #: The name the counters and the status report use.
    domain: str
    fact_type: str
    facts_sql: str
    table: str
    #: The projected columns, in the order the reconciliation writes and
    #: compares them. `profile_id` is the identity; `fact_id` is the audit link.
    fields: tuple[str, ...]
    decode: Callable[[str], Any]
    columns: Callable[[Any], tuple[object, ...]]
    #: The fact-value keys the columns above correspond to, in the same order.
    #: Reading a row back means rebuilding that object and decoding it.
    json_shape: tuple[str, ...]
    #: Which of those keys are stored as a JSON array rather than as a scalar.
    json_arrays: frozenset[str]


#: The four projections, in the order the reconciliation walks them.
_PROJECTIONS: tuple[_Projection, ...] = (
    _Projection(
        domain="availability",
        fact_type="AVAILABILITY",
        facts_sql=_ACCEPTED_AVAILABILITY_FACTS,
        table="profile_availability",
        fields=("availability_status", "available_from"),
        decode=decode_availability,
        columns=_availability_columns,
        json_shape=("status", "available_from"),
        json_arrays=frozenset(),
    ),
    _Projection(
        domain="mobility",
        fact_type="MOBILITY",
        facts_sql=_ACCEPTED_MOBILITY_FACTS,
        table="profile_mobility",
        fields=("mobility_scope", "locations_json"),
        decode=decode_mobility,
        columns=_mobility_columns,
        json_shape=("scope", "locations"),
        json_arrays=frozenset({"locations"}),
    ),
    _Projection(
        domain="preferences",
        fact_type="PREFERENCE",
        facts_sql=_ACCEPTED_PREFERENCE_FACTS,
        table="profile_preferences",
        fields=(
            "opportunity_types_json",
            "work_modes_json",
            "preferred_domains_json",
            "convention_status",
            "visa_sponsorship_required",
            "constraints_json",
        ),
        decode=decode_preferences,
        columns=_preference_columns,
        json_shape=(
            "opportunity_types",
            "work_modes",
            "preferred_domains",
            "convention_status",
            "visa_sponsorship_required",
            "constraints",
        ),
        json_arrays=frozenset(
            {"opportunity_types", "work_modes", "preferred_domains", "constraints"}
        ),
    ),
    _Projection(
        domain="career_objectives",
        fact_type="CAREER_OBJECTIVE",
        facts_sql=_ACCEPTED_CAREER_OBJECTIVE_FACTS,
        table="profile_career_objectives",
        fields=("objectives_json",),
        decode=decode_career_objectives,
        columns=_career_objective_columns,
        json_shape=("objectives",),
        json_arrays=frozenset({"objectives"}),
    ),
)

#: The domains, in the order every summary lists them.
PREFERENCE_DOMAINS: tuple[str, ...] = tuple(
    projection.domain for projection in _PROJECTIONS
)


@dataclass(frozen=True)
class ProfilePreferenceRow:
    """One projected statement: which fact justifies it, and what it decodes to.

    `value` is the frozen dataclass from `models.py`, so a caller reads
    `row.value.status` rather than a JSON string. The row carries no
    provenance: `fact_id` is the way to it, through `profile_facts`.
    """

    profile_id: int
    fact_id: int
    #: `AvailabilityPreference`, `MobilityPreference`, `OpportunityPreferences`
    #: or `CareerObjectives`, depending on the table.
    value: Any
    input_version: str
    created_at: str


@dataclass(frozen=True)
class ProfilePreferenceSynchronization:
    """What one reconciliation did. Counters and a version, never a value.

    `changed` is the idempotence answer: it is False when the run found the
    projection already equal to what the accepted facts produce, and therefore
    wrote nothing. A row the facts now decode differently counts as one removal
    and one creation, because that is what happened to the row.

    Every counter here is a count. There is no objective, no constraint, no
    location and no domain anywhere in this object, because the summary is
    printed and logged and personal content is not. `known_*` is a boolean
    saying whether the person stated that domain at all — `False` is UNKNOWN,
    never "the person said no".
    """

    profile_id: int
    accepted_availability_facts: int
    accepted_mobility_facts: int
    accepted_preference_facts: int
    accepted_career_objective_facts: int
    availability_rows: int
    mobility_rows: int
    preferences_rows: int
    career_objectives_rows: int
    created: int
    removed: int
    input_version: str

    @property
    def changed(self) -> bool:
        return bool(self.created or self.removed)

    def as_dict(self) -> dict[str, object]:
        """The privacy-safe summary: counters, a version and a flag."""
        return {
            "accepted_availability_facts": self.accepted_availability_facts,
            "accepted_mobility_facts": self.accepted_mobility_facts,
            "accepted_preference_facts": self.accepted_preference_facts,
            "accepted_career_objective_facts": self.accepted_career_objective_facts,
            "availability_rows": self.availability_rows,
            "mobility_rows": self.mobility_rows,
            "preferences_rows": self.preferences_rows,
            "career_objectives_rows": self.career_objectives_rows,
            "created": self.created,
            "removed": self.removed,
            "input_version": self.input_version,
            "changed": self.changed,
        }


@dataclass(frozen=True)
class ProfilePreferenceStatus:
    """What is known about a profile, as counts and flags. Never as values.

    `KNOWN` means the person stated that domain and the projection holds it.
    `UNKNOWN` means no accepted fact exists for it — **not** that the answer is
    no. A count of zero on a list inside a stated preference means the person
    named none of that thing, which is different again, and both are printed as
    numbers so neither can be read as a value.
    """

    profile_id: int
    availability_known: bool
    mobility_known: bool
    preferences_known: bool
    career_objectives_known: bool
    mobility_location_count: int
    opportunity_type_count: int
    work_mode_count: int
    preferred_domain_count: int
    constraint_count: int
    career_objective_count: int

    @staticmethod
    def _flag(known: bool) -> str:
        return "KNOWN" if known else "UNKNOWN"

    def as_dict(self) -> dict[str, object]:
        """The privacy-safe report the CLI prints, in a fixed order."""
        return {
            "availability": self._flag(self.availability_known),
            "mobility": self._flag(self.mobility_known),
            "preferences": self._flag(self.preferences_known),
            "career_objectives": self._flag(self.career_objectives_known),
            "mobility_location_count": self.mobility_location_count,
            "opportunity_type_count": self.opportunity_type_count,
            "work_mode_count": self.work_mode_count,
            "preferred_domain_count": self.preferred_domain_count,
            "constraint_count": self.constraint_count,
            "career_objective_count": self.career_objective_count,
        }


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    row = connection.execute(
        "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if row is None:
        raise ProfilePreferenceNotFoundError("profile does not exist")


def _projection_for(fact_type: str) -> _Projection:
    """The projection describing one domain, by its fact type."""
    for projection in _PROJECTIONS:
        if projection.fact_type == fact_type:
            return projection
    raise ProfilePreferenceError(f"{fact_type} is not a Phase 3.4C domain")


def find_stated_fact(
    connection: sqlite3.Connection, profile_id: int, fact_type: ProfileFactType | str
) -> tuple[int, str] | None:
    """The one fact this person **stated** for that domain, as `(id, value)`.

    "Stated" is the whole definition, and it lives here so that there is only
    one of it: `ACCEPTED`, of that type, belonging to this profile, and
    carrying at least one `USER_INPUT` provenance row. The projection and the
    write side both ask this question through this function, so the rows can
    never describe a different set of facts than the one `set_*` is deciding
    against.

    `None` when the person stated nothing — which is UNKNOWN, and which an
    `ACCEPTED` fact of the same type evidenced only by a CV, a GitHub page or
    other accepted evidence does **not** change: that is a document's claim,
    not theirs.

    Two stated facts is refused rather than resolved — see
    `AmbiguousExplicitInputError`.
    """
    stored_type = (
        fact_type.value if isinstance(fact_type, ProfileFactType) else str(fact_type)
    )
    projection = _projection_for(stored_type)
    rows = connection.execute(projection.facts_sql, (profile_id,)).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousExplicitInputError(
            f"profile {profile_id} holds {len(rows)} accepted "
            f"{stored_type} facts stated by the person; a person states one"
        )
    return int(rows[0][0]), str(rows[0][1])


def _read(
    connection: sqlite3.Connection, projection: _Projection, profile_id: int
) -> tuple[int, Any] | None:
    """The one stated fact of this type for this profile, decoded, or None.

    `normalized_value` is deliberately not consulted: the canonical form *is*
    `value` for these four types, and a second input the decoder could disagree
    with would be one too many.
    """
    stated = find_stated_fact(connection, profile_id, projection.fact_type)
    if stated is None:
        return None
    fact_id, value = stated
    return fact_id, projection.decode(value)


def _existing_row(
    connection: sqlite3.Connection, projection: _Projection, profile_id: int
) -> tuple[int, tuple[object, ...]] | None:
    """The projected row of this profile, as `(fact_id, stored tuple)`, or None."""
    columns = ", ".join(projection.fields)
    row = connection.execute(
        f"SELECT fact_id, {columns}, input_version FROM {projection.table} "
        "WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()
    return None if row is None else (int(row[0]), tuple(row[1:]))


def _reconcile(
    connection: sqlite3.Connection,
    projection: _Projection,
    profile_id: int,
    desired: tuple[int, Any] | None,
) -> tuple[int, int]:
    """Make one table equal to `desired` for this profile. Returns created, removed.

    A row whose stored tuple already equals what the facts decode to, and which
    points at the same fact, is left untouched, so its `created_at` survives the
    run — that is what makes a second synchronization write nothing at all. A
    row that differs is deleted and re-inserted rather than patched, so what is
    stored is always one whole decoding of one whole statement.
    """
    table = projection.table
    existing = _existing_row(connection, projection, profile_id)

    if desired is None:
        if existing is None:
            return 0, 0
        connection.execute(f"DELETE FROM {table} WHERE profile_id = ?", (profile_id,))
        return 0, 1

    fact_id, value = desired
    wanted = (*projection.columns(value), EXPLICIT_PROFILE_INPUT_VERSION)
    removed = 0
    if existing is not None:
        known_fact_id, stored = existing
        if known_fact_id == fact_id and stored == wanted:
            return 0, 0
        connection.execute(f"DELETE FROM {table} WHERE profile_id = ?", (profile_id,))
        removed = 1
    columns = ", ".join(projection.fields)
    placeholders = ", ".join("?" for _ in range(len(projection.fields) + 3))
    connection.execute(
        f"INSERT INTO {table} (profile_id, fact_id, {columns}, input_version) "
        f"VALUES ({placeholders})",
        (profile_id, fact_id, *wanted),
    )
    return 1, removed


def _count(connection: sqlite3.Connection, table: str, profile_id: int) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE profile_id = ?", (profile_id,)
        ).fetchone()[0]
    )


def synchronize_profile_preferences(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    after_read: Callable[[], None] | None = None,
) -> ProfilePreferenceSynchronization:
    """Make this profile's four projections equal to what its accepted facts say.

    The whole reconciliation is one `BEGIN IMMEDIATE` transaction:

    1. the profile must exist — synchronizing creates no user and no profile;
    2. the `ACCEPTED` `AVAILABILITY`, `MOBILITY`, `PREFERENCE` and
       `CAREER_OBJECTIVE` facts are read inside the transaction, so the set
       being projected cannot change underneath it;
    3. each of them is decoded strictly. A value this package did not write is
       refused, and the whole run is rolled back rather than partly applied;
    4. a domain holding two accepted facts is refused for the same reason, and
       the projection of the *other three* domains is rolled back with it: a
       synchronization either describes the profile as a whole or does nothing;
    5. a domain holding none has no row — its row is deleted if one survives
       from an earlier statement, and none is created. That absence is UNKNOWN;
    6. a row the current facts decode differently is replaced whole;
    7. `profile_facts`, `profile_fact_provenance`, the Phase 3.4A skill tables
       and the Phase 3.4B structured tables are never written.

    `after_read` is a test seam invoked once the facts have been read and
    before anything is written; production callers leave it unset.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_profile(connection, profile_id)
        desired = {
            projection.domain: _read(connection, projection, profile_id)
            for projection in _PROJECTIONS
        }
        if after_read is not None:
            after_read()

        created = 0
        removed = 0
        for projection in _PROJECTIONS:
            added, dropped = _reconcile(
                connection, projection, profile_id, desired[projection.domain]
            )
            created += added
            removed += dropped
        rows = {
            projection.domain: _count(connection, projection.table, profile_id)
            for projection in _PROJECTIONS
        }
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise

    return ProfilePreferenceSynchronization(
        profile_id=profile_id,
        accepted_availability_facts=int(desired["availability"] is not None),
        accepted_mobility_facts=int(desired["mobility"] is not None),
        accepted_preference_facts=int(desired["preferences"] is not None),
        accepted_career_objective_facts=int(desired["career_objectives"] is not None),
        availability_rows=rows["availability"],
        mobility_rows=rows["mobility"],
        preferences_rows=rows["preferences"],
        career_objectives_rows=rows["career_objectives"],
        created=created,
        removed=removed,
        input_version=EXPLICIT_PROFILE_INPUT_VERSION,
    )


def _read_projection(
    connection: sqlite3.Connection, projection: _Projection, profile_id: int
) -> ProfilePreferenceRow | None:
    """One stored row, decoded back into its frozen dataclass, or None.

    Reading is not projecting: this returns what is stored, and synchronizing
    is the only thing that changes it. The stored JSON is decoded through the
    same strict codec, so a row that stopped being canonical is refused here
    too rather than read approximately.
    """
    columns = ", ".join(projection.fields)
    row = connection.execute(
        f"SELECT profile_id, fact_id, {columns}, input_version, created_at "
        f"FROM {projection.table} WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()
    if row is None:
        return None
    return ProfilePreferenceRow(
        profile_id=int(row[0]),
        fact_id=int(row[1]),
        value=_stored_value(projection, row[2 : 2 + len(projection.fields)]),
        input_version=str(row[2 + len(projection.fields)]),
        created_at=str(row[3 + len(projection.fields)]),
    )


def _stored_value(projection: _Projection, stored: tuple) -> Any:
    """Rebuild the domain object a row's columns hold, through the codec.

    The columns hold the canonical JSON the codec wrote, so the value is
    reassembled by handing that same JSON back to the same strict decoder
    rather than by a second reading rule that could drift away from it. A row
    that stopped being canonical is therefore refused here too.
    """
    payload = dict(zip(projection.json_shape, stored))
    for key in projection.json_arrays:
        payload[key] = json.loads(payload[key])
    return projection.decode(canonical_json(payload))


def get_profile_availability(
    connection: sqlite3.Connection, profile_id: int
) -> ProfilePreferenceRow | None:
    """The projected availability of this profile, or None when UNKNOWN."""
    return _read_projection(connection, _PROJECTIONS[0], profile_id)


def get_profile_mobility(
    connection: sqlite3.Connection, profile_id: int
) -> ProfilePreferenceRow | None:
    """The projected mobility of this profile, or None when UNKNOWN."""
    return _read_projection(connection, _PROJECTIONS[1], profile_id)


def get_profile_preferences(
    connection: sqlite3.Connection, profile_id: int
) -> ProfilePreferenceRow | None:
    """The projected preferences of this profile, or None when UNKNOWN."""
    return _read_projection(connection, _PROJECTIONS[2], profile_id)


def get_profile_career_objectives(
    connection: sqlite3.Connection, profile_id: int
) -> ProfilePreferenceRow | None:
    """The projected career objectives of this profile, or None when UNKNOWN."""
    return _read_projection(connection, _PROJECTIONS[3], profile_id)


def summarize_profile_preferences(
    connection: sqlite3.Connection, profile_id: int
) -> ProfilePreferenceStatus:
    """What is known about this profile, as flags and counts. Never as values.

    Absence answers `UNKNOWN`, and the counts of an absent domain are zero
    because there is nothing to count — not because the person named zero
    things. The two are told apart by the flag, which is why the flag is
    printed next to them.
    """
    _require_profile(connection, profile_id)
    availability = get_profile_availability(connection, profile_id)
    mobility = get_profile_mobility(connection, profile_id)
    preferences = get_profile_preferences(connection, profile_id)
    objectives = get_profile_career_objectives(connection, profile_id)
    return ProfilePreferenceStatus(
        profile_id=profile_id,
        availability_known=availability is not None,
        mobility_known=mobility is not None,
        preferences_known=preferences is not None,
        career_objectives_known=objectives is not None,
        mobility_location_count=0 if mobility is None else len(mobility.value.locations),
        opportunity_type_count=(
            0 if preferences is None else len(preferences.value.opportunity_types)
        ),
        work_mode_count=0 if preferences is None else len(preferences.value.work_modes),
        preferred_domain_count=(
            0 if preferences is None else len(preferences.value.preferred_domains)
        ),
        constraint_count=(
            0 if preferences is None else len(preferences.value.constraints)
        ),
        career_objective_count=(
            0 if objectives is None else len(objectives.value.objectives)
        ),
    )
