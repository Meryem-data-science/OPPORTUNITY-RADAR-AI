"""Deterministic, explainable and geography-neutral opportunity classifier."""

from dataclasses import asdict, dataclass
import re
import unicodedata

from .taxonomy import (
    ADJACENT_SIGNALS, APPRENTICESHIP_SIGNALS, CORE_SIGNALS, DOMAIN_PRECEDENCE,
    DESCRIPTION_PFE_SIGNALS, DOMAIN_CONTEXT_SIGNALS, EMPLOYMENT_SIGNALS,
    GENERIC_CAREERS_TITLES,
    GENERIC_JOBS_TITLES, GENERIC_TECHNICAL_TITLES, GRADUATE_SIGNALS,
    INTERNSHIP_SIGNALS, NON_TARGET_ROLE_SIGNALS, PFE_SIGNALS,
    POSTDOC_ROLE_SIGNALS, STRONG_DESCRIPTION_CONCEPTS, TECHNICAL_ROLE_SIGNALS,
    Domain, EmploymentType, ListingQuality, OpportunityType, Qualification,
)


CLASSIFIER_VERSION = "qualification-rules-v1"


@dataclass(frozen=True)
class Classification:
    qualification: Qualification
    primary_domain: Domain
    matched_domains: tuple[Domain, ...]
    opportunity_type: OpportunityType
    employment_type: EmploymentType
    listing_quality: ListingQuality
    quality_flags: tuple[str, ...]
    matched_title_signals: tuple[str, ...]
    matched_description_signals: tuple[str, ...]
    matched_exclusion_signals: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_text(value: str | None) -> str:
    """NFKC/casefold text and turn punctuation into phrase-safe spaces."""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    # Accent-fold after NFKC so equivalent French spellings share transparent rules.
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", normalized)
        if not unicodedata.combining(character)
    )
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def contains_phrase(text: str, phrase: str) -> bool:
    """Boundary-safe phrase containment shared by every deterministic rule set."""
    return bool(text) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def matched_phrases(text: str, signals: tuple[str, ...]) -> tuple[str, ...]:
    """Return the matching signals in declaration order, never a set."""
    return tuple(signal for signal in signals if contains_phrase(text, signal))


def _domain_matches(text: str, rules: dict[Domain, tuple[str, ...]]) -> dict[Domain, tuple[str, ...]]:
    return {domain: found for domain, signals in rules.items() if (found := matched_phrases(text, signals))}


def _context_matches(text: str) -> dict[Domain, tuple[str, ...]]:
    """Return distinct context evidence, suppressing phrases nested in stronger ones."""
    found = _domain_matches(text, DOMAIN_CONTEXT_SIGNALS)
    all_signals = tuple(signal for signals in found.values() for signal in signals)
    return {
        domain: independent
        for domain, signals in found.items()
        if (independent := tuple(
            signal
            for signal in signals
            if not any(signal != other and contains_phrase(other, signal) for other in all_signals)
        ))
    }


def _strong_description_concepts(text: str) -> dict[Domain, tuple[str, ...]]:
    """Return independently-counted concrete concepts, collapsing all aliases."""
    return {
        domain: matched
        for domain, concepts in STRONG_DESCRIPTION_CONCEPTS.items()
        if (matched := tuple(
            concept for concept, aliases in concepts.items() if matched_phrases(text, aliases)
        ))
    }


def _infer_opportunity_type(title: str, description: str) -> OpportunityType:
    """Infer from title first; ordinary description vocabulary cannot override it."""
    title_pfe = matched_phrases(title, PFE_SIGNALS)
    title_apprenticeship = matched_phrases(title, APPRENTICESHIP_SIGNALS)
    title_internship = matched_phrases(title, INTERNSHIP_SIGNALS)
    title_graduate = matched_phrases(title, GRADUATE_SIGNALS)
    description_pfe = matched_phrases(description, DESCRIPTION_PFE_SIGNALS)
    if title_pfe:
        return OpportunityType.PFE
    if title_apprenticeship:
        return OpportunityType.APPRENTICESHIP
    if title_internship:
        return OpportunityType.PFE if description_pfe else OpportunityType.INTERNSHIP
    if title_graduate:
        return OpportunityType.GRADUATE
    if not title or title in GENERIC_CAREERS_TITLES or title in GENERIC_JOBS_TITLES:
        return OpportunityType.UNKNOWN
    return OpportunityType.JOB


def _infer_employment_type(title: str, description: str) -> EmploymentType:
    combined = f"{title} {description}"
    matched = [kind for kind, signals in EMPLOYMENT_SIGNALS.items() if matched_phrases(combined, signals)]
    return matched[0] if len(matched) == 1 else EmploymentType.UNKNOWN


