"""Transactional persistence for the Phase 3.5A constraint projection.

The style is the one `services/collector/qualification/persistence.py` already
uses: plain SQLite, no ORM, no second connection factory, no second migration
runner, and one explicit `BEGIN` / `COMMIT` / `ROLLBACK` around a whole
reconciliation. The idempotence key is the same pair that table uses —
`source_fingerprint` and `extractor_version` — so an operator who understands
qualification already understands this.

Two rules are enforced here rather than trusted to callers:

* **`opportunities` is never written.** There is no `INSERT`, `UPDATE` or
  `DELETE` against it anywhere below, and a test reads this file to keep it so.
  The projection is derived; the postings are the source, and a derivation that
  edits its own input is not a derivation;
* **a posting is replaced whole or not at all.** Writing one row means deleting
  the previous constraint row and everything hanging off it, then inserting the
  new scalar row, its locations, its education levels, its evidence and its
  conflicts, inside one transaction. A half-written extraction — scalars from
  the new reading beside evidence from the old — would be a projection that
  explains itself wrongly, which is worse than no projection at all.

`UNKNOWN` becomes `NULL` on the way in and `UNKNOWN` again on the way out. The
enums keep an `UNKNOWN` member so the extractor's output is total; the database
keeps `NULL` as its single spelling of "not asserted", so the `CHECK`
constraints can enumerate only affirmative values and a wrong string is refused
by SQLite instead of stored. The mapping is one function each way, right here,
and a round-trip test pins it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from services.collector.extractors.opportunity_constraints.models import (
    ConstraintConflict,
    ConstraintEvidence,
    ConstraintKind,
    ConventionRequirement,
    DurationRequirement,
    EducationLevel,
    EducationRequirement,
    EducationRequirementMode,
    ExperienceObligation,
    ExperienceRequirement,
    ExtractedConstraints,
    OpportunityType,
    SourceField,
    StartPrecision,
    StartRequirement,
    VisaSponsorship,
    WorkAuthorization,
    WorkMode,
)

__all__ = [
    "OpportunityConstraintRepositoryError",
    "delete_opportunity_constraints",
    "read_opportunity_constraints",
    "store_opportunity_constraints",
    "stored_signature",
]


class OpportunityConstraintRepositoryError(RuntimeError):
    """Raised when the constraint projection cannot be read or written safely."""


#: The tables one posting's projection occupies, children first so a manual
#: delete in this order never leaves an orphan even with foreign keys off.
_PROJECTION_TABLES = (
    "opportunity_constraint_conflicts",
    "opportunity_constraint_evidence",
    "opportunity_education_requirements",
    "opportunity_constraint_locations",
    "opportunity_skill_requirements",
    "opportunity_constraints",
)

_SCALAR_COLUMNS = (
    "opportunity_type", "experience_min_months", "experience_max_months",
    "experience_obligation", "duration_min_months", "duration_max_months",
    "start_year", "start_month", "start_day", "start_precision", "work_mode",
    "visa_sponsorship", "work_authorization", "convention_requirement",
    "extractor_version", "source_fingerprint", "extracted_at",
)


def _stored(value) -> str | None:
    """An enum member as the database spells it. `UNKNOWN` becomes `NULL`."""
    if value is None:
        return None
    text = value.value if hasattr(value, "value") else str(value)
    return None if text == "UNKNOWN" else text


def _canonical_json(values) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def stored_signature(
    connection: sqlite3.Connection, opportunity_id: int
) -> tuple[str, str] | None:
    """The `(fingerprint, version)` already stored for one posting, or None.

    This is the whole idempotence check. A caller that finds its own pair here
    has nothing to do: the posting has not changed and neither have the rules.
    """
    row = connection.execute(
        "SELECT source_fingerprint, extractor_version "
        "FROM opportunity_constraints WHERE opportunity_id = ?",
        (opportunity_id,),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def delete_opportunity_constraints(
    connection: sqlite3.Connection, opportunity_id: int
) -> None:
    """Remove one posting's whole projection. Writes nothing to `opportunities`."""
    for table in _PROJECTION_TABLES:
        connection.execute(
            f"DELETE FROM {table} WHERE opportunity_id = ?", (opportunity_id,)
        )


