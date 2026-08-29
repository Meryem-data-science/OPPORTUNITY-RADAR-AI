"""Transactional projection of verified SKILL facts onto normalized skills.

The style is the one `services/digital_twin/facts/repository.py` already uses:
plain SQLite, no ORM, no second connection factory, no second migration runner,
and one explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` around the whole
operation. A failure anywhere rolls the projection back whole, so a profile is
never left holding half a reconciliation.

Three rules are enforced here rather than trusted to callers:

* the only facts read are `fact_type = 'SKILL' AND status = 'ACCEPTED'`,
  filtered in SQL. A `PROPOSED`, `REJECTED` or `CORRECTED` fact, and an
  `ACCEPTED` fact of any other type, cannot reach a skill;
* nothing in this module writes to `profile_facts` or to
  `profile_fact_provenance`. There is no `INSERT`, `UPDATE` or `DELETE`
  against either table anywhere below, and a test reads the SQL to keep it so;
* every statement is scoped by `profile_id`, so one profile's skills can never
  be reconciled out of another profile's facts.

`synchronize_profile_skills` is a reconciliation, not an append. It computes
what the verified facts say now, and makes the projection equal to that:
associations that are still justified are left exactly as they are, ids
included; what is missing is created; what has stopped being justified is
deleted. Running it twice on unchanged facts writes nothing at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from services.digital_twin.skills.models import (
    SKILL_NORMALIZER_VERSION,
    NormalizedSkill,
    ProfileSkill,
    ProfileSkillEvidence,
    Skill,
)
from services.digital_twin.skills.normalizer import normalize_skill

#: The one reading this projection is built on. `SKILL` and `ACCEPTED` are
#: written into the statement rather than passed in, so no caller can widen it.
_VERIFIED_SKILL_FACTS = (
    "SELECT id, value FROM profile_facts "
    "WHERE profile_id = ? AND fact_type = 'SKILL' AND status = 'ACCEPTED' "
    "ORDER BY id"
)


class ProfileSkillError(RuntimeError):
    """Raised when the skill projection cannot be read or written safely."""


class ProfileSkillNotFoundError(ProfileSkillError):
    """Raised when the profile the projection was asked about does not exist.

    Synchronizing is not a way to bring a profile into existence: an absent
    profile is an absence, and this module creates no user and no profile.
    """


@dataclass(frozen=True)
class SkillSynchronization:
    """What one reconciliation did. Counters and a version, never a value.

    `changed` is the idempotence answer: it is False when the run found the
    projection already equal to the verified facts and therefore wrote nothing.
    A re-routed evidence — the same fact now justifying another skill because
    the normalizer version or the registry moved — counts as one removal and
    one creation, because that is what happened to the rows.
    """

    profile_id: int
    #: How many `ACCEPTED` `SKILL` facts the transaction read.
    verified_skill_facts: int
    #: How many skills the profile holds once the reconciliation is done.
    profile_skills: int
    skills_created: int
    profile_skills_created: int
    profile_skills_removed: int
    evidence_created: int
    evidence_removed: int
    normalizer_version: str

    @property
    def changed(self) -> bool:
        return bool(
            self.skills_created
            or self.profile_skills_created
            or self.profile_skills_removed
            or self.evidence_created
            or self.evidence_removed
        )

    def as_dict(self) -> dict[str, object]:
        """The privacy-safe summary: counters, a version and a flag."""
        return {
            "verified_skill_facts": self.verified_skill_facts,
            "profile_skills": self.profile_skills,
            "skills_created": self.skills_created,
            "profile_skills_created": self.profile_skills_created,
            "profile_skills_removed": self.profile_skills_removed,
            "evidence_created": self.evidence_created,
            "evidence_removed": self.evidence_removed,
            "normalizer_version": self.normalizer_version,
            "changed": self.changed,
        }


def _require_profile(connection: sqlite3.Connection, profile_id: int) -> None:
    row = connection.execute(
        "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if row is None:
        raise ProfileSkillNotFoundError("profile does not exist")


def _read_verified_skill_facts(
    connection: sqlite3.Connection, profile_id: int
) -> dict[int, NormalizedSkill]:
    """Normalize every verified skill fact of this profile, oldest id first.

    The value read is `profile_facts.value`, the wording the source used.
    `normalized_value` is deliberately not consulted: `0007` sets it only where
    a *technical* normal form is unambiguous — an address, a phone number — and
    no rule sets it on a `SKILL` fact, so reading it here would add a branch
    nothing feeds and a second input the normalizer could disagree with.
    """
    normalized: dict[int, NormalizedSkill] = {}
    for fact_id, value in connection.execute(
        _VERIFIED_SKILL_FACTS, (profile_id,)
    ).fetchall():
        normalized[int(fact_id)] = normalize_skill(str(value))
    return normalized


def _ensure_skill(
    connection: sqlite3.Connection, normalized: NormalizedSkill
) -> tuple[int, bool]:
    """Return the id of the canonical skill, creating the vocabulary row once.

    An existing row is returned untouched, `canonical_name` included: the
    vocabulary is shared by every profile, and rewriting the display form of a
    skill because one profile spelled it differently would change what other
    profiles read. `canonical_key` is what identifies a skill, and it is
    identical for both spellings by construction.
    """
    row = connection.execute(
        "SELECT id FROM skills WHERE canonical_key = ?", (normalized.canonical_key,)
    ).fetchone()
    if row is not None:
        return int(row[0]), False
    created = connection.execute(
        "INSERT INTO skills (canonical_key, canonical_name) VALUES (?, ?) "
        "RETURNING id",
        (normalized.canonical_key, normalized.canonical_name),
    ).fetchone()
    if created is None:
        raise ProfileSkillError("skill insert returned no row")
    return int(created[0]), True


def _ensure_profile_skill(
    connection: sqlite3.Connection, profile_id: int, skill_id: int
) -> tuple[int, bool]:
    row = connection.execute(
        "SELECT id FROM profile_skills WHERE profile_id = ? AND skill_id = ?",
        (profile_id, skill_id),
    ).fetchone()
    if row is not None:
        return int(row[0]), False
    created = connection.execute(
        "INSERT INTO profile_skills (profile_id, skill_id) VALUES (?, ?) "
        "RETURNING id",
        (profile_id, skill_id),
    ).fetchone()
    if created is None:
        raise ProfileSkillError("profile skill insert returned no row")
    return int(created[0]), True


def _current_evidence(
    connection: sqlite3.Connection, profile_id: int
) -> dict[int, tuple[int, int, str, str]]:
    """Every evidence row of this profile, keyed by the fact it points at.

    The join on `profile_skills` is what scopes the read: `fact_id` is unique
    across the whole table, so another profile's evidence must stay invisible
    from here rather than be reconciled by mistake.
    """
    rows = connection.execute(
        """SELECT e.id, e.profile_skill_id, e.fact_id, e.normalizer_version,
                  e.normalization_rule_id
             FROM profile_skill_evidence AS e
             JOIN profile_skills AS ps ON ps.id = e.profile_skill_id
            WHERE ps.profile_id = ?""",
        (profile_id,),
    ).fetchall()
    return {
        int(row[2]): (int(row[0]), int(row[1]), str(row[3]), str(row[4]))
        for row in rows
    }


def synchronize_profile_skills(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    after_read: Callable[[], None] | None = None,
) -> SkillSynchronization:
    """Make this profile's skills equal to what its verified facts say.

    The whole reconciliation is one `BEGIN IMMEDIATE` transaction:

    1. the profile must exist;
    2. the `ACCEPTED` `SKILL` facts are read inside the transaction, so the set
       being projected cannot change underneath the projection;
    3. each of them is normalized by `normalize_skill`, deterministically;
    4. evidence pointing at a fact that is no longer a verified skill fact is
       deleted — a fact that became `CORRECTED` or `REJECTED` stops proving
       anything, at the next synchronization and without any other action;
    5. missing canonical skills, associations and evidences are created;
    6. an association left with no evidence at all is deleted, because a skill
       nothing justifies is not a skill the person holds.

    Several facts that normalize to one skill — an alias and its canonical
    form, for instance — produce **one** association carrying several
    evidences. That is a count of proofs, not a level, and nothing anywhere
    reads it as one.

    Nothing here writes to `profile_facts` or `profile_fact_provenance`, and a
    canonical `skills` row is never deleted: an unused vocabulary row is
    harmless, and on its own it says nothing about any profile.

    `after_read` is a test seam invoked once the facts have been read and
    before anything is written; production callers leave it unset.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_profile(connection, profile_id)
        desired = _read_verified_skill_facts(connection, profile_id)
        if after_read is not None:
            after_read()

        existing_evidence = _current_evidence(connection, profile_id)
        evidence_removed = 0
        evidence_created = 0
        skills_created = 0
        profile_skills_created = 0

        # Evidence whose fact stopped being a verified skill fact.
        for fact_id, (evidence_id, _, _, _) in existing_evidence.items():
            if fact_id not in desired:
                connection.execute(
                    "DELETE FROM profile_skill_evidence WHERE id = ?", (evidence_id,)
                )
                evidence_removed += 1

        # What the verified facts say now, created only where it is missing.
        association_by_key: dict[str, int] = {}
        for fact_id, normalized in desired.items():
            profile_skill_id = association_by_key.get(normalized.canonical_key)
            if profile_skill_id is None:
                skill_id, skill_is_new = _ensure_skill(connection, normalized)
                skills_created += int(skill_is_new)
                profile_skill_id, association_is_new = _ensure_profile_skill(
                    connection, profile_id, skill_id
                )
                profile_skills_created += int(association_is_new)
                association_by_key[normalized.canonical_key] = profile_skill_id

            known = existing_evidence.get(fact_id)
            if known is not None:
                evidence_id, known_profile_skill_id, version, rule = known
                if (
                    known_profile_skill_id == profile_skill_id
                    and version == normalized.normalizer_version
                    and rule == normalized.normalization_rule_id.value
                ):
                    # Still exactly what this run would write: left untouched,
                    # so its id and its created_at survive the run.
                    continue
                # The same fact now reads as another skill, or was recorded by
                # another normalizer version. The row is replaced rather than
                # patched, so what is stored is always what the current rules
                # produced, whole.
                connection.execute(
                    "DELETE FROM profile_skill_evidence WHERE id = ?",
                    (evidence_id,),
                )
                evidence_removed += 1
            connection.execute(
                """INSERT INTO profile_skill_evidence (
                       profile_skill_id, fact_id, normalizer_version,
                       normalization_rule_id
                   ) VALUES (?, ?, ?, ?)""",
                (
                    profile_skill_id,
                    fact_id,
                    normalized.normalizer_version,
                    normalized.normalization_rule_id.value,
                ),
            )
            evidence_created += 1

        removed = connection.execute(
            """DELETE FROM profile_skills
                WHERE profile_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM profile_skill_evidence AS e
                       WHERE e.profile_skill_id = profile_skills.id
                  )
            RETURNING id""",
            (profile_id,),
        ).fetchall()

        remaining = connection.execute(
            "SELECT COUNT(*) FROM profile_skills WHERE profile_id = ?", (profile_id,)
        ).fetchone()[0]
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise

    return SkillSynchronization(
        profile_id=profile_id,
        verified_skill_facts=len(desired),
        profile_skills=int(remaining),
        skills_created=skills_created,
        profile_skills_created=profile_skills_created,
        profile_skills_removed=len(removed),
        evidence_created=evidence_created,
        evidence_removed=evidence_removed,
        normalizer_version=SKILL_NORMALIZER_VERSION,
    )


