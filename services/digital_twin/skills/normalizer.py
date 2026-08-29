"""Deterministic, local, conservative normalization of one skill mention.

The whole module is a pure function of its input and of
`SKILL_NORMALIZER_VERSION`: no clock, no counter, no environment variable, no
network, no model, no external service. The same mention always resolves to
the same canonical skill, in this process and in the next one.

What it does, and the complete list of it:

1. computes a conservative technical key — Unicode NFKC, trim, inner runs of
   whitespace collapsed to one space, casefold for comparison;
2. looks that key up in a **closed** registry of five aliases and their
   canonical forms;
3. keeps the mention literally when the registry does not know it.

What it refuses to do, deliberately and by absence rather than by flag: no
stemming, no lemmatization, no fuzzy matching, no Levenshtein or any other edit
distance, no similarity of any kind, no global punctuation stripping, no accent
stripping, no automatic splitting of a mention into several skills, no
inference from one skill to a neighbouring one, and no enrichment or completion
of a name. `C`, `C++` and `C#` are three different keys and stay three
different skills, `CI/CD` stays one mention, and `Azure Data Platform (avancé)`
stays one skill whose name still reads `Azure Data Platform (avancé)` — the
parenthesis is part of the name the CV wrote, and no level is read out of it,
here or anywhere else in this slice.

Where the registry does not know a mention, the fallback is prudent rather than
clever: the mention is kept as written. A neighbouring technology is never
substituted for it, and nothing is appended to it to make it look canonical.
"""

from __future__ import annotations

import unicodedata

from services.digital_twin.skills.models import (
    SKILL_NORMALIZER_VERSION,
    NormalizedSkill,
    SkillAliasRegistryError,
    SkillNormalizationError,
    SkillNormalizationRule,
)

#: The closed v1 alias registry: written form → canonical name.
#:
#: It is small on purpose. Every entry is one somebody asked for explicitly,
#: and a mapping nobody asked for is a guess about what a person meant, made
#: once and then believed forever. `Postgres → PostgreSQL` is a spelling of one
#: product; `Spark → PySpark` would be a different product, so it is not here,
#: and neither is any other "obvious" pair. Growing this registry is a decision
#: with its own review, and it moves `SKILL_NORMALIZER_VERSION`.
ALIAS_REGISTRY_V1: dict[str, str] = {
    "PowerBI": "Power BI",
    "Postgres": "PostgreSQL",
    "sklearn": "Scikit-learn",
    "ML": "Machine Learning",
    "IA": "Artificial Intelligence",
}


def technical_key(value: str) -> str:
    """Return the conservative comparison key of one written mention.

    Exactly four operations, in this order: NFKC composition, trim, collapse of
    every inner run of whitespace into a single space, casefold. Nothing else
    is touched — `+`, `#`, `/`, `.`, `-`, parentheses and accented letters all
    survive — because each of them is what distinguishes one real technology
    from another.

    `str.split()` with no argument is what collapses the runs: it splits on
    every kind of whitespace, so a tabulation or a non-breaking space inside a
    mention reads as the space it looks like, and the trim comes with it.
    """
    if value is None:
        raise SkillNormalizationError("a skill mention must be a string")
    composed = unicodedata.normalize("NFKC", value)
    collapsed = " ".join(composed.split())
    if collapsed == "":
        raise SkillNormalizationError("a skill mention must not be blank")
    return collapsed.casefold()


def display_form(value: str) -> str:
    """Return the mention as it will be displayed: composed, trimmed, collapsed.

    Same three first operations as `technical_key`, without the casefold, so
    the capitalisation the CV used survives into `canonical_name` when no
    registry entry knows the mention.
    """
    composed = unicodedata.normalize("NFKC", value)
    collapsed = " ".join(composed.split())
    if collapsed == "":
        raise SkillNormalizationError("a skill mention must not be blank")
    return collapsed


def _build_registry() -> dict[str, tuple[str, SkillNormalizationRule]]:
    """Index the registry by comparison key, refusing every collision.

    Each entry produces two keys: the alias as written, and the canonical name
    itself — so `PowerBI`, `Power BI` and `power bi` all resolve to the same
    skill, the first through its alias and the two others as the canonical form.

    A collision is a hard failure at import time, not a resolution order. Two
    different canonical skills sharing one key would mean this registry decides
    silently which of two technologies a person meant.
    """
    indexed: dict[str, tuple[str, SkillNormalizationRule]] = {}

    def register(written: str, canonical: str, rule: SkillNormalizationRule) -> None:
        key = technical_key(written)
        known = indexed.get(key)
        if known is not None and known[0] != canonical:
            raise SkillAliasRegistryError(
                f"alias key {key!r} claims both {known[0]!r} and {canonical!r}"
            )
        if known is None:
            indexed[key] = (canonical, rule)

    for canonical in ALIAS_REGISTRY_V1.values():
        register(canonical, canonical, SkillNormalizationRule.CANONICAL_FORM_V1)
    for alias, canonical in ALIAS_REGISTRY_V1.items():
        register(alias, canonical, SkillNormalizationRule.ALIAS_REGISTRY_V1)
    return indexed


#: The registry, resolved once at import time. A collision breaks the import
#: loudly rather than being discovered on somebody's real profile.
CANONICAL_BY_KEY: dict[str, tuple[str, SkillNormalizationRule]] = _build_registry()


def normalize_skill(value: str) -> NormalizedSkill:
    """Resolve one written mention to its canonical skill, and explain how.

    The result carries the whole explanation — the technical input, the
    comparison key, the canonical key and name, the normalizer version and the
    rule that applied — so a reader never has to re-run the function to know
    why a mention became this skill.

    A blank mention raises rather than resolving to an empty skill: the absence
    of a name is not a skill nobody named.
    """
    comparison_key = technical_key(value)
    known = CANONICAL_BY_KEY.get(comparison_key)
    if known is None:
        canonical_name = display_form(value)
        rule = SkillNormalizationRule.LITERAL_V1
    else:
        canonical_name, rule = known
    return NormalizedSkill(
        source_value=value,
        comparison_key=comparison_key,
        canonical_key=technical_key(canonical_name),
        canonical_name=canonical_name,
        normalizer_version=SKILL_NORMALIZER_VERSION,
        normalization_rule_id=rule,
    )
