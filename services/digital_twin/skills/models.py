"""Typed, immutable vocabulary of the Phase 3.4A skill projection.

A `NormalizedSkill` is the whole explanation of one normalization: what came
in, the conservative technical key it was compared on, the canonical identity
it resolved to, the version of the normalizer that decided it, and the named
rule that applied. Nothing else is derived from a skill mention.

There is no level here, in any spelling. No proficiency, no seniority, no
score, no confidence, no years count and no occurrence count: none of those can
be read off a mention, and repeating a mention is not evidence of a level. A
`ProfileSkill` says the person holds a skill, and stops there.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: The version of the whole normalization contract: the conservative key, the
#: closed alias registry and the literal fallback below. Any change to what
#: this package resolves — a new alias included — moves this version, because
#: it is what an evidence row records to explain how it was produced.
SKILL_NORMALIZER_VERSION = "skill-normalizer-v1"


class SkillNormalizationRule(StrEnum):
    """Why one mention resolved to one canonical skill.

    The three rules are exhaustive and mutually exclusive: a mention matches a
    registered alias, or it already is a registered canonical form, or no entry
    of the registry knows it and it is kept literally.
    """

    #: The mention matched an alias of the closed v1 registry.
    ALIAS_REGISTRY_V1 = "ALIAS_REGISTRY_V1"
    #: The mention already was the canonical form of a registry entry.
    CANONICAL_FORM_V1 = "CANONICAL_FORM_V1"
    #: No registry entry knows this mention. It is kept as written, with only
    #: the conservative technical normalization applied to its key.
    LITERAL_V1 = "LITERAL_V1"


class SkillNormalizationError(ValueError):
    """Raised when a mention carries nothing that could name a skill."""


class SkillAliasRegistryError(RuntimeError):
    """Raised at import time when the closed alias registry is incoherent.

    The one case it covers is a collision: two different canonical skills
    claiming the same comparison key. Resolving it by picking one would make
    the registry decide silently which technology somebody meant.
    """


@dataclass(frozen=True)
class NormalizedSkill:
    """One mention, resolved. Every field of the explanation is here.

    `source_value` is the wording the fact carried; `comparison_key` is the
    conservative technical form it was compared on. The two are kept apart so
    a reader can see what was compared without guessing at the transformation,
    and so nothing has to re-derive it.
    """

    #: The technical input: the fact's own value, unchanged.
    source_value: str
    #: NFKC, trimmed, inner space runs collapsed, casefolded. This, and only
    #: this, is what the registry is looked up on.
    comparison_key: str
    #: The canonical identity, as `skills.canonical_key` stores it.
    canonical_key: str
    #: The canonical identity, as `skills.canonical_name` stores it.
    canonical_name: str
    normalizer_version: str
    normalization_rule_id: SkillNormalizationRule


@dataclass(frozen=True)
class Skill:
    """One persisted canonical skill. Vocabulary, never a claim about anybody."""

    id: int
    canonical_key: str
    canonical_name: str
    created_at: str


@dataclass(frozen=True)
class ProfileSkillEvidence:
    """One verified fact justifying one skill of one profile.

    It carries no value and no provenance of its own: `fact_id` is the way to
    the fact, and the fact is the way to `profile_fact_provenance`.
    """

    id: int
    profile_skill_id: int
    fact_id: int
    normalizer_version: str
    normalization_rule_id: str
    created_at: str


@dataclass(frozen=True)
class ProfileSkill:
    """One skill a profile holds, with the evidence that justifies it.

    There is no level field, and adding one would need evidence this project
    does not have: a mention states that a skill was named, never how well.
    """

    id: int
    profile_id: int
    skill_id: int
    canonical_key: str
    canonical_name: str
    created_at: str
    evidence: tuple[ProfileSkillEvidence, ...]
