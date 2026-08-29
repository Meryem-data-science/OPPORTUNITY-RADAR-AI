"""Phase 3.4A: verified SKILL facts, projected onto normalized skills.

This slice adds a derived reading and no new truth. `profile_facts` stays the
source of truth, "verified" keeps its single definition — `status = 'ACCEPTED'`
— and the projection reads only:

    fact_type = 'SKILL' AND status = 'ACCEPTED'

A `PROPOSED`, `REJECTED` or `CORRECTED` fact, and an `ACCEPTED` fact of any
other type, are invisible to it. Nothing from the Phase 3.2B extractor reaches
a skill without passing through a fact a human accepted: this package imports
no CV module and knows nothing about candidates.

The audit chain is a chain, not a copy:

    profile_skills → profile_skill_evidence → profile_facts
                                            → profile_fact_provenance

so an evidence row records only what the projection decided — which normalizer
version, which normalization rule — and points at the fact for the rest.

**No level is inferred, anywhere.** There is no level, proficiency, score,
confidence or seniority in this package or in its schema, and no occurrence
count that could be read as one: several facts naming one skill are several
proofs of the same association, never "more" of that skill. A mention like
`Azure Data Platform (avancé)` stays one skill whose name still carries the
parenthesis. Recording a level that is genuinely known is a separate slice, and
it starts by defining what evidence would prove it.

The normalizer is closed, small and explainable: NFKC, trim, collapsed inner
spaces and casefold for comparison, then a **closed registry of five aliases**,
then a literal fallback that keeps an unknown mention exactly as written. No
stemming, no fuzzy matching, no edit distance, no similarity, no punctuation or
accent stripping, no splitting of a mention, no enrichment — so `C`, `C++` and
`C#` stay three skills. It reads no network, loads no model and calls no API.

What this slice does **not** do: no structured project, experience, education,
certification, language, preference, availability, mobility or career
objective; no opportunity constraint, no eligibility rule, no skill extraction
from an offer, no TF-IDF, no cosine similarity, no matching, no match score, no
ranking, no recommendation, no notification, no CV adaptation and no
auto-apply. There is no HTTP endpoint, no web interface and no remote write
path; the operational database stays local SQLite.
"""

from services.digital_twin.skills.models import (
    SKILL_NORMALIZER_VERSION,
    NormalizedSkill,
    ProfileSkill,
    ProfileSkillEvidence,
    Skill,
    SkillAliasRegistryError,
    SkillNormalizationError,
    SkillNormalizationRule,
)
from services.digital_twin.skills.normalizer import (
    ALIAS_REGISTRY_V1,
    CANONICAL_BY_KEY,
    display_form,
    normalize_skill,
    technical_key,
)
from services.digital_twin.skills.repository import (
    ProfileSkillError,
    ProfileSkillNotFoundError,
    SkillSynchronization,
    get_skill_by_canonical_key,
    list_profile_skills,
    synchronize_profile_skills,
)

__all__ = [
    "ALIAS_REGISTRY_V1",
    "CANONICAL_BY_KEY",
    "SKILL_NORMALIZER_VERSION",
    "NormalizedSkill",
    "ProfileSkill",
    "ProfileSkillError",
    "ProfileSkillEvidence",
    "ProfileSkillNotFoundError",
    "Skill",
    "SkillAliasRegistryError",
    "SkillNormalizationError",
    "SkillNormalizationRule",
    "SkillSynchronization",
    "display_form",
    "get_skill_by_canonical_key",
    "list_profile_skills",
    "normalize_skill",
    "synchronize_profile_skills",
    "technical_key",
]
