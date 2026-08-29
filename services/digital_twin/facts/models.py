"""Typed, immutable vocabulary of the Phase 3.3A verified profile facts.

A `ProfileFact` says what a human has decided about one claim, and a
`FactProvenance` says where that claim was read and by which rule. The two are
deliberately separate: evidence is not a decision, and neither is a score.

There is exactly one definition of "verified" in this package — the status is
`ACCEPTED` — so there is no `verified` field here and no `verified` column in
the database. There is no confidence, no proficiency and no skill level either,
because none of those can be derived from a human clicking "accept".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum


class FactStatus(StrEnum):
    """Where a claim stands in the human validation cycle.

    `PROPOSED` is the honest default: something produced this value and nobody
    has confirmed it. `CORRECTED` and `REJECTED` are terminal — a fact that has
    been replaced or refused is history, and history is not edited.
    """

    #: Recorded with its evidence, decided by nobody yet.
    PROPOSED = "PROPOSED"
    #: A human confirmed it. This, and only this, means verified.
    ACCEPTED = "ACCEPTED"
    #: A human replaced it; `replaced_by_fact_id` points at what replaced it.
    CORRECTED = "CORRECTED"
    #: A human refused it. The row stays, so the refusal stays auditable.
    REJECTED = "REJECTED"


class FactSourceType(StrEnum):
    """What kind of thing produced a piece of evidence."""

    #: Read out of a CV PDF the person supplied.
    CV = "CV"
    #: Read from a GitHub source.
    GITHUB = "GITHUB"
    #: Stated by the person themselves, including every correction.
    USER_INPUT = "USER_INPUT"
    #: Derived from evidence that was itself already accepted.
    OTHER_ACCEPTED_EVIDENCE = "OTHER_ACCEPTED_EVIDENCE"


class ProfileFactType(StrEnum):
    """What a verified fact is about.

    This is **not** `CandidateType`. A candidate type names what a rule found in
    a document; a fact type names what is true of the person, once a human has
    said so. `NAME_CANDIDATE` therefore has no counterpart here: a confirmed
    identity is a `NAME`, and the word "candidate" would be a contradiction.

    Nothing in this taxonomy carries a level, a proficiency or a score, and
    nothing splits an employer, an institution or a date out of a value: that
    business normalization is Phase 3.4 and is not implemented.
    """

    NAME = "NAME"
    PROFESSIONAL_TITLE = "PROFESSIONAL_TITLE"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    EDUCATION = "EDUCATION"
    EXPERIENCE = "EXPERIENCE"
    PROJECT = "PROJECT"
    SKILL = "SKILL"
    CERTIFICATION = "CERTIFICATION"
    LANGUAGE = "LANGUAGE"
    GITHUB_URL = "GITHUB_URL"
    LINKEDIN_URL = "LINKEDIN_URL"
    PORTFOLIO_URL = "PORTFOLIO_URL"
    PROFESSIONAL_URL = "PROFESSIONAL_URL"
    PREFERENCE = "PREFERENCE"
    AVAILABILITY = "AVAILABILITY"
    MOBILITY = "MOBILITY"
    CAREER_OBJECTIVE = "CAREER_OBJECTIVE"


#: A decision that cannot be revisited: the fact has been replaced or refused.
TERMINAL_STATUSES = frozenset({FactStatus.CORRECTED, FactStatus.REJECTED})

#: The transitions the lifecycle allows, target status by current status. An
#: `ACCEPTED` fact can still be corrected or rejected later; a terminal one
#: cannot move at all. Re-accepting an `ACCEPTED` fact and re-rejecting a
#: `REJECTED` one are idempotent rather than transitions, so they are listed.
ALLOWED_TRANSITIONS: dict[FactStatus, frozenset[FactStatus]] = {
    FactStatus.PROPOSED: frozenset(
        {FactStatus.ACCEPTED, FactStatus.CORRECTED, FactStatus.REJECTED}
    ),
    FactStatus.ACCEPTED: frozenset(
        {FactStatus.ACCEPTED, FactStatus.CORRECTED, FactStatus.REJECTED}
    ),
    FactStatus.REJECTED: frozenset({FactStatus.REJECTED}),
    FactStatus.CORRECTED: frozenset(),
}


class ProfileFactValueError(ValueError):
    """Raised when a fact or a provenance is malformed before any write."""


def encode_page_numbers(page_numbers: tuple[int, ...] | None) -> str | None:
    """Return the canonical text form of ascending page numbers, or None.

    The form is a JSON array written without spaces — `'[1,2]'` — so the same
    pages always produce the same bytes and two provenance rows can be compared
    literally. An empty tuple is `None`: "no page" is an absence, never `'[]'`.
    """
    if not page_numbers:
        return None
    return "[" + ",".join(str(number) for number in page_numbers) + "]"


def decode_page_numbers(encoded: str | None) -> tuple[int, ...]:
    """Read back what `encode_page_numbers` wrote. An absence is an empty tuple."""
    if encoded is None or encoded == "":
        return ()
    body = encoded.strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise ProfileFactValueError("page_numbers is not a canonical array")
    inner = body[1:-1]
    if inner == "":
        return ()
    try:
        return tuple(int(part) for part in inner.split(","))
    except ValueError as error:
        raise ProfileFactValueError("page_numbers is not a canonical array") from error


def _optional_text(value: str | None, label: str) -> str | None:
    """Refuse a present-but-empty value; an absence stays an absence."""
    if value is None:
        return None
    if value.strip() == "":
        raise ProfileFactValueError(f"{label} is present but empty")
    return value


@dataclass(frozen=True)
class ProvenanceInput:
    """One piece of evidence, as a caller supplies it.

    Every field beyond `source_type` is optional because evidence differs by
    source, and a field left `None` means the source did not carry it. Nothing
    here is defaulted to a plausible value: inventing a page number, a rule or a
    parser version would make the audit trail claim something it never saw.

    The CV fields are exactly what Phase 3.2B already produces for an
    `ExtractedCandidate`, so a later slice can carry a candidate's provenance
    over field for field. Building that mapping is Phase 3.3B and is **not**
    implemented here.
    """

    source_type: FactSourceType
    #: Identifies this evidence for this fact. Left unset, it is derived from
    #: the fields below, so recording the same proof twice is a no-op.
    provenance_key: str | None = None
    #: Where the evidence sits, in the source's own terms. Never a local path
    #: to a real CV file: a CV filename usually carries the person's name.
    source_locator: str | None = None
    cv_sha256: str | None = None
    parser_version: str | None = None
    extractor_version: str | None = None
    candidate_fingerprint: str | None = None
    rule_id: str | None = None
    page_numbers: tuple[int, ...] = field(default_factory=tuple)
    section_type: str | None = None
    section_index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_type, FactSourceType):
            raise ProfileFactValueError("source_type must be a FactSourceType")
        if self.provenance_key is not None:
            key = self.provenance_key
            if key.strip() == "" or key != key.strip():
                raise ProfileFactValueError("provenance_key is empty or untrimmed")
        for label in (
            "source_locator",
            "cv_sha256",
            "parser_version",
            "extractor_version",
            "candidate_fingerprint",
            "rule_id",
            "section_type",
        ):
            _optional_text(getattr(self, label), label)
        previous = 0
        for number in self.page_numbers:
            if not isinstance(number, int) or isinstance(number, bool):
                raise ProfileFactValueError("page_numbers must be integers")
            if number < 1 or number <= previous:
                raise ProfileFactValueError(
                    "page_numbers must be ascending and start at 1"
                )
            previous = number
        if self.section_index is not None and self.section_index < 0:
            raise ProfileFactValueError("section_index must not be negative")

    @property
    def encoded_page_numbers(self) -> str | None:
        return encode_page_numbers(self.page_numbers)

    def resolved_provenance_key(self) -> str:
        """The explicit key, or a deterministic one derived from the evidence.

        The derived key is a pure function of the fields above — no clock, no
        counter, no row id — so the same proof presented twice for one fact
        resolves to the same key and is refused as a duplicate by the schema.
        """
        if self.provenance_key is not None:
            return self.provenance_key
        payload = "\x1f".join(
            (
                self.source_type.value,
                self.source_locator or "",
                self.cv_sha256 or "",
                self.parser_version or "",
                self.extractor_version or "",
                self.candidate_fingerprint or "",
                self.rule_id or "",
                self.encoded_page_numbers or "",
                self.section_type or "",
                "" if self.section_index is None else str(self.section_index),
            )
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        return f"auto:{self.source_type.value}:{digest}"


@dataclass(frozen=True)
class FactProvenance:
    """One persisted piece of evidence for one fact."""

    id: int
    fact_id: int
    source_type: FactSourceType
    provenance_key: str
    source_locator: str | None
    cv_sha256: str | None
    parser_version: str | None
    extractor_version: str | None
    candidate_fingerprint: str | None
    rule_id: str | None
    page_numbers: tuple[int, ...]
    section_type: str | None
    section_index: int | None
    created_at: str


@dataclass(frozen=True)
class ProfileFact:
    """One claim about the person, and where it stands in the cycle."""

    id: int
    profile_id: int
    fact_type: str
    #: The value as its source expressed it, kept verbatim. A correction never
    #: rewrites it: it creates another fact and leaves this one readable.
    value: str
    #: A technical normal form, set only where one is unambiguous. `None`
    #: everywhere else — a business normalization would be an interpretation.
    normalized_value: str | None
    status: FactStatus
    #: The fact that replaced this one. Set only on a `CORRECTED` fact.
    replaced_by_fact_id: int | None
    created_at: str
    updated_at: str
    #: When a human decided. `None` for as long as the fact is `PROPOSED`.
    decided_at: str | None

    @property
    def is_verified(self) -> bool:
        """Computed, never stored. `ACCEPTED` is the only verified status."""
        return self.status is FactStatus.ACCEPTED

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES
