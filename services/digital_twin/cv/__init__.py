"""Phase 3.2A: the local, deterministic foundation of the CV PDF parser.

This slice reads a local PDF, normalizes its text conservatively, keeps the
page each fragment came from, and splits the document on section headings it
recognises from a fixed French/English lexicon. That is all it does.

It asserts no fact about the person. No `profile_facts` row, no accept/correct/
reject workflow, no validated Master CV, no skill table or skill level, no
matching, eligibility, ranking or score, and no persistence of anything read
here into the Digital Twin exist yet — they belong to later slices. Nothing in
this package calls an LLM, a CV-parsing service, or any remote endpoint: the
whole parse is local, offline and reproducible.
"""

from services.digital_twin.cv.models import (
    PARSER_VERSION,
    DetectedSection,
    ExtractedPage,
    ParsedCv,
    ParserWarning,
    SectionType,
    WarningCode,
)
from services.digital_twin.cv.parser import parse_cv_pdf
from services.digital_twin.cv.pdf import (
    EmptyPdfTextError,
    EncryptedPdfError,
    InvalidPdfError,
    PdfExtractionError,
    PdfFileNotFoundError,
    PdfNotAFileError,
)

__all__ = [
    "PARSER_VERSION",
    "DetectedSection",
    "EmptyPdfTextError",
    "EncryptedPdfError",
    "ExtractedPage",
    "InvalidPdfError",
    "ParsedCv",
    "ParserWarning",
    "PdfExtractionError",
    "PdfFileNotFoundError",
    "PdfNotAFileError",
    "SectionType",
    "WarningCode",
    "parse_cv_pdf",
]
