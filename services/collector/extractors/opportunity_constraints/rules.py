"""The closed rules that read a posting, and nothing else.

Every value this package asserts comes from one named rule in this module, and
every rule is a pattern over words a posting actually wrote. There is no
scoring, no ranking, no nearest-match and no default: a rule either recognises
an explicit statement or it stays silent, and silence is UNKNOWN.

The rules are deliberately narrow, and most of the work here is refusing to
fire. The mistakes worth preventing are all of the same shape — reading a
property of the *posting* as a *requirement* it never stated:

* a title is not a quantity. "Senior Data Scientist" states no number of
  years, so no experience rule looks at the title at all;
* a language is not a requirement. A description written in English does not
  require English, and no rule in this module inspects the language of the
  text;
* an address is not an attendance policy. A posting located in Casablanca has
  not said the work is on-site;
* the word "internship" is not an agreement, a duration or a school. It does
  not imply a convention, it does not imply six months, and it does not imply
  a student;
* a country is not a visa policy. A posting in the United States has not said
  it refuses to sponsor.

Two conventions are documented rather than hidden, because both are choices:

* **weeks become months only when the count divides by four.** A posting that
  writes "12-week internship" is using the four-week month itself, so 12 → 3
  is its own arithmetic. Six weeks would be one-and-a-half months, and
  rounding it either way would store a bound nobody wrote, so a six-week
  posting simply has no asserted duration;
* **a slashed date is never read.** `01/02/2027` is 1 February to most of the
  world and 2 January to some of it, and the posting does not say which. Only
  an unambiguous ISO date is accepted as a whole date; a month name with or
  without a year is read at the precision it was written.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from services.collector.extractors.opportunity_constraints.models import (
    MAX_EVIDENCE_LENGTH,
    ConstraintEvidence,
    ConstraintKind,
    ConventionRequirement,
    DurationRequirement,
    EducationLevel,
    EducationRequirement,
    EducationRequirementMode,
    ExperienceObligation,
    ExperienceRequirement,
    OpportunitySource,
    OpportunityType,
    Slot,
    SourceField,
    StartPrecision,
    StartRequirement,
    VisaSponsorship,
    WorkAuthorization,
    WorkMode,
)
from services.collector.extractors.opportunity_constraints.text import (
    normalize_description,
    normalize_field,
    segments,
    shorten_evidence,
)

__all__ = ["TYPE_FIELD_PRECEDENCE", "RuleHit", "Slot", "read_posting"]


@dataclass(frozen=True)
class RuleHit:
    """One rule fired: what it concluded, where, and on what words."""

    slot: Slot
    kind: ConstraintKind
    #: The conclusion, compared by equality to detect a contradiction.
    value: object
    #: The stored text form of that conclusion, for evidence and conflicts.
    value_key: str
    rule_id: str
    source_field: SourceField
    fragment: str

    def evidence(self) -> ConstraintEvidence:
        return ConstraintEvidence(
            kind=self.kind,
            source_field=self.source_field,
            rule_id=self.rule_id,
            text=shorten_evidence(self.fragment, MAX_EVIDENCE_LENGTH),
            normalized_value=self.value_key,
        )


def _folded(text: str) -> str:
    return text.casefold()


# --------------------------------------------------------------------------
# Opportunity type
# --------------------------------------------------------------------------

#: Explicit wordings, longest and most specific first so `stage de fin
#: d'études` is read as a PFE rather than as a plain internship.
#:
#: The fourth element says whether the wording may be believed **in a
#: description**. Two types are title-and-classifier only, and the corpus is
#: why: both are statements about the seniority of the role, and descriptions
#: talk about seniority constantly without describing the offer.
#:
#: * `JUNIOR_ROLE` — "mentoring junior consultants", "demonstrated ability to
#:   mentor junior team members". The posting is for whoever mentors;
#: * `FIRST_JOB` — "currently enrolled in an undergraduate or graduate
#:   program". That is the candidate's studies, not a graduate programme the
#:   company runs.
#:
#: A title reading "Junior Data Engineer" still settles it, and so does the
#: Phase 2 classifier. What is refused is inferring the seniority of an offer
#: from a sentence that merely contains the word.
_TYPE_SIGNALS: tuple[tuple[OpportunityType, str, tuple[str, ...], bool], ...] = (
    (
        OpportunityType.PFE,
        "OPPORTUNITY_TYPE_PFE_V1",
        ("pfe", "projet de fin d'etudes", "projet de fin d'études",
         "stage de fin d'etudes", "stage de fin d'études", "final year project",
         "final-year internship", "end of studies internship"),
        True,
    ),
    (
        OpportunityType.PFA,
        "OPPORTUNITY_TYPE_PFA_V1",
        ("pfa", "projet de fin d'annee", "projet de fin d'année"),
        True,
    ),
    (
        OpportunityType.ALTERNANCE,
        "OPPORTUNITY_TYPE_ALTERNANCE_V1",
        ("alternance", "alternant", "alternante", "apprenticeship",
         "contrat d'apprentissage", "contrat de professionnalisation"),
        True,
    ),
    (
        OpportunityType.SUMMER_INTERNSHIP,
        "OPPORTUNITY_TYPE_SUMMER_V1",
        ("summer internship", "stage d'ete", "stage d'été", "summer intern program"),
        True,
    ),
    (
        OpportunityType.PRE_HIRE_INTERNSHIP,
        "OPPORTUNITY_TYPE_PRE_HIRE_V1",
        ("stage pre-embauche", "stage pré-embauche", "pre-hire internship",
         "internship with a view to hiring", "stage pre embauche"),
        True,
    ),
    (
        OpportunityType.INTERNSHIP,
        "OPPORTUNITY_TYPE_INTERNSHIP_V2",
        ("internship", "intern position", "stage", "stagiaire"),
        True,
    ),
    (
        OpportunityType.FIRST_JOB,
        "OPPORTUNITY_TYPE_FIRST_JOB_V2",
        ("first job", "premier emploi", "graduate programme", "graduate program",
         "jeune diplome", "jeune diplômé", "new graduate", "entry level",
         "entry-level"),
        False,
    ),
    (
        OpportunityType.JUNIOR_ROLE,
        "OPPORTUNITY_TYPE_JUNIOR_V2",
        ("junior",),
        False,
    ),
)

#: Words that, just before a type wording, mean the posting is denying it:
#: "this isn't a research internship" is not an internship.
_TYPE_NEGATIONS = (
    "isn't a", "isn't an", "is not a", "is not an", "not a ", "not an ",
    "rather than a", "instead of a", "n'est pas un", "n'est pas une",
    "pas un ", "pas une ",
)
#: Words that, just before a type wording, mean it describes the **candidate's
#: past**: "prior internship or project experience" is a requirement about
#: somebody's history, not a statement about this offer.
_TYPE_PAST_MARKERS = (
    "prior", "previous", "past ", "former", "earlier", "précédent", "precedent",
    "antérieur", "anterieur", "during your", "during their", "completed a",
)
#: Words that, just before a type wording, mean the posting is about
#: **supervising** people of that kind rather than being one: "mentoring junior
#: consultants and interns".
_TYPE_SUPERVISION_MARKERS = (
    "mentor", "manage", "managing", "supervise", "supervising", "coach",
    "onboard", "encadrer", "encadrement", "recruit", "hiring intern",
)
#: Words that, just after a type wording, say the same thing the other way
#: round: "internship experience", "internships you completed".
_TYPE_PAST_SUFFIXES = ("experience", "expérience", "you completed", "you have done")

#: How far before or after a wording those markers are believed. Long enough
#: for "demonstrated ability to mentor junior team members", short enough that
#: an unrelated clause on the same line does not reach.
_TYPE_CONTEXT_WINDOW = 45


def _incidental(folded: str, start: int, end: int) -> bool:
    """True when a type wording describes something other than this offer.

    Three ways a posting can name a kind of job without being one: it can deny
    it, it can ask for it in somebody's past, or it can be about supervising
    people who are it. All three appear in the real corpus, and all three
    produced a wrong type before this guard existed.
    """
    before = folded[max(0, start - _TYPE_CONTEXT_WINDOW):start]
    after = folded[end:end + _TYPE_CONTEXT_WINDOW]
    if any(marker in before for marker in _TYPE_NEGATIONS):
        return True
    if any(marker in before for marker in _TYPE_PAST_MARKERS):
        return True
    if any(marker in before for marker in _TYPE_SUPERVISION_MARKERS):
        return True
    return any(suffix in after for suffix in _TYPE_PAST_SUFFIXES)


#: Which field settles the type when two disagree. The title wins, exactly as
#: the Phase 2 classifier already decides it — "infer from title first;
#: ordinary description vocabulary cannot override it" — because a description
#: that mentions an internship inside a posting titled PFE is describing the
#: same thing at a coarser grain, not contradicting it.
#:
#: The classifier's own reading sits **above** the description, not below it.
#: That is a change from `v1` and the corpus argued for it: the classifier is a
#: versioned reading whose entire subject is the type of an opportunity, while
#: a description mention is one word in a paragraph about something else. An
#: incidental "internship" three lines into a job posting should not outrank a
#: dedicated classification of that posting.
#:
#: Within one tier a disagreement is a real conflict and nothing is asserted.
TYPE_FIELD_PRECEDENCE: tuple[SourceField, ...] = (
    SourceField.TITLE,
    SourceField.QUALIFICATION_TYPE,
    SourceField.DESCRIPTION,
)

#: How the Phase 2 classifier's own reading maps onto the shared registry.
#: `GRADUATE` and `JOB` are absent on purpose: the first sits between
#: `FIRST_JOB` and `JUNIOR_ROLE` and the second between those and nothing at
#: all, and picking one would be this package inventing a category the
#: classifier never claimed.
_QUALIFICATION_TYPE_MAP = {
    "PFE": OpportunityType.PFE,
    "INTERNSHIP": OpportunityType.INTERNSHIP,
    "APPRENTICESHIP": OpportunityType.ALTERNANCE,
}


def _opportunity_type_hits(
    title: str, description_segments: Sequence[str], qualification_type: str | None
) -> Iterator[RuleHit]:
    for source_field, texts in (
        (SourceField.TITLE, (title,) if title else ()),
        (SourceField.DESCRIPTION, tuple(description_segments)),
    ):
        in_description = source_field is SourceField.DESCRIPTION
        for fragment in texts:
            folded = _folded(fragment)
            for opportunity_type, rule_id, signals, description_ok in _TYPE_SIGNALS:
                if in_description and not description_ok:
                    continue
                position, matched = -1, ""
                for signal in signals:
                    found = folded.find(signal)
                    if found != -1:
                        position, matched = found, signal
                        break
                if position == -1:
                    continue
                if in_description and _incidental(
                    folded, position, position + len(matched)
                ):
                    continue
                yield RuleHit(
                    slot=Slot.OPPORTUNITY_TYPE,
                    kind=ConstraintKind.OPPORTUNITY_TYPE,
                    value=opportunity_type,
                    value_key=opportunity_type.value,
                    rule_id=rule_id,
                    source_field=source_field,
                    fragment=fragment,
                )
                break
    mapped = _QUALIFICATION_TYPE_MAP.get((qualification_type or "").strip().upper())
    if mapped is not None:
        yield RuleHit(
            slot=Slot.OPPORTUNITY_TYPE,
            kind=ConstraintKind.OPPORTUNITY_TYPE,
            value=mapped,
            value_key=mapped.value,
            rule_id="OPPORTUNITY_TYPE_QUALIFICATION_V1",
            source_field=SourceField.QUALIFICATION_TYPE,
            fragment=str(qualification_type),
        )



# --------------------------------------------------------------------------
# Education
# --------------------------------------------------------------------------

_BAC_PLUS = re.compile(r"\bbac\s*\+\s*(\d)\b", re.IGNORECASE)
_BAC_LEVELS = {
    2: EducationLevel.BAC_PLUS_2,
    3: EducationLevel.BAC_PLUS_3,
    4: EducationLevel.BAC_PLUS_4,
    5: EducationLevel.BAC_PLUS_5,
}
_DEGREE_SIGNALS: tuple[tuple[EducationLevel, tuple[str, ...]], ...] = (
    (EducationLevel.PHD, ("phd", "ph.d", "doctorate", "doctorat")),
    (EducationLevel.MASTER, ("master", "msc", "m.sc", "mastère", "mastere")),
    (EducationLevel.BACHELOR, ("bachelor", "bsc", "b.sc", "licence")),
    (
        EducationLevel.ENGINEERING_DEGREE,
        ("engineering degree", "engineer's degree", "diplome d'ingenieur",
         "diplôme d'ingénieur", "ingenieur d'etat", "ingénieur d'état"),
    ),
)
#: Words that turn a named level into a floor. Without one, a level is read as
#: itself: "Master's degree" is `EXACT`, and reading it as "Master or above"
#: would admit a doctorate the posting never mentioned.
_MINIMUM_MARKERS = (
    "minimum", "at least", "or higher", "or above", "ou plus", "et plus",
    "au moins", "minimum required", "or more",
)
#: A level only counts when the sentence is about studies. The list holds no
#: level word: "master" cannot be its own context, or "you will master our data
#: pipeline" would award a diploma. A level named with no surrounding word
#: about education is left unasserted, which is the conservative half of the
#: trade and the right one.
_EDUCATION_CONTEXT = (
    "degree", "diploma", "diplome", "diplôme", "education", "formation",
    "studies", "etudes", "études", "graduate", "bac", "student", "etudiant",
    "étudiant", "school", "university", "universite", "université", "ecole",
    "école", "level", "niveau", "qualification", "background",
)


def _education_hits(description_segments: Sequence[str]) -> Iterator[RuleHit]:
    for fragment in description_segments:
        folded = _folded(fragment)
        if not any(marker in folded for marker in _EDUCATION_CONTEXT):
            continue
        is_minimum = any(marker in folded for marker in _MINIMUM_MARKERS)
        mode = (
            EducationRequirementMode.MINIMUM
            if is_minimum
            else EducationRequirementMode.EXACT
        )
        found: list[tuple[EducationLevel, str]] = []
        for match in _BAC_PLUS.finditer(fragment):
            level = _BAC_LEVELS.get(int(match.group(1)))
            if level is not None:
                found.append((level, "EDUCATION_BAC_PLUS_V1"))
        for level, signals in _DEGREE_SIGNALS:
            if any(signal in folded for signal in signals):
                found.append((level, "EDUCATION_DEGREE_V1"))
        for level, rule_id in found:
            requirement = EducationRequirement(level=level, mode=mode)
            yield RuleHit(
                slot=Slot.EDUCATION,
                kind=ConstraintKind.EDUCATION,
                value=requirement,
                value_key=f"{level.value}:{mode.value}",
                rule_id=rule_id,
                source_field=SourceField.DESCRIPTION,
                fragment=fragment,
            )


# --------------------------------------------------------------------------
# Experience
# --------------------------------------------------------------------------
#
# Everything here is tied to the word "experience" by the pattern itself, not
# by sharing a sentence with it. The corpus is why. A posting whose salary
# boilerplate reads "…reflects the minimum and maximum target for new hire
# salaries…" was being read as stating a *required* experience, because the
# segment happened to contain the word "minimum" and, somewhere else, a word
# about careers. "Minimum" qualified the salary. A marker that is merely
# present in the same sentence qualifies nothing.

_EXP = r"(?:experiences?|expériences?|experiences|expérience)"
_UNIT = r"(?:years?|yrs?|ans?|années?|annees?|months?|mois)"
#: How far a quantity or a marker may sit from the experience it qualifies,
#: without crossing a sentence boundary. Wide enough for "7+ years of AI/ML
#: production experience", narrow enough that a salary range on the same line
#: never reaches it.
#:
#: The gap may not contain another quantity, which is what keeps two
#: requirements written on one line from collapsing into one: in "7+ years
#: engineering, 2+ years ML experience" the first phrase cannot reach past the
#: second to borrow its "experience", so each is read against its own.
#:
#: A known and deliberate limit follows: where two quantities on one line share
#: a single trailing "experience", only the nearer one is attached and the
#: other is left unread. That is a missing requirement — an UNKNOWN — rather
#: than a merged number nobody wrote, and it is the side to err on. Postings
#: that write the two as separate sentences, which is the common shape, are
#: read in full.
_NEAR = (
    r"(?:(?!\d{1,2}\s*\+?\s*"
    r"(?:years?|yrs?|ans?|années?|annees?|months?|mois))[^.\n]){0,40}?"
)

#: "1-2 years of experience", "2 to 4 years of X experience".
_RANGE = re.compile(
    rf"\b(\d{{1,2}})\s*(?:-|–|to|à|a|and|et)\s*(\d{{1,2}})\s*\+?\s*({_UNIT})"
    rf"{_NEAR}{_EXP}",
    re.IGNORECASE,
)
#: "at least 3 years of experience", "minimum 6 months of X experience".
_AT_LEAST = re.compile(
    rf"\b(?:at least|minimum(?: of)?|min\.?|au moins|minimum de|plus de|over)\s*"
    rf"(\d{{1,2}})\s*({_UNIT}){_NEAR}{_EXP}",
    re.IGNORECASE,
)
#: "7+ years of engineering experience".
_PLUS = re.compile(
    rf"\b(\d{{1,2}})\s*\+\s*({_UNIT}){_NEAR}{_EXP}", re.IGNORECASE
)
#: "experience: 3+ years", the same claim written backwards.
_EXP_FIRST = re.compile(
    rf"{_EXP}{_NEAR}\b(\d{{1,2}})\s*\+?\s*({_UNIT})", re.IGNORECASE
)

#: An obligation counts only when it is attached to the experience it
#: qualifies, in either order and within one sentence.
_PREFERRED_NEAR = re.compile(
    rf"(?:{_EXP}{_NEAR}\b(?:preferred|a plus|nice to have|desirable|welcome|"
    rf"souhait[ée]e?s?|appréci[ée]e?s?|apprecie[es]?|bonus)\b"
    rf"|\b(?:preferred|ideally|idéalement|idealement|nice to have)\b{_NEAR}{_EXP})",
    re.IGNORECASE,
)
_REQUIRED_NEAR = re.compile(
    rf"(?:{_EXP}{_NEAR}\b(?:required|mandatory|requise?s?|obligatoires?|"
    rf"exig[ée]e?s?)\b"
    rf"|\b(?:must have|we require|requires?|required)\b{_NEAR}{_EXP})",
    re.IGNORECASE,
)


def _to_months(quantity: int, unit: str) -> int:
    return quantity * 12 if re.match(r"(?:years?|yrs?|ans?|ann)", unit, re.IGNORECASE) else quantity


def _segment_obligation(fragment: str, from_at_least: bool) -> ExperienceObligation:
    """The obligation attached to this segment's experience, or UNKNOWN.

    `PREFERRED` is tested first and wins, which is what makes "prior experience
    is a plus but not required" read as preferred rather than required: the
    word "required" is in the sentence, but it is the thing being denied.
    Preferring the weaker reading is the conservative direction — it never
    invents a demand the posting did not make.

    `from_at_least` is the one case where a marker needs no separate test: a
    quantity written "at least 3 years" carries its own obligation inside the
    expression that matched, so the attachment is structural.
    """
    if _PREFERRED_NEAR.search(fragment) is not None:
        return ExperienceObligation.PREFERRED
    if _REQUIRED_NEAR.search(fragment) is not None or from_at_least:
        return ExperienceObligation.REQUIRED
    return ExperienceObligation.UNKNOWN


#: The quantity patterns, in the order a match is preferred when two overlap.
#: A range is the most specific reading, then an explicit floor, then a `+`,
#: then the same claim written backwards.
_EXPERIENCE_QUANTITIES = (
    ("EXPERIENCE_RANGE_V2", _RANGE, True, False),
    ("EXPERIENCE_MIN_YEARS_V2", _AT_LEAST, False, True),
    ("EXPERIENCE_MIN_PLUS_V2", _PLUS, False, False),
    ("EXPERIENCE_MIN_TRAILING_V2", _EXP_FIRST, False, False),
)


def _quantities(fragment: str):
    """Every experience quantity in one segment, left to right, non-overlapping.

    `finditer` rather than `search`, because one bullet can carry two: "7+
    years engineering, 2+ years ML experience" is two requirements on one line.
    Overlapping matches are resolved by pattern order — a range beats the floor
    hiding inside it — and never by taking both.
    """
    found: list[tuple[int, int, tuple[int | None, int | None], str, bool]] = []
    for rule_id, pattern, is_range, from_at_least in _EXPERIENCE_QUANTITIES:
        for match in pattern.finditer(fragment):
            if is_range:
                unit = match.group(3)
                low, high = int(match.group(1)), int(match.group(2))
                if low > high:
                    continue
                bounds = (_to_months(low, unit), _to_months(high, unit))
            else:
                bounds = (_to_months(int(match.group(1)), match.group(2)), None)
            found.append((match.start(), match.end(), bounds, rule_id, from_at_least))

    kept: list[tuple[int, int, tuple[int | None, int | None], str, bool]] = []
    for candidate in found:
        start, end = candidate[0], candidate[1]
        if any(start < other[1] and other[0] < end for other in kept):
            continue
        kept.append(candidate)
    return sorted(kept, key=lambda item: item[0])


def _experience_hits(description_segments: Sequence[str]) -> Iterator[RuleHit]:
    """Every experience the posting asked for, one hit each.

    A posting may ask for several, and usually does. Each quantity is its own
    requirement, carrying the obligation its own sentence attached; a sentence
    naming an obligation and no quantity becomes a requirement of its own, with
    no bounds. Two requirements are never a contradiction, so nothing here
    competes with anything.
    """
    for fragment in description_segments:
        quantities = _quantities(fragment)
        obligation = _segment_obligation(
            fragment, any(item[4] for item in quantities)
        )
        if not quantities:
            if obligation is not ExperienceObligation.UNKNOWN:
                yield _experience_hit(fragment, None, None, obligation,
                                      f"EXPERIENCE_{obligation.value}_V2")
            continue
        for _start, _end, (minimum, maximum), rule_id, _from_at_least in quantities:
            yield _experience_hit(fragment, minimum, maximum, obligation, rule_id)


def _experience_hit(
    fragment: str,
    minimum: int | None,
    maximum: int | None,
    obligation: ExperienceObligation,
    rule_id: str,
) -> RuleHit:
    requirement = ExperienceRequirement(
        min_months=minimum, max_months=maximum, obligation=obligation
    )
    return RuleHit(
        slot=Slot.EXPERIENCE,
        kind=ConstraintKind.EXPERIENCE,
        value=requirement,
        value_key=(
            f"{'' if minimum is None else minimum}-"
            f"{'' if maximum is None else maximum}:{obligation.value}"
        ),
        rule_id=rule_id,
        source_field=SourceField.DESCRIPTION,
        fragment=fragment,
    )


# --------------------------------------------------------------------------
# Duration
# --------------------------------------------------------------------------

_MONTHS = r"(?:months?|mois)"
_DURATION_WORDS = ("duration", "durée", "duree", "lasts", "internship", "stage",
                   "contract", "mission", "programme", "program")
_DURATION_RANGE = re.compile(
    rf"\b(\d{{1,2}})\s*(?:-|–|to|à|a|and|et)\s*(\d{{1,2}})\s*({_MONTHS}|weeks?|semaines?)\b",
    re.IGNORECASE,
)
_DURATION_SINGLE = re.compile(
    rf"\b(\d{{1,2}})[-\s]*({_MONTHS}|weeks?|semaines?)\b", re.IGNORECASE
)


def _duration_months(quantity: int, unit: str) -> int | None:
    """Months, or None when the posting's unit cannot become months exactly.

    See the module docstring: four weeks make a month here because that is the
    arithmetic a posting writing "12-week" is already using, and a count that
    does not divide by four is left unasserted rather than rounded.
    """
    if re.match(r"(?:weeks?|semaines?)", unit, re.IGNORECASE):
        return quantity // 4 if quantity % 4 == 0 and quantity >= 4 else None
    return quantity


def _duration_hits(description_segments: Sequence[str]) -> Iterator[RuleHit]:
    for fragment in description_segments:
        folded = _folded(fragment)
        if not any(word in folded for word in _DURATION_WORDS):
            continue
        if re.search(_EXP, folded, re.IGNORECASE) is not None:
            # "3 years of experience" is not how long the job lasts.
            continue
        if (match := _DURATION_RANGE.search(fragment)) is not None:
            low = _duration_months(int(match.group(1)), match.group(3))
            high = _duration_months(int(match.group(2)), match.group(3))
            if low is not None and high is not None and low <= high:
                yield RuleHit(
                    slot=Slot.DURATION,
                    kind=ConstraintKind.DURATION,
                    value=(low, high),
                    value_key=f"{low}-{high}",
                    rule_id="DURATION_RANGE_V1",
                    source_field=SourceField.DESCRIPTION,
                    fragment=fragment,
                )
            continue
        if (match := _DURATION_SINGLE.search(fragment)) is not None:
            months = _duration_months(int(match.group(1)), match.group(2))
            if months is not None:
                yield RuleHit(
                    slot=Slot.DURATION,
                    kind=ConstraintKind.DURATION,
                    value=(months, months),
                    value_key=f"{months}-{months}",
                    rule_id="DURATION_SINGLE_V1",
                    source_field=SourceField.DESCRIPTION,
                    fragment=fragment,
                )


# --------------------------------------------------------------------------
# Start date
# --------------------------------------------------------------------------

_MONTH_NAMES = {
    "january": 1, "janvier": 1, "february": 2, "fevrier": 2, "février": 2,
    "march": 3, "mars": 3, "april": 4, "avril": 4, "may": 5, "mai": 5,
    "june": 6, "juin": 6, "july": 7, "juillet": 7, "august": 8, "aout": 8,
    "août": 8, "september": 9, "septembre": 9, "october": 10, "octobre": 10,
    "november": 11, "novembre": 11, "december": 12, "decembre": 12,
    "décembre": 12,
}
_START_WORDS = ("start", "starting", "begins", "beginning", "commence",
                "debut", "début", "à partir de", "a partir de", "as of")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_YEAR = re.compile(
    r"\b(" + "|".join(sorted(_MONTH_NAMES, key=len, reverse=True)) + r")\b"
    r"(?:\s+(\d{4}))?",
    re.IGNORECASE,
)
_YEAR_ONLY = re.compile(r"\b(20\d{2})\b")


def _start_hits(description_segments: Sequence[str]) -> Iterator[RuleHit]:
    for fragment in description_segments:
        folded = _folded(fragment)
        if not any(word in folded for word in _START_WORDS):
            continue
        if (match := _ISO_DATE.search(fragment)) is not None:
            year, month, day = (int(part) for part in match.groups())
            try:
                requirement = StartRequirement(
                    year=year, month=month, day=day, precision=StartPrecision.DATE
                )
            except Exception:
                continue
            yield RuleHit(
                slot=Slot.START,
                kind=ConstraintKind.START,
                value=requirement,
                value_key=f"{year:04d}-{month:02d}-{day:02d}",
                rule_id="START_ISO_DATE_V1",
                source_field=SourceField.DESCRIPTION,
                fragment=fragment,
            )
            continue
        if (match := _MONTH_YEAR.search(folded)) is not None:
            month = _MONTH_NAMES[match.group(1)]
            # The year stays None when the posting did not write one. It is
            # never taken from the clock, from the publication date or from
            # anywhere else.
            year = int(match.group(2)) if match.group(2) else None
            requirement = StartRequirement(
                year=year, month=month, precision=StartPrecision.MONTH
            )
            yield RuleHit(
                slot=Slot.START,
                kind=ConstraintKind.START,
                value=requirement,
                value_key=f"{year or ''}-{month:02d}",
                rule_id="START_MONTH_V1",
                source_field=SourceField.DESCRIPTION,
                fragment=fragment,
            )
            continue
        if (match := _YEAR_ONLY.search(fragment)) is not None:
            year = int(match.group(1))
            requirement = StartRequirement(year=year, precision=StartPrecision.YEAR)
            yield RuleHit(
                slot=Slot.START,
                kind=ConstraintKind.START,
                value=requirement,
                value_key=str(year),
                rule_id="START_YEAR_V1",
                source_field=SourceField.DESCRIPTION,
                fragment=fragment,
            )


# --------------------------------------------------------------------------
# Work mode
# --------------------------------------------------------------------------

_REMOTE_TYPE_MAP = {
    "remote": WorkMode.REMOTE, "fully_remote": WorkMode.REMOTE,
    "full_remote": WorkMode.REMOTE, "telework": WorkMode.REMOTE,
    "hybrid": WorkMode.HYBRID, "hybride": WorkMode.HYBRID,
    "on_site": WorkMode.ON_SITE, "onsite": WorkMode.ON_SITE,
    "on-site": WorkMode.ON_SITE, "sur_site": WorkMode.ON_SITE,
}

#: A work mode is a **global property of the role**, so only a statement about
#: the role counts. The corpus made the distinction unavoidable:
#:
#: * "fully remote … position" beside the tracking tag `#LI-Onsite` was read as
#:   a contradiction. A tag an applicant-tracking system appends is not the
#:   employer describing the job;
#: * "developing onsite solutions" was read as on-site. The word qualified the
#:   solutions, not the contract;
#: * "hybrid customer-facing engineering role" beside "significant time on-site
#:   with customers" was read as a contradiction. Spending time on site is what
#:   hybrid *means*; the second sentence elaborates the first.
#:
#: So every pattern below requires the mode to be attached to the role — by a
#: noun like role, position or job, by an explicit "this role is …", or by a
#: whole-role adverb like "fully" or "100%". A bare occurrence of the word
#: never suffices, which is what makes all three of those cases resolve
#: correctly without any priority rule deciding between them.
_ROLE = r"(?:role|position|job|opportunity|poste)"
_WORK_MODE_PATTERNS: tuple[tuple[WorkMode, str, str], ...] = (
    (
        WorkMode.HYBRID,
        "WORK_MODE_HYBRID_V2",
        rf"(?:\bhybrid\b[^.\n]{{0,30}}?\b{_ROLE}\b"
        rf"|\bthis\s+{_ROLE}\s+is\s+[^.\n]{{0,15}}?hybrid\b"
        rf"|\bhybrid\s+(?:work|working)\s+(?:model|setup|arrangement|policy)\b"
        rf"|\bmode\s+hybride\b|\bposte\s+hybride\b)",
    ),
    (
        WorkMode.REMOTE,
        "WORK_MODE_REMOTE_V2",
        rf"(?:\b(?:fully|100\s*%|full)[\s-]*remote\b"
        rf"|\bremote\s+{_ROLE}\b"
        rf"|\bremote[\s-]first\b"
        rf"|\bthis\s+{_ROLE}\s+is\s+[^.\n]{{0,15}}?remote\b"
        rf"|\bwork\s+from\s+home\s+{_ROLE}\b"
        rf"|\bt[ée]l[ée]travail\s+(?:complet|total|int[ée]gral)\b"
        rf"|\benti[èe]rement\s+[àa]\s+distance\b)",
    ),
    (
        WorkMode.ON_SITE,
        "WORK_MODE_ON_SITE_V2",
        rf"(?:\b(?:fully|100\s*%)[\s-]*on[\s-]?site\b"
        rf"|\bon[\s-]?site\s+{_ROLE}\b"
        rf"|\bthis\s+{_ROLE}\s+is\s+[^.\n]{{0,15}}?on[\s-]?site\b"
        rf"|\brequires?\s+(?:full[\s-]?time\s+)?on[\s-]?site\s+presence\b"
        rf"|\bposte\s+en\s+pr[ée]sentiel\b"
        rf"|\b(?:100\s*%|enti[èe]rement)\s+en\s+pr[ée]sentiel\b)",
    ),
)
_WORK_MODE_RULES = tuple(
    (mode, rule_id, re.compile(pattern, re.IGNORECASE))
    for mode, rule_id, pattern in _WORK_MODE_PATTERNS
)

#: A refusal of remote is not a claim of on-site. "No remote work" says where
#: the work is not done; it does not say the office is mandatory, and this
#: package will not fill in the rest. The phrases exist here only to stop the
#: REMOTE rule from firing on the word inside them.
_NO_REMOTE = re.compile(
    r"(?:\bno\s+(?:remote|telework)\b|\bnot\s+(?:a\s+)?remote\b"
    r"|\bwithout\s+remote\b|\bremote\s+is\s+not\b"
    r"|\bpas\s+de\s+t[ée]l[ée]travail\b|\baucun\s+t[ée]l[ée]travail\b)",
    re.IGNORECASE,
)


def _work_mode_hits(
    description_segments: Sequence[str], remote_type: str | None
) -> Iterator[RuleHit]:
    normalized = (remote_type or "").strip().casefold().replace(" ", "_")
    mapped = _REMOTE_TYPE_MAP.get(normalized)
    if mapped is not None:
        yield RuleHit(
            slot=Slot.WORK_MODE,
            kind=ConstraintKind.WORK_MODE,
            value=mapped,
            value_key=mapped.value,
            rule_id="WORK_MODE_COLLECTED_FIELD_V1",
            source_field=SourceField.REMOTE_TYPE,
            fragment=str(remote_type),
        )
    for fragment in description_segments:
        suppressed = _NO_REMOTE.search(fragment) is not None
        for work_mode, rule_id, pattern in _WORK_MODE_RULES:
            if work_mode is WorkMode.REMOTE and suppressed:
                continue
            if pattern.search(fragment) is None:
                continue
            yield RuleHit(
                slot=Slot.WORK_MODE,
                kind=ConstraintKind.WORK_MODE,
                value=work_mode,
                value_key=work_mode.value,
                rule_id=rule_id,
                source_field=SourceField.DESCRIPTION,
                fragment=fragment,
            )
            break


# --------------------------------------------------------------------------
# Visa sponsorship, work authorization, convention
# --------------------------------------------------------------------------

#: Checked before the affirmative wordings, and short-circuiting them for the
#: same segment: "no sponsorship available" contains "sponsorship available",
#: and a reader who checked the positive first would report the opposite of
#: what the posting says.
_SPONSORSHIP_NEGATIVE = (
    "do not sponsor", "does not sponsor", "will not sponsor", "cannot sponsor",
    "can not sponsor", "unable to sponsor", "not able to sponsor",
    "no sponsorship", "without sponsorship", "no visa sponsorship",
    "sponsorship is not available", "sponsorship not available",
    "not offer sponsorship", "does not offer visa", "no visa support",
    "ne sponsorise pas", "pas de sponsorship", "sans sponsorisation",
)
_SPONSORSHIP_POSITIVE = (
    "visa sponsorship available", "sponsorship available",
    "we sponsor visas", "we do sponsor", "sponsorship provided",
    "we offer visa sponsorship", "visa sponsorship is available",
    "will sponsor", "visa support available", "sponsorship offered",
    "sponsorisation possible",
)
_AUTHORIZATION_REQUIRED = (
    "must be authorized to work", "must be authorised to work",
    "must have the right to work", "right to work in", "work authorization required",
    "work authorisation required", "must hold a valid work permit",
    "valid work permit required", "authorized to work in",
    "authorised to work in", "doit etre autorise a travailler",
    "doit être autorisé à travailler", "autorisation de travail requise",
    "permis de travail requis",
)
_AUTHORIZATION_NOT_REQUIRED = (
    "no work permit required", "work permit is not required",
    "no work authorization required", "no work authorisation required",
    "aucun permis de travail", "sans autorisation de travail",
)
_CONVENTION_REQUIRED = (
    "convention de stage obligatoire", "convention de stage requise",
    "convention de stage est obligatoire", "internship agreement required",
    "internship agreement is required", "school agreement required",
    "convention obligatoire", "stage conventionne", "stage conventionné",
    "must provide an internship agreement", "convention de stage necessaire",
    "convention de stage nécessaire",
)
_CONVENTION_NOT_REQUIRED = (
    "sans convention de stage", "convention non obligatoire",
    "no internship agreement required", "internship agreement is not required",
    "aucune convention de stage",
)


def _phrase_hits(
    description_segments: Sequence[str],
    slot: Slot,
    kind: ConstraintKind,
    negative: tuple[str, ...],
    negative_value,
    negative_rule: str,
    positive: tuple[str, ...],
    positive_value,
    positive_rule: str,
) -> Iterator[RuleHit]:
    """One negation-aware reading. The negative wording always wins its segment."""
    for fragment in description_segments:
        folded = _folded(fragment)
        matched = next((phrase for phrase in negative if phrase in folded), None)
        if matched is not None:
            yield RuleHit(
                slot=slot, kind=kind, value=negative_value,
                value_key=negative_value.value, rule_id=negative_rule,
                source_field=SourceField.DESCRIPTION, fragment=fragment,
            )
            continue
        matched = next((phrase for phrase in positive if phrase in folded), None)
        if matched is not None:
            yield RuleHit(
                slot=slot, kind=kind, value=positive_value,
                value_key=positive_value.value, rule_id=positive_rule,
                source_field=SourceField.DESCRIPTION, fragment=fragment,
            )


def _location_hits(source: OpportunitySource) -> Iterator[RuleHit]:
    """The places the collected fields named, each with its own evidence.

    A projected location is a value like any other, so it is explainable like
    any other: the rule says which collected field it came from, and the
    evidence quotes it. Without a hit here, `opportunity_constraint_locations`
    would be the one projection a reader could not trace back to anything.

    Nothing is geocoded, no country is deduced from a city, no city from a
    country, and no region is expanded into the places inside it. Each rule
    reads one field and copies what it found, trimmed.
    """
    for value, source_field, rule_id in (
        (source.location, SourceField.LOCATION, "LOCATION_COLLECTED_FIELD_V1"),
        (source.country, SourceField.COUNTRY, "COUNTRY_COLLECTED_FIELD_V1"),
    ):
        text = normalize_field(value)
        if not text:
            continue
        yield RuleHit(
            slot=Slot.LOCATION,
            kind=ConstraintKind.LOCATION,
            value=text,
            value_key=text,
            rule_id=rule_id,
            source_field=source_field,
            fragment=text,
        )


def read_posting(source: OpportunitySource) -> tuple[RuleHit, ...]:
    """Run every rule over one posting, and return everything they found."""
    title = normalize_field(source.canonical_title)
    description = normalize_description(source.description)
    parts = segments(description)

    hits: list[RuleHit] = []
    hits.extend(_opportunity_type_hits(title, parts, source.qualification_type))
    hits.extend(_education_hits(parts))
    hits.extend(_experience_hits(parts))
    hits.extend(_duration_hits(parts))
    hits.extend(_start_hits(parts))
    hits.extend(_work_mode_hits(parts, source.remote_type))
    hits.extend(
        _phrase_hits(
            parts, Slot.VISA_SPONSORSHIP, ConstraintKind.VISA_SPONSORSHIP,
            _SPONSORSHIP_NEGATIVE, VisaSponsorship.NOT_AVAILABLE,
            "SPONSORSHIP_NOT_AVAILABLE_V1",
            _SPONSORSHIP_POSITIVE, VisaSponsorship.AVAILABLE,
            "SPONSORSHIP_AVAILABLE_V1",
        )
    )
    hits.extend(
        _phrase_hits(
            parts, Slot.WORK_AUTHORIZATION, ConstraintKind.WORK_AUTHORIZATION,
            _AUTHORIZATION_NOT_REQUIRED, WorkAuthorization.NOT_REQUIRED,
            "WORK_AUTHORIZATION_NOT_REQUIRED_V1",
            _AUTHORIZATION_REQUIRED, WorkAuthorization.REQUIRED,
            "WORK_AUTHORIZATION_REQUIRED_V1",
        )
    )
    hits.extend(
        _phrase_hits(
            parts, Slot.CONVENTION, ConstraintKind.CONVENTION,
            _CONVENTION_NOT_REQUIRED, ConventionRequirement.NOT_REQUIRED,
            "CONVENTION_NOT_REQUIRED_V1",
            _CONVENTION_REQUIRED, ConventionRequirement.REQUIRED,
            "CONVENTION_REQUIRED_V1",
        )
    )
    hits.extend(_location_hits(source))
    return tuple(hits)
