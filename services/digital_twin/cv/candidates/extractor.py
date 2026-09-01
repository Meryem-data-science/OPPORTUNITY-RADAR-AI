"""The Phase 3.2B entry point: one `ParsedCv` in, its candidates out.

`extract_candidates` is a pure function. It reads the parsed document and
nothing else: no clock, no environment variable, no network, no file and no
database connection take part, it holds no state between calls and it writes
nothing anywhere. The same `ParsedCv` and the same
`CANDIDATE_EXTRACTOR_VERSION` therefore always produce the same
`StructuredCvExtraction`, field for field.

It does not re-read the PDF. The pages, the SHA-256 and the segmentation all
come from Phase 3.2A — the segmentation through `segment_document`, the very
function `parse_cv_pdf` used — so there is exactly one implementation of "where
does a section start" in the codebase, and a candidate's provenance always
points back at the parse it was built on.

The physical layout of the document is read the same way: from the pages Phase
3.2A already carries, once, for the whole document, and before any section is
cut. There are two such readings, and they are kept apart because they rest on
different evidence and a candidate has to be able to say which one grouped its
lines: `layout.py` reads what the document's own spacing demonstrates, and
`typography.py` reads which typeface each of the document's columns is seen to
open its entries in, for the documents whose spacing cannot tell a wrapped line
from a new entry. What each refuses to conclude lives in its own module; a
`ParsedCv` carrying no layout — one built from text alone, or one read from a
PDF whose text state could not be placed — yields evidence that proves nothing
either way, and every section is then cut exactly as it was before.

What comes out is a list of unverified readings. Not one of them is a fact
about the person, none is accepted, corrected or rejected, and none is stored:
this slice adds no table, no migration and no `profile_facts` row. The
accept/correct/reject workflow is Phase 3.3, and it does not exist yet.
"""

from __future__ import annotations

from collections.abc import Sequence

from services.digital_twin.cv.candidates import contact as contact_rules
from services.digital_twin.cv.candidates import entries as entry_rules
from services.digital_twin.cv.candidates import identity as identity_rules
from services.digital_twin.cv.candidates import layout as layout_rules
from services.digital_twin.cv.candidates import labelled_lists as labelled_rules
from services.digital_twin.cv.candidates import skills as skill_rules
from services.digital_twin.cv.candidates import typography as typography_rules
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
from services.digital_twin.cv.candidates.text import comparison_form
from services.digital_twin.cv.models import ParsedCv, SectionType
from services.digital_twin.cv.sections import (
    SectionSegment,
    document_lines,
    segment_document,
)

#: Candidate types produced by a contact rule, used only to decide whether the
#: `NO_CONTACT_CANDIDATE` warning applies.
_CONTACT_TYPES = frozenset(
    {
        CandidateType.EMAIL,
        CandidateType.PHONE,
        CandidateType.GITHUB_URL,
        CandidateType.LINKEDIN_URL,
        CandidateType.PORTFOLIO_URL,
        CandidateType.PROFESSIONAL_URL,
    }
)


def _header_index(segments: Sequence[SectionSegment]) -> int | None:
    """Return the index of the leading header block, if the document has one.

    Phase 3.2A only ever produces an `UNCLASSIFIED` section for the run of
    lines before the first recognised heading, and always in first position, so
    that is the only place an identity is looked for.
    """
    if segments and segments[0].section_type is SectionType.UNCLASSIFIED:
        return 0
    return None


