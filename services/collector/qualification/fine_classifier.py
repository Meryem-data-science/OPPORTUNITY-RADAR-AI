"""Deterministic, explainable fine Data/AI category classifier.

The coarse classifier in :mod:`.classifier` decides whether an opportunity is
Data/AI at all. This module answers the second question — *which* Data/AI
sub-domain — and it runs only on opportunities the coarse classifier already
qualified as ``CORE_TARGET`` or ``ADJACENT_TARGET``.

Everything here is a pure function over normalized title and description text.
Location, employer, source and profile are not parameters, so geography and
employer neutrality are structural rather than a rule that could be forgotten.
"""

from dataclasses import asdict, dataclass

from .classifier import contains_phrase, matched_phrases, normalize_text
from .fine_taxonomy import (
    FINE_CATEGORY_PRECEDENCE, FINE_CONTEXT_SIGNALS, FINE_DESCRIPTION_CONCEPTS,
    FINE_ROLE_SIGNALS, MINIMUM_DESCRIPTION_CONCEPTS,
    EvidenceField, EvidenceKind, FineCategory,
)
from .taxonomy import Qualification


FINE_CLASSIFIER_VERSION = "fine-data-ai-rules-v1"

#: Only these two coarse outcomes are demonstrably Data/AI. ``UNCERTAIN`` and
#: ``OUT_OF_SCOPE`` receive no fine category at all, because absence of evidence
#: is not contradictory evidence and unknown is not ``OTHER``.
FINE_ELIGIBLE_QUALIFICATIONS = (Qualification.CORE_TARGET, Qualification.ADJACENT_TARGET)

NOT_QUALIFIED_REASON = "opportunity is not qualified as Data/AI; no fine category is assigned"
NO_EVIDENCE_REASON = (
    "qualified as Data/AI but no supported fine category is evidenced; classified OTHER"
)
TITLE_EVIDENCE_REASON = "fine categories evidenced by the title take precedence"
DESCRIPTION_EVIDENCE_REASON = (
    "no fine title evidence; concrete description concepts decide the primary category"
)


@dataclass(frozen=True)
class FineEvidence:
    """One explainable reason a fine category was considered."""

    category: FineCategory
    field: EvidenceField
    kind: EvidenceKind
    signal: str


@dataclass(frozen=True)
class FineClassification:
    #: ``None`` means "no safe fine category", never ``OTHER``.
    primary_category: FineCategory | None
    secondary_categories: tuple[FineCategory, ...]
    evidence: tuple[FineEvidence, ...]
    reasons: tuple[str, ...]
    classifier_version: str = FINE_CLASSIFIER_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _independent(signal: str, others: tuple[str, ...]) -> bool:
    """Reject a phrase that is nested inside a longer matched phrase."""
    return not any(other != signal and contains_phrase(other, signal) for other in others)


def _title_evidence(title: str) -> tuple[FineEvidence, ...]:
    """Match complete role phrases first, then the context phrases they do not contain."""
    roles = {
        category: matched_phrases(title, signals)
        for category, signals in FINE_ROLE_SIGNALS.items()
    }
    all_roles = tuple(signal for signals in roles.values() for signal in signals)
    kept_roles = {
        category: tuple(signal for signal in signals if _independent(signal, all_roles))
        for category, signals in roles.items()
    }
    contexts = {
        category: matched_phrases(title, signals)
        for category, signals in FINE_CONTEXT_SIGNALS.items()
    }
    stronger = tuple(
        signal for signals in kept_roles.values() for signal in signals
    ) + tuple(signal for signals in contexts.values() for signal in signals)
    kept_contexts = {
        category: tuple(signal for signal in signals if _independent(signal, stronger))
        for category, signals in contexts.items()
    }
    return tuple(
        FineEvidence(category, EvidenceField.TITLE, kind, signal)
        for category in FINE_CATEGORY_PRECEDENCE
        for kind, table in (
            (EvidenceKind.ROLE_PHRASE, kept_roles), (EvidenceKind.CONTEXT_PHRASE, kept_contexts),
        )
        for signal in table.get(category, ())
    )


def _description_evidence(description: str) -> tuple[FineEvidence, ...]:
    """Keep only categories showing several independent concrete concepts."""
    matched: dict[FineCategory, tuple[str, ...]] = {}
    for category, concepts in FINE_DESCRIPTION_CONCEPTS.items():
        # One concept contributes one signal: the first alias it matched, so
        # naming the same idea twice never counts as two pieces of evidence.
        found = tuple(
            hits[0] for aliases in concepts.values()
            if (hits := matched_phrases(description, aliases))
        )
        if len(found) >= MINIMUM_DESCRIPTION_CONCEPTS:
            matched[category] = found
    return tuple(
        FineEvidence(category, EvidenceField.DESCRIPTION, EvidenceKind.CONCRETE_CONCEPT, signal)
        for category in FINE_CATEGORY_PRECEDENCE
        for signal in matched.get(category, ())
    )


def _ordered(categories: set[FineCategory]) -> tuple[FineCategory, ...]:
    return tuple(category for category in FINE_CATEGORY_PRECEDENCE if category in categories)


def classify_fine_categories(
    title: str | None,
    description: str | None = None,
    *,
    qualification: Qualification,
) -> FineClassification:
    """Assign fine Data/AI categories to an already-qualified opportunity.

    The primary category is chosen by :data:`FINE_CATEGORY_PRECEDENCE` among the
    title-evidenced categories, and only when the title evidences nothing among
    the description-evidenced ones. That keeps the existing title-first evidence
    strategy: a "Data Engineer" whose description also describes model serving
    stays primarily Data Engineering, with MLOps exposed as a secondary.
    """
    if qualification not in FINE_ELIGIBLE_QUALIFICATIONS:
        return FineClassification(None, (), (), (NOT_QUALIFIED_REASON,))

    title_evidence = _title_evidence(normalize_text(title))
    description_evidence = _description_evidence(normalize_text(description))
    evidence = title_evidence + description_evidence
    if not evidence:
        return FineClassification(FineCategory.OTHER, (), (), (NO_EVIDENCE_REASON,))

    primary_pool = {item.category for item in (title_evidence or description_evidence)}
    matched = _ordered({item.category for item in evidence})
    primary = next(category for category in FINE_CATEGORY_PRECEDENCE if category in primary_pool)
    secondaries = tuple(category for category in matched if category is not primary)
    reason = TITLE_EVIDENCE_REASON if title_evidence else DESCRIPTION_EVIDENCE_REASON
    return FineClassification(primary, secondaries, evidence, (reason,))
