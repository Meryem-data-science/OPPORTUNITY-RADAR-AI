"""The manifest chain, as pure functions over handmade candidates.

No database, no PDF, no real CV. Every candidate below is built by hand from
invented text marked TEST ONLY, and the digests are synthetic: nothing here
hashed a real file.
"""

import pytest

from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    ExtractedCandidate,
    ExtractionRule,
    StructuredCvExtraction,
    candidate_fingerprint,
)
from services.digital_twin.cv.models import PARSER_VERSION, SectionType
from services.digital_twin.cv.replacement.manifest import (
    MANIFEST_CHAIN_VERSION,
    build_manifest_entries,
    chain_digest_of,
)

TEST_ONLY_SHA256 = "ab" * 32
TEST_ONLY_OTHER_SHA256 = "cd" * 32
TEST_ONLY_NAME = "Alex Test-Only"
TEST_ONLY_SKILL = "TestOnlyToolkit"
TEST_ONLY_OTHER_SKILL = "OtherTestOnlyToolkit"


def make_candidate(
    candidate_type: CandidateType,
    raw_text: str,
    *,
    normalized_value: str | None = None,
    rule_id: ExtractionRule = ExtractionRule.SECTION_LINE_BLOCK,
    page_numbers: tuple[int, ...] = (1,),
    section_type: SectionType | None = SectionType.UNCLASSIFIED,
    section_index: int | None = 0,
    cv_sha256: str = TEST_ONLY_SHA256,
) -> ExtractedCandidate:
    return ExtractedCandidate(
        candidate_type=candidate_type,
        raw_text=raw_text,
        normalized_value=normalized_value,
        page_numbers=page_numbers,
        section_type=section_type,
        section_index=section_index,
        rule_id=rule_id,
        fingerprint=candidate_fingerprint(
            candidate_type, normalized_value or raw_text.casefold()
        ),
        cv_sha256=cv_sha256,
        parser_version=PARSER_VERSION,
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
    )


def make_extraction(
    candidates: tuple[ExtractedCandidate, ...], *, cv_sha256: str = TEST_ONLY_SHA256
) -> StructuredCvExtraction:
    return StructuredCvExtraction(
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        parser_version=PARSER_VERSION,
        cv_sha256=cv_sha256,
        candidates=candidates,
        warnings=(),
    )


@pytest.fixture
def two_candidates() -> tuple[ExtractedCandidate, ...]:
    return (
        make_candidate(
            CandidateType.NAME_CANDIDATE,
            TEST_ONLY_NAME,
            rule_id=ExtractionRule.HEADER_FIRST_LINE_NAME_SHAPE,
        ),
        make_candidate(CandidateType.SKILL, TEST_ONLY_SKILL),
    )


def test_the_manifest_keeps_the_extraction_order(two_candidates):
    entries = build_manifest_entries(make_extraction(two_candidates))
    assert [entry.ordinal for entry in entries] == [0, 1]
    assert entries[0].fact_type == "NAME"
    assert entries[1].fact_type == "SKILL"


def test_the_chain_is_deterministic(two_candidates):
    first = chain_digest_of(make_extraction(two_candidates))
    second = chain_digest_of(make_extraction(two_candidates))
    assert first == second
    assert len(first) == 64


def test_the_same_readings_of_another_document_chain_differently(two_candidates):
    """The seed carries the document digest, so one manifest cannot pass for
    another document's even when every reading is identical."""
    others = tuple(
        make_candidate(
            candidate.candidate_type,
            candidate.raw_text,
            rule_id=candidate.rule_id,
            cv_sha256=TEST_ONLY_OTHER_SHA256,
        )
        for candidate in two_candidates
    )
    assert chain_digest_of(
        make_extraction(two_candidates)
    ) != chain_digest_of(make_extraction(others, cv_sha256=TEST_ONLY_OTHER_SHA256))


def test_changing_one_reading_changes_that_link_and_every_later_one(two_candidates):
    original = build_manifest_entries(make_extraction(two_candidates))
    edited = build_manifest_entries(
        make_extraction(
            (
                two_candidates[0],
                make_candidate(CandidateType.SKILL, TEST_ONLY_OTHER_SKILL),
            )
        )
    )
    assert edited[0].chain_digest == original[0].chain_digest
    assert edited[1].chain_digest != original[1].chain_digest


def test_reordering_two_readings_changes_the_chain(two_candidates):
    straight = chain_digest_of(make_extraction(two_candidates))
    swapped = chain_digest_of(make_extraction((two_candidates[1], two_candidates[0])))
    assert straight != swapped


def test_an_empty_extraction_still_states_which_document_was_read():
    empty = chain_digest_of(make_extraction(()))
    other = chain_digest_of(
        make_extraction((), cv_sha256=TEST_ONLY_OTHER_SHA256)
    )
    assert len(empty) == 64
    assert empty != other


def test_the_provenance_key_is_the_evidence_not_the_attempt(two_candidates):
    """Reading the same document twice yields the same proof, so a second
    attempt cannot propose one reading as if it were a new one."""
    first = build_manifest_entries(make_extraction(two_candidates))
    second = build_manifest_entries(make_extraction(two_candidates))
    assert [entry.provenance_key for entry in first] == [
        entry.provenance_key for entry in second
    ]


def test_the_summary_carries_no_cv_text(two_candidates):
    entries = build_manifest_entries(make_extraction(two_candidates))
    for entry in entries:
        printed = repr(entry.summary())
        assert TEST_ONLY_NAME not in printed
        assert TEST_ONLY_SKILL not in printed
        assert "value" not in entry.summary()
        assert "normalized_value" not in entry.summary()


def test_the_chain_version_is_named():
    assert MANIFEST_CHAIN_VERSION == "cv-manifest-chain-v1"
