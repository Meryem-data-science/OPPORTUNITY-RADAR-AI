"""Transactional projection of verified EXPERIENCE and PROJECT facts.

The style is the one `services/digital_twin/skills/repository.py` already uses:
plain SQLite, no ORM, no second connection factory, no second migration runner,
and one explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` around the whole
operation. A failure anywhere rolls the projection back whole, so a profile is
never left holding half a reconciliation.

Four rules are enforced here rather than trusted to callers:

* the only facts read are
  `fact_type IN ('EXPERIENCE', 'PROJECT') AND status = 'ACCEPTED'`, filtered in
  SQL. A `PROPOSED`, `REJECTED` or `CORRECTED` fact, and an `ACCEPTED` fact of
  any other type, cannot reach a projected row;
* nothing in this module writes to `profile_facts` or to
  `profile_fact_provenance`. There is no `INSERT`, `UPDATE` or `DELETE`
  against either table anywhere below, and a test reads the SQL to keep it so;
* nothing in this module touches `skills`, `profile_skills` or
  `profile_skill_evidence`: the Phase 3.4A projection is a neighbour, not a
  dependency, and no skill is ever inferred from an experience or a project;
* every statement is scoped by `profile_id`, so one profile's rows can never be
  reconciled out of another profile's facts. `0009` makes that a database rule
  too, with a composite foreign key on `(fact_id, profile_id)`.

`synchronize_structured_profile_entries` is a reconciliation, not an append. It
computes what the verified facts say now, and makes the projection equal to
that: a row the current rules would write identically is left exactly as it is,
id and timestamp included; what is missing is created; what has stopped being
justified is deleted; and a row the rules now read differently — because the
fact was corrected, or because `STRUCTURED_PROFILE_VERSION` moved — is replaced
whole rather than patched. Running it twice on unchanged facts writes nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from services.digital_twin.structured_profile.models import (
    STRUCTURED_PROFILE_VERSION,
    ProfileExperience,
    ProfileProject,
    StructuredExperience,
    StructuredProfileNotFoundError,
    StructuredProject,
    StructuringRule,
)
from services.digital_twin.structured_profile.structurer import (
    structure_experience,
    structure_project,
)

#: The one reading this projection is built on. The fact type and `ACCEPTED`
#: are written into each statement rather than passed in, so no caller can
#: widen either of them.
_VERIFIED_EXPERIENCE_FACTS = (
    "SELECT id, value FROM profile_facts "
    "WHERE profile_id = ? AND fact_type = 'EXPERIENCE' AND status = 'ACCEPTED' "
    "ORDER BY id"
)
_VERIFIED_PROJECT_FACTS = (
    "SELECT id, value FROM profile_facts "
    "WHERE profile_id = ? AND fact_type = 'PROJECT' AND status = 'ACCEPTED' "
    "ORDER BY id"
)

#: The projected columns of each table, in the order the reconciliation writes
#: and compares them. `fact_id` is the identity; the rest is the reading.
_EXPERIENCE_FIELDS = (
    "role_text",
    "organization_text",
    "period_text",
    "description_text",
)
_PROJECT_FIELDS = ("title_text", "period_text", "description_text")


@dataclass(frozen=True)
class StructuredProfileSynchronization:
    """What one reconciliation did. Counters and a version, never a value.

    `changed` is the idempotence answer: it is False when the run found the
    projection already equal to what the verified facts produce, and therefore
    wrote nothing. A row the rules now read differently counts as one removal
    and one creation, because that is what happened to the rows.

    `structured_*` and `unparsed_*` are a tally of the rules that applied, not
    a quality score: an `UNPARSED_V1` row is a fact whose wording carries no
    structure the closed rules recognise, which is a property of the document,
    never of the person. `experience_rows` always equals
    `accepted_experience_facts`, and `project_rows` equals
    `accepted_project_facts`: every accepted fact is projected, exactly once.
    """

    profile_id: int
    accepted_experience_facts: int
    accepted_project_facts: int
    experience_rows: int
    project_rows: int
    structured_experiences: int
    unparsed_experiences: int
    structured_projects: int
    unparsed_projects: int
    created: int
    removed: int
    structurer_version: str

    @property
    def changed(self) -> bool:
        return bool(self.created or self.removed)

    def as_dict(self) -> dict[str, object]:
        """The privacy-safe summary: counters, a version and a flag."""
        return {
            "accepted_experience_facts": self.accepted_experience_facts,
            "accepted_project_facts": self.accepted_project_facts,
            "experience_rows": self.experience_rows,
            "project_rows": self.project_rows,
            "structured_experiences": self.structured_experiences,
            "unparsed_experiences": self.unparsed_experiences,
            "structured_projects": self.structured_projects,
            "unparsed_projects": self.unparsed_projects,
            "created": self.created,
            "removed": self.removed,
            "structurer_version": self.structurer_version,
            "changed": self.changed,
        }


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    row = connection.execute(
        "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if row is None:
        raise StructuredProfileNotFoundError("profile does not exist")


def _read_experiences(
    connection: sqlite3.Connection, profile_id: int
) -> dict[int, StructuredExperience]:
    """Structure every verified experience fact of this profile, oldest first.

    The value read is `profile_facts.value`, the wording the source used.
    `normalized_value` is deliberately not consulted: `0007` sets it only where
    a *technical* normal form is unambiguous, no rule sets it on an
    `EXPERIENCE` fact, and reading it would add a second input the structurer
    could disagree with.
    """
    return {
        int(fact_id): structure_experience(str(value))
        for fact_id, value in connection.execute(
            _VERIFIED_EXPERIENCE_FACTS, (profile_id,)
        ).fetchall()
    }


def _read_projects(
    connection: sqlite3.Connection, profile_id: int
) -> dict[int, StructuredProject]:
    return {
        int(fact_id): structure_project(str(value))
        for fact_id, value in connection.execute(
            _VERIFIED_PROJECT_FACTS, (profile_id,)
        ).fetchall()
    }


def _desired_row(
    structured: StructuredExperience | StructuredProject, fields: tuple[str, ...]
) -> tuple[object, ...]:
    """The tuple a row must hold to be the one the current rules would write."""
    return (
        *(getattr(structured, name) for name in fields),
        structured.structurer_version,
        structured.structuring_rule_id.value,
    )


def _existing_rows(
    connection: sqlite3.Connection,
    table: str,
    fields: tuple[str, ...],
    profile_id: int,
) -> dict[int, tuple[int, tuple[object, ...]]]:
    """Every projected row of this profile, keyed by the fact it points at."""
    columns = ", ".join(fields)
    rows = connection.execute(
        f"SELECT id, fact_id, {columns}, structurer_version, structuring_rule_id "
        f"FROM {table} WHERE profile_id = ? ORDER BY fact_id",
        (profile_id,),
    ).fetchall()
    return {int(row[1]): (int(row[0]), tuple(row[2:])) for row in rows}


def _reconcile(
    connection: sqlite3.Connection,
    table: str,
    fields: tuple[str, ...],
    profile_id: int,
    desired: dict[int, StructuredExperience | StructuredProject],
) -> tuple[int, int]:
    """Make one table equal to `desired` for this profile. Returns created, removed.

    A row whose stored tuple already equals what the rules produce is left
    untouched, so its id and its `created_at` survive the run — that is what
    makes a second synchronization write nothing at all. A row that differs is
    deleted and re-inserted rather than patched, so what is stored is always
    what one version of the rules produced, whole.
    """
    existing = _existing_rows(connection, table, fields, profile_id)
    created = 0
    removed = 0

    for fact_id, (row_id, _) in existing.items():
        if fact_id not in desired:
            connection.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            removed += 1

    placeholders = ", ".join("?" for _ in range(len(fields) + 4))
    columns = ", ".join(fields)
    for fact_id, structured in desired.items():
        wanted = _desired_row(structured, fields)
        known = existing.get(fact_id)
        if known is not None:
            row_id, stored = known
            if stored == wanted:
                continue
            connection.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            removed += 1
        connection.execute(
            f"INSERT INTO {table} (profile_id, fact_id, {columns}, "
            f"structurer_version, structuring_rule_id) VALUES ({placeholders})",
            (profile_id, fact_id, *wanted),
        )
        created += 1
    return created, removed


def _tally(
    desired: dict[int, StructuredExperience | StructuredProject]
) -> tuple[int, int]:
    """How many readings a closed rule named, and how many stayed unparsed."""
    unparsed = sum(
        1
        for structured in desired.values()
        if structured.structuring_rule_id is StructuringRule.UNPARSED_V1
    )
    return len(desired) - unparsed, unparsed


def _count(connection: sqlite3.Connection, table: str, profile_id: int) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE profile_id = ?", (profile_id,)
        ).fetchone()[0]
    )


def synchronize_structured_profile_entries(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    after_read: Callable[[], None] | None = None,
) -> StructuredProfileSynchronization:
    """Make this profile's structured rows equal to what its verified facts say.

    The whole reconciliation is one `BEGIN IMMEDIATE` transaction:

    1. the profile must exist — synchronizing creates no user and no profile;
    2. the `ACCEPTED` `EXPERIENCE` and `PROJECT` facts are read inside the
       transaction, so the set being projected cannot change underneath it;
    3. each of them is structured deterministically, and **every** one of them
       is projected: a fact the closed rules cannot read becomes an
       `UNPARSED_V1` row with `NULL` fragments, never a silently dropped fact;
    4. a row pointing at a fact that is no longer verified is deleted — a fact
       that became `CORRECTED` or `REJECTED` stops justifying anything, at the
       next synchronization and without any other action;
    5. a row the current rules would write differently is replaced whole;
    6. `profile_facts`, `profile_fact_provenance` and the Phase 3.4A skill
       tables are never written.

    `after_read` is a test seam invoked once the facts have been read and
    before anything is written; production callers leave it unset.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_profile(connection, profile_id)
        experiences = _read_experiences(connection, profile_id)
        projects = _read_projects(connection, profile_id)
        if after_read is not None:
            after_read()

        experience_created, experience_removed = _reconcile(
            connection, "profile_experiences", _EXPERIENCE_FIELDS, profile_id,
            experiences,
        )
        project_created, project_removed = _reconcile(
            connection, "profile_projects", _PROJECT_FIELDS, profile_id, projects
        )
        experience_rows = _count(connection, "profile_experiences", profile_id)
        project_rows = _count(connection, "profile_projects", profile_id)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise

    structured_experiences, unparsed_experiences = _tally(experiences)
    structured_projects, unparsed_projects = _tally(projects)
    return StructuredProfileSynchronization(
        profile_id=profile_id,
        accepted_experience_facts=len(experiences),
        accepted_project_facts=len(projects),
        experience_rows=experience_rows,
        project_rows=project_rows,
        structured_experiences=structured_experiences,
        unparsed_experiences=unparsed_experiences,
        structured_projects=structured_projects,
        unparsed_projects=unparsed_projects,
        created=experience_created + project_created,
        removed=experience_removed + project_removed,
        structurer_version=STRUCTURED_PROFILE_VERSION,
    )


