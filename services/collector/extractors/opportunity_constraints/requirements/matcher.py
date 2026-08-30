r"""Finding catalogue terms in a sentence without finding them where they are not.

Substring search is the wrong tool for this and it fails in ways that are easy
to demonstrate and hard to notice in production:

* `R` appears inside every word containing the letter;
* `Go` appears inside `Google`;
* `SQL` appears inside `PostgreSQL`, so a posting requiring Postgres would be
  read as also requiring SQL — a requirement nobody wrote;
* `Spark` appears inside `PySpark`, so one technology would produce two;
* `C` appears inside `C++` and `C#`, which are three different languages.

So a match here has to satisfy three rules at once.

**Token boundaries that know about punctuation, in every script.** A term may
neither start nor end adjacent to a letter, a digit, `_`, a combining mark,
`+`, `#` or `&`.

Letters and digits are `\w`, which in Python 3 is **Unicode-aware**, and that
is load-bearing rather than incidental. An ASCII-only boundary — the
`[A-Za-z0-9_]` this module used first — leaves every accented letter outside
the class, so `é` reads as a word boundary and the one-letter alias `R` matches
the start of `Réseaux`, `Régression` and `Réalisation`. The corpus is partly in
French, `R` is a real technology, and a false `R` under a `Required
Qualifications` heading becomes a hard requirement nobody wrote. `C` and
`Câblage` are the same bug, and so is `C` and `Cœur`.

Combining marks are in the class for the same reason one step further down. A
posting need not be normalized: `Ça` may arrive as `C` followed by U+0327
COMBINING CEDILLA rather than as the single character `Ç`, and a boundary that
only knew about letters would see `C` followed by something that is not a
letter and call it a standalone `C`. The ranges below cover the combining marks
of the scripts this corpus can plausibly contain — Latin, Greek, Cyrillic,
Hebrew and Arabic — plus the general-purpose combining blocks.

`+` and `#` are in the set because they belong to the names `C++` and `C#`: `C`
immediately followed by `+` is not the language `C`. `&` is there because `R&D`
is not the language `R`. A trailing `.` or `,` is fine — a sentence has to end
somewhere — and so is `/`, which is why `CI/CD` and `Python/R` both read
correctly.

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

#: The combining-mark ranges a term may not touch. A combining mark belongs to
#: the character in front of it, so a catalogue alias sitting next to one is
#: inside a word, not beside it — `C` + U+0327 is `Ç`, whatever the posting's
#: normalization form happens to be.
#:
#: Latin/Greek/Cyrillic diacritics, the two extension blocks, Cyrillic, Hebrew,
#: Arabic, the marks for symbols, and the half marks. Written as explicit
#: ranges rather than derived from the Unicode database at import time, so the
#: compiled pattern is a constant and costs nothing to build.
_COMBINING_RANGES = (
    "\u0300-\u036f",  # Combining Diacritical Marks
    "\u0483-\u0489",  # Cyrillic
    "\u0591-\u05bd\u05bf\u05c1-\u05c2\u05c4-\u05c5\u05c7",  # Hebrew
    "\u0610-\u061a\u064b-\u065f\u0670",  # Arabic
    "\u06d6-\u06dc\u06df-\u06e4\u06e7-\u06e8\u06ea-\u06ed",  # Arabic
    "\u0711\u0730-\u074a",  # Syriac
    "\u1ab0-\u1aff",  # Combining Diacritical Marks Extended
    "\u1dc0-\u1dff",  # Combining Diacritical Marks Supplement
    "\u20d0-\u20f0",  # Combining Diacritical Marks for Symbols
    "\ufe20-\ufe2f",  # Combining Half Marks
)

#: Characters a term may not touch on either side. `\w` is Unicode-aware in
#: Python 3, so every letter and digit of every script is a boundary — see the
#: module docstring for why an ASCII-only class was a real defect and not a
#: theoretical one. `+`, `#` and `&` are here because `C++`, `C#` and `R&D`
#: exist and each would otherwise swallow, or be swallowed by, a shorter
#: catalogue entry.
_BOUNDARY = r"\w+#&" + "".join(_COMBINING_RANGES)
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
