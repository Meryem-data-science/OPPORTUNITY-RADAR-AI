"""Reading the two sides out of SQLite, and stating plainly what is not there.

This is the only module of Phase 3.6 that runs a query. It reads through the
repositories Phases 3.4 and 3.5 already own — `read_opportunity_constraints`,
`read_opportunity_requirements`, `list_profile_languages`,
`list_profile_skills`, `get_profile_preferences` — rather than issuing its own
SQL over their tables, so the shapes those phases guarantee stay theirs to
guarantee. It writes nothing, anywhere.

It also does the honest, uncomfortable half of the job: saying which comparisons
the current schema cannot support. Three of them, each recorded as a named
constant below so that a reader of the code, an operator reading a report and a
test asserting the limitation are all looking at the same sentence.

The pattern in each case is the same, and it is the rule of this whole phase:

    the datum is absent -> the input field stays empty -> the rule answers
    UNKNOWN -> the verdict is UNKNOWN

and never: the datum is absent, therefore the candidate does not qualify. The
engine is written to compare these things properly the day the Digital Twin
records them; today it says it cannot, which is a smaller claim than any
alternative and the only true one.
"""

from __future__ import annotations

import sqlite3
import unicodedata

from services.collector.extractors.opportunity_constraints.repository import (
    read_opportunity_constraints,
)
from services.collector.extractors.opportunity_constraints.requirements.language_catalog import (
    LANGUAGE_CATALOG,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    RequirementLevel,
)
from services.collector.extractors.opportunity_constraints.requirements.repository import (
    read_opportunity_requirements,
)
from services.digital_twin.preferences.repository import get_profile_preferences
from services.digital_twin.skills.repository import list_profile_skills
from services.digital_twin.structured_profile.repository import list_profile_languages
from services.eligibility.models import (
    ConventionCapability,
    EducationRequirementInput,
    ExperienceRequirementInput,
    LanguageRequirementInput,
    OpportunityEligibilityInput,
    ProfileEligibilityInput,
    ProfileLanguageInput,
    RequirementAmbiguityInput,
    RequirementKind,
    SkillRequirementInput,
    SponsorshipNeed,
)

__all__ = [
    "EDUCATION_LIMITATION",
    "ENROLLMENT_LIMITATION",
    "EXPERIENCE_LIMITATION",
    "SCHEMA_LIMITATIONS",
    "EligibilityInputError",
    "language_key_for",
    "load_opportunity_input",
    "load_profile_input",
]


class EligibilityInputError(RuntimeError):
    """Raised when one side's inputs cannot be assembled from the database."""


#: Why every education comparison currently answers UNKNOWN rather than either
#: verdict. `0010` states it as a design decision of Phase 3.4, not an oversight:
#: an education is stored as the institution, the programme and the period a CV
#: wrote, verbatim, with "no `degree_level`, no `bac_plus` and no
#: `graduation_year`". Deriving `BAC_PLUS_5` from the words of a programme title
#: here would be exactly the fragile free-text reading Phase 3.5 was built to
#: replace on the other side, and it would do it in the one direction that can
#: reject somebody. The engine's education rule is complete and tested; it will
#: decide the day a normalized level exists to feed it.
EDUCATION_LIMITATION = (
    "Phase 3.4 stores education verbatim and normalizes no degree level, so "
    "eligibility-rules-v1 answers UNKNOWN whenever a posting states an "
    "education requirement."
)

#: Why the enrolment rule never fires. Neither side represents it: Phase 3.5
#: extracts no "must currently be enrolled" requirement, and Phase 3.4 records
#: no current-student fact. Manufacturing the offer side from an internship
#: type, from the word *student*, or from `convention_requirement` would invent
#: a demand no employer wrote; manufacturing the profile side from a degree or
#: from a period ending in a future year would invent a status no person stated.
ENROLLMENT_LIMITATION = (
    "Neither Phase 3.5 nor Phase 3.4 represents current enrolment, so "
    "eligibility-rules-v1 reports it NOT_APPLICABLE and never infers it."
)

#: Why a quantified experience requirement answers UNKNOWN. Phase 3.4 stores a
#: period as the text the CV wrote — "Jan 2023 – Jun 2023", "2 ans" — and
#: normalizes no total. Two refusals are stacked here on purpose: this module
#: will not parse those strings into months, and even a total would not answer
#: "three years of data engineering", because Phase 3.5 stores the number
#: without the field it qualified. Deciding that a data analyst's months count
#: towards a data engineer's requirement is role similarity, which is Phase 4.
EXPERIENCE_LIMITATION = (
    "Phase 3.4 normalizes no experience duration and Phase 3.5 stores no domain "
    "beside a quantity, so eligibility-rules-v1 answers UNKNOWN whenever a "
    "posting states a REQUIRED quantified experience requirement."
)

#: The three, in one place, so a report can print them without repeating them.
SCHEMA_LIMITATIONS: tuple[str, ...] = (
    EDUCATION_LIMITATION,
    ENROLLMENT_LIMITATION,
    EXPERIENCE_LIMITATION,
)

#: Every spelling the shared registry knows, folded, pointing at its key. The
#: registry is `0013`'s, reused rather than copied: the offer side and the
#: profile side name a language the same way or they are not comparable at all,
#: which is the argument `0008` already made for skills.
_LANGUAGE_ALIASES: dict[str, str] = {
    " ".join(unicodedata.normalize("NFKC", alias).split()).casefold(): term.language_key
    for term in LANGUAGE_CATALOG
    for alias in term.aliases
}

