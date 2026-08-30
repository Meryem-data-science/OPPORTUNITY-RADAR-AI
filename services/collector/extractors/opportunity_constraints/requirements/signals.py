"""The sentence-level signals both the skill rules and the language rules read.

Three questions are asked of every **clause**, always in this order, and the
order is the contract:

1. **does the sentence cancel the demand?** "No prior Python experience is
   required", "training will be provided", "SQL is not required". A cancelled
   sentence produces nothing at all — not a weaker requirement, not an
   ambiguity, nothing. It is the one signal that can override every other,
   because a rule that read `required` out of "no experience required" would
   invert the posting's meaning;
2. **does the sentence itself say how hard the demand is?** "Must have",
   "is required", "is a plus", "preferred". A local marker outranks the section
   it sits in: an item written "Python preferred" under "Required
   Qualifications" is preferred, and reading the heading instead would promote
   a preference into a demand;
3. **what does the section say?** `REQUIRED` and `PREFERRED` sections lend
   their level to the items listed under them. `NEUTRAL` lends nothing, so a
   sentence with no local marker in a responsibilities or stack section states
   no requirement.

If none of the three answers, the clause states no requirement. That is the
default and it is meant to be: this package would rather record nothing than
record a demand somebody has to disprove later.

**A clause, and not the whole sentence.** This is what `v2` fixes and it was a
real defect. One sentence can hold two demands of different strength:

    Python required and Spark preferred.
    Python preferred and SQL required.
    No Python experience required, but SQL is required.

Reading the markers over the whole sentence made the first two produce two
`PREFERRED` rows, promoted nothing and demoted a real requirement; and it let
one skill's cancelling clause delete a demand stated about another skill
entirely. `term_clauses` below cuts the sentence at the connectors *between*
terms, so each group of terms is scored against its own words.

The cut is deliberately conservative: it happens only in the text **between**
two matched terms, and only at a connector, so a subordinate clause with no
term in it never splits anything. When one clause still carries both marks —
"Python required and preferred", which nobody writes — the weaker one wins,
because over-claiming is the failure mode that costs a real candidate a real
opportunity in Phase 3.6.

The fourth thing this module knows is **how a posting joins two terms**. `and`
is a conjunction and gives two requirements; `or` is a choice and gives none,
because storing both would turn the employer's alternative into two
obligations. A bare comma is neither on its own: it inherits the run it belongs
to, so "Python, SQL and Spark" is three demands while "Python, R or Julia" is
one choice among three. Runs joined by a bare connector share one clause, which
is exactly why "Python and SQL required" still yields two requirements: the
marker sits at the end of a clause that both terms belong to.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from services.collector.extractors.opportunity_constraints.requirements.models import (
    RequirementLevel,
    SectionContext,
)

__all__ = [
    "CANCELLING_RULE_ID",
    "LOCAL_PREFERRED_RULE_ID",
    "LOCAL_REQUIRED_RULE_ID",
    "Link",
    "SECTION_PREFERRED_RULE_ID",
    "SECTION_REQUIRED_RULE_ID",
    "TermRun",
    "cancels_requirement",
    "clause_level",
    "link_between",
    "term_clauses",
    "term_runs",
]

#: The rule ids stored on evidence, so an audit reads *why* a fragment was
#: taken as a demand without re-running anything.
#:
#: The three local rules are `_V2` because their contract genuinely changed:
#: they used to be scored over a whole sentence and are now scored over the
#: clause the term belongs to. A stored `_V1` row was produced by a rule that
#: could read a marker belonging to another skill, and leaving the id alone
#: would have made the two indistinguishable. The two section rules are `_V1`
#: still: a heading meant the same thing before and means it now.
LOCAL_REQUIRED_RULE_ID = "REQUIREMENT_LOCAL_REQUIRED_V2"
LOCAL_PREFERRED_RULE_ID = "REQUIREMENT_LOCAL_PREFERRED_V2"
SECTION_REQUIRED_RULE_ID = "REQUIREMENT_SECTION_REQUIRED_V1"
SECTION_PREFERRED_RULE_ID = "REQUIREMENT_SECTION_PREFERRED_V1"
CANCELLING_RULE_ID = "REQUIREMENT_CANCELLED_V2"

#: Sentences that state the absence of a demand, or promise to supply the
#: skill. Each is anchored on words a posting actually writes; none of them
#: guesses from tone.
_CANCELLING = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # "No prior Python experience required", "no experience necessary"
        r"\bno\b[^.;]{0,60}\b(?:required|necessary|needed|expected)\b",
        r"\bnot\s+(?:be\s+)?(?:required|necessary|needed|mandatory|expected)\b",
        r"\bdo(?:es)?\s+not\s+require\b",
        r"\bno\s+(?:prior\s+|previous\s+)?experience\b",
        r"\bwithout\s+(?:prior\s+|previous\s+)?experience\b",
        # "Training in Python will be provided", "we will train you"
        r"\btraining\b[^.;]{0,60}\b(?:provided|offered|available)\b",
        r"\bwill\s+be\s+(?:trained|taught)\b",
        r"\bwe(?:'ll| will)?\s+(?:will\s+)?(?:train|teach)\b",
        r"\byou\s+will\s+learn\b",
        r"\bopportunity\s+to\s+learn\b",
        r"\bno\s+need\s+(?:for|to)\b",
        # French
        r"\bpas\s+(?:d[eu']\s*)?(?:exp[ée]rience|pr[ée]requis)\b",
        r"\bn'est\s+pas\s+(?:requis|obligatoire|n[ée]cessaire|exig[ée])\b",
        r"\bne\s+sont\s+pas\s+(?:requis|obligatoires|n[ée]cessaires|exig[ée]s)\b",
        r"\baucune?\s+exp[ée]rience\b",
        r"\bformation\s+(?:assur[ée]e|fournie|dispens[ée]e)\b",
    )
)

#: Sentence-level statements that a demand is firm.
_LOCAL_REQUIRED = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bmust\s+(?:have|possess|be\s+able|demonstrate|bring|know)\b",
        r"\byou\s+must\b",
        r"\bis\s+(?:a\s+)?(?:must|requirement)\b",
        r"\b(?:is|are|were)\s+required\b",
        r"\brequired\b",
        r"\brequirements?\s*:",
        r"\bwe\s+require\b",
        r"\brequires?\s+(?:strong|solid|proven|demonstrated|hands-on)\b",
        r"\bmandatory\b",
        r"\bessential\b",
        r"\bproficiency\s+(?:in|with)\b",
        r"\bproficient\s+(?:in|with)\b",
        r"\bstrong\s+(?:command|knowledge|grasp)\s+of\b",
        r"\bexpertise\s+(?:in|with)\s+.{0,40}\bis\s+expected\b",
        # French
        r"\b(?:est|sont)\s+(?:requis|requise|requises|exig[ée]s?|obligatoires?)\b",
        r"\bma[îi]trise\s+(?:de|du|des|d')\b",
        r"\bindispensables?\b",
        r"\bimp[ée]ratif\b",
        r"\bobligatoires?\b",
    )
)

#: Sentence-level statements that a demand is a preference.
_LOCAL_PREFERRED = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bpreferred\b",
        r"\bpreferable\b",
        r"\bwe\s+prefer\b",
        r"\bis\s+(?:a\s+)?plus\b",
        r"\bare\s+(?:a\s+)?plus\b",
        r"\ba\s+(?:big\s+|strong\s+|major\s+)?plus\b",
        # The copula is required on purpose: "asset management" is a domain,
        # "is a significant asset" is a preference. The corpus writes both.
        r"\b(?:is|are|would\s+be)\s+(?:an?\s+)?(?:significant\s+|strong\s+|real\s+|"
        r"major\s+|considerable\s+|definite\s+|distinct\s+|clear\s+)?assets?\b",
        r"\bnice\s+to\s+have\b",
        r"\bnice-to-have\b",
        r"\bgood\s+to\s+have\b",
        r"\bbonus\b",
        r"\bdesirable\b",
        r"\bdesired\b",
        r"\badvantageous\b",
        r"\bappreciated\b",
        r"\bideally\b",
        r"\bwould\s+be\s+(?:great|ideal|appreciated|welcome)\b",
        r"\boptional\b",
        # French
        r"\b(?:est|sont)\s+un\s+(?:vrai\s+|r[ée]el\s+)?plus\b",
        r"\bun\s+atout\b",
        r"\batouts?\b",
        r"\bappr[ée]ci[ée]e?s?\b",
        r"\bsouhait[ée]e?s?\b",
        r"\bid[ée]alement\b",
    )
)


def cancels_requirement(text: str) -> bool:
    """Whether the clause states the absence of a demand, or promises to teach."""
    return any(pattern.search(text) for pattern in _CANCELLING)


def clause_level(
    text: str, context: SectionContext
) -> tuple[RequirementLevel, str] | None:
    """The level this clause states, and the rule that said so, or None.

    `text` is one clause of one segment — the words `term_clauses` decided
    belong to the terms being scored — never the whole posting and, since `v2`,
    never the whole sentence when the sentence holds more than one demand.

    `None` means "no requirement", which is the answer for every clause that
    neither carries a marker nor sits in a demanding section — the "our stack
    includes…" case, and the "you will build pipelines using…" case.
    """
    if cancels_requirement(text):
        return None
    preferred = any(pattern.search(text) for pattern in _LOCAL_PREFERRED)
    required = any(pattern.search(text) for pattern in _LOCAL_REQUIRED)
    if preferred:
        # The weaker claim wins when one sentence carries both marks; see the
        # module docstring.
        return RequirementLevel.PREFERRED, LOCAL_PREFERRED_RULE_ID
    if required:
        return RequirementLevel.REQUIRED, LOCAL_REQUIRED_RULE_ID
    if context is SectionContext.REQUIRED:
        return RequirementLevel.REQUIRED, SECTION_REQUIRED_RULE_ID
    if context is SectionContext.PREFERRED:
        return RequirementLevel.PREFERRED, SECTION_PREFERRED_RULE_ID
    return None


class Link(StrEnum):
    """How a posting joined two terms it named next to each other.

    `OR` and `SLASH` are separate members, and the corpus is what separated
    them. A written `or` says the posting will accept either term. A bare `/`
    says far less than that: `AI/ML`, `ML/LLM` and `AI/ML engineering` are
    lexical compounds naming one field, not offers to accept either half, and
    `v2` read all of them as choices and refused perfectly ordinary sentences
    over it. Reading them the other way round would be worse — `AI/ML` is not
    two obligations either — so the two links exist to be handled differently,
    and neither ever becomes an `AND`.
    """

    #: "Python and SQL" — two demands.
    AND = "AND"
    #: "Python or R", "and/or", "|" — one demand, satisfied several ways.
    OR = "OR"
    #: "AI/ML", "Python/R" — a bare slash, which says less than an `or`.
    SLASH = "SLASH"
    #: A bare comma. Neither on its own; it inherits the run it belongs to.
    LIST = "LIST"
    #: Anything else, including ordinary words between the two terms.
    NONE = "NONE"


#: `and/or` and `et/ou` are listed **before** the bare words, and they stay in
#: the `OR` gap rather than falling through to `SLASH`: a posting writing
#: `and/or` has said the word, whatever punctuation it wrapped it in.
_OR_GAP = re.compile(
    r"^(?:,\s*)?(?:and\s*/\s*or|et\s*/\s*ou|or|ou|\|)$", re.IGNORECASE
)
_SLASH_GAP = re.compile(r"^/$")
_AND_GAP = re.compile(r"^(?:,\s*)?(?:and|et|&|\+)$", re.IGNORECASE)
_LIST_GAP = re.compile(r"^,$")


def link_between(gap: str) -> Link:
    """Classify the text a posting wrote between two catalogue terms.

    Only the connector itself counts. `Python, which we use daily, and SQL` has
    a gap full of prose, so the two terms are simply two separate mentions
    rather than a joined pair — which is the conservative reading, and the one
    that cannot turn a subordinate clause into an alternative group.
    """
    collapsed = " ".join(gap.split())
    if _OR_GAP.fullmatch(collapsed):
        return Link.OR
    if _SLASH_GAP.fullmatch(collapsed):
        return Link.SLASH
    if _AND_GAP.fullmatch(collapsed):
        return Link.AND
    if _LIST_GAP.fullmatch(collapsed):
        return Link.LIST
    return Link.NONE


_ALTERNATIVE_OPENER = re.compile(
    r"\b(?:one\s+of|any\s+of|at\s+least\s+one\s+of|either|l'un\s+de|l'une\s+de|"
    r"au\s+moins\s+l'un)\b",
    re.IGNORECASE,
)
#: An `or` immediately after a run — "Python or Julia" where the catalogue does
#: not know Julia. The choice is still a choice even when only one side of it is
#: a term this package can name.
#:
#: A bare slash is deliberately **not** in these: it has its own pair below, so
#: that `AI/ML` is never classified by a rule whose name claims the posting
#: wrote `or`.
_TRAILING_OR = re.compile(
    r"^\s*(?:,\s*)?(?:and\s*/\s*or|et\s*/\s*ou|or|ou)\b", re.IGNORECASE
)
_LEADING_OR = re.compile(
    r"(?:\band\s*/\s*or|\bet\s*/\s*ou|\bor|\bou)\s*$", re.IGNORECASE
)

#: A bare slash immediately outside a run — "Python/Julia" where the catalogue
#: does not know Julia. It is not an `or`, and it is not an `and` either, so the
#: run is marked slashed and the caller decides what a slash means there.
_TRAILING_SLASH = re.compile(r"^\s*/\s*\w")
_LEADING_SLASH = re.compile(r"\w\s*/\s*$")


@dataclass(frozen=True)
class TermRun:
    """A group of terms one sentence joined, and how it joined them.

    `alternative` is true when the posting offered several ways to satisfy one
    demand **in words**. A run of one term can be alternative too: "Python or
    Julia required" names a choice whose other side is not in the catalogue, and
    storing Python as a hard requirement would still be reading an OR as an AND.

    `slashed` is true when the only thing joining the terms was a bare `/`. That
    is reported separately because a slash is genuinely less informative than an
    `or`: `AI/ML` names one field, `Python/R` probably names a choice, and the
    caller — not this module — holds the closed registry that tells them apart.
    An explicit `or` anywhere in the run wins, so a run is never both.
    """

    indexes: tuple[int, ...]
    alternative: bool
    #: Defaulted so a caller that only cares about choices still reads clearly.
    slashed: bool = False


def term_runs(
    spans: Sequence[tuple[int, int]], text: str
) -> tuple[TermRun, ...]:
    """Group the terms a sentence joined, and mark the groups that are choices.

    `and` and unrelated prose **break** a run, so each side stands alone and
    each becomes its own requirement. `or` and a bare comma **continue** one.

    A run is a choice when any of four things is true: a connector inside it was
    an `or`; the sentence opened with `one of`, `either` or `any of`, which
    makes every term in it an alternative whatever the connectors look like; an
    `or` sits immediately before the run; or an `or` sits immediately after it.
    The last two matter because a posting is under no obligation to choose
    between technologies this catalogue knows — "one of Python, R or Julia"
    ends in a term that is not in it, and the choice is a choice regardless.

    A run is **slashed** when a bare `/` joined it — inside, or immediately
    outside, so that `Python/Julia` stays as guarded as `Python or Julia` even
    though the catalogue knows only one half. Slashed is not a choice and not a
    conjunction; it is "the posting used a slash", and what a slash means is the
    caller's closed decision.

    "Python, SQL and Spark" is therefore three requirements — the comma joins
    the first two into a run with no `or` in it, so its members stand alone
    anyway — while "Python, R or Julia" is one refused choice.
    """
    if not spans:
        return ()
    runs: list[list[int]] = [[0]]
    has_or: list[bool] = [False]
    has_slash: list[bool] = [False]
    for index in range(1, len(spans)):
        link = link_between(text[spans[index - 1][1] : spans[index][0]])
        if link in (Link.OR, Link.SLASH, Link.LIST):
            runs[-1].append(index)
            has_or[-1] = has_or[-1] or link is Link.OR
            has_slash[-1] = has_slash[-1] or link is Link.SLASH
            continue
        runs.append([index])
        has_or.append(False)
        has_slash.append(False)

    opened = bool(_ALTERNATIVE_OPENER.search(text))
    grouped: list[TermRun] = []
    for members, or_flag, slash_flag in zip(runs, has_or, has_slash, strict=True):
        before = text[: spans[members[0]][0]]
        after = text[spans[members[-1]][1] :]
        alternative = (
            opened
            or or_flag
            or bool(_LEADING_OR.search(before))
            or bool(_TRAILING_OR.search(after))
        )
        slashed = slash_flag or bool(
            _LEADING_SLASH.search(before) or _TRAILING_SLASH.search(after)
        )
        grouped.append(
            TermRun(
                indexes=tuple(members),
                alternative=alternative,
                # A written `or` outranks punctuation: "AI/ML or Python" is a
                # choice, whatever the slash was doing inside it.
                slashed=slashed and not alternative,
            )
        )
    return tuple(grouped)


#: What separates two independent statements inside one sentence. Looked for
#: **only in the text between two matched terms**, so a connector buried in a
#: subordinate clause that mentions no technology never cuts anything.
_CLAUSE_CONNECTOR = re.compile(
    r"[,;:]|\b(?:and|but|or|while|whereas|though|although|yet|however|plus|"
    r"et|mais|ou|mais\s+aussi|mais\s+[ée]galement|alors\s+que|tandis\s+que|"
    r"cependant|toutefois)\b",
    re.IGNORECASE,
)


def term_clauses(
    spans: Sequence[tuple[int, int]], text: str, runs: Sequence[TermRun]
) -> tuple[tuple[int, int], ...]:
    """One `(start, end)` window per run: the words that run's level is read from.

    Returns a tuple parallel to `runs`. A segment holding a single run gets the
    whole segment, which is what makes every one-demand sentence read exactly as
    it did before `v2`.

    Two rules decide the windows, and both are conservative.

    **Runs joined by a bare connector share one window.** `term_runs` already
    split on `and` and on prose; a gap that `link_between` still recognises as a
    connector — `and`, `or`, a bare comma — means the posting was listing, not
    changing subject. So "Python and SQL required" is one clause and the trailing
    marker reaches both terms, exactly as it must.

    **Otherwise the gap is cut at its first connector.** "Python required and
    Spark preferred" has prose between the two terms, so it is two clauses, and
    the cut at `and` gives `Python required` its own words and
    `Spark preferred` its own. With no connector in the gap at all the whole gap
    stays with the clause on the left, because a marker trails the term it
    qualifies far more often than it leads one.

    Nothing here looks outside the sentence, and nothing invents a boundary
    where a posting wrote none: the first window always starts at the beginning
    of the segment and the last always ends at its end, so no words are lost.
    """
    if not runs:
        return ()
    boundaries: list[int] = []
    groups: list[list[int]] = [[0]]
    for index in range(1, len(runs)):
        gap_start = spans[runs[index - 1].indexes[-1]][1]
        gap_end = spans[runs[index].indexes[0]][0]
        if link_between(text[gap_start:gap_end]) is not Link.NONE:
            # A bare connector: still one list, still one clause.
            groups[-1].append(index)
            continue
        groups.append([index])
        found = _CLAUSE_CONNECTOR.search(text, gap_start, gap_end)
        boundaries.append(gap_end if found is None else found.start())

    windows: list[tuple[int, int]] = []
    for position, members in enumerate(groups):
        start = 0 if position == 0 else boundaries[position - 1]
        end = len(text) if position == len(groups) - 1 else boundaries[position]
        for _ in members:
            windows.append((start, end))
    return tuple(windows)
