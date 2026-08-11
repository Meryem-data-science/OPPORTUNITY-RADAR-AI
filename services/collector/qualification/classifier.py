"""Deterministic, explainable and geography-neutral opportunity classifier."""

from dataclasses import asdict, dataclass
import re
import unicodedata

from .taxonomy import (
    ADJACENT_SIGNALS, APPRENTICESHIP_SIGNALS, CORE_SIGNALS, DOMAIN_PRECEDENCE,
    EMPLOYMENT_SIGNALS, EXCLUSION_SIGNALS, GENERIC_CAREERS_TITLES,
    GENERIC_JOBS_TITLES, GENERIC_TECHNICAL_TITLES, GRADUATE_SIGNALS,
    INTERNSHIP_SIGNALS, PFE_SIGNALS, Domain, EmploymentType, ListingQuality,
    OpportunityType, Qualification,
)


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


def _contains(text: str, phrase: str) -> bool:
    return bool(text) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _matches(text: str, signals: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(signal for signal in signals if _contains(text, signal))


def _domain_matches(text: str, rules: dict[Domain, tuple[str, ...]]) -> dict[Domain, tuple[str, ...]]:
    return {domain: found for domain, signals in rules.items() if (found := _matches(text, signals))}


def _infer_opportunity_type(title: str, description: str, looks_like_role: bool) -> OpportunityType:
    combined = f"{title} {description}"
    for result, signals in (
        (OpportunityType.PFE, PFE_SIGNALS),
        (OpportunityType.APPRENTICESHIP, APPRENTICESHIP_SIGNALS),
        (OpportunityType.GRADUATE, GRADUATE_SIGNALS),
        (OpportunityType.INTERNSHIP, INTERNSHIP_SIGNALS),
    ):
        if _matches(combined, signals):
            return result
    return OpportunityType.JOB if looks_like_role else OpportunityType.UNKNOWN


def _infer_employment_type(title: str, description: str) -> EmploymentType:
    combined = f"{title} {description}"
    matched = [kind for kind, signals in EMPLOYMENT_SIGNALS.items() if _matches(combined, signals)]
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
    description_core = _domain_matches(normalized_description, CORE_SIGNALS)
    description_adjacent = _domain_matches(normalized_description, ADJACENT_SIGNALS)
    exclusions = _matches(normalized_title, EXCLUSION_SIGNALS)

    title_positive = {**title_adjacent, **title_core}
    description_positive = {**description_adjacent, **description_core}
    is_generic_technical = bool(_matches(normalized_title, GENERIC_TECHNICAL_TITLES))
    # Description-only promotion requires two distinct explicit domain phrases.
    description_signal_count = sum(len(values) for values in description_positive.values())
    description_promotes = is_generic_technical and description_signal_count >= 2

    reasons: list[str] = []
    if exclusions:
        qualification = Qualification.OUT_OF_SCOPE
        primary = Domain.NON_TARGET
        reasons.append("explicit non-target job-family signal in title takes precedence")
        relevant_domains: set[Domain] = set()
    elif title_core:
        qualification = Qualification.CORE_TARGET
        relevant_domains = set(title_positive) | set(description_positive)
        primary = next(domain for domain in DOMAIN_PRECEDENCE if domain in title_core)
        reasons.append("explicit core Data/AI title signal")
    elif title_adjacent:
        qualification = Qualification.ADJACENT_TARGET
        relevant_domains = set(title_positive) | set(description_positive)
        primary = next(domain for domain in DOMAIN_PRECEDENCE if domain in title_adjacent)
        reasons.append("explicit adjacent Data/AI title signal")
    elif description_promotes:
        qualification = Qualification.CORE_TARGET if description_core else Qualification.ADJACENT_TARGET
        relevant_domains = set(description_positive)
        primary = next(domain for domain in DOMAIN_PRECEDENCE if domain in relevant_domains)
        reasons.append("generic technical title is disambiguated by at least two explicit description signals")
    else:
        qualification = Qualification.UNCERTAIN
        primary = Domain.UNKNOWN
        relevant_domains = set(description_positive)
        reasons.append("no authoritative title signal and description evidence is insufficient")

    matched_domains = tuple(domain for domain in DOMAIN_PRECEDENCE if domain in relevant_domains)
    title_signals = tuple(
        signal for domain in DOMAIN_PRECEDENCE for signal in title_positive.get(domain, ())
    )
    description_signals = tuple(
        signal for domain in DOMAIN_PRECEDENCE for signal in description_positive.get(domain, ())
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

    looks_like_role = qualification is not Qualification.UNCERTAIN or bool(normalized_description)
    return Classification(
        qualification, primary, matched_domains,
        _infer_opportunity_type(normalized_title, normalized_description, looks_like_role),
        _infer_employment_type(normalized_title, normalized_description), quality,
        tuple(flags), title_signals, description_signals, exclusions, tuple(reasons),
    )
