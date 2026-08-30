"""Phase 3.5B: the skills and the languages an opportunity explicitly asks for.

    opportunities.description
        -> parse_requirement_segments   (sections: REQUIRED / PREFERRED / NEUTRAL)
        -> extract_opportunity_requirements (pure, offline, deterministic)
             -> opportunity_skill_requirements      (the table 0012 reserved)
             |     +-> opportunity_skill_requirement_evidence
             +-> opportunity_language_requirements
             |     +-> opportunity_language_requirement_evidence
             +-> opportunity_requirement_ambiguities
             +-> opportunity_requirement_extraction_state

It continues Phase 3.5A rather than duplicating it: the same normalized text,
the same evidence discipline, the same idempotence pattern, and every row
hanging off `opportunity_constraints` so a posting nobody has run 3.5A over
cannot receive a 3.5B reading.

The question is only ever **what does this posting ask for**. It is never
*does anybody have it* — there is no profile here, no comparison to one, no
gap, no verdict, no score and no rank. Phase 3.6 joins the two sides; Phase 4
ranks; neither exists.

A mention is not a requirement: "our stack includes Python" describes a
company, and "no prior Python experience is required" is the opposite of a
demand. An `or` is not an `and`: "Python or R required" is one requirement
satisfied two ways, and storing both would let a later phase demand both — so
v1 stores neither and records the refusal, because UNKNOWN on the record is
worth more than UNKNOWN by silence. A level is what the posting wrote:
`Fluent` never becomes `C1`, and no language is ever inferred from a country,
a city or the language the advertisement happens to be written in.
"""

from services.collector.extractors.opportunity_constraints.requirements.extractor import (
    extract_opportunity_requirements,
    requirement_source_fingerprint,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    REQUIREMENT_EXTRACTOR_VERSION,
    ExtractedRequirements,
    LanguageRequirement,
    RequirementAmbiguity,
    RequirementLevel,
    RequirementSource,
    SkillRequirement,
)

__all__ = [
    "ExtractedRequirements",
    "LanguageRequirement",
    "REQUIREMENT_EXTRACTOR_VERSION",
    "RequirementAmbiguity",
    "RequirementLevel",
    "RequirementSource",
    "SkillRequirement",
    "extract_opportunity_requirements",
    "requirement_source_fingerprint",
]