_REQUIREMENT_KINDS = {
    RequirementLevel.REQUIRED: RequirementKind.REQUIRED,
    RequirementLevel.PREFERRED: RequirementKind.PREFERRED,
}

#: 3.5A's `ExperienceObligation` as this phase reads it. `UNKNOWN` becomes
#: `None`: the posting named a quantity and did not say it was a condition, and
#: the strict test for a contradiction begins with an explicit obligation.
_EXPERIENCE_KINDS: dict[str, RequirementKind | None] = {
    "REQUIRED": RequirementKind.REQUIRED,
    "PREFERRED": RequirementKind.PREFERRED,
    "UNKNOWN": None,
}


def language_key_for(text: str | None) -> str | None:
    """The registry key this written language name identifies, or None.

    None for `None`, for the empty string, and for any name the closed registry
    does not know — a language nobody registered is a language this engine
    cannot compare, and guessing from a prefix would make `Anglo-Saxon studies`
    into English.
    """
    if text is None:
        return None
    folded = " ".join(unicodedata.normalize("NFKC", text).split()).casefold()
    if not folded:
        return None
    return _LANGUAGE_ALIASES.get(folded)


def load_opportunity_input(
    connection: sqlite3.Connection, opportunity_id: int
) -> OpportunityEligibilityInput | None:
    """One posting's demands, or None when Phase 3.5 has not read it.

    Both halves are required. A posting with a 3.5A projection and no 3.5B
    reading has had its languages and skills read by nobody, and answering as
    though it demanded no language would be reporting a silence the extractor
    never produced.
    """
    constraints = read_opportunity_constraints(connection, opportunity_id)
    if constraints is None:
        return None
    requirements = read_opportunity_requirements(connection, opportunity_id)
    if requirements is None:
        return None

    return OpportunityEligibilityInput(
        opportunity_id=opportunity_id,
        constraints_extractor_version=constraints.extractor_version,
        requirements_extractor_version=requirements.extractor_version,
        education=tuple(
            EducationRequirementInput(level=str(item.level), mode=str(item.mode))
            for item in constraints.education
        ),
        # Never derived. See `ENROLLMENT_LIMITATION`.
        enrollment_required=None,
        experience=tuple(
            ExperienceRequirementInput(
                min_months=item.min_months,
                max_months=item.max_months,
                kind=_EXPERIENCE_KINDS[str(item.obligation)],
            )
            for item in constraints.experience
        ),
        languages=tuple(
            LanguageRequirementInput(
                language_key=item.language_key,
                kind=_REQUIREMENT_KINDS[item.requirement],
                proficiency_text=item.proficiency_text,
            )
            for item in requirements.languages
        ),
        skills=tuple(
            SkillRequirementInput(
                canonical_key=item.canonical_key,
                kind=_REQUIREMENT_KINDS[item.requirement],
            )
            for item in requirements.skills
        ),
        ambiguities=tuple(
            RequirementAmbiguityInput(kind=str(item.kind), reason=str(item.reason))
            for item in requirements.ambiguities
        ),
        visa_sponsorship=str(constraints.visa_sponsorship),
        work_authorization=str(constraints.work_authorization),
        convention=str(constraints.convention),
    )


def load_profile_input(
    connection: sqlite3.Connection, profile_id: int
) -> ProfileEligibilityInput:
    """One person's relevant facts, and only the relevant ones.

    Five reads, and each is here because a rule needs it: the projected
    languages, the projected skills, and — from the one stated-preferences row —
    the convention capability and the sponsorship need. Nothing reads
    `profile_facts` directly: the projections are Phase 3.4's own answer to
    "which facts are verified", and asking the question a second way here would
    let this phase disagree with the phase that owns it.

    Nothing reads the person's name, contact details, links, certifications,
    projects, career objectives, availability or mobility. They are not
    parameters of any rule, so reading them would only put them in the digest
    and make an edited telephone number recompute a verdict it cannot affect.

    A language whose written name the closed registry does not know is dropped
    rather than guessed at. That costs nothing a verdict depends on: an unknown
    language can only fail to match a requirement, and a requirement with no
    matching profile language already answers UNKNOWN.
    """
    languages: list[ProfileLanguageInput] = []
    seen: set[str] = set()
    for row in list_profile_languages(connection, profile_id):
        key = language_key_for(row.language_text)
        if key is None or key in seen:
            # One person stating one language twice is one language. The first
            # projected row wins, deterministically: the reader orders by
            # `fact_id`, so this is the oldest stated fact rather than whichever
            # row SQLite happened to return first.
            continue
        seen.add(key)
        languages.append(
            ProfileLanguageInput(
                language_key=key, proficiency_text=row.proficiency_text
            )
        )

    preferences = get_profile_preferences(connection, profile_id)
    sponsorship_need = SponsorshipNeed.UNKNOWN
    convention_capability = ConventionCapability.UNKNOWN
    if preferences is not None:
        stated = preferences.value
        sponsorship_need = SponsorshipNeed(str(stated.visa_sponsorship_required))
        convention_capability = ConventionCapability(str(stated.convention_status))

    return ProfileEligibilityInput(
        profile_id=profile_id,
        # Empty is UNKNOWN. See `EDUCATION_LIMITATION`.
        education_levels=(),
        # See `ENROLLMENT_LIMITATION`.
        currently_enrolled=None,
        # See `EXPERIENCE_LIMITATION`.
        comparable_experience_months=None,
        languages=tuple(languages),
        skills=tuple(
            skill.canonical_key for skill in list_profile_skills(connection, profile_id)
        ),
        sponsorship_need=sponsorship_need,
        convention_capability=convention_capability,
    )
