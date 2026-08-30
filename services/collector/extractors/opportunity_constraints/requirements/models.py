"""Typed, immutable vocabulary of the Phase 3.5B requirement reading.

This package answers one question and refuses the other one:

    what does this posting explicitly ask for, in skills and in languages?

It never asks whether anybody has those skills or speaks those languages.
Nothing here names a profile, a candidate, a gap, a fit, a score or a verdict,
and nothing downstream of here reads the Digital Twin. Comparing the two sides
is Phase 3.6; ranking is Phase 4; neither is implemented.

**A mention is not a requirement**, and most of this package exists to keep
those apart. "Our stack includes Python and Spark" describes a company, "you
will build pipelines using Python" describes a job, and "no prior Python
experience is required" describes the opposite of a requirement. None of them
is a demand, and where v1 cannot tell, it says nothing: the absence of a
requirement is UNKNOWN, and UNKNOWN is never FALSE — a posting that never named
SQL has not said SQL is unwanted.

**An OR is never widened into an AND.** "Python or R required" is one
requirement satisfied two ways. Storing it as two requirements would let a
later phase demand both and reject somebody the posting would have accepted, so
v1 stores neither and records a `RequirementAmbiguity` instead. UNKNOWN is
then a decision on the record rather than a silence.

The version below is this package's own. It is **not**
`opportunity-constraints-v3`: 3.5A's rules and 3.5B's rules change for
different reasons, and one shared label would make every skill fix recompute
every start date. Two contracts, two versions, two fingerprints, two states.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.collector.extractors.opportunity_constraints.models import (
    MAX_EVIDENCE_LENGTH,
)

#: The version of the whole 3.5B contract: the section vocabulary, the closed
#: catalogues, the matcher's boundary rules, the required/preferred markers and
#: the shape of everything below. Any change to what this package produces
#: **from a given description** moves this version, and moving it is what makes
#: the next synchronization recompute the rows it explains.
#:
#: It is deliberately independent of `EXTRACTOR_VERSION` in `..models`. 3.5A
#: reads dates, durations and work modes; 3.5B reads technologies and
#: languages; a fix to one is not a reason to rewrite the other's projection.
REQUIREMENT_EXTRACTOR_VERSION = "opportunity-requirements-v1"

#: The longest proficiency text stored. A level is a word or a code — `B2`,
#: `Fluent`, `Professional proficiency` — and anything longer is a sentence
#: that got captured by accident.
MAX_PROFICIENCY_LENGTH = 60

__all__ = [
    "AmbiguityReason",
    "ExtractedRequirements",
    "LanguageRequirement",
    "MAX_EVIDENCE_LENGTH",
    "MAX_PROFICIENCY_LENGTH",
    "REQUIREMENT_EXTRACTOR_VERSION",
    "RequirementAmbiguity",
    "RequirementError",
    "RequirementEvidence",
    "RequirementKind",
    "RequirementLevel",
    "RequirementSegment",
    "RequirementSource",
    "RequirementSourceField",
    "SectionContext",
    "SkillRequirement",
    "stronger",
]


class RequirementError(ValueError):
    """Raised when a requirement reading is malformed, before any write."""


class RequirementLevel(StrEnum):
    """How hard the posting made one demand.

    There is no `UNKNOWN` member and there is no third value on purpose: a
    demand this package cannot classify as one of these two is not stored at
    all. UNKNOWN here is the absence of a row, exactly as in `0012`.
    """

    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"


def stronger(left: RequirementLevel, right: RequirementLevel) -> RequirementLevel:
    """`REQUIRED` beats `PREFERRED`. The whole projection rule, in one place.

    A posting naming Python under "Nice to have" and again under "Required
    Qualifications" requires Python: the stronger statement is the one it made,
    and the weaker one does not weaken it. This is not a contradiction and it
    produces no ambiguity — but it does not erase the weaker mention either,
    which survives as evidence carrying its own `observed_requirement`.
    """
    if left is RequirementLevel.REQUIRED or right is RequirementLevel.REQUIRED:
        return RequirementLevel.REQUIRED
    return RequirementLevel.PREFERRED


class SectionContext(StrEnum):
    """What a section of the posting says about the items listed under it.

    The registry is closed and has exactly three members. `NEUTRAL` is not a
    weaker `REQUIRED`; it is "this heading says nothing about obligation", and
    an item under it produces no requirement at all. Responsibilities, benefits
    and stack listings are all `NEUTRAL`, and that is why a posting's
    technology paragraph does not become a list of demands.
    """

    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"
    NEUTRAL = "NEUTRAL"


class RequirementKind(StrEnum):
    """What a requirement or an ambiguity is about."""

    SKILL = "SKILL"
    LANGUAGE = "LANGUAGE"


class RequirementSourceField(StrEnum):
    """Where a fragment was read.

    One member, because v1 reads one field. A title names a role and a name is
    not a demand: "Python Developer" states no requirement, and a rule reading
    it as one would make every posting require whatever its title mentions.
    """

    DESCRIPTION = "DESCRIPTION"


class AmbiguityReason(StrEnum):
    """Why an explicit demand was understood and deliberately not stored.

    Both members exist because the code raises them; neither is a severity and
    neither is a queue. A row is an audit entry saying "this posting asked for
    something v1 cannot represent without corrupting it".
    """

    #: "Python or R required" — a choice, not two obligations.
    ALTERNATIVE_GROUP_UNSUPPORTED = "ALTERNATIVE_GROUP_UNSUPPORTED"
    #: "English B2 required" beside "English C1 required" — the language is
    #: still required; only the level is unknowable.
    CONFLICTING_LANGUAGE_PROFICIENCY = "CONFLICTING_LANGUAGE_PROFICIENCY"


@dataclass(frozen=True)
class RequirementSource:
    """Exactly the collected values 3.5B is allowed to read.

    One field, and the fingerprint in `extractor.py` covers exactly it. A value
    object rather than a row, so the pure extractor never opens a database and
    never sees a column it was not handed.
    """

    opportunity_id: int
    description: str | None = None


@dataclass(frozen=True)
class RequirementSegment:
    """One readable unit of the posting, and the section it sat in.

    `context` is the section's, not the sentence's: a local marker can still
    override it, and `skills.py` and `languages.py` are where that ordering
    lives. `heading_text` is the heading verbatim, kept **separate** from the
    segment text so nothing ever quotes a sentence the employer did not write.
    """

    text: str
    position: int
    context: SectionContext
    heading_text: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise RequirementError("a segment must carry text")
        if self.position < 0:
            raise RequirementError("a segment position must not be negative")


@dataclass(frozen=True)
class RequirementEvidence:
    """One observation: the words, the rule, the section, and what it said.

    `observed_requirement` is what **this fragment** stated, which may be
    weaker than the requirement it now explains. A posting that preferred
    Python in one line and required it in another projects one `REQUIRED` row
    and keeps both observations, and the preferred one still reads
    `PREFERRED` — otherwise an audit would find two fragments both claiming to
    have demanded something, one of which never did.
    """

    position: int
    source_field: RequirementSourceField
    observed_requirement: RequirementLevel
    rule_id: str
    text: str
    context_heading_text: str | None = None
    #: For languages: the level this fragment named, as written. `None` when it
    #: named none. Unused by skills, which have no levels in v1.
    observed_proficiency_text: str | None = None

    def __post_init__(self) -> None:
        if self.position < 0:
            raise RequirementError("an evidence position must not be negative")
        if not self.rule_id.strip():
            raise RequirementError("evidence must name the rule that fired")
        if not self.text.strip():
            raise RequirementError("evidence must quote what it matched")
        if len(self.text) > MAX_EVIDENCE_LENGTH:
            raise RequirementError(
                f"evidence must be a fragment, not a copy of the posting "
                f"(over {MAX_EVIDENCE_LENGTH} characters)"
            )
        if self.context_heading_text is not None and (
            not self.context_heading_text.strip()
            or len(self.context_heading_text) > MAX_EVIDENCE_LENGTH
        ):
            raise RequirementError("a context heading must be a short fragment")
        if self.observed_proficiency_text is not None and (
            not self.observed_proficiency_text.strip()
            or len(self.observed_proficiency_text) > MAX_PROFICIENCY_LENGTH
        ):
            raise RequirementError("a proficiency must be a short written level")


@dataclass(frozen=True)
class SkillRequirement:
    """One technology the posting asked for, and every mention that said so.

    `canonical_key` and `canonical_name` are the shared `skills` vocabulary's,
    produced by the Phase 3.4A normalizer, so the offer side and the profile
    side name a technology identically. The row this becomes says the posting
    asks for the term; it says nothing about anybody holding it.
    """

    canonical_key: str
    canonical_name: str
    requirement: RequirementLevel
    evidence: tuple[RequirementEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not self.canonical_key.strip() or not self.canonical_name.strip():
            raise RequirementError("a skill requirement needs a canonical name")
        if not self.evidence:
            raise RequirementError(
                "a skill requirement must quote the words that stated it"
            )


@dataclass(frozen=True)
class LanguageRequirement:
    """One language the posting asked for, at the level it wrote, if it wrote one.

    `proficiency_text` is the employer's own text. `Fluent` never becomes `C1`
    and `Native` never becomes `C2`: those are somebody's convention, not the
    posting's sentence, and `profile_languages` keeps proficiency as written for
    the same reason. `None` means the posting named no level — or named two
    incompatible ones for one demand, in which case the language stays required
    and only the level is unknown.
    """

    language_key: str
    language_name: str
    requirement: RequirementLevel
    proficiency_text: str | None = None
    evidence: tuple[RequirementEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not self.language_key.strip() or not self.language_name.strip():
            raise RequirementError("a language requirement needs a named language")
        if self.proficiency_text is not None and (
            not self.proficiency_text.strip()
            or len(self.proficiency_text) > MAX_PROFICIENCY_LENGTH
        ):
            raise RequirementError("a proficiency must be a short written level")
        if not self.evidence:
            raise RequirementError(
                "a language requirement must quote the words that stated it"
            )


@dataclass(frozen=True)
class RequirementAmbiguity:
    """A demand the extractor understood and deliberately refused to store.

    It is the difference between "the posting said nothing" and "the posting
    said something this version cannot write down without changing what it
    means". Nothing reads these rows to decide anything.
    """

    position: int
    kind: RequirementKind
    reason: AmbiguityReason
    rule_id: str
    text: str
    context_heading_text: str | None = None

    def __post_init__(self) -> None:
        if self.position < 0:
            raise RequirementError("an ambiguity position must not be negative")
        if not self.rule_id.strip():
            raise RequirementError("an ambiguity must name the rule that refused")
        if not self.text.strip():
            raise RequirementError("an ambiguity must quote what it refused")
        if len(self.text) > MAX_EVIDENCE_LENGTH:
            raise RequirementError("an ambiguity quotes a fragment, not the posting")
        if self.context_heading_text is not None and (
            not self.context_heading_text.strip()
            or len(self.context_heading_text) > MAX_EVIDENCE_LENGTH
        ):
            raise RequirementError("a context heading must be a short fragment")


@dataclass(frozen=True)
class ExtractedRequirements:
    """Everything one posting was found to ask for, and why.

    Empty tuples are a real answer: a posting can genuinely require no named
    skill and no language. That is why `repository.py` writes an extraction
    state row even when all three collections are empty — otherwise "read, and
    it asks for nothing" would be indistinguishable from "never read".
    """

    opportunity_id: int
    source_fingerprint: str
    extractor_version: str = REQUIREMENT_EXTRACTOR_VERSION
    skills: tuple[SkillRequirement, ...] = ()
    languages: tuple[LanguageRequirement, ...] = ()
    ambiguities: tuple[RequirementAmbiguity, ...] = ()

    def __post_init__(self) -> None:
        if len(self.source_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_fingerprint
        ):
            raise RequirementError(
                "source_fingerprint must be a lowercase sha256 digest"
            )
        keys = [requirement.canonical_key for requirement in self.skills]
        if len(keys) != len(set(keys)):
            raise RequirementError("a skill is required twice")
        languages = [requirement.language_key for requirement in self.languages]
        if len(languages) != len(set(languages)):
            raise RequirementError("a language is required twice")

    def skills_at(self, level: RequirementLevel) -> tuple[SkillRequirement, ...]:
        return tuple(item for item in self.skills if item.requirement is level)

    def languages_at(self, level: RequirementLevel) -> tuple[LanguageRequirement, ...]:
        return tuple(item for item in self.languages if item.requirement is level)