class _Builder:
    """Collects candidates in production order, keeping the first of each value.

    Deduplication is by `(candidate_type, comparison value)`, where the
    comparison value is the candidate's `normalized_value` when it has one and
    the compared form of its `raw_text` otherwise. `normalized_value` exists
    exactly where a technical normal form is unambiguous, so using it is what
    makes "+33 6 00 00 00 00" and "+33600000000" one phone number rather than
    two. Where it is `None` — URLs, section entries, skill mentions — the text
    itself is compared, because any further folding would already be an
    interpretation.

    A CV that repeats its email in a header and a footer describes one address,
    and a validating human should see it once; the occurrence that is kept is
    the first in document order, so both the `raw_text` and the provenance stay
    those of the earliest place the value appears.
    """

    def __init__(self, *, cv_sha256: str, parser_version: str) -> None:
        self._cv_sha256 = cv_sha256
        self._parser_version = parser_version
        self._candidates: list[ExtractedCandidate] = []
        self._seen: set[tuple[CandidateType, str]] = set()

    def add(
        self,
        *,
        candidate_type: CandidateType,
        raw_text: str,
        rule_id: ExtractionRule,
        page_numbers: tuple[int, ...],
        section_type: SectionType | None,
        section_index: int | None,
        normalized_value: str | None = None,
    ) -> None:
        compared = (
            normalized_value
            if normalized_value is not None
            else comparison_form(raw_text)
        )
        if not compared:
            return
        key = (candidate_type, compared)
        if key in self._seen:
            return
        self._seen.add(key)
        self._candidates.append(
            ExtractedCandidate(
                candidate_type=candidate_type,
                raw_text=raw_text,
                normalized_value=normalized_value,
                page_numbers=page_numbers,
                section_type=section_type,
                section_index=section_index,
                rule_id=rule_id,
                fingerprint=candidate_fingerprint(candidate_type, compared),
                cv_sha256=self._cv_sha256,
                parser_version=self._parser_version,
                extractor_version=CANDIDATE_EXTRACTOR_VERSION,
            )
        )

    @property
    def candidates(self) -> tuple[ExtractedCandidate, ...]:
        return tuple(self._candidates)

    def holds_any(self, candidate_types: frozenset[CandidateType]) -> bool:
        return any(
            candidate.candidate_type in candidate_types
            for candidate in self._candidates
        )


def _add_identity(
    builder: _Builder,
    segments: Sequence[SectionSegment],
    warnings: list[CandidateWarning],
) -> None:
    header_index = _header_index(segments)
    if header_index is None:
        warnings.append(
            CandidateWarning(
                code=CandidateWarningCode.NO_HEADER_BLOCK,
                message=(
                    "the document opens on a recognised heading, so no header "
                    "block was available and no identity was looked for"
                ),
            )
        )
        return

    body = segments[header_index].body
    proposed = identity_rules.name_line(body)
    if proposed is None:
        warnings.append(
            CandidateWarning(
                code=CandidateWarningCode.NO_IDENTITY_CANDIDATE,
                message=(
                    "no header line satisfied the name shape rule; no name is "
                    "proposed rather than one being guessed"
                ),
                section_type=SectionType.UNCLASSIFIED,
            )
        )
    else:
        builder.add(
            candidate_type=CandidateType.NAME_CANDIDATE,
            raw_text=proposed.text,
            rule_id=ExtractionRule.HEADER_FIRST_LINE_NAME_SHAPE,
            page_numbers=(proposed.page_number,),
            section_type=SectionType.UNCLASSIFIED,
            section_index=header_index,
        )

    for line in identity_rules.title_lines(body):
        builder.add(
            candidate_type=CandidateType.PROFESSIONAL_TITLE,
            raw_text=line.text,
            rule_id=ExtractionRule.HEADER_TITLE_KEYWORD_LINE,
            page_numbers=(line.page_number,),
            section_type=SectionType.UNCLASSIFIED,
            section_index=header_index,
        )


def _add_contacts(
    builder: _Builder,
    segments: Sequence[SectionSegment],
    header_index: int | None,
) -> None:
    """Read contacts from every line of the document, in document order.

    Headings are skipped: a heading is the CV's label for a section, not
    content, so nothing is ever extracted from one.
    """
    for section_index, segment in enumerate(segments):
        in_header = section_index == header_index
        for line in segment.body:
            if not line.text:
                continue
            for found in contact_rules.find_contacts(line.text, in_header=in_header):
                builder.add(
                    candidate_type=found.candidate_type,
                    raw_text=found.raw_text,
                    rule_id=found.rule_id,
                    page_numbers=(line.page_number,),
                    section_type=segment.section_type,
                    section_index=section_index,
                    normalized_value=found.normalized_value,
                )


