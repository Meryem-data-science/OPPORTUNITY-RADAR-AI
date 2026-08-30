"""Reading a posting's shape: which section each sentence sits in.

This is the heart of 3.5B. Everything else — the catalogues, the matcher, the
skill and language rules — depends on being able to say *where* a term was
written, because the same word means different things in different sections:

    Required Qualifications          Our stack
    - Python                         - Python

The left one is a demand. The right one is a description of a company. Without
sections, both are "the posting contains Python", and the only ways to proceed
are to store both (a projection full of requirements nobody wrote) or neither
(a projection that misses the plainest requirement there is).

The normalized text comes from 3.5A's `normalize_description`, unchanged. There
is deliberately no second HTML cleaner here: two cleaners disagreeing about
what a `</li>` means would give the two phases two different readings of one
posting, and the bug would surface as a requirement that exists in one table
and not in the other.

**How a line is classified.**

1. It is stripped of its bullet marker and any trailing colon;
2. if what remains is *exactly* one of the known headings, the context becomes
   that heading's and the line is a heading, not an item. Exactly, and not "the
   line starts with" — `Requirements: 3 years of Python` is a sentence stating
   a requirement, not a section title, and reading it as a heading would throw
   away the sentence;
3. otherwise, if the line is heading-*shaped* and names no catalogue term, the
   context resets to `NEUTRAL`. This is the leak guard: a posting with an
   unrecognised heading between "Required Qualifications" and a list of
   technologies must not project that list as demands;
4. otherwise the line is content, split into sentences by 3.5A's `segments`,
   and each sentence becomes a `RequirementSegment` carrying the current
   context and the current heading.

**Why a heading-shaped line must name no catalogue term.** A one-word bullet is
heading-shaped by every structural measure: short, capitalised, no final
punctuation. `Python` under `Required Qualifications` is exactly that, and a
parser without this guard would read it as a section title and lose the most
common way a posting states a requirement. A line naming a technology or a
language is an item somebody listed. The known-heading table is consulted
first, so `Tech Stack` and `Our stack` still classify as headings.

**Unknown headings reset to NEUTRAL rather than persisting the last context**,
because the failure modes are not symmetric: persisting `REQUIRED` invents
demands, resetting loses them. This package would rather miss a requirement
than manufacture one.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable

from services.collector.extractors.opportunity_constraints.models import (
    MAX_EVIDENCE_LENGTH,
)
from services.collector.extractors.opportunity_constraints.requirements.matcher import (
    mentions_catalogue_term,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    RequirementSegment,
    SectionContext,
)
from services.collector.extractors.opportunity_constraints.text import (
    SEGMENT_SEPARATOR,
    normalize_description,
    segments,
    shorten_evidence,
)

__all__ = [
    "HEADINGS",
    "NEUTRAL_HEADINGS",
    "PREFERRED_HEADINGS",
    "REQUIRED_HEADINGS",
    "heading_context",
    "looks_like_heading",
    "parse_requirement_segments",
    "strip_bullet",
]

#: Headings under which a listed item is something the posting demands.
#:
#: Not every heading containing "skills" is here, and that is deliberate:
#: `Preferred Skills` is a preference and `Skills you'll build` is a promise.
#: The word is not the signal; the whole heading is.
REQUIRED_HEADINGS: tuple[str, ...] = (
    "requirements",
    "requirement",
    "required qualifications",
    "required skills",
    "required experience",
    "minimum qualifications",
    "minimum requirements",
    "basic qualifications",
    "qualifications",
    "must have",
    "must haves",
    "must-have",
    "must-haves",
    "what we're looking for",
    "what we are looking for",
    "what you'll bring",
    "what you will bring",
    "what you bring",
    "who you are",
    "your profile",
    "profil recherché",
    "profil recherche",
    "votre profil",
    "prérequis",
    "prerequis",
    "prerequisites",
    "compétences requises",
    "competences requises",
    "compétences techniques requises",
    "qualifications requises",
    "exigences",
    "technical requirements",
    "skills and qualifications",
    "skills & qualifications",
    "experience and qualifications",
    "education and experience",
)

#: Headings under which a listed item is something the posting would like.
PREFERRED_HEADINGS: tuple[str, ...] = (
    "preferred qualifications",
    "preferred skills",
    "preferred experience",
    "preferred",
    "nice to have",
    "nice to haves",
    "nice-to-have",
    "nice-to-haves",
    "good to have",
    "bonus",
    "bonus points",
    "bonus skills",
    "desired qualifications",
    "desired skills",
    "desirable",
    "desirables",
    "a plus",
    "pluses",
    "atouts",
    "atout",
    "compétences appréciées",
    "competences appreciees",
    "un plus",
    "optional",
    "optional skills",
)

#: Headings under which a listed item is a description, not a demand. A new
#: neutral section **ends** whatever required or preferred section preceded it:
#: that is what stops "Responsibilities: build pipelines with Spark" from
#: inheriting the requirements list above it.
NEUTRAL_HEADINGS: tuple[str, ...] = (
    "responsibilities",
    "responsibility",
    "key responsibilities",
    "your responsibilities",
    "what you'll do",
    "what you will do",
    "what you'll be doing",
    "what you will be doing",
    "your mission",
    "mission",
    "missions",
    "vos missions",
    "about the role",
    "about this role",
    "about the job",
    "the role",
    "about us",
    "about the company",
    "about",
    "who we are",
    "notre entreprise",
    "à propos",
    "a propos",
    "benefits",
    "perks",
    "perks and benefits",
    "what we offer",
    "ce que nous offrons",
    "nous offrons",
    "compensation",
    "compensation and benefits",
    "salary",
    "salaire",
    "our stack",
    "tech stack",
    "technical stack",
    "technology stack",
    "technologies",
    "technology",
    "our technologies",
    "environnement technique",
    "stack technique",
    "how to apply",
    "application process",
    "interview process",
    "hiring process",
    "equal opportunity employer",
    "diversity and inclusion",
    "location",
    "contract",
    "contract type",
    "type de contrat",
    "duration",
    "durée",
    "duree",
    "start date",
    "date de début",
    "date de debut",
    "team",
    "the team",
    "notre équipe",
    "notre equipe",
    "why join us",
    "pourquoi nous rejoindre",
)


def _folded(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _build_headings() -> dict[str, SectionContext]:
    """Index every heading by its folded form, refusing a phrase claimed twice."""
    indexed: dict[str, SectionContext] = {}
    for phrases, context in (
        (REQUIRED_HEADINGS, SectionContext.REQUIRED),
        (PREFERRED_HEADINGS, SectionContext.PREFERRED),
        (NEUTRAL_HEADINGS, SectionContext.NEUTRAL),
    ):
        for phrase in phrases:
            key = _folded(phrase)
            known = indexed.get(key)
            if known is not None and known is not context:
                raise ValueError(
                    f"heading {phrase!r} is claimed by {known.value} and {context.value}"
                )
            indexed[key] = context
    return indexed


#: Every known heading, folded, resolved once at import time.
HEADINGS: dict[str, SectionContext] = _build_headings()

#: Leading list markers a collected description keeps once its markup is gone.
_BULLET = re.compile(r"^\s*(?:[-–—•*·▪◦]+|\(?\d{1,2}[.)])\s*")
_TRAILING_COLON = re.compile(r"\s*[:：]\s*$")
#: A line ending in one of these is a sentence, whatever its length.
_SENTENCE_END = re.compile(r"[.!?,;]$")

#: An unknown line has to be at least this title-like before it is allowed to
#: end a section. Both bounds are deliberately tight: the cost of treating a
#: sentence as a heading is a lost requirement, and the cost of treating a
#: heading as a sentence is an invented one.
_MAX_HEADING_WORDS = 7
_MAX_HEADING_CHARS = 60


def strip_bullet(line: str) -> str:
    """The line without its list marker. Nothing else is removed."""
    return _BULLET.sub("", line).strip()


def heading_context(line: str) -> SectionContext | None:
    """The context a line declares as a known heading, or None.

    The match is on the **whole** line, once its bullet and trailing colon are
    gone. A line that merely starts with a heading word is a sentence.
    """
    stripped = _TRAILING_COLON.sub("", strip_bullet(line))
    return HEADINGS.get(_folded(stripped))


def looks_like_heading(
    line: str, *, names_catalogue_term: Callable[[str], bool] | None = None
) -> bool:
    """Whether an unknown line is title-shaped enough to end a section.

    Five conditions, all required: it carries no list marker, it is short in
    both words and characters, it does not end like a sentence, it reads like a
    title (a trailing colon, an all-capitals line, or an initial capital), and
    it names no catalogue term. The last one is what keeps a one-word bullet
    such as `Python` an item rather than a title — see the module docstring.
    """
    knows_term = (
        mentions_catalogue_term if names_catalogue_term is None else names_catalogue_term
    )
    raw = line.strip()
    if not raw or raw != strip_bullet(raw):
        return False
    stripped = _TRAILING_COLON.sub("", raw)
    if not stripped or _SENTENCE_END.search(stripped):
        return False
    if len(stripped) > _MAX_HEADING_CHARS or len(stripped.split()) > _MAX_HEADING_WORDS:
        return False
    if knows_term(stripped):
        return False
    # A trailing colon is a title marker by itself; without one, the line has
    # to read like a title — capitalised, or shouted.
    if raw.endswith((":", "：")):
        return True
    words = re.findall(r"[^\W\d_]+", stripped, re.UNICODE)
    if not words:
        return False
    return bool(stripped.isupper() or words[0][:1].isupper())


def parse_requirement_segments(
    description: str | None,
    *,
    names_catalogue_term: Callable[[str], bool] | None = None,
) -> tuple[RequirementSegment, ...]:
    """Split one description into sentences, each carrying its section.

    Pure: no clock, no database, no network. `names_catalogue_term` is a seam
    for tests that want to exercise the heading heuristic against a catalogue
    of their own; production callers leave it unset.
    """
    text = normalize_description(description)
    if not text:
        return ()
    knows_term = (
        mentions_catalogue_term if names_catalogue_term is None else names_catalogue_term
    )
    parsed: list[RequirementSegment] = []
    context = SectionContext.NEUTRAL
    heading: str | None = None
    position = 0
    for line in text.split(SEGMENT_SEPARATOR):
        if not line.strip():
            continue
        known = heading_context(line)
        if known is not None:
            context = known
            heading = shorten_evidence(line.strip(), MAX_EVIDENCE_LENGTH)
            continue
        if looks_like_heading(line, names_catalogue_term=knows_term):
            context = SectionContext.NEUTRAL
            heading = shorten_evidence(line.strip(), MAX_EVIDENCE_LENGTH)
            continue
        for sentence in segments(strip_bullet(line)):
            parsed.append(
                RequirementSegment(
                    text=sentence,
                    position=position,
                    context=context,
                    heading_text=heading,
                )
            )
            position += 1
    return tuple(parsed)
