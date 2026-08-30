"""The closed rules that read the languages a posting asks for.

The skill rules and these share their shape — cancel, local marker, section,
alternative group, each read over the **clause** a language belongs to rather
than over the whole sentence, so "English required and French preferred" is one
demand and one preference and "English not required but French required" leaves
English alone without losing French — and differ in three ways that all come
from what a language requirement actually is.

**A language is mentioned far more often than it is required.** A posting is
*written* in a language, sells to a market in one, ships documentation in one,
and none of that is a demand. So a language name is ignored outright in the
attributive positions where it describes something other than the applicant:

    work with English-speaking customers   -> nothing
    the French market                      -> nothing
    English documentation                  -> nothing
    native speakers                        -> nothing

and, more fundamentally, no language is ever inferred from a country, a city, a
company name, a nationality or the language of the advertisement itself. A
posting located in Paris has not required French, and a posting written in
English has not required English. There is no rule in this module that looks at
any of those things, because there is no field in `RequirementSource` holding
them.

**A level is kept as written, and never translated.** `B2` stays `B2`, `Fluent`
stays `Fluent`, `Native` stays `Native`. `Fluent` does not become `C1` and
`Native` does not become `C2`: those equivalences are conventions somebody
chose, and storing one would put a claim in the database that the employer did
not make. `profile_languages` keeps proficiency as written for exactly the same
reason, so both sides of the future comparison hold text somebody wrote.

**Two levels for one demand is a conflict about the level, not about the
language.** "English B2 required" beside "English C1 required" leaves English
`REQUIRED` — that part is not in dispute — and the level `NULL`, with a
`CONFLICTING_LANGUAGE_PROFICIENCY` ambiguity saying why. Picking B2 or C1 would
be this package settling a contradiction in somebody's advertisement.

One special case earns its own rule: **"bilingual X/Y" means both.** The slash
in `Python/R` is a choice; the slash in "bilingual English/French" is not,
because the word in front of it says the posting wants the two together.
`or` still wins over it — "bilingual English or French" is a choice, however
oddly phrased.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from services.collector.extractors.opportunity_constraints.models import (
    MAX_EVIDENCE_LENGTH,
)
from services.collector.extractors.opportunity_constraints.requirements.language_catalog import (
    LANGUAGE_CATALOG,
)
from services.collector.extractors.opportunity_constraints.requirements.matcher import (
    LANGUAGE_MATCHER,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    MAX_PROFICIENCY_LENGTH,
    AmbiguityReason,
    LanguageRequirement,
    RequirementAmbiguity,
    RequirementEvidence,
    RequirementKind,
    RequirementLevel,
    RequirementSegment,
    RequirementSourceField,
    stronger,
)
from services.collector.extractors.opportunity_constraints.requirements.signals import (
    clause_level,
    term_clauses,
    term_runs,
)
from services.collector.extractors.opportunity_constraints.text import shorten_evidence

__all__ = [
    "ALTERNATIVE_GROUP_RULE_ID",
    "PROFICIENCY_CONFLICT_RULE_ID",
    "read_language_requirements",
]

ALTERNATIVE_GROUP_RULE_ID = "LANGUAGE_ALTERNATIVE_GROUP_V1"
PROFICIENCY_CONFLICT_RULE_ID = "LANGUAGE_PROFICIENCY_CONFLICT_V1"

_NAMES = {term.language_key: term.language_name for term in LANGUAGE_CATALOG}

#: The positions in which a language name describes something other than the
#: applicant. Each is anchored on the words immediately around the name, so a
#: sentence that also states a real demand keeps it.
_ATTRIBUTIVE_AFTER = re.compile(
    r"^(?:\s*-\s*|\s+)?(?:speaking|speaker|speakers|market|markets|documentation|"
    r"docs|version|content|subtitles|localization|localisation|translation)\b",
    re.IGNORECASE,
)
_ATTRIBUTIVE_BEFORE = re.compile(
    r"\b(?:written\s+in|translated\s+into|localized\s+(?:in|into)|"
    r"r[ée]dig[ée]e?\s+en)\s*$",
    re.IGNORECASE,
)

#: "Bilingual English/French" — the two together, not a choice between them.
_BOTH_MARKER = re.compile(r"\b(?:bilingual|bilingue|trilingual|trilingue)\b", re.IGNORECASE)
#: An explicit `or` always beats the marker above.
_EXPLICIT_OR = re.compile(r"\b(?:or|ou)\b", re.IGNORECASE)

#: The Common European Framework codes, which postings write as codes. Stored
#: as written; compared case-insensitively, so `b2` and `B2` are one level.
_CEFR = r"[ABC][12]"
#: Levels postings write as words. `bilingual` is deliberately absent: it says
#: something about the pair of languages, not about a level in one of them.
_LEVEL_WORD = (
    r"(?:full\s+|limited\s+)?(?:professional|working|native|business|academic)"
    r"\s+(?:working\s+)?proficiency"
    r"|fluent|fluency|native|advanced|intermediate|beginner|basic|conversational"
    r"|mother\s+tongue"
    r"|courant|courante|natif|native|maternelle|langue\s+maternelle|avanc[ée]e?"
    r"|interm[ée]diaire|professionnel|professionnelle|scolaire|notions"
)
_LEVEL_AFTER = re.compile(
    rf"^\s*[:\-–(]?\s*(?P<level>{_CEFR}|{_LEVEL_WORD})\b", re.IGNORECASE
)
_LEVEL_BEFORE = re.compile(
    rf"(?P<level>{_CEFR}|{_LEVEL_WORD})\s+(?:level\s+)?(?:in\s+|of\s+|en\s+|de\s+|du\s+)?$",
    re.IGNORECASE,
)


def _attributive(text: str, start: int, end: int) -> bool:
    """Whether the language name at this span describes something else."""
    return bool(
        _ATTRIBUTIVE_AFTER.match(text[end:]) or _ATTRIBUTIVE_BEFORE.search(text[:start])
    )


def _proficiency(text: str, start: int, end: int) -> str | None:
    """The level written next to this language, as written, or None.

    Before the name first — "Fluent English", "Professional proficiency in
    English" — then after it — "English B2", "English: fluent". Nothing further
    away is read: a level three clauses down belongs to another sentence's
    language as often as to this one.
    """
    for pattern, window in (
        (_LEVEL_BEFORE, text[max(0, start - 60) : start]),
        (_LEVEL_AFTER, text[end : end + 60]),
    ):
        found = pattern.search(window)
        if found is not None:
            level = " ".join(found.group("level").split())
            if level and len(level) <= MAX_PROFICIENCY_LENGTH:
                return level
    return None


def read_language_requirements(
    segments: Sequence[RequirementSegment],
) -> tuple[tuple[LanguageRequirement, ...], tuple[RequirementAmbiguity, ...]]:
    """Every language the posting asks for, at the level it wrote, plus refusals.

    Requirements come back sorted by language key; ambiguities in the order the
    posting produced them, renumbered later by `extractor.py`.
    """
    observations: dict[
        str, list[tuple[RequirementLevel, str | None, str, str, str | None]]
    ] = {}
    refused: list[RequirementAmbiguity] = []

    for segment in segments:
        matches = [
            item
            for item in LANGUAGE_MATCHER.find(segment.text)
            if not _attributive(segment.text, item.start, item.end)
        ]
        if not matches:
            continue
        spans = [(item.start, item.end) for item in matches]
        runs = term_runs(spans, segment.text)
        fragment = shorten_evidence(segment.text, MAX_EVIDENCE_LENGTH)
        for run, (start, end) in zip(
            runs, term_clauses(spans, segment.text, runs), strict=True
        ):
            clause = segment.text[start:end]
            stated = clause_level(clause, segment.context)
            if stated is None:
                continue
            level, rule_id = stated
            # "Bilingual X/Y" is read in the clause that carries it, so a
            # neighbouring clause cannot lend its marker to an unrelated pair.
            both = bool(_BOTH_MARKER.search(clause)) and not _EXPLICIT_OR.search(clause)
            if run.alternative and not both:
                refused.append(
                    RequirementAmbiguity(
                        position=len(refused),
                        kind=RequirementKind.LANGUAGE,
                        reason=AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED,
                        rule_id=ALTERNATIVE_GROUP_RULE_ID,
                        text=fragment,
                        context_heading_text=segment.heading_text,
                    )
                )
                continue
            for index in run.indexes:
                match = matches[index]
                observations.setdefault(match.key, []).append(
                    (
                        level,
                        _proficiency(segment.text, match.start, match.end),
                        rule_id,
                        fragment,
                        segment.heading_text,
                    )
                )

    requirements: list[LanguageRequirement] = []
    for key in sorted(observations):
        seen = observations[key]
        level = seen[0][0]
        for observation in seen[1:]:
            level = stronger(level, observation[0])
        proficiency, conflict = _settle_proficiency(seen, level)
        if conflict is not None:
            refused.append(
                RequirementAmbiguity(
                    position=len(refused),
                    kind=RequirementKind.LANGUAGE,
                    reason=AmbiguityReason.CONFLICTING_LANGUAGE_PROFICIENCY,
                    rule_id=PROFICIENCY_CONFLICT_RULE_ID,
                    text=conflict[0],
                    context_heading_text=conflict[1],
                )
            )
        requirements.append(
            LanguageRequirement(
                language_key=key,
                language_name=_NAMES[key],
                requirement=level,
                proficiency_text=proficiency,
                evidence=tuple(
                    RequirementEvidence(
                        position=position,
                        source_field=RequirementSourceField.DESCRIPTION,
                        observed_requirement=observed,
                        rule_id=rule_id,
                        text=fragment,
                        context_heading_text=heading,
                        observed_proficiency_text=written,
                    )
                    for position, (
                        observed,
                        written,
                        rule_id,
                        fragment,
                        heading,
                    ) in enumerate(seen)
                ),
            )
        )
    return tuple(requirements), tuple(refused)


def _settle_proficiency(
    seen: Sequence[tuple[RequirementLevel, str | None, str, str, str | None]],
    level: RequirementLevel,
) -> tuple[str | None, tuple[str, str | None] | None]:
    """The level the projection keeps, and the conflict that stopped it, if any.

    Only the observations that stated the **projected** level are consulted. A
    posting demanding B2 and merely preferring C1 requires B2: the preference is
    real, it is kept as evidence, and it does not get to blur the demand.

    Comparison folds case, because `b2` and `B2` are the same code written two
    ways, while the text stored is still the first spelling the posting used.
    """
    written = [
        (item[1], item[3], item[4]) for item in seen if item[0] is level and item[1]
    ]
    if not written:
        return None, None
    distinct: dict[str, tuple[str, str, str | None]] = {}
    for text, fragment, heading in written:
        distinct.setdefault(text.casefold(), (text, fragment, heading))
    if len(distinct) == 1:
        return next(iter(distinct.values()))[0], None
    # Two incompatible levels for one demand. The language stays required; only
    # the level is unknowable, and the refusal points at the mention that made
    # it one.
    latest = list(distinct.values())[-1]
    return None, (latest[1], latest[2])