def _insert(
    connection: sqlite3.Connection,
    constraints: ExtractedConstraints,
    extracted_at: str,
) -> None:
    start = constraints.start
    experience = constraints.experience
    duration = constraints.duration
    connection.execute(
        f"INSERT INTO opportunity_constraints (opportunity_id, "
        f"{', '.join(_SCALAR_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in range(len(_SCALAR_COLUMNS) + 1))})",
        (
            constraints.opportunity_id,
            _stored(constraints.opportunity_type),
            experience.min_months,
            experience.max_months,
            _stored(experience.obligation),
            duration.min_months,
            duration.max_months,
            start.year,
            start.month,
            start.day,
            _stored(start.precision),
            _stored(constraints.work_mode),
            _stored(constraints.visa_sponsorship),
            _stored(constraints.work_authorization),
            _stored(constraints.convention),
            constraints.extractor_version,
            constraints.source_fingerprint,
            extracted_at,
        ),
    )
    for position, location in enumerate(constraints.locations):
        connection.execute(
            "INSERT INTO opportunity_constraint_locations "
            "(opportunity_id, position, location_text) VALUES (?, ?, ?)",
            (constraints.opportunity_id, position, location),
        )
    for position, requirement in enumerate(constraints.education):
        connection.execute(
            "INSERT INTO opportunity_education_requirements "
            "(opportunity_id, position, level, requirement_mode) VALUES (?, ?, ?, ?)",
            (
                constraints.opportunity_id,
                position,
                requirement.level.value,
                requirement.mode.value,
            ),
        )
    for position, evidence in enumerate(constraints.evidence):
        connection.execute(
            "INSERT INTO opportunity_constraint_evidence (opportunity_id, position, "
            "constraint_kind, source_field, rule_id, evidence_text, normalized_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                constraints.opportunity_id,
                position,
                evidence.kind.value,
                evidence.source_field.value,
                evidence.rule_id,
                evidence.text,
                evidence.normalized_value,
            ),
        )
    for conflict in constraints.conflicts:
        connection.execute(
            "INSERT INTO opportunity_constraint_conflicts (opportunity_id, "
            "constraint_kind, conflicting_values_json, rule_ids_json) "
            "VALUES (?, ?, ?, ?)",
            (
                constraints.opportunity_id,
                conflict.kind.value,
                _canonical_json(conflict.values),
                _canonical_json(conflict.rule_ids),
            ),
        )


def store_opportunity_constraints(
    connection: sqlite3.Connection,
    constraints: ExtractedConstraints,
    *,
    extracted_at: str | None = None,
    after_delete: Callable[[], None] | None = None,
) -> None:
    """Replace one posting's projection, whole, in one transaction.

    The caller supplies an already-extracted reading, so nothing is computed
    here and nothing is decided here. A failure at any point — a `CHECK` the
    reading violates, a foreign key, an interruption — rolls back everything,
    including the delete, so the previous projection survives intact rather
    than being replaced by a partial one.

    `after_delete` is a test seam invoked once the old rows are gone and before
    the new ones are written; production callers leave it unset.
    """
    if not isinstance(constraints, ExtractedConstraints):
        raise OpportunityConstraintRepositoryError(
            "constraints must be an ExtractedConstraints"
        )
    timestamp = extracted_at or datetime.now(UTC).isoformat(timespec="microseconds")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute(
            "SELECT 1 FROM opportunities WHERE id = ?", (constraints.opportunity_id,)
        ).fetchone() is None:
            raise OpportunityConstraintRepositoryError(
                f"opportunity {constraints.opportunity_id} does not exist"
            )
        delete_opportunity_constraints(connection, constraints.opportunity_id)
        if after_delete is not None:
            after_delete()
        _insert(connection, constraints, timestamp)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _member(registry, value: str | None, unknown):
    """The enum member a stored value names. `NULL` becomes `UNKNOWN`."""
    if value is None:
        return unknown
    return registry(value)


