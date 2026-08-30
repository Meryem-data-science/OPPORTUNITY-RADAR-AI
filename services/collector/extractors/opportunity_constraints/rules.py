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
_TYPE_SIGNALS: tuple[tuple[OpportunityType, str, tuple[str, ...]], ...] = (
    (
        OpportunityType.PFE,
        "OPPORTUNITY_TYPE_PFE_V1",
        ("pfe", "projet de fin d'etudes", "projet de fin d'études",
         "stage de fin d'etudes", "stage de fin d'études", "final year project",
         "final-year internship", "end of studies internship"),
    ),
    (
        OpportunityType.PFA,
        "OPPORTUNITY_TYPE_PFA_V1",
        ("pfa", "projet de fin d'annee", "projet de fin d'année"),
    ),
    (
        OpportunityType.ALTERNANCE,
        "OPPORTUNITY_TYPE_ALTERNANCE_V1",
        ("alternance", "alternant", "alternante", "apprenticeship",
         "contrat d'apprentissage", "contrat de professionnalisation"),
    ),
    (
        OpportunityType.SUMMER_INTERNSHIP,
        "OPPORTUNITY_TYPE_SUMMER_V1",
        ("summer internship", "stage d'ete", "stage d'été", "summer intern program"),
    ),
    (
        OpportunityType.PRE_HIRE_INTERNSHIP,
        "OPPORTUNITY_TYPE_PRE_HIRE_V1",
        ("stage pre-embauche", "stage pré-embauche", "pre-hire internship",
         "internship with a view to hiring", "stage pre embauche"),
    ),
    (
        OpportunityType.INTERNSHIP,
        "OPPORTUNITY_TYPE_INTERNSHIP_V1",
        ("internship", "intern position", "stage", "stagiaire"),
    ),
    (
        OpportunityType.FIRST_JOB,
        "OPPORTUNITY_TYPE_FIRST_JOB_V1",
        ("first job", "premier emploi", "graduate programme", "graduate program",
         "jeune diplome", "jeune diplômé", "new graduate", "entry level",
         "entry-level"),
    ),
    (
        OpportunityType.JUNIOR_ROLE,
        "OPPORTUNITY_TYPE_JUNIOR_V1",
        ("junior",),
    ),
)

