"""Unit coverage for the Phase 3.4A skill normalizer.

Nothing here touches a database and nothing here is personal: every mention is
a technology name invented for the test or taken from the closed registry the
slice ships with. The tests come in three halves — what the five mandatory
aliases resolve to, what the conservative key does and does not touch, and what
the module refuses to contain at all.
"""

import io
import tokenize
from pathlib import Path

import pytest

from services.digital_twin.skills.models import (
    SKILL_NORMALIZER_VERSION,
    SkillAliasRegistryError,
    SkillNormalizationError,
    SkillNormalizationRule,
)
from services.digital_twin.skills.normalizer import (
    ALIAS_REGISTRY_V1,
    CANONICAL_BY_KEY,
    _build_registry,
    normalize_skill,
    technical_key,
)

NORMALIZER_SOURCE = Path("services/digital_twin/skills/normalizer.py")
MODELS_SOURCE = Path("services/digital_twin/skills/models.py")

#: The five aliases the slice is required to know, and nothing more.
MANDATORY_ALIASES = {
    "PowerBI": "Power BI",
    "Postgres": "PostgreSQL",
    "sklearn": "Scikit-learn",
    "ML": "Machine Learning",
    "IA": "Artificial Intelligence",
}


def code_only(path: Path) -> str:
    """Return a module's executable source, without comments or docstrings.

    This package documents at length what it refuses to do, so a plain
    substring search over the file would match its own prose.
    """
    tokens = tokenize.generate_tokens(
        io.StringIO(path.read_text(encoding="utf-8")).readline
    )
    return "".join(
        token.string
        for token in tokens
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


# --------------------------------------------------------------------------
# The closed v1 alias registry
# --------------------------------------------------------------------------


def test_the_registry_is_exactly_the_five_mandatory_aliases() -> None:
    """v1 stays small: an alias nobody asked for is a guess made permanent."""
    assert ALIAS_REGISTRY_V1 == MANDATORY_ALIASES


@pytest.mark.parametrize("alias, canonical", sorted(MANDATORY_ALIASES.items()))
def test_each_mandatory_alias_resolves_to_its_canonical_skill(
    alias: str, canonical: str
) -> None:
    resolved = normalize_skill(alias)

    assert resolved.canonical_name == canonical
    assert resolved.canonical_key == technical_key(canonical)
    assert resolved.normalization_rule_id is SkillNormalizationRule.ALIAS_REGISTRY_V1
    assert resolved.normalizer_version == SKILL_NORMALIZER_VERSION


@pytest.mark.parametrize("canonical", sorted(set(MANDATORY_ALIASES.values())))
def test_the_canonical_form_resolves_to_the_same_skill(canonical: str) -> None:
    """Writing the canonical name is not a different skill from its alias."""
    resolved = normalize_skill(canonical)

    assert resolved.canonical_name == canonical
    assert resolved.normalization_rule_id is SkillNormalizationRule.CANONICAL_FORM_V1


@pytest.mark.parametrize(
    "alias, canonical",
    [(alias, canonical) for alias, canonical in MANDATORY_ALIASES.items()],
)
def test_an_alias_and_its_canonical_form_share_one_canonical_key(
    alias: str, canonical: str
) -> None:
    assert (
        normalize_skill(alias).canonical_key
        == normalize_skill(canonical).canonical_key
    )


@pytest.mark.parametrize(
    "written",
    ["powerbi", "POWERBI", "  PowerBI  ", "Power   BI", "power bi", "POWER BI"],
)
def test_case_and_spacing_variations_reach_the_same_skill(written: str) -> None:
    resolved = normalize_skill(written)

    assert resolved.canonical_key == "power bi"
    assert resolved.canonical_name == "Power BI"


def test_the_registry_refuses_a_collision() -> None:
    """Two canonical skills claiming one key is a defect, not a priority order."""
    original = dict(ALIAS_REGISTRY_V1)
    try:
        ALIAS_REGISTRY_V1["ml"] = "Meta Language"
        with pytest.raises(SkillAliasRegistryError):
            _build_registry()
    finally:
        ALIAS_REGISTRY_V1.clear()
        ALIAS_REGISTRY_V1.update(original)

    assert _build_registry() == CANONICAL_BY_KEY


def test_every_canonical_name_resolves_to_itself() -> None:
    """The registry has no chain: a canonical name is where resolution stops."""
    for canonical in ALIAS_REGISTRY_V1.values():
        resolved = normalize_skill(canonical)

        assert resolved.canonical_name == canonical
        assert normalize_skill(resolved.canonical_name).canonical_name == canonical


# --------------------------------------------------------------------------
# The conservative technical key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("written", ["C", "C++", "C#"])
def test_the_c_family_stays_three_distinct_skills(written: str) -> None:
    resolved = normalize_skill(written)

    assert resolved.canonical_name == written
    assert resolved.canonical_key == written.casefold()


def test_the_three_c_keys_are_pairwise_different() -> None:
    keys = {normalize_skill(written).canonical_key for written in ("C", "C++", "C#")}

    assert len(keys) == 3


@pytest.mark.parametrize(
    "written",
    ["CI/CD", "Node.js", "ASP.NET", "Scikit-learn", "R&D", "F#", "Objective-C"],
)
def test_punctuation_is_never_stripped(written: str) -> None:
    resolved = normalize_skill(written)

    for character in written:
        if not character.isalnum():
            assert character in resolved.canonical_name
            assert character in resolved.canonical_key


@pytest.mark.parametrize(
    "written", ["Élastique", "Modélisation", "Traitement décalé"]
)
def test_accents_are_never_stripped(written: str) -> None:
    resolved = normalize_skill(written)

    assert resolved.canonical_name == written
    assert resolved.canonical_key == written.casefold()


def test_a_mention_with_a_parenthesis_stays_one_whole_skill() -> None:
    """No level is read out of it, and nothing is split off it."""
    resolved = normalize_skill("Azure Data Platform (avancé)")

    assert resolved.canonical_name == "Azure Data Platform (avancé)"
    assert resolved.canonical_key == "azure data platform (avancé)"
    assert resolved.normalization_rule_id is SkillNormalizationRule.LITERAL_V1


@pytest.mark.parametrize(
    "written, expected",
    [
        ("Apache  Spark", "apache spark"),
        ("  Apache Spark  ", "apache spark"),
        ("Apache\tSpark", "apache spark"),
        ("Apache Spark", "apache spark"),
    ],
)
def test_whitespace_is_trimmed_and_collapsed(written: str, expected: str) -> None:
    assert technical_key(written) == expected


def test_nfkc_composes_before_anything_else_is_decided() -> None:
    """A decomposed accent and a precomposed one are the same skill.

    Both spellings are written as escapes, so the difference between them
    survives every editor and every normalization this file passes through.
    """
    decomposed = "Mode\u0301lisation"
    precomposed = "Mod\u00e9lisation"

    assert decomposed != precomposed
    assert technical_key(decomposed) == technical_key(precomposed)
    assert (
        normalize_skill(decomposed).canonical_key
        == normalize_skill(precomposed).canonical_key
    )


@pytest.mark.parametrize("written", ["", "   ", "\t\n"])
def test_a_blank_mention_is_refused_rather_than_resolved(written: str) -> None:
    with pytest.raises(SkillNormalizationError):
        normalize_skill(written)


# --------------------------------------------------------------------------
# The literal fallback
# --------------------------------------------------------------------------


def test_an_unknown_mention_is_kept_exactly_as_written() -> None:
    resolved = normalize_skill("Terraform")

    assert resolved.canonical_name == "Terraform"
    assert resolved.canonical_key == "terraform"
    assert resolved.normalization_rule_id is SkillNormalizationRule.LITERAL_V1


def test_an_unknown_mention_is_never_completed_or_enriched() -> None:
    """Nothing is appended to make a name look canonical."""
    for written in ("Spark", "Postgre", "scikit", "Power"):
        resolved = normalize_skill(written)

        assert resolved.canonical_name == written
        assert resolved.canonical_key == written.casefold()


def test_a_near_miss_is_a_different_skill_and_not_a_correction() -> None:
    """No edit distance: `Postgre` is not `Postgres`, and stays its own skill."""
    assert normalize_skill("Postgre").canonical_key != normalize_skill(
        "Postgres"
    ).canonical_key


def test_a_mention_is_never_split_into_several_skills() -> None:
    resolved = normalize_skill("Python, SQL")

    assert resolved.canonical_name == "Python, SQL"


# --------------------------------------------------------------------------
# Determinism and explainability
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written", ["PowerBI", "C++", "Azure Data Platform (avancé)", "Terraform"]
)
def test_normalization_is_deterministic(written: str) -> None:
    assert normalize_skill(written) == normalize_skill(written)