def _add_entries(
    builder: _Builder,
    segments: Sequence[SectionSegment],
    layout_evidence: layout_rules.DocumentLayoutEvidence,
    style_evidence: typography_rules.DocumentStyleEvidence,
) -> None:
    for section_index, segment in enumerate(segments):
        candidate_type = entry_rules.ENTRY_CANDIDATE_TYPES.get(segment.section_type)
        if candidate_type is None or not segment.body:
            continue
        rule_id, blocks = entry_rules.segment_entries(
            segment.body,
            section_type=segment.section_type,
            layout_evidence=layout_evidence,
            style_evidence=style_evidence,
        )
        for block in blocks:
            builder.add(
                candidate_type=candidate_type,
                raw_text=entry_rules.block_text(block),
                rule_id=rule_id,
                page_numbers=entry_rules.block_pages(block),
                section_type=segment.section_type,
                section_index=section_index,
            )


def _add_skills(builder: _Builder, segments: Sequence[SectionSegment]) -> None:
    for section_index, segment in enumerate(segments):
        if segment.section_type is not SectionType.SKILLS:
            continue
        for line in segment.body:
            if not line.text:
                continue
            if labelled_rules.is_language_labelled_line(line.text):
                continue
            for rule_id, mention in skill_rules.skill_mentions(line.text):
                builder.add(
                    candidate_type=CandidateType.SKILL,
                    raw_text=mention,
                    rule_id=rule_id,
                    page_numbers=(line.page_number,),
                    section_type=SectionType.SKILLS,
                    section_index=section_index,
                )


def _add_labelled_lists(
    builder: _Builder,
    segments: Sequence[SectionSegment],
) -> None:
    """Add inline lists whose exact label and colon are structural evidence."""
    for section_index, segment in enumerate(segments):
        for line in segment.body:
            for item in labelled_rules.language_items(line.text):
                builder.add(
                    candidate_type=CandidateType.LANGUAGE_ENTRY,
                    raw_text=item,
                    rule_id=ExtractionRule.LANGUAGES_LABELLED_LIST_ITEM,
                    page_numbers=(line.page_number,),
                    section_type=segment.section_type,
                    section_index=section_index,
                )

        if segment.section_type is not SectionType.PROFESSIONAL_DEVELOPMENT:
            continue
        for item in labelled_rules.scan_certification_lists(segment.body):
            rule_id = (
                ExtractionRule.CERTIFICATION_PLANNED_LIST_ITEM
                if item.meaning == "planned"
                else ExtractionRule.CERTIFICATION_PREPARING_LIST_ITEM
            )
            builder.add(
                candidate_type=CandidateType.CERTIFICATION_ENTRY,
                raw_text=item.text,
                rule_id=rule_id,
                page_numbers=item.page_numbers,
                section_type=segment.section_type,
                section_index=section_index,
            )


def extract_candidates(parsed: ParsedCv) -> StructuredCvExtraction:
    """Return every unverified candidate the rules of this package found.

    The candidates come out grouped by family — identity, contacts, section
    entries, then skill mentions — and each family is in document order, so the
    order is a property of the document and of these rules alone.
    """
    segments, _ = segment_document(parsed.pages)
    # Read once, from the whole document, and from the very same line stream the
    # segmentation was computed on: what a section's own two or three lines can
    # show about spacing or about a repeated opening typeface is nothing, and
    # re-reading either per section would make the answer depend on where the
    # section boundaries fell.
    lines = document_lines(parsed.pages)
    layout_evidence = layout_rules.read_layout_evidence(lines)
    style_evidence = typography_rules.read_style_evidence(lines)
    builder = _Builder(
        cv_sha256=parsed.content_sha256, parser_version=parsed.parser_version
    )
    warnings: list[CandidateWarning] = []

    _add_identity(builder, segments, warnings)
    _add_contacts(builder, segments, _header_index(segments))
    _add_entries(builder, segments, layout_evidence, style_evidence)
    _add_labelled_lists(builder, segments)
    _add_skills(builder, segments)

    if not builder.holds_any(_CONTACT_TYPES):
        warnings.append(
            CandidateWarning(
                code=CandidateWarningCode.NO_CONTACT_CANDIDATE,
                message=(
                    "no email address, phone number or URL matched anywhere in "
                    "the document"
                ),
            )
        )
    candidates = builder.candidates
    if not candidates:
        warnings.append(
            CandidateWarning(
                code=CandidateWarningCode.NO_CANDIDATE_EXTRACTED,
                message="no rule of cv-candidates-v7 produced a candidate",
            )
        )

    return StructuredCvExtraction(
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        parser_version=parsed.parser_version,
        cv_sha256=parsed.content_sha256,
        candidates=candidates,
        warnings=tuple(warnings),
    )
