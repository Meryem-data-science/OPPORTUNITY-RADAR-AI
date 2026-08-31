"""Typed, immutable vocabulary of the Phase 3.2B candidate extractor.

An `ExtractedCandidate` says exactly one thing:

    a named deterministic rule found this text at this place in this document.

It never says that the information is true of the person. There is no
`verified` flag, no accepted/rejected state, no `profile_fact`, no skill level,
no proficiency, no confidence and no score in this module, because none of
those can be derived from reading a PDF. Deciding what is true is the job of
the Phase 3.3A validation cycle in `services/digital_twin/facts/`; nothing in
this module reaches it, and the mapping that would is Phase 3.3B.

Like Phase 3.2A, every value here is a pure function of the parsed document and
of `CANDIDATE_EXTRACTOR_VERSION`: no clock, no environment variable, no network
and no timestamp takes part, so the same `ParsedCv` always yields the same
result, field for field.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from services.digital_twin.cv.models import SectionType

#: Bumped whenever a rule of this package changes what it produces, so a
#: candidate can always be compared against the rules that produced it. It is
#: independent of `PARSER_VERSION`: the two versions move for different
#: reasons, and a candidate carries both.
CANDIDATE_EXTRACTOR_VERSION = "cv-candidates-v5"


class CandidateType(StrEnum):
    """What kind of thing a rule believes it found. Nothing more.

    Every name is read as "the document appears to carry this", never as "the
    person has this". `NAME_CANDIDATE` is spelled with its suffix on purpose:
    an identity read off a CV header is a proposal for a human to confirm.
    """

    #: A header line whose shape is that of a person's name.
    NAME_CANDIDATE = "NAME_CANDIDATE"
    #: A header line carrying a role keyword of the closed dictionary.
    PROFESSIONAL_TITLE = "PROFESSIONAL_TITLE"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    GITHUB_URL = "GITHUB_URL"
    LINKEDIN_URL = "LINKEDIN_URL"
    #: A URL a portfolio label explicitly introduces. Never a guess.
    PORTFOLIO_URL = "PORTFOLIO_URL"
    #: Any other URL. The honest answer when the kind of link is unknown.
    PROFESSIONAL_URL = "PROFESSIONAL_URL"
    EDUCATION_ENTRY = "EDUCATION_ENTRY"
    EXPERIENCE_ENTRY = "EXPERIENCE_ENTRY"
    PROJECT_ENTRY = "PROJECT_ENTRY"
    CERTIFICATION_ENTRY = "CERTIFICATION_ENTRY"
    LANGUAGE_ENTRY = "LANGUAGE_ENTRY"
    SKILL = "SKILL"


class ExtractionRule(StrEnum):
    """The rule that produced a candidate, named so a reader can check it.

    A candidate without a rule would be an assertion with no justification, so
    the field is mandatory. Every value here is implemented in exactly one
    place in this package and documented where it is implemented.
    """

    #: The first non-blank header line has the shape of a name.
    HEADER_FIRST_LINE_NAME_SHAPE = "HEADER_FIRST_LINE_NAME_SHAPE"
    #: A header line carries a role keyword of the closed dictionary.
    HEADER_TITLE_KEYWORD_LINE = "HEADER_TITLE_KEYWORD_LINE"

    #: A conservative email pattern matched.
    EMAIL_PATTERN = "EMAIL_PATTERN"
    #: A digit run opening with an explicit international prefix.
    PHONE_INTERNATIONAL_PREFIX = "PHONE_INTERNATIONAL_PREFIX"
    #: A digit run announced by a phone label earlier on the same line.
    PHONE_LABELLED_LINE = "PHONE_LABELLED_LINE"
    #: A digit run opening with a national trunk zero, in the header block.
    PHONE_HEADER_TRUNK_ZERO = "PHONE_HEADER_TRUNK_ZERO"

    #: The URL's hostname is github.com/github.io or a subdomain of one.
    URL_GITHUB_HOST = "URL_GITHUB_HOST"
    #: The URL's hostname is linkedin.com or a subdomain of it.
    URL_LINKEDIN_HOST = "URL_LINKEDIN_HOST"
    #: A portfolio label of the closed dictionary introduces the URL.
    URL_PORTFOLIO_LABELLED_LINE = "URL_PORTFOLIO_LABELLED_LINE"
    #: A URL no other rule classified. Kept as-is rather than guessed at.
    URL_UNLABELLED_PROFESSIONAL = "URL_UNLABELLED_PROFESSIONAL"

    #: The section body was cut into entries on its blank lines.
    SECTION_BLANK_LINE_BLOCK = "SECTION_BLANK_LINE_BLOCK"
    #: The section body was cut into entries on its list markers.
    SECTION_BULLET_BLOCK = "SECTION_BULLET_BLOCK"
    #: The section body has neither, so each line is one entry.
    SECTION_LINE_BLOCK = "SECTION_LINE_BLOCK"
    #: The section body has neither, and the document's own layout showed that
    #: one or more of its line breaks were the layout engine running out of
    #: room: those lines were kept in the block they physically continue. The
    #: name describes the geometry of the page and nothing else — no
    #: institution, employer, diploma, date, place or level is read from the
    #: joined lines, here or anywhere downstream of this rule.
    SECTION_LAYOUT_CONTINUATION_BLOCK = "SECTION_LAYOUT_CONTINUATION_BLOCK"
    #: The section body has neither, the document's spacing could not tell a
    #: line from a block, and its own typography could: the column was seen
    #: repeatedly opening its entries in one typeface, and a line set in
    #: another — under a full line, in the same column, on an adjacent baseline
    #: of the same page — was kept in the block it physically continues. The
    #: name describes the typeface the PDF names and the geometry of the page,
    #: and nothing else: the signature is an opaque `/BaseFont` token compared
    #: for equality, never read as bold, regular, heading or emphasis, and no
    #: institution, diploma, date, place or level is read from the joined
    #: lines, here or anywhere downstream of this rule.
    SECTION_LAYOUT_STYLE_CONTINUATION_BLOCK = (
        "SECTION_LAYOUT_STYLE_CONTINUATION_BLOCK"
    )
    #: An EXPERIENCE body written as pipe-delimited header lines was cut on
    #: those lines. The name describes the punctuation the document used and
    #: nothing else: no employer, role, date, place or duration is read from
    #: the parts, here or anywhere downstream of this rule.
    EXPERIENCE_PIPE_DELIMITED_BLOCK = "EXPERIENCE_PIPE_DELIMITED_BLOCK"

    #: A "label: a, b, c" line; the label is dropped, the list is split.
    SKILLS_LABELLED_LIST_LINE = "SKILLS_LABELLED_LIST_LINE"
    #: A line split on its commas, semicolons or pipes.
    SKILLS_SEPARATED_LIST_LINE = "SKILLS_SEPARATED_LIST_LINE"
    #: A line holding a single mention, kept whole.
    SKILLS_PLAIN_LINE = "SKILLS_PLAIN_LINE"


class CandidateWarningCode(StrEnum):
    """Deterministic diagnostics. A warning states a fact about the extraction."""

    #: The document opens on a recognised heading, so there is no header block
    #: to read an identity from. No identity was looked for.
    NO_HEADER_BLOCK = "NO_HEADER_BLOCK"
    #: A header block exists, but no line in it justified a name candidate.
    #: The extractor proposes nothing rather than picking a line.
    NO_IDENTITY_CANDIDATE = "NO_IDENTITY_CANDIDATE"
    #: No email, phone or URL was found anywhere in the document.
    NO_CONTACT_CANDIDATE = "NO_CONTACT_CANDIDATE"
    #: No rule produced anything at all.
    NO_CANDIDATE_EXTRACTED = "NO_CANDIDATE_EXTRACTED"


@dataclass(frozen=True)
class CandidateWarning:
    """One diagnostic. Its `message` never quotes CV content."""

    code: CandidateWarningCode
    message: str
    section_type: SectionType | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "section_type": (
                None if self.section_type is None else self.section_type.value
            ),
        }


def candidate_fingerprint(candidate_type: CandidateType, comparison_value: str) -> str:
    """Return the stable identity of "this kind of value", and of nothing else.

    It is a pure function of exactly three things — the extractor version, the
    candidate type and the compared form of the value. The document is
    deliberately **not** part of it: the same address read from two different
    CVs fingerprints the same, which is what lets two runs, or two versions of
    one CV, be lined up value by value. What ties a candidate to the file it
    came from is its own `cv_sha256`, not this.

    It is **not** a persistent identifier either: no `fact_id` exists in this
    slice, nothing is stored, and a later phase is free to key its own rows
    differently.
    """
    payload = "\x1f".join(
        (CANDIDATE_EXTRACTOR_VERSION, candidate_type.value, comparison_value)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ExtractedCandidate:
    """One unverified reading of the document, with the reason it was made."""

    candidate_type: CandidateType
    #: The source text exactly as the CV wrote it: one token for a contact
    #: candidate, the whole block for an entry. Never rewritten, completed or
    #: reworded, so a human validating it later reads what the CV said.
    raw_text: str
    #: A technical normal form, kept strictly apart from `raw_text` and set
    #: only where one is unambiguous: a lowercased email, a phone compacted to
    #: its digits. `None` everywhere else — including URLs, entries and skills,
    #: where "the obvious normalization" would already be an interpretation.
    normalized_value: str | None
    #: Every page the source text covers, ascending. A single-line candidate
    #: carries exactly one page.
    page_numbers: tuple[int, ...]
    #: The canonical section the text sits in, `UNCLASSIFIED` for the header
    #: block, `None` if the document had no section at all.
    section_type: SectionType | None
    #: Index of that section in `ParsedCv.sections`, so a repeated heading
    #: stays distinguishable.
    section_index: int | None
    rule_id: ExtractionRule
    #: Stable across runs and across documents for one value of one type; it
    #: identifies the value, not this candidate. See `candidate_fingerprint`.
    fingerprint: str
    cv_sha256: str
    parser_version: str
    extractor_version: str

    @property
    def first_page_number(self) -> int | None:
        return self.page_numbers[0] if self.page_numbers else None

    def as_dict(self) -> dict[str, object]:
        """The full candidate, CV text included. Never printed by default."""
        return {
            "candidate_type": self.candidate_type.value,
            "raw_text": self.raw_text,
            "normalized_value": self.normalized_value,
            "page_numbers": list(self.page_numbers),
            "section_type": (
                None if self.section_type is None else self.section_type.value
            ),
            "section_index": self.section_index,
            "rule_id": self.rule_id.value,
            "fingerprint": self.fingerprint,
            "cv_sha256": self.cv_sha256,
            "parser_version": self.parser_version,
            "extractor_version": self.extractor_version,
        }

    def provenance(self) -> dict[str, object]:
        """The privacy-safe half: where it came from and why, never what it says."""
        return {
            "candidate_type": self.candidate_type.value,
            "rule_id": self.rule_id.value,
            "page_numbers": list(self.page_numbers),
            "section_type": (
                None if self.section_type is None else self.section_type.value
            ),
            "section_index": self.section_index,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class StructuredCvExtraction:
    """Every candidate one parsed CV yielded, and nothing that was decided."""

    extractor_version: str
    parser_version: str
    #: SHA-256 of the PDF bytes, carried over from the parse it was built on.
    cv_sha256: str
    candidates: tuple[ExtractedCandidate, ...]
    warnings: tuple[CandidateWarning, ...]

    @property
    def short_sha256(self) -> str:
        return self.cv_sha256[:12]

    @property
    def candidate_types(self) -> tuple[CandidateType, ...]:
        """Types present, sorted by name, each once."""
        return tuple(
            sorted({candidate.candidate_type for candidate in self.candidates})
        )

    def counts_by_type(self) -> dict[str, int]:
        """How many candidates of each type, by ascending type name."""
        counts: dict[str, int] = {}
        for candidate in self.candidates:
            key = candidate.candidate_type.value
            counts[key] = counts.get(key, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def of_type(self, candidate_type: CandidateType) -> tuple[ExtractedCandidate, ...]:
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.candidate_type is candidate_type
        )

    def summary(self) -> dict[str, object]:
        """The privacy-safe view: how many of what, from where, and why.

        No `raw_text`, so no name, email address, phone number, URL, employer,
        school or skill mention. Safe to print and to log.
        """
        return {
            "extractor_version": self.extractor_version,
            "parser_version": self.parser_version,
            "sha256": self.cv_sha256,
            "short_sha256": self.short_sha256,
            "candidate_count": len(self.candidates),
            "counts_by_type": self.counts_by_type(),
            "rule_ids": sorted(
                {candidate.rule_id.value for candidate in self.candidates}
            ),
            "warnings": [warning.as_dict() for warning in self.warnings],
        }

    def as_dict(self) -> dict[str, object]:
        """The detailed view, including the CV text every candidate holds.

        Only ever written where the operator explicitly asked for it.
        """
        detailed = self.summary()
        detailed["candidates"] = [candidate.as_dict() for candidate in self.candidates]
        return detailed
