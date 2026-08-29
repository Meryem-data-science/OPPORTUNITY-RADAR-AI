"""Phase 3.4B: verified facts about a person's history, projected onto rows.

This package adds a derived reading and no new truth. `profile_facts` stays the
source of truth, "verified" keeps its single definition — `status = 'ACCEPTED'`
— and the projection reads only:

    fact_type IN ('EXPERIENCE', 'PROJECT', 'EDUCATION', 'CERTIFICATION',
                  'LANGUAGE')
    AND status = 'ACCEPTED'

A `PROPOSED`, `REJECTED` or `CORRECTED` fact, and an `ACCEPTED` fact of any
other type, are invisible to it. Nothing from the Phase 3.2B extractor reaches
a row without passing through a fact a human accepted: this package imports no
CV module and reads no PDF.

The audit chain is a chain, not a copy:

    profile_experiences     → profile_facts → profile_fact_provenance
    profile_projects        → profile_facts → profile_fact_provenance
    profile_educations      → profile_facts → profile_fact_provenance
    profile_certifications  → profile_facts → profile_fact_provenance
    profile_languages       → profile_facts → profile_fact_provenance

so a projected row records only what the projection decided — which structurer
version, which structuring rule — and points at the fact for the rest. It
copies no `source_type`, `cv_sha256`, `parser_version`, `extractor_version` or
`provenance_key`.

**Nothing is invented, anywhere.** Every projected fragment is a piece of text
the document itself delimited with punctuation it wrote: a pipe-delimited
header for an experience or a diploma, a colon after an optional list marker
for a project, an explicit label for a certification, an explicit separator for
a language, and a period only in a closed set of explicit temporal forms.

Punctuation proves that segments exist; it never proves what they are about. So
a pipe-delimited education line is read as a school and a programme only when a
closed, tiny marker registry can tell them apart, and otherwise only its period
is kept. Where the wording is not unambiguous the fragment stays `NULL` and the
row records the type's `*_UNPARSED_V1` rule — **a `NULL` is worth more than an
invented value**. No employer is deduced from a sentence, no role from a
technology, no seniority from the word "stage", no duration, no calendar date
from a school year, no skill from a project description, no diploma from a
school, no school from a diploma, no `Bac+N` from the word "Master", no
obtained certification from a stated intention, no issuer, no obtention or
expiry date, no language from a text written in one, and no CEFR level from
"courant" or "fluent". There is no level, no confidence, no score and no
`match_score` in this package or in its schema.

It is offline and deterministic: no LLM, no API, no network, no embedding, no
fuzzy comparison, no similarity and no clock in the reading.

What this slice does **not** do: no availability, mobility, preference or
career objective; no eligibility rule, no opportunity constraint, no matching,
no `match_score`, no TF-IDF, no cosine similarity, no ranking, no
recommendation, no notification, no skill inference, no CV adaptation and no
auto-apply. It never touches `profile_skills` or `profile_skill_evidence`.
There is no HTTP endpoint, no web interface and no remote write path; the
operational database stays local SQLite.
"""

from services.digital_twin.structured_profile.models import (
    STRUCTURED_PROFILE_VERSION,
    ProfileCertification,
    ProfileEducation,
    ProfileExperience,
    ProfileLanguage,
    ProfileProject,
    StructuredCertification,
    StructuredEducation,
    StructuredEntry,
    StructuredExperience,
    StructuredLanguage,
    StructuredProfileError,
    StructuredProfileNotFoundError,
    StructuredProject,
    StructuringRule,
)
from services.digital_twin.structured_profile.repository import (
    StructuredProfileSynchronization,
    list_profile_certifications,
    list_profile_educations,
    list_profile_experiences,
    list_profile_languages,
    list_profile_projects,
    synchronize_structured_profile_entries,
)
from services.digital_twin.structured_profile.structurer import (
    BULLET_MARKERS,
    CERTIFICATION_LABELS,
    INSTITUTION_MARKERS,
    INTENTION_MARKERS,
    ISSUER_LABELS,
    MONTH_NAMES,
    OPEN_END_MARKERS,
    PROFICIENCY_FORMS,
    is_explicit_period,
    is_explicit_proficiency,
    names_an_institution,
    states_an_intention,
    structure_certification,
    structure_education,
    structure_experience,
    structure_language,
    structure_project,
)

__all__ = [
    "BULLET_MARKERS",
    "CERTIFICATION_LABELS",
    "INSTITUTION_MARKERS",
    "INTENTION_MARKERS",
    "ISSUER_LABELS",
    "MONTH_NAMES",
    "OPEN_END_MARKERS",
    "PROFICIENCY_FORMS",
    "STRUCTURED_PROFILE_VERSION",
    "ProfileCertification",
    "ProfileEducation",
    "ProfileExperience",
    "ProfileLanguage",
    "ProfileProject",
    "StructuredCertification",
    "StructuredEducation",
    "StructuredEntry",
    "StructuredExperience",
    "StructuredLanguage",
    "StructuredProfileError",
    "StructuredProfileNotFoundError",
    "StructuredProfileSynchronization",
    "StructuredProject",
    "StructuringRule",
    "is_explicit_period",
    "is_explicit_proficiency",
    "list_profile_certifications",
    "list_profile_educations",
    "list_profile_experiences",
    "list_profile_languages",
    "list_profile_projects",
    "names_an_institution",
    "states_an_intention",
    "structure_certification",
    "structure_education",
    "structure_experience",
    "structure_language",
    "structure_project",
    "synchronize_structured_profile_entries",
]
