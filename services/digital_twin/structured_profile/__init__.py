"""Phase 3.4B1: verified EXPERIENCE and PROJECT facts, projected onto rows.

This slice adds a derived reading and no new truth. `profile_facts` stays the
source of truth, "verified" keeps its single definition — `status = 'ACCEPTED'`
— and the projection reads only:

    fact_type IN ('EXPERIENCE', 'PROJECT') AND status = 'ACCEPTED'

A `PROPOSED`, `REJECTED` or `CORRECTED` fact, and an `ACCEPTED` fact of any
other type, are invisible to it. Nothing from the Phase 3.2B extractor reaches
a row without passing through a fact a human accepted: this package imports no
CV module and reads no PDF.

The audit chain is a chain, not a copy:

    profile_experiences → profile_facts → profile_fact_provenance
    profile_projects    → profile_facts → profile_fact_provenance

so a projected row records only what the projection decided — which structurer
version, which structuring rule — and points at the fact for the rest. It
copies no `source_type`, `cv_sha256`, `parser_version`, `extractor_version` or
`provenance_key`.

**Nothing is invented, anywhere.** Every projected fragment is a piece of text
the document itself delimited with punctuation it wrote: a pipe-delimited
header for an experience, a colon after an optional list marker for a project,
and a period only in a closed set of explicit temporal forms. Where the wording
is not unambiguous the fragment stays `NULL` and the row records `UNPARSED_V1`
— **a `NULL` is worth more than an invented value**. No employer is deduced
from a sentence, no role from a technology, no seniority from the word
"stage", no duration, no calendar date from a school year, no skill from a
project description, no certification and no language. There is no level, no
confidence, no score and no `match_score` in this package or in its schema.

It is offline and deterministic: no LLM, no API, no network, no embedding, no
fuzzy matching, no similarity and no clock in the reading.

What this slice does **not** do: no structured education, certification or
language; no availability, mobility, preference or career objective; no
eligibility rule, no opportunity constraint, no matching, no `match_score`, no
TF-IDF, no cosine similarity, no ranking, no recommendation, no notification,
no CV adaptation and no auto-apply. It never touches `profile_skills` or
`profile_skill_evidence`. There is no HTTP endpoint, no web interface and no
remote write path; the operational database stays local SQLite.
"""

from services.digital_twin.structured_profile.models import (
    STRUCTURED_PROFILE_VERSION,
    ProfileExperience,
    ProfileProject,
    StructuredExperience,
    StructuredProfileError,
    StructuredProfileNotFoundError,
    StructuredProject,
    StructuringRule,
)
from services.digital_twin.structured_profile.repository import (
    StructuredProfileSynchronization,
    list_profile_experiences,
    list_profile_projects,
    synchronize_structured_profile_entries,
)
from services.digital_twin.structured_profile.structurer import (
    BULLET_MARKERS,
    MONTH_NAMES,
    OPEN_END_MARKERS,
    is_explicit_period,
    structure_experience,
    structure_project,
)

__all__ = [
    "BULLET_MARKERS",
    "MONTH_NAMES",
    "OPEN_END_MARKERS",
    "STRUCTURED_PROFILE_VERSION",
    "ProfileExperience",
    "ProfileProject",
    "StructuredExperience",
    "StructuredProfileError",
    "StructuredProfileNotFoundError",
    "StructuredProfileSynchronization",
    "StructuredProject",
    "StructuringRule",
    "is_explicit_period",
    "list_profile_experiences",
    "list_profile_projects",
    "structure_experience",
    "structure_project",
    "synchronize_structured_profile_entries",
]