def test_a_result_explains_itself_field_by_field() -> None:
    resolved = normalize_skill("  sklearn ")

    assert resolved.source_value == "  sklearn "
    assert resolved.comparison_key == "sklearn"
    assert resolved.canonical_key == "scikit-learn"
    assert resolved.canonical_name == "Scikit-learn"
    assert resolved.normalizer_version == "skill-normalizer-v1"
    assert resolved.normalization_rule_id.value == "ALIAS_REGISTRY_V1"


def test_the_version_is_the_one_the_slice_promises() -> None:
    assert SKILL_NORMALIZER_VERSION == "skill-normalizer-v1"


# --------------------------------------------------------------------------
# What the module refuses to contain
# --------------------------------------------------------------------------


def test_the_normalizer_infers_no_level_of_any_kind() -> None:
    lowered = (code_only(NORMALIZER_SOURCE) + code_only(MODELS_SOURCE)).casefold()

    for forbidden in (
        "level",
        "proficiency",
        "seniority",
        "confidence",
        "score",
        "expert",
        "beginner",
        "advanced",
        "years",
    ):
        assert forbidden not in lowered, forbidden


def test_the_normalizer_uses_no_similarity_and_no_stemming() -> None:
    lowered = code_only(NORMALIZER_SOURCE).casefold()

    for forbidden in (
        "levenshtein",
        "difflib",
        "get_close_matches",
        "sequencematcher",
        "fuzz",
        "stem",
        "lemmat",
        "similarity",
        "cosine",
        "tfidf",
        "tf_idf",
        "startswith",
        "endswith",
        "unidecode",
    ):
        assert forbidden not in lowered, forbidden


def test_the_normalizer_needs_no_network_and_no_model() -> None:
    lowered = code_only(NORMALIZER_SOURCE).casefold()

    for forbidden in ("http", "requests", "openai", "anthropic", "socket", "urllib"):
        assert forbidden not in lowered, forbidden
