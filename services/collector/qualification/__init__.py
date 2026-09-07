"""Read-only deterministic opportunity qualification and fine Data/AI categories."""

from .classifier import (
    CLASSIFIER_VERSION, Classification, classify_opportunity, contains_phrase,
    matched_phrases, normalize_text,
)
from .fine_classifier import (
    FINE_CLASSIFIER_VERSION, FineClassification, FineEvidence, classify_fine_categories,
)
from .fine_taxonomy import FINE_CATEGORY_PRECEDENCE, EvidenceField, EvidenceKind, FineCategory
from .taxonomy import Domain, EmploymentType, ListingQuality, OpportunityType, Qualification

__all__ = [
    "CLASSIFIER_VERSION", "Classification", "Domain", "EmploymentType",
    "FINE_CATEGORY_PRECEDENCE", "FINE_CLASSIFIER_VERSION", "EvidenceField", "EvidenceKind",
    "FineCategory", "FineClassification", "FineEvidence", "ListingQuality", "OpportunityType",
    "Qualification", "classify_fine_categories", "classify_opportunity", "contains_phrase",
    "matched_phrases", "normalize_text",
]
