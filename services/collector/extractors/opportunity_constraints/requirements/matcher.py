"""Finding catalogue terms in a sentence without finding them where they are not.

Substring search is the wrong tool for this and it fails in ways that are easy
to demonstrate and hard to notice in production:

* `R` appears inside every word containing the letter;
* `Go` appears inside `Google`;
* `SQL` appears inside `PostgreSQL`, so a posting requiring Postgres would be
  read as also requiring SQL — a requirement nobody wrote;
* `Spark` appears inside `PySpark`, so one technology would produce two;
* `C` appears inside `C++` and `C#`, which are three different languages.

So a match here has to satisfy three rules at once.

**Token boundaries that know about punctuation.** A term may neither start nor
end adjacent to a letter, a digit, `_`, `+`, `#` or `&`. `+` and `#` are in
that set because they belong to the names `C++` and `C#`: `C` immediately
followed by `+` is not the language `C`. `&` is there because `R&D` is not the
language `R`. A trailing `.` or `,` is fine — a sentence has to end somewhere.

**Longest alias first, and no span reused.** Aliases are sorted by descending
length and joined into one alternation, so at any position the longest
catalogue spelling wins: `Apache Spark` is matched as Apache Spark and never as
Spark, and scanning resumes after the match, so a shorter alias can never claim
part of a span a longer one already took. `PySpark` therefore produces PySpark
alone; Spark appears only where the posting wrote it separately.

**One- and two-character aliases are matched case-sensitively.** `R`, `C` and
`Go` are real catalogue entries and also ordinary English: `go` is a verb, `c`
is a letter. Requiring the capitalisation the catalogue uses is the cheapest
guard that keeps "we go fast" out of the projection, and it costs only the
postings that shout their requirements in lower case. Everything longer stays
case-insensitive, because `PYTHON`, `Python` and `python` are one technology.

The whole alternation is compiled **once**, at import time. Matching a posting
is then one pass of one regex over each segment, not one pass per catalogue
entry, and adding a term costs nothing at scan time.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from services.collector.extractors.opportunity_constraints.requirements.language_catalog import (
    LANGUAGE_CATALOG,
    LanguageTerm,
)
from services.collector.extractors.opportunity_constraints.requirements.skill_catalog import (
    SKILL_CATALOG,
    SkillTerm,
)

__all__ = [
    "LANGUAGE_MATCHER",
    "SKILL_MATCHER",
    "TermMatch",
    "TermMatcher",
    "mentions_catalogue_term",
]

#: Characters a term may not touch on either side. Letters and digits are the
#: ordinary word boundary; `+`, `#` and `&` are here because `C++`, `C#` and
#: `R&D` exist and each of them would otherwise swallow, or be swallowed by, a
#: shorter catalogue entry.
_BOUNDARY = r"A-Za-z0-9_+#&"
_LEFT = f"(?<![{_BOUNDARY}])"
_RIGHT = f"(?![{_BOUNDARY}])"

#: At or below this length, an alias must be written exactly as catalogued.
#: See the module docstring: `go`, `c` and `r` are ordinary prose.
_CASE_SENSITIVE_LENGTH = 2


@dataclass(frozen=True)
class TermMatch:
    """One catalogue term found in one piece of text, and where."""

    key: str
    start: int
    end: int
    #: The words as the posting wrote them, for evidence.
    text: str


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


class TermMatcher:
    """One compiled alternation over one catalogue's aliases.

    `find` returns non-overlapping matches in the order they appear, at most one
    per span of text. A caller never has to deduplicate an overlap, because an
    overlap cannot be produced.
    """

    def __init__(self, aliases_by_key: dict[str, tuple[str, ...]]) -> None:
        patterns: list[str] = []
        # Longest first so the alternation's leftmost-first semantics become
        # longest-match-at-this-position. Ties broken by the alias itself, so
        # the compiled pattern is identical from one process to the next.
        flattened = sorted(
            (
                (alias, key)
                for key, aliases in aliases_by_key.items()
                for alias in aliases
            ),
            key=lambda pair: (-len(pair[0]), pair[0], pair[1]),
        )
        self._keys: list[str] = []
        for index, (alias, key) in enumerate(flattened):
            group = f"t{index}"
            body = re.escape(_normalized(alias))
            if len(alias) > _CASE_SENSITIVE_LENGTH:
                body = f"(?i:{body})"
            patterns.append(f"(?P<{group}>{_LEFT}{body}{_RIGHT})")
            self._keys.append(key)
        # No IGNORECASE on the whole pattern: case-insensitivity is opted into
        # alias by alias, so a short alias cannot be lower-cased into prose.
        self._pattern = re.compile("|".join(patterns)) if patterns else None

    def find(self, text: str) -> tuple[TermMatch, ...]:
        """Every catalogue term in `text`, non-overlapping, in reading order."""
        if self._pattern is None or not text:
            return ()
        found: list[TermMatch] = []
        for match in self._pattern.finditer(text):
            index = match.lastindex
            if index is None:
                continue
            found.append(
                TermMatch(
                    key=self._keys[index - 1],
                    start=match.start(),
                    end=match.end(),
                    text=match.group(),
                )
            )
        return tuple(found)


def _skill_aliases(catalog: tuple[SkillTerm, ...]) -> dict[str, tuple[str, ...]]:
    return {term.canonical_key: term.aliases for term in catalog}


def _language_aliases(catalog: tuple[LanguageTerm, ...]) -> dict[str, tuple[str, ...]]:
    return {term.language_key: term.aliases for term in catalog}


#: Compiled once, at import time.
SKILL_MATCHER = TermMatcher(_skill_aliases(SKILL_CATALOG))
LANGUAGE_MATCHER = TermMatcher(_language_aliases(LANGUAGE_CATALOG))


def mentions_catalogue_term(text: str) -> bool:
    """Whether a line names any technology or language this package knows.

    Used by the section parser to refuse to read `Python` as a section heading.
    A line naming a technology is an item somebody listed, not a title.
    """
    return bool(SKILL_MATCHER.find(text) or LANGUAGE_MATCHER.find(text))
