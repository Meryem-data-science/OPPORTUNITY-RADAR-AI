"""Transactional projection of the verified facts Phase 3.4B knows how to read.

The style is the one `services/digital_twin/skills/repository.py` already uses:
plain SQLite, no ORM, no second connection factory, no second migration runner,
and one explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` around the whole
operation. A failure anywhere rolls the projection back whole, so a profile is
never left holding half a reconciliation.

Five fact types are projected, by one reconciliation and into five tables:

    EXPERIENCE     → profile_experiences       (Phase 3.4B1)
    PROJECT        → profile_projects          (Phase 3.4B1)
    EDUCATION      → profile_educations        (Phase 3.4B2)
    CERTIFICATION  → profile_certifications    (Phase 3.4B2)
    LANGUAGE       → profile_languages         (Phase 3.4B2)

Adding the last three changed nothing about the first two: they are read by the
same rules, over the same temporal grammar, and produce the same rows, so a
synchronization run after this slice leaves every existing experience and
project row exactly where it was, id and timestamp included.

Four rules are enforced here rather than trusted to callers:

* the only facts read are `status = 'ACCEPTED'` facts of those five types, each
  filtered by its own literal SQL statement below. A `PROPOSED`, `REJECTED` or
  `CORRECTED` fact, and an `ACCEPTED` fact of any other type, cannot reach a
  projected row, and no caller can widen the filter because no caller supplies
  it;
* nothing in this module writes to `profile_facts` or to
  `profile_fact_provenance`. There is no `INSERT`, `UPDATE` or `DELETE`
  against either table anywhere below, and a test reads the SQL to keep it so;
* nothing in this module touches `skills`, `profile_skills` or
  `profile_skill_evidence`: the Phase 3.4A projection is a neighbour, not a
  dependency, and no skill is ever inferred from an experience, a project, a
  diploma, a certification or a language;
* every statement is scoped by `profile_id`, so one profile's rows can never be
  reconciled out of another profile's facts. `0009` and `0010` make that a
  database rule too, with a composite foreign key on `(fact_id, profile_id)`.

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
    ProfileCertification,
    ProfileEducation,
    ProfileExperience,
    ProfileLanguage,
    ProfileProject,
    StructuredEntry,
    StructuredProfileNotFoundError,
    StructuringRule,
)
from services.digital_twin.structured_profile.structurer import (
    structure_certification,
    structure_education,
    structure_experience,
    structure_language,
    structure_project,
)

#: The one reading this projection is built on, written out once per type. The
#: fact type and `ACCEPTED` are literals inside each statement rather than
#: parameters, so widening either of them means editing this file — which a
#: test then catches.
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
_VERIFIED_EDUCATION_FACTS = (
    "SELECT id, value FROM profile_facts "
    "WHERE profile_id = ? AND fact_type = 'EDUCATION' AND status = 'ACCEPTED' "
    "ORDER BY id"
)
_VERIFIED_CERTIFICATION_FACTS = (
    "SELECT id, value FROM profile_facts "
    "WHERE profile_id = ? AND fact_type = 'CERTIFICATION' AND status = 'ACCEPTED' "
    "ORDER BY id"
)
_VERIFIED_LANGUAGE_FACTS = (
    "SELECT id, value FROM profile_facts "
    "WHERE profile_id = ? AND fact_type = 'LANGUAGE' AND status = 'ACCEPTED' "
    "ORDER BY id"
)


@dataclass(frozen=True)
class _Projection:
    """One fact type, the table it lands in, and how it is read.

    It holds no policy of its own: the SQL is a literal written above, the
    fields are the table's own columns, and `structure` is the pure function
    that decides the reading. Adding a type is adding one of these.
    """

    #: The name the counters use: `accepted_<counted>_facts`, `<counted>_rows`.
    counted: str
    #: The plural the rule tallies use: `structured_<tallied>`.
    tallied: str
    facts_sql: str
    table: str
    #: The projected columns, in the order the reconciliation writes and
    #: compares them. `fact_id` is the identity; these are the reading.
    fields: tuple[str, ...]
    structure: Callable[[str], StructuredEntry]
    #: The rule this type falls back to. A row carrying it is a fact whose
    #: wording no closed rule recognised — a property of the document, never of
    #: the person.
    unparsed: StructuringRule


#: The five projections, in the order the reconciliation walks them. The first
#: two are Phase 3.4B1 and are unchanged by Phase 3.4B2.
_PROJECTIONS: tuple[_Projection, ...] = (
    _Projection(
        counted="experience",
        tallied="experiences",
        facts_sql=_VERIFIED_EXPERIENCE_FACTS,
        table="profile_experiences",
        fields=("role_text", "organization_text", "period_text", "description_text"),
        structure=structure_experience,
        unparsed=StructuringRule.UNPARSED_V1,
    ),
    _Projection(
        counted="project",
        tallied="projects",
        facts_sql=_VERIFIED_PROJECT_FACTS,
        table="profile_projects",
        fields=("title_text", "period_text", "description_text"),
        structure=structure_project,
        unparsed=StructuringRule.UNPARSED_V1,
    ),
    _Projection(
        counted="education",
        tallied="educations",
        facts_sql=_VERIFIED_EDUCATION_FACTS,
        table="profile_educations",
        fields=(
            "institution_text",
            "program_text",
            "period_text",
            "description_text",
        ),
        structure=structure_education,
        unparsed=StructuringRule.EDUCATION_UNPARSED_V1,
    ),
    _Projection(
        counted="certification",
        tallied="certifications",
        facts_sql=_VERIFIED_CERTIFICATION_FACTS,
        table="profile_certifications",
        fields=(
            "certification_text",
            "issuer_text",
            "period_text",
            "description_text",
        ),
        structure=structure_certification,
        unparsed=StructuringRule.CERTIFICATION_UNPARSED_V1,
    ),
    _Projection(
        counted="language",
        tallied="languages",
        facts_sql=_VERIFIED_LANGUAGE_FACTS,
        table="profile_languages",
        fields=("language_text", "proficiency_text"),
        structure=structure_language,
        unparsed=StructuringRule.LANGUAGE_UNPARSED_V1,
    ),
)


@dataclass(frozen=True)
class StructuredProfileSynchronization:
    """What one reconciliation did. Counters and a version, never a value.

    `changed` is the idempotence answer: it is False when the run found the
    projection already equal to what the verified facts produce, and therefore
    wrote nothing. A row the rules now read differently counts as one removal
    and one creation, because that is what happened to the rows.

    `structured` and `unparsed` are a tally of the rules that applied, not a
    quality score: an unparsed row is a fact whose wording carries no structure
    the closed rules recognise, which is a property of the document, never of
    the person. A profile whose whole education section is
    `EDUCATION_UNPARSED_V1` has exactly the same diplomas as one whose section
    happened to be typed with pipes. For every type, `<type>_rows` equals
    `accepted_<type>_facts`: every accepted fact is projected, exactly once.

    Nothing in here is an institution, a diploma, a certification, a language,
    a level or any other fragment of a fact. The summary is printed and logged,
    and personal content is not.
    """

    profile_id: int
    accepted_experience_facts: int
    accepted_project_facts: int
    accepted_education_facts: int
    accepted_certification_facts: int
    accepted_language_facts: int
    experience_rows: int
    project_rows: int
    education_rows: int
    certification_rows: int
    language_rows: int
    structured_experiences: int
    unparsed_experiences: int
    structured_projects: int
    unparsed_projects: int
    structured_educations: int
    unparsed_educations: int
    structured_certifications: int
    unparsed_certifications: int
    structured_languages: int
    unparsed_languages: int
    created: int
    removed: int
    structurer_version: str

    @property
    def changed(self) -> bool:
        return bool(self.created or self.removed)

    def as_dict(self) -> dict[str, object]:
        """The privacy-safe summary: counters, a version and a flag."""
        summary: dict[str, object] = {}
        for projection in _PROJECTIONS:
            summary[f"accepted_{projection.counted}_facts"] = getattr(
                self, f"accepted_{projection.counted}_facts"
            )
        for projection in _PROJECTIONS:
            summary[f"{projection.counted}_rows"] = getattr(
                self, f"{projection.counted}_rows"
            )
        for projection in _PROJECTIONS:
            summary[f"structured_{projection.tallied}"] = getattr(
                self, f"structured_{projection.tallied}"
            )
            summary[f"unparsed_{projection.tallied}"] = getattr(
                self, f"unparsed_{projection.tallied}"
            )
        summary["created"] = self.created
        summary["removed"] = self.removed
        summary["structurer_version"] = self.structurer_version
        summary["changed"] = self.changed
        return summary


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    row = connection.execute(
        "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if row is None:
        raise StructuredProfileNotFoundError("profile does not exist")


def _read(
    connection: sqlite3.Connection, projection: _Projection, profile_id: int
) -> dict[int, StructuredEntry]:
    """Structure every verified fact of one type for this profile, oldest first.

    The value read is `profile_facts.value`, the wording the source used.
    `normalized_value` is deliberately not consulted: `0007` sets it only where
    a *technical* normal form is unambiguous, no rule sets it on any of the
    five types projected here, and reading it would add a second input the
    structurer could disagree with.
    """
    return {
        int(fact_id): projection.structure(str(value))
        for fact_id, value in connection.execute(
            projection.facts_sql, (profile_id,)
        ).fetchall()
    }


def _desired_row(
    structured: StructuredEntry, fields: tuple[str, ...]
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
    projection: _Projection,
    profile_id: int,
    desired: dict[int, StructuredEntry],
) -> tuple[int, int]:
    """Make one table equal to `desired` for this profile. Returns created, removed.

    A row whose stored tuple already equals what the rules produce is left
    untouched, so its id and its `created_at` survive the run — that is what
    makes a second synchronization write nothing at all, and what makes this
    slice leave every Phase 3.4B1 row alone. A row that differs is deleted and
    re-inserted rather than patched, so what is stored is always what one
    version of the rules produced, whole.
    """
    table = projection.table
    fields = projection.fields
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
    desired: dict[int, StructuredEntry], unparsed_rule: StructuringRule
) -> tuple[int, int]:
    """How many readings a closed rule named, and how many stayed unparsed.

    `EDUCATION_PIPE_PERIOD_ONLY_V1` counts as structured: it named the period,
    and it declined the rest honestly rather than failing to read anything.
    """
    unparsed = sum(
        1
        for structured in desired.values()
        if structured.structuring_rule_id is unparsed_rule
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
    2. the `ACCEPTED` `EXPERIENCE`, `PROJECT`, `EDUCATION`, `CERTIFICATION` and
       `LANGUAGE` facts are read inside the transaction, so the set being
       projected cannot change underneath it;
    3. each of them is structured deterministically, and **every** one of them
       is projected: a fact the closed rules cannot read becomes an unparsed
       row with `NULL` fragments, never a silently dropped fact;
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
        desired = {
            projection.counted: _read(connection, projection, profile_id)
            for projection in _PROJECTIONS
        }
        if after_read is not None:
            after_read()

        created = 0
        removed = 0
        rows: dict[str, int] = {}
        for projection in _PROJECTIONS:
            added, dropped = _reconcile(
                connection, projection, profile_id, desired[projection.counted]
            )
            created += added
            removed += dropped
        for projection in _PROJECTIONS:
            rows[projection.counted] = _count(
                connection, projection.table, profile_id
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise

    counters: dict[str, int] = {}
    for projection in _PROJECTIONS:
        entries = desired[projection.counted]
        structured, unparsed = _tally(entries, projection.unparsed)
        counters[f"accepted_{projection.counted}_facts"] = len(entries)
        counters[f"{projection.counted}_rows"] = rows[projection.counted]
        counters[f"structured_{projection.tallied}"] = structured
        counters[f"unparsed_{projection.tallied}"] = unparsed
    return StructuredProfileSynchronization(
        profile_id=profile_id,
        created=created,
        removed=removed,
        structurer_version=STRUCTURED_PROFILE_VERSION,
        **counters,
    )


def _listed(
    connection: sqlite3.Connection,
    projection: _Projection,
    profile_id: int,
) -> list[tuple]:
    """The stored rows of one table, oldest fact first.

    Reading is not projecting: this returns what is stored, and synchronizing
    is the only thing that changes it.
    """
    columns = ", ".join(projection.fields)
    return connection.execute(
        f"SELECT id, profile_id, fact_id, {columns}, structurer_version, "
        f"structuring_rule_id, created_at FROM {projection.table} "
        "WHERE profile_id = ? ORDER BY fact_id",
        (profile_id,),
    ).fetchall()


def _rebuild(row: tuple, model, fields: tuple[str, ...]):
    """One stored row, as its frozen dataclass. `NULL` stays `None`."""
    return model(
        id=int(row[0]),
        profile_id=int(row[1]),
        fact_id=int(row[2]),
        **{name: row[3 + offset] for offset, name in enumerate(fields)},
        structurer_version=str(row[3 + len(fields)]),
        structuring_rule_id=str(row[4 + len(fields)]),
        created_at=str(row[5 + len(fields)]),
    )


def list_profile_experiences(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[ProfileExperience, ...]:
    """Every projected experience of this profile, oldest fact first."""
    projection = _PROJECTIONS[0]
    return tuple(
        _rebuild(row, ProfileExperience, projection.fields)
        for row in _listed(connection, projection, profile_id)
    )


def list_profile_projects(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[ProfileProject, ...]:
    """Every projected project of this profile, oldest fact first."""
    projection = _PROJECTIONS[1]
    return tuple(
        _rebuild(row, ProfileProject, projection.fields)
        for row in _listed(connection, projection, profile_id)
    )


def list_profile_educations(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[ProfileEducation, ...]:
    """Every projected education of this profile, oldest fact first."""
    projection = _PROJECTIONS[2]
    return tuple(
        _rebuild(row, ProfileEducation, projection.fields)
        for row in _listed(connection, projection, profile_id)
    )


def list_profile_certifications(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[ProfileCertification, ...]:
    """Every projected certification of this profile, oldest fact first."""
    projection = _PROJECTIONS[3]
    return tuple(
        _rebuild(row, ProfileCertification, projection.fields)
        for row in _listed(connection, projection, profile_id)
    )


def list_profile_languages(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[ProfileLanguage, ...]:
    """Every projected language of this profile, oldest fact first."""
    projection = _PROJECTIONS[4]
    return tuple(
        _rebuild(row, ProfileLanguage, projection.fields)
        for row in _listed(connection, projection, profile_id)
    )