def classify_opportunity(
    title: str | None,
    description: str | None = None,
    *,
    application_url: str | None = None,
    canonical_url: str | None = None,
    source_url: str | None = None,
    location: str | None = None,
) -> Classification:
    """Classify content only; ``location`` is accepted as metadata and never examined."""
    del location
    normalized_title, normalized_description = normalize_text(title), normalize_text(description)
    title_core = _domain_matches(normalized_title, CORE_SIGNALS)
    title_adjacent = _domain_matches(normalized_title, ADJACENT_SIGNALS)
    title_context = _context_matches(normalized_title)
    description_context = _context_matches(normalized_description)
    strong_description = _strong_description_concepts(normalized_description)
    exclusions = matched_phrases(normalized_title, NON_TARGET_ROLE_SIGNALS)

    title_positive = {**title_adjacent, **title_core}
    technical_roles = matched_phrases(normalized_title, TECHNICAL_ROLE_SIGNALS)
    postdoc_roles = matched_phrases(normalized_title, POSTDOC_ROLE_SIGNALS)
    is_generic_technical = bool(matched_phrases(normalized_title, GENERIC_TECHNICAL_TITLES))
    strong_concept_count = sum(len(concepts) for concepts in strong_description.values())
    description_promotes = is_generic_technical and strong_concept_count >= 2
    structural_title_match = bool((technical_roles or postdoc_roles) and title_context)

    reasons: list[str] = []
    if exclusions:
        qualification = Qualification.OUT_OF_SCOPE
        primary = Domain.NON_TARGET
        reasons.append("explicit non-target job-family signal in title takes precedence")
        relevant_domains: set[Domain] = set()
    elif title_core:
        qualification = Qualification.CORE_TARGET
        relevant_domains = set(title_positive) | set(title_context) | set(description_context)
        primary = next(domain for domain in DOMAIN_PRECEDENCE if domain in title_core)
        reasons.append("explicit core Data/AI title signal")
    elif title_adjacent:
        qualification = Qualification.ADJACENT_TARGET
        relevant_domains = set(title_positive) | set(title_context) | set(description_context)
        primary = next(domain for domain in DOMAIN_PRECEDENCE if domain in title_adjacent)
        reasons.append("explicit adjacent Data/AI title signal")
    elif structural_title_match:
        qualification = Qualification.CORE_TARGET
        relevant_domains = set(title_context) | set(description_context)
        primary = next(domain for domain in DOMAIN_PRECEDENCE if domain in title_context)
        reasons.append("technical role family + explicit Data/AI title context")
    elif description_promotes:
        qualification = Qualification.CORE_TARGET
        relevant_domains = set(description_context)
        primary = next(domain for domain in DOMAIN_PRECEDENCE if domain in strong_description)
        reasons.append(
            "generic technical/research title + strong description concepts: "
            + ", ".join(
                concept
                for domain in DOMAIN_PRECEDENCE
                for concept in strong_description.get(domain, ())
            )
        )
    else:
        qualification = Qualification.UNCERTAIN
        primary = Domain.UNKNOWN
        relevant_domains = set(title_context) | set(description_context)
        reasons.append("no authoritative title signal and description evidence is insufficient")

    matched_domains = tuple(domain for domain in DOMAIN_PRECEDENCE if domain in relevant_domains)
    title_signals = tuple(
        signal for domain in DOMAIN_PRECEDENCE for signal in title_positive.get(domain, ())
    ) + technical_roles + postdoc_roles + tuple(
        signal for domain in DOMAIN_PRECEDENCE for signal in title_context.get(domain, ())
    )
    description_signals = tuple(
        signal for domain in DOMAIN_PRECEDENCE for signal in description_context.get(domain, ())
    )

    flags: list[str] = []
    if normalized_title in GENERIC_CAREERS_TITLES:
        flags.append("GENERIC_CAREERS_TITLE")
    if normalized_title in GENERIC_JOBS_TITLES:
        flags.append("GENERIC_JOBS_PAGE_TITLE")
    if len(normalized_description) < 20:
        flags.append("EMPTY_OR_NEAR_EMPTY_DESCRIPTION")
    if not application_url:
        flags.append("MISSING_APPLICATION_URL")
    if not canonical_url:
        flags.append("MISSING_CANONICAL_URL")
    if "EMPTY_OR_NEAR_EMPTY_DESCRIPTION" in flags:
        quality = ListingQuality.INSUFFICIENT_CONTENT
    elif {"GENERIC_CAREERS_TITLE", "GENERIC_JOBS_PAGE_TITLE"} & set(flags):
        quality = ListingQuality.POSSIBLE_NON_JOB_PAGE
    else:
        quality = ListingQuality.NORMAL_LISTING
    if not application_url and source_url:
        reasons.append("application URL is missing; source URL remains available (diagnostic only)")

    return Classification(
        qualification, primary, matched_domains,
        _infer_opportunity_type(normalized_title, normalized_description),
        _infer_employment_type(normalized_title, normalized_description), quality,
        tuple(flags), title_signals, description_signals, exclusions, tuple(reasons),
    )