def list_profile_experiences(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[ProfileExperience, ...]:
    """Every projected experience of this profile, oldest fact first.

    Reading is not projecting: this returns what is stored, and synchronizing
    is the only thing that changes it.
    """
    rows = connection.execute(
        """SELECT id, profile_id, fact_id, role_text, organization_text,
                  period_text, description_text, structurer_version,
                  structuring_rule_id, created_at
             FROM profile_experiences
            WHERE profile_id = ?
            ORDER BY fact_id""",
        (profile_id,),
    ).fetchall()
    return tuple(
        ProfileExperience(
            id=int(row[0]),
            profile_id=int(row[1]),
            fact_id=int(row[2]),
            role_text=row[3],
            organization_text=row[4],
            period_text=row[5],
            description_text=row[6],
            structurer_version=str(row[7]),
            structuring_rule_id=str(row[8]),
            created_at=str(row[9]),
        )
        for row in rows
    )


def list_profile_projects(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[ProfileProject, ...]:
    """Every projected project of this profile, oldest fact first."""
    rows = connection.execute(
        """SELECT id, profile_id, fact_id, title_text, period_text,
                  description_text, structurer_version, structuring_rule_id,
                  created_at
             FROM profile_projects
            WHERE profile_id = ?
            ORDER BY fact_id""",
        (profile_id,),
    ).fetchall()
    return tuple(
        ProfileProject(
            id=int(row[0]),
            profile_id=int(row[1]),
            fact_id=int(row[2]),
            title_text=row[3],
            period_text=row[4],
            description_text=row[5],
            structurer_version=str(row[6]),
            structuring_rule_id=str(row[7]),
            created_at=str(row[8]),
        )
        for row in rows
    )