def read_opportunity_constraints(
    connection: sqlite3.Connection, opportunity_id: int
) -> ExtractedConstraints | None:
    """Rebuild one posting's stored projection, or None when it has none.

    Reading is not extracting: this returns what was stored, and storing is the
    only thing that changes it. `NULL` comes back as `UNKNOWN` for the three
    enums that have such a member, and as `None` for the fields whose absence
    has no name.
    """
    row = connection.execute(
        f"SELECT {', '.join(_SCALAR_COLUMNS)} FROM opportunity_constraints "
        "WHERE opportunity_id = ?",
        (opportunity_id,),
    ).fetchone()
    if row is None:
        return None
    values = dict(zip(_SCALAR_COLUMNS, row, strict=True))

    locations = tuple(
        str(item[0])
        for item in connection.execute(
            "SELECT location_text FROM opportunity_constraint_locations "
            "WHERE opportunity_id = ? ORDER BY position",
            (opportunity_id,),
        )
    )
    education = tuple(
        EducationRequirement(
            level=EducationLevel(str(item[0])),
            mode=EducationRequirementMode(str(item[1])),
        )
        for item in connection.execute(
            "SELECT level, requirement_mode FROM opportunity_education_requirements "
            "WHERE opportunity_id = ? ORDER BY position",
            (opportunity_id,),
        )
    )
    evidence = tuple(
        ConstraintEvidence(
            kind=ConstraintKind(str(item[0])),
            source_field=SourceField(str(item[1])),
            rule_id=str(item[2]),
            text=str(item[3]),
            normalized_value=None if item[4] is None else str(item[4]),
        )
        for item in connection.execute(
            "SELECT constraint_kind, source_field, rule_id, evidence_text, "
            "normalized_value FROM opportunity_constraint_evidence "
            "WHERE opportunity_id = ? ORDER BY position",
            (opportunity_id,),
        )
    )
    conflicts = tuple(
        ConstraintConflict(
            kind=ConstraintKind(str(item[0])),
            values=tuple(json.loads(item[1])),
            rule_ids=tuple(json.loads(item[2])),
        )
        for item in connection.execute(
            "SELECT constraint_kind, conflicting_values_json, rule_ids_json "
            "FROM opportunity_constraint_conflicts "
            "WHERE opportunity_id = ? ORDER BY constraint_kind",
            (opportunity_id,),
        )
    )
    precision = values["start_precision"]
    return ExtractedConstraints(
        opportunity_id=opportunity_id,
        source_fingerprint=str(values["source_fingerprint"]),
        extractor_version=str(values["extractor_version"]),
        opportunity_type=(
            None if values["opportunity_type"] is None
            else OpportunityType(str(values["opportunity_type"]))
        ),
        education=education,
        experience=ExperienceRequirement(
            min_months=values["experience_min_months"],
            max_months=values["experience_max_months"],
            obligation=_member(
                ExperienceObligation,
                values["experience_obligation"],
                ExperienceObligation.UNKNOWN,
            ),
        ),
        duration=DurationRequirement(
            min_months=values["duration_min_months"],
            max_months=values["duration_max_months"],
        ),
        start=StartRequirement(
            year=values["start_year"],
            month=values["start_month"],
            day=values["start_day"],
            precision=None if precision is None else StartPrecision(str(precision)),
        ),
        locations=locations,
        work_mode=(
            None if values["work_mode"] is None else WorkMode(str(values["work_mode"]))
        ),
        visa_sponsorship=_member(
            VisaSponsorship, values["visa_sponsorship"], VisaSponsorship.UNKNOWN
        ),
        work_authorization=_member(
            WorkAuthorization, values["work_authorization"], WorkAuthorization.UNKNOWN
        ),
        convention=_member(
            ConventionRequirement,
            values["convention_requirement"],
            ConventionRequirement.UNKNOWN,
        ),
        evidence=evidence,
        conflicts=conflicts,
    )