def list_profile_skills(
    connection: sqlite3.Connection, profile_id: int
) -> tuple[ProfileSkill, ...]:
    """Every skill this profile holds, with the evidence behind each one.

    Ordered by `canonical_key` so two reads of one unchanged projection are
    identical. Reading is not projecting: this returns what is stored, and
    synchronizing is the only thing that changes it.
    """
    rows = connection.execute(
        """SELECT ps.id, ps.profile_id, ps.skill_id, s.canonical_key,
                  s.canonical_name, ps.created_at
             FROM profile_skills AS ps
             JOIN skills AS s ON s.id = ps.skill_id
            WHERE ps.profile_id = ?
            ORDER BY s.canonical_key""",
        (profile_id,),
    ).fetchall()
    return tuple(
        ProfileSkill(
            id=int(row[0]),
            profile_id=int(row[1]),
            skill_id=int(row[2]),
            canonical_key=str(row[3]),
            canonical_name=str(row[4]),
            created_at=str(row[5]),
            evidence=_list_evidence(connection, int(row[0])),
        )
        for row in rows
    )


def _list_evidence(
    connection: sqlite3.Connection, profile_skill_id: int
) -> tuple[ProfileSkillEvidence, ...]:
    rows = connection.execute(
        """SELECT id, profile_skill_id, fact_id, normalizer_version,
                  normalization_rule_id, created_at
             FROM profile_skill_evidence
            WHERE profile_skill_id = ?
            ORDER BY fact_id""",
        (profile_skill_id,),
    ).fetchall()
    return tuple(
        ProfileSkillEvidence(
            id=int(row[0]),
            profile_skill_id=int(row[1]),
            fact_id=int(row[2]),
            normalizer_version=str(row[3]),
            normalization_rule_id=str(row[4]),
            created_at=str(row[5]),
        )
        for row in rows
    )


def get_skill_by_canonical_key(
    connection: sqlite3.Connection, canonical_key: str
) -> Skill | None:
    """Read one canonical skill of the shared vocabulary, or None.

    A row existing here means the vocabulary knows the name. It never means any
    profile holds it: only a `profile_skills` row says that.
    """
    row = connection.execute(
        "SELECT id, canonical_key, canonical_name, created_at FROM skills "
        "WHERE canonical_key = ?",
        (canonical_key,),
    ).fetchone()
    if row is None:
        return None
    return Skill(
        id=int(row[0]),
        canonical_key=str(row[1]),
        canonical_name=str(row[2]),
        created_at=str(row[3]),
    )
