"""Transactional persistence for the Phase 3.5B requirement projection.

Same style as 3.5A's repository, and for the same reasons: plain SQLite, no
ORM, no second connection factory, no second migration runner, and one explicit
`BEGIN` / `COMMIT` / `ROLLBACK` around a whole reconciliation.

Four rules are enforced here rather than trusted to callers.

* **`opportunities` is never written.** No `INSERT`, `UPDATE` or `DELETE`
  against it appears below, and a test reads this file to keep it so;
* **no profile table is read or written.** `profiles`, `profile_skills`,
  `profile_skill_evidence` and `profile_languages` are not mentioned anywhere in
  this module. What a posting requires is not a question about a person, and a
  join here would be the first half of an eligibility check nobody asked for;
* **3.5A's projection is never modified.** `opportunity_constraints` is read to
  confirm a posting has been through 3.5A, and that is the only thing this
  module does with it — no update, no delete, no insert;
* **a posting is replaced whole or not at all.** Writing one posting means
  deleting its previous requirement rows and everything hanging off them, then
  inserting the vocabulary it needs, the skill requirements, their evidence, the
  language requirements, their evidence, the ambiguities and the state row —
  inside one transaction. A half-written reading is worse than none: a
  requirement with no evidence explains nothing, and a state row claiming an
  extraction that partly failed would make the next run skip the posting.

**The vocabulary rows created here roll back with everything else.** A `skills`
row is inserted inside the same transaction as the requirement that needed it,
so a failure leaves no orphan term behind. What a rollback does *not* do is
remove a term some earlier, successful extraction created: an unused vocabulary
row is allowed to stay, exactly as `0008` says. It is not a claim about a person
and not a claim about a posting; it says a canonical name exists.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Callable

from services.collector.extractors.opportunity_constraints.requirements.models import (
    AmbiguityReason,
    ExtractedRequirements,
    LanguageRequirement,
    RequirementAmbiguity,
    RequirementEvidence,
    RequirementKind,
    RequirementLevel,
    RequirementSourceField,
    SkillRequirement,
)

__all__ = [
    "OpportunityRequirementRepositoryError",
    "delete_opportunity_requirements",
    "read_opportunity_requirements",
    "store_opportunity_requirements",
    "stored_requirement_signature",
]


class OpportunityRequirementRepositoryError(RuntimeError):
    """Raised when the requirement projection cannot be read or written safely."""


def stored_requirement_signature(
    connection: sqlite3.Connection, opportunity_id: int
) -> tuple[str, str] | None:
    """The `(fingerprint, version)` already stored for one posting, or None.

    This is the whole idempotence check, and it reads the **state** table rather
    than counting requirement rows. A posting that genuinely requires nothing
    has no requirement rows and has been read; the state row is what tells the
    two apart.
    """
    row = connection.execute(
        "SELECT source_fingerprint, extractor_version "
        "FROM opportunity_requirement_extraction_state WHERE opportunity_id = ?",
        (opportunity_id,),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def delete_opportunity_requirements(
    connection: sqlite3.Connection, opportunity_id: int
) -> None:
    """Remove one posting's whole requirement reading. Touches no source table.

    Children first, so the order alone leaves no orphan even if foreign keys are
    off. The two evidence tables key on their requirement rather than on the
    posting — evidence belongs to the requirement it explains — so they are
    reached through a subquery rather than a column.
    """
    connection.execute(
        "DELETE FROM opportunity_skill_requirement_evidence "
        "WHERE opportunity_skill_requirement_id IN "
        "(SELECT id FROM opportunity_skill_requirements WHERE opportunity_id = ?)",
        (opportunity_id,),
    )
    connection.execute(
        "DELETE FROM opportunity_language_requirement_evidence "
        "WHERE opportunity_language_requirement_id IN "
        "(SELECT id FROM opportunity_language_requirements WHERE opportunity_id = ?)",
        (opportunity_id,),
    )
    for table in (
        "opportunity_skill_requirements",
        "opportunity_language_requirements",
        "opportunity_requirement_ambiguities",
        "opportunity_requirement_extraction_state",
    ):
        connection.execute(
            f"DELETE FROM {table} WHERE opportunity_id = ?", (opportunity_id,)
        )


def _ensure_skill_vocabulary(
    connection: sqlite3.Connection, canonical_key: str, canonical_name: str
) -> tuple[int, bool]:
    """The id of the canonical skill, creating the shared vocabulary row once.

    An existing row is returned **untouched**, `canonical_name` included. The
    vocabulary is shared with every profile, and rewriting the display form of a
    skill because one posting spelled it differently would change what the other
    side reads. `canonical_key` is what identifies a skill, and the Phase 3.4A
    normalizer computes it identically on both sides.

    This mirrors `_ensure_skill` in `services/digital_twin/skills/repository.py`
    rather than calling it: that module is the profile side's, it takes a
    `NormalizedSkill` built from a person's CV, and importing it here would put
    a profile-shaped dependency in the middle of a package that must not have
    one. The contract they share is `skills`, and `0008` owns it.
    """
    row = connection.execute(
        "SELECT id FROM skills WHERE canonical_key = ?", (canonical_key,)
    ).fetchone()
    if row is not None:
        return int(row[0]), False
    created = connection.execute(
        "INSERT INTO skills (canonical_key, canonical_name) VALUES (?, ?) RETURNING id",
        (canonical_key, canonical_name),
    ).fetchone()
    if created is None:
        raise OpportunityRequirementRepositoryError("skill insert returned no row")
    return int(created[0]), True


def _insert_evidence(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    requirement_id: int,
    evidence: tuple[RequirementEvidence, ...],
    version: str,
    *,
    with_proficiency: bool,
) -> None:
    for item in evidence:
        columns = [
            column, "position", "source_field", "observed_requirement", "rule_id",
            "evidence_text", "context_heading_text", "extractor_version",
        ]
        values: list[object] = [
            requirement_id,
            item.position,
            item.source_field.value,
            item.observed_requirement.value,
            item.rule_id,
            item.text,
            item.context_heading_text,
            version,
        ]
        if with_proficiency:
            columns.insert(4, "observed_proficiency_text")
            values.insert(4, item.observed_proficiency_text)
        connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(values),
        )


def _insert(
    connection: sqlite3.Connection,
    requirements: ExtractedRequirements,
    extracted_at: str,
) -> int:
    """Write one whole reading. Returns how many vocabulary rows it created."""
    version = requirements.extractor_version
    created_terms = 0
    for requirement in requirements.skills:
        skill_id, created = _ensure_skill_vocabulary(
            connection, requirement.canonical_key, requirement.canonical_name
        )
        created_terms += int(created)
        row = connection.execute(
            "INSERT INTO opportunity_skill_requirements "
            "(opportunity_id, skill_id, requirement, extractor_version) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (
                requirements.opportunity_id,
                skill_id,
                requirement.requirement.value,
                version,
            ),
        ).fetchone()
        if row is None:
            raise OpportunityRequirementRepositoryError(
                "skill requirement insert returned no row"
            )
        _insert_evidence(
            connection,
            "opportunity_skill_requirement_evidence",
            "opportunity_skill_requirement_id",
            int(row[0]),
            requirement.evidence,
            version,
            with_proficiency=False,
        )
    for language in requirements.languages:
        row = connection.execute(
            "INSERT INTO opportunity_language_requirements "
            "(opportunity_id, language_key, language_name, proficiency_text, "
            "requirement, extractor_version) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (
                requirements.opportunity_id,
                language.language_key,
                language.language_name,
                language.proficiency_text,
                language.requirement.value,
                version,
            ),
        ).fetchone()
        if row is None:
            raise OpportunityRequirementRepositoryError(
                "language requirement insert returned no row"
            )
        _insert_evidence(
            connection,
            "opportunity_language_requirement_evidence",
            "opportunity_language_requirement_id",
            int(row[0]),
            language.evidence,
            version,
            with_proficiency=True,
        )
    for ambiguity in requirements.ambiguities:
        connection.execute(
            "INSERT INTO opportunity_requirement_ambiguities "
            "(opportunity_id, position, kind, reason, rule_id, evidence_text, "
            "context_heading_text, extractor_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                requirements.opportunity_id,
                ambiguity.position,
                ambiguity.kind.value,
                ambiguity.reason.value,
                ambiguity.rule_id,
                ambiguity.text,
                ambiguity.context_heading_text,
                version,
            ),
        )
    connection.execute(
        "INSERT INTO opportunity_requirement_extraction_state "
        "(opportunity_id, source_fingerprint, extractor_version, extracted_at) "
        "VALUES (?, ?, ?, ?)",
        (
            requirements.opportunity_id,
            requirements.source_fingerprint,
            version,
            extracted_at,
        ),
    )
    return created_terms


def store_opportunity_requirements(
    connection: sqlite3.Connection,
    requirements: ExtractedRequirements,
    *,
    extracted_at: str | None = None,
    after_delete: Callable[[], None] | None = None,
) -> int:
    """Replace one posting's requirement reading, whole, in one transaction.

    Returns how many `skills` vocabulary rows the write had to create. The
    caller supplies an already-extracted reading, so nothing is computed here
    and nothing is decided here. A failure at any point — a `CHECK` the reading
    violates, a foreign key, an interruption — rolls back everything, the delete
    and the new vocabulary rows included, so the previous reading survives
    intact rather than being replaced by a partial one.

    `after_delete` is a test seam invoked once the old rows are gone and before
    the new ones are written; production callers leave it unset.
    """
    if not isinstance(requirements, ExtractedRequirements):
        raise OpportunityRequirementRepositoryError(
            "requirements must be an ExtractedRequirements"
        )
    timestamp = extracted_at or datetime.now(UTC).isoformat(timespec="microseconds")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute(
            "SELECT 1 FROM opportunity_constraints WHERE opportunity_id = ?",
            (requirements.opportunity_id,),
        ).fetchone() is None:
            raise OpportunityRequirementRepositoryError(
                f"opportunity {requirements.opportunity_id} has no Phase 3.5A "
                "constraint projection; run the Phase 3.5A constraint sync first"
            )
        delete_opportunity_requirements(connection, requirements.opportunity_id)
        if after_delete is not None:
            after_delete()
        created = _insert(connection, requirements, timestamp)
        connection.execute("COMMIT")
        return created
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def read_opportunity_requirements(
    connection: sqlite3.Connection, opportunity_id: int
) -> ExtractedRequirements | None:
    """Rebuild one posting's stored reading, or None when it has none.

    Reading is not extracting: this returns what was stored, and storing is the
    only thing that changes it. `None` means no state row — the posting has not
    been read — which is deliberately different from a reading that found
    nothing.
    """
    signature = stored_requirement_signature(connection, opportunity_id)
    if signature is None:
        return None
    fingerprint, version = signature

    skills: list[SkillRequirement] = []
    for row in connection.execute(
        "SELECT r.id, s.canonical_key, s.canonical_name, r.requirement "
        "FROM opportunity_skill_requirements AS r "
        "JOIN skills AS s ON s.id = r.skill_id "
        "WHERE r.opportunity_id = ? ORDER BY s.canonical_key",
        (opportunity_id,),
    ).fetchall():
        skills.append(
            SkillRequirement(
                canonical_key=str(row[1]),
                canonical_name=str(row[2]),
                requirement=RequirementLevel(str(row[3])),
                evidence=_read_evidence(
                    connection,
                    "opportunity_skill_requirement_evidence",
                    "opportunity_skill_requirement_id",
                    int(row[0]),
                    with_proficiency=False,
                ),
            )
        )

    languages: list[LanguageRequirement] = []
    for row in connection.execute(
        "SELECT id, language_key, language_name, proficiency_text, requirement "
        "FROM opportunity_language_requirements "
        "WHERE opportunity_id = ? ORDER BY language_key",
        (opportunity_id,),
    ).fetchall():
        languages.append(
            LanguageRequirement(
                language_key=str(row[1]),
                language_name=str(row[2]),
                requirement=RequirementLevel(str(row[4])),
                proficiency_text=None if row[3] is None else str(row[3]),
                evidence=_read_evidence(
                    connection,
                    "opportunity_language_requirement_evidence",
                    "opportunity_language_requirement_id",
                    int(row[0]),
                    with_proficiency=True,
                ),
            )
        )

    ambiguities = tuple(
        RequirementAmbiguity(
            position=int(row[0]),
            kind=RequirementKind(str(row[1])),
            reason=AmbiguityReason(str(row[2])),
            rule_id=str(row[3]),
            text=str(row[4]),
            context_heading_text=None if row[5] is None else str(row[5]),
        )
        for row in connection.execute(
            "SELECT position, kind, reason, rule_id, evidence_text, "
            "context_heading_text FROM opportunity_requirement_ambiguities "
            "WHERE opportunity_id = ? ORDER BY position",
            (opportunity_id,),
        )
    )
    return ExtractedRequirements(
        opportunity_id=opportunity_id,
        source_fingerprint=fingerprint,
        extractor_version=version,
        skills=tuple(skills),
        languages=tuple(languages),
        ambiguities=ambiguities,
    )


def _read_evidence(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    requirement_id: int,
    *,
    with_proficiency: bool,
) -> tuple[RequirementEvidence, ...]:
    proficiency = "observed_proficiency_text" if with_proficiency else "NULL"
    return tuple(
        RequirementEvidence(
            position=int(row[0]),
            source_field=RequirementSourceField(str(row[1])),
            observed_requirement=RequirementLevel(str(row[2])),
            rule_id=str(row[3]),
            text=str(row[4]),
            context_heading_text=None if row[5] is None else str(row[5]),
            observed_proficiency_text=None if row[6] is None else str(row[6]),
        )
        for row in connection.execute(
            f"SELECT position, source_field, observed_requirement, rule_id, "
            f"evidence_text, context_heading_text, {proficiency} FROM {table} "
            f"WHERE {column} = ? ORDER BY position",
            (requirement_id,),
        )
    )