#: Which field settles the type when two disagree. The title wins, exactly as
#: the Phase 2 classifier already decides it — "infer from title first;
#: ordinary description vocabulary cannot override it" — because a description
#: that mentions an internship inside a posting titled PFE is describing the
#: same thing at a coarser grain, not contradicting it. Within one tier a
#: disagreement is a real conflict and nothing is asserted. The derived
#: classifier reading is the weakest tier: it is another program's opinion,
#: not the posting's words.
TYPE_FIELD_PRECEDENCE: tuple[SourceField, ...] = (
    SourceField.TITLE,
    SourceField.DESCRIPTION,
    SourceField.QUALIFICATION_TYPE,
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
    for field, source_field, texts in (
        ("title", SourceField.TITLE, (title,) if title else ()),
        ("description", SourceField.DESCRIPTION, tuple(description_segments)),
    ):
        for fragment in texts:
            folded = _folded(fragment)
            for opportunity_type, rule_id, signals in _TYPE_SIGNALS:
                matched = next((s for s in signals if s in folded), None)
                if matched is None:
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

_EXPERIENCE_WORDS = ("experience", "expérience", "experiences", "expériences")
_YEARS = r"(?:years?|yrs?|ans?|années?|annees?)"
_MONTHS = r"(?:months?|mois)"
_RANGE = re.compile(
    rf"\b(\d{{1,2}})\s*(?:-|–|to|à|a|and|et)\s*(\d{{1,2}})\s*\+?\s*({_YEARS}|{_MONTHS})\b",
    re.IGNORECASE,
)
_AT_LEAST = re.compile(
    rf"\b(?:at least|minimum(?: of)?|min\.?|au moins|minimum de|plus de|over)\s*"
    rf"(\d{{1,2}})\s*({_YEARS}|{_MONTHS})\b",
    re.IGNORECASE,
)
_PLUS = re.compile(rf"\b(\d{{1,2}})\s*\+\s*({_YEARS}|{_MONTHS})\b", re.IGNORECASE)
_REQUIRED_MARKERS = (
    "required", "must have", "requis", "obligatoire", "exige", "exigé",
    "minimum", "at least", "au moins", "we require",
)
_PREFERRED_MARKERS = (
    "preferred", "nice to have", "a plus", "is a plus", "souhaite", "souhaité",
    "souhaitee", "souhaitée", "apprecie", "apprécié", "ideally", "idealement",
    "idéalement", "bonus",
)


def _to_months(quantity: int, unit: str) -> int:
    return quantity * 12 if re.match(_YEARS, unit, re.IGNORECASE) else quantity


def _experience_hits(description_segments: Sequence[str]) -> Iterator[RuleHit]:
    """Read quantities and obligations, and keep them attached to each other.

    An obligation qualifies a requirement, so a sentence carrying both — "at
    least 3 years of experience required" — settles the obligation for the
    quantity it states. A sentence carrying only an obligation word is used
    only when no sentence quantified anything: otherwise "3 years required"
    beside "Spark experience is preferred" would read as one posting
    contradicting itself, when it is really two different requirements.
    """
    quantified: list[tuple[str, tuple[int | None, int | None], str]] = []
    obligations: list[tuple[str, ExperienceObligation, bool]] = []

    for fragment in description_segments:
        folded = _folded(fragment)
        if not any(word in folded for word in _EXPERIENCE_WORDS):
            # No experience word, no experience claim. A seniority adjective in
            # a title is not a number and never reaches this function anyway.
            continue

        bounds: tuple[int | None, int | None] | None = None
        rule_id = ""
        if (match := _RANGE.search(fragment)) is not None:
            unit = match.group(3)
            low, high = int(match.group(1)), int(match.group(2))
            if low <= high:
                bounds = (_to_months(low, unit), _to_months(high, unit))
                rule_id = "EXPERIENCE_RANGE_V1"
        if bounds is None and (match := _PLUS.search(fragment)) is not None:
            bounds = (_to_months(int(match.group(1)), match.group(2)), None)
            rule_id = "EXPERIENCE_MIN_PLUS_V1"
        if bounds is None and (match := _AT_LEAST.search(fragment)) is not None:
            bounds = (_to_months(int(match.group(1)), match.group(2)), None)
            rule_id = "EXPERIENCE_MIN_YEARS_V1"
        if bounds is not None:
            quantified.append((fragment, bounds, rule_id))

        obligation: ExperienceObligation | None = None
        if any(marker in folded for marker in _PREFERRED_MARKERS):
            obligation = ExperienceObligation.PREFERRED
        elif any(marker in folded for marker in _REQUIRED_MARKERS):
            obligation = ExperienceObligation.REQUIRED
        if obligation is not None:
            obligations.append((fragment, obligation, bounds is not None))

    for fragment, (minimum, maximum), rule_id in quantified:
        yield RuleHit(
            slot=Slot.EXPERIENCE_BOUNDS,
            kind=ConstraintKind.EXPERIENCE,
            value=(minimum, maximum),
            value_key=f"{'' if minimum is None else minimum}-"
            f"{'' if maximum is None else maximum}",
            rule_id=rule_id,
            source_field=SourceField.DESCRIPTION,
            fragment=fragment,
        )

    attached = [item for item in obligations if item[2]]
    chosen = attached if attached else ([] if quantified else obligations)
    for fragment, obligation, _ in chosen:
        yield RuleHit(
            slot=Slot.EXPERIENCE_OBLIGATION,
            kind=ConstraintKind.EXPERIENCE,
            value=obligation,
            value_key=obligation.value,
            rule_id=f"EXPERIENCE_{obligation.value}_V1",
            source_field=SourceField.DESCRIPTION,
            fragment=fragment,
        )


# --------------------------------------------------------------------------
# Duration
# --------------------------------------------------------------------------

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
        if any(word in folded for word in _EXPERIENCE_WORDS):
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
_WORK_MODE_SIGNALS: tuple[tuple[WorkMode, tuple[str, ...]], ...] = (
    (WorkMode.HYBRID, ("hybrid", "hybride", "mode hybride")),
    (
        WorkMode.REMOTE,
        ("fully remote", "100% remote", "full remote", "remote position",
         "remote role", "work from home", "teletravail", "télétravail",
         "remote-first", "remote first", "entierement a distance",
         "entièrement à distance"),
    ),
    (
        WorkMode.ON_SITE,
        ("on-site", "on site", "onsite", "sur site", "en presentiel",
         "en présentiel", "presentiel", "présentiel"),
    ),
)
#: A refusal of remote is not a claim of on-site. "No remote work" says where
#: the work is not done; it does not say the office is mandatory, and this
#: package will not fill in the rest. The phrases exist here only to stop the
#: REMOTE rule from firing on the word inside them.
_NO_REMOTE = (
    "no remote", "not remote", "without remote", "pas de teletravail",
    "pas de télétravail", "no remote work", "aucun teletravail",
    "aucun télétravail", "remote is not", "no telework",
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
        folded = _folded(fragment)
        suppressed = any(phrase in folded for phrase in _NO_REMOTE)
        for work_mode, signals in _WORK_MODE_SIGNALS:
            if work_mode is WorkMode.REMOTE and suppressed:
                continue
            if any(signal in folded for signal in signals):
                yield RuleHit(
                    slot=Slot.WORK_MODE,
                    kind=ConstraintKind.WORK_MODE,
                    value=work_mode,
                    value_key=work_mode.value,
                    rule_id=f"WORK_MODE_{work_mode.value}_V1",
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
