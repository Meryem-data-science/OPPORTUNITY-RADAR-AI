"""Phase 3.2B: structured, UNVERIFIED candidates read out of a parsed CV.

This slice takes the `ParsedCv` Phase 3.2A produced and turns it into a list of
`ExtractedCandidate` values. A candidate means one thing and one thing only:

    a named deterministic rule found this text at this place in this document.

It never means "this is true of the person". Nothing here is verified,
accepted, corrected or rejected; there is no `profile_fact`, no validated
Master CV, no skill level or proficiency, no confidence and no score.

Phase 3.3A now provides the place a validated fact would live
(`services/digital_twin/facts/`), but this package is not connected to it: no
code here imports it, no candidate becomes a `profile_facts` row, and the
mapping from an `ExtractedCandidate` to a proposed fact is Phase 3.3B and does
not exist. The business normalization of institutions, employers and dates is
Phase 3.4 and does not exist either; skill aliases are resolved by Phase 3.4A,
from facts a human has accepted rather than from a candidate, so no mention
produced here reaches a normalized skill without being validated first. Phase
3.2 as a whole is still not a validated Master CV.

Every rule is local, free, deterministic and explainable — regular expressions,
closed dictionaries and fixed segmentation rules, all readable in this package.
No LLM, no external API, no commercial CV-parsing service and no network call
takes part. Nothing is persisted: no table, no migration, no SQLite write, no
row in `users` or `profiles`, and no file outside the export the operator
explicitly asks the CLI for. The result lives in memory.
"""

from services.digital_twin.cv.candidates.extractor import extract_candidates
from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    CandidateWarning,
    CandidateWarningCode,
    ExtractedCandidate,
    ExtractionRule,
    StructuredCvExtraction,
    candidate_fingerprint,
)

__all__ = [
    "CANDIDATE_EXTRACTOR_VERSION",
    "CandidateType",
    "CandidateWarning",
    "CandidateWarningCode",
    "ExtractedCandidate",
    "ExtractionRule",
    "StructuredCvExtraction",
    "candidate_fingerprint",
    "extract_candidates",
]
