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

from .classifier import (
    advisory_role_families, contains_phrase, matched_phrases, normalize_text,
    technical_role_families,
)
from .fine_taxonomy import (
    FINE_CATEGORY_PRECEDENCE, FINE_CONTEXT_SIGNALS, FINE_DESCRIPTION_CONCEPTS,
    FINE_ROLE_SIGNALS, MINIMUM_DESCRIPTION_CONCEPTS, SINGLE_CONCEPT_FALLBACK_MINIMUM,
    EvidenceField, EvidenceKind, FineCategory,
)
from .taxonomy import Qualification


#: Bumped from ``fine-data-ai-rules-v1`` by the Phase 8A.2 calibration: the
#: single-concept fallback and the concrete NLP/computer-vision concepts change
#: observable fine output. It stays independent of ``CLASSIFIER_VERSION``.
#: Bumped to ``v3`` by the Phase 11.3A-R4A French correction: the artificial
#: intelligence role row now also names the French "IA" phrases, so a posting
#: written "Ingénieur IA" receives the same sub-domain its English spelling
#: already did instead of falling through to ``OTHER``.
FINE_CLASSIFIER_VERSION = "fine-data-ai-rules-v3"

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
SINGLE_CONCEPT_REASON = (
    "qualified technical Data/AI role with no fine title evidence and no category "
    "reaching the description threshold; the single concrete concepts matched are "
    "more faithful than OTHER"
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


def _description_concepts(description: str) -> dict[FineCategory, tuple[str, ...]]:
    """Return each category's distinct concrete concepts, aliases collapsed."""
    return {
        category: found
        for category, concepts in FINE_DESCRIPTION_CONCEPTS.items()
        # One concept contributes one signal: the first alias it matched, so
        # naming the same idea twice never counts as two pieces of evidence.
        if (found := tuple(
            hits[0] for aliases in concepts.values()
            if (hits := matched_phrases(description, aliases))
        ))
    }


def _concept_evidence(
    concepts: dict[FineCategory, tuple[str, ...]], minimum: int
) -> tuple[FineEvidence, ...]:
    """Turn the concepts of every category meeting ``minimum`` into ordered evidence."""
    return tuple(
        FineEvidence(category, EvidenceField.DESCRIPTION, EvidenceKind.CONCRETE_CONCEPT, signal)
        for category in FINE_CATEGORY_PRECEDENCE
        for signal in (
            concepts.get(category, ()) if len(concepts.get(category, ())) >= minimum else ()
        )
    )


def _single_concept_fallback_applies(normalized_title: str) -> bool:
    """Restrict the weaker one-concept rule to the population that motivated it.

    The rule exists for a *technical* role the coarse gate proved Data/AI on
    concepts scattered across domains — a Forward Deployed Software Engineer, a
    Research Scientist. It reuses the coarse classifier's own technical role
    vocabulary rather than a second list, so the two cannot drift, and it
    deliberately does not reuse GENERIC_TECHNICAL_TITLES, which also holds
    "analyst" and "consultant".

    An advisory or strategy marker vetoes it even next to a technical family:
    advising on Data/AI is not building it, and the coarse classifier already
    caps such a title at ADJACENT_TARGET for the same reason.

    So a Data Governance Analyst or an AI Advisory Consultant that mentions
    model serving once stays OTHER — which is the true statement about it: it is
    demonstrably Data/AI, and no supported technical sub-domain is evidenced.
    """
    return bool(technical_role_families(normalized_title)) and not advisory_role_families(
        normalized_title
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

    normalized_title = normalize_text(title)
    title_evidence = _title_evidence(normalized_title)
    concepts = _description_concepts(normalize_text(description))
    description_evidence = _concept_evidence(concepts, MINIMUM_DESCRIPTION_CONCEPTS)
    if title_evidence or description_evidence:
        # The normal rules, unchanged and unrestricted: title evidence, or a
        # category showing MINIMUM_DESCRIPTION_CONCEPTS distinct concepts. Both
        # apply to every qualified opportunity, technical or not.
        evidence = title_evidence + description_evidence
        reason = TITLE_EVIDENCE_REASON if title_evidence else DESCRIPTION_EVIDENCE_REASON
        primary_pool = {item.category for item in (title_evidence or description_evidence)}
    elif concepts and _single_concept_fallback_applies(normalized_title):
        # The result would otherwise be OTHER, which claims no sub-domain applies.
        # The concepts that did match are weaker evidence, but they are evidence.
        evidence = _concept_evidence(concepts, SINGLE_CONCEPT_FALLBACK_MINIMUM)
        reason = SINGLE_CONCEPT_REASON
        primary_pool = set(concepts)
    else:
        return FineClassification(FineCategory.OTHER, (), (), (NO_EVIDENCE_REASON,))

    matched = _ordered({item.category for item in evidence})
    primary = next(category for category in FINE_CATEGORY_PRECEDENCE if category in primary_pool)
    secondaries = tuple(category for category in matched if category is not primary)
    return FineClassification(primary, secondaries, evidence, (reason,))
