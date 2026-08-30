"""Unit coverage for the Phase 3.3B candidate-to-fact bridge.

Nothing here touches a database: these are the pure parts — what a candidate
type means as a fact, and what its provenance carries over. Every candidate is
built by hand from invented text. No real CV, no real person and no real
contact detail enters the test suite or the git history, and `.invalid` never
resolves.
"""

import io
import tokenize
from pathlib import Path

import pytest

from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    ExtractedCandidate,
    ExtractionRule,
    StructuredCvExtraction,
    candidate_fingerprint,
)
from services.digital_twin.cv.fact_bridge import (
    CANDIDATE_TYPE_TO_FACT_TYPE,
    UNMAPPED_FACT_TYPES,
    UnmappedCandidateTypeError,
    fact_type_for_candidate_type,
    provenance_for_candidate,
)
from services.digital_twin.cv.models import PARSER_VERSION, SectionType
from services.digital_twin.facts.models import FactSourceType, ProfileFactType

BRIDGE_SOURCE = Path("services/digital_twin/cv/fact_bridge.py")


def code_only(path: Path) -> str:
    """Return a module's executable source, without comments or docstrings.

    The bridge documents at length what it refuses to do, so a plain substring
    search over the file would match its own prose. Dropping every comment and
    every string literal leaves the code, which is what these assertions are
    actually about.
    """
    tokens = tokenize.generate_tokens(io.StringIO(
        path.read_text(encoding="utf-8")
    ).readline)
    return "".join(
        token.string
        for token in tokens
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


# TEST ONLY values; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
# A synthetic 64-hex digest; no real file was hashed to produce it.
TEST_ONLY_SHA256 = "ab" * 32


def make_candidate(**overrides) -> ExtractedCandidate:
    """One handmade candidate, shaped exactly like the extractor's own."""
    values = {
        "candidate_type": CandidateType.EMAIL,
        "raw_text": TEST_ONLY_EMAIL,
        "normalized_value": TEST_ONLY_EMAIL,
        "page_numbers": (1, 2),
        "section_type": SectionType.UNCLASSIFIED,
        "section_index": 0,
        "rule_id": ExtractionRule.EMAIL_PATTERN,
        "fingerprint": candidate_fingerprint(CandidateType.EMAIL, TEST_ONLY_EMAIL),
        "cv_sha256": TEST_ONLY_SHA256,
        "parser_version": PARSER_VERSION,
        "extractor_version": CANDIDATE_EXTRACTOR_VERSION,
    }
    values.update(overrides)
    return ExtractedCandidate(**values)


def make_extraction(*candidates: ExtractedCandidate) -> StructuredCvExtraction:
    return StructuredCvExtraction(
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        parser_version=PARSER_VERSION,
        cv_sha256=TEST_ONLY_SHA256,
        candidates=tuple(candidates),
        warnings=(),
    )


# --------------------------------------------------------------------------
# The mapping is closed, total and explicit
# --------------------------------------------------------------------------


def test_every_candidate_type_has_a_decided_meaning() -> None:
    """The mapping is total: no candidate can be silently dropped."""
    assert set(CANDIDATE_TYPE_TO_FACT_TYPE) == set(CandidateType)


def test_the_mapping_is_exactly_the_agreed_table() -> None:
    """Written out in full, so a change to it is visible in the diff."""
    assert {
        candidate_type.value: fact_type.value
        for candidate_type, fact_type in CANDIDATE_TYPE_TO_FACT_TYPE.items()
    } == {
        "NAME_CANDIDATE": "NAME",
        "PROFESSIONAL_TITLE": "PROFESSIONAL_TITLE",
        "EMAIL": "EMAIL",
        "PHONE": "PHONE",
        "GITHUB_URL": "GITHUB_URL",
        "LINKEDIN_URL": "LINKEDIN_URL",
        "PORTFOLIO_URL": "PORTFOLIO_URL",
        "PROFESSIONAL_URL": "PROFESSIONAL_URL",
        "EDUCATION_ENTRY": "EDUCATION",
        "EXPERIENCE_ENTRY": "EXPERIENCE",
        "PROJECT_ENTRY": "PROJECT",
        "CERTIFICATION_ENTRY": "CERTIFICATION",
        "LANGUAGE_ENTRY": "LANGUAGE",
        "SKILL": "SKILL",
    }


def test_a_name_candidate_proposes_a_plain_name() -> None:
    """The suffix belongs to the reading, not to the confirmed identity."""
    assert (
        fact_type_for_candidate_type(CandidateType.NAME_CANDIDATE)
        is ProfileFactType.NAME
    )


def test_no_candidate_proposes_a_category_no_rule_produces() -> None:
    """Phase 3.2B reads no preference, availability, mobility or objective."""
    assert UNMAPPED_FACT_TYPES == frozenset(
        {
            ProfileFactType.PREFERENCE,
            ProfileFactType.AVAILABILITY,
            ProfileFactType.MOBILITY,
            ProfileFactType.CAREER_OBJECTIVE,
        }
    )
    assert not UNMAPPED_FACT_TYPES & set(CANDIDATE_TYPE_TO_FACT_TYPE.values())


def test_an_uncovered_candidate_type_is_refused_rather_than_guessed(
    monkeypatch,
) -> None:
    """A new candidate type with no decided meaning fails loudly, both ways."""
    from services.digital_twin.cv import fact_bridge

    partial = dict(CANDIDATE_TYPE_TO_FACT_TYPE)
    del partial[CandidateType.SKILL]
    monkeypatch.setattr(fact_bridge, "CANDIDATE_TYPE_TO_FACT_TYPE", partial)

    # The module refuses to load with a gap...
    with pytest.raises(UnmappedCandidateTypeError) as loading:
        fact_bridge._verify_mapping_is_total()
    assert "SKILL" in str(loading.value)

    # ...and no lookup falls back to a default either.
    with pytest.raises(UnmappedCandidateTypeError):
        fact_bridge.fact_type_for_candidate_type(CandidateType.SKILL)


def test_the_mapping_never_targets_a_type_the_taxonomy_lacks() -> None:
    for fact_type in CANDIDATE_TYPE_TO_FACT_TYPE.values():
        assert fact_type in set(ProfileFactType)


# --------------------------------------------------------------------------
# Provenance is carried over, not invented
# --------------------------------------------------------------------------


def test_cv_provenance_is_carried_over_field_for_field() -> None:
    candidate = make_candidate(
        page_numbers=(2, 3),
        section_type=SectionType.EXPERIENCE,
        section_index=4,
        rule_id=ExtractionRule.SECTION_BULLET_BLOCK,
    )

    provenance = provenance_for_candidate(candidate)

    assert provenance.source_type is FactSourceType.CV
    assert provenance.cv_sha256 == candidate.cv_sha256
    assert provenance.parser_version == candidate.parser_version
    assert provenance.extractor_version == candidate.extractor_version
    assert provenance.candidate_fingerprint == candidate.fingerprint
    assert provenance.rule_id == candidate.rule_id.value
    assert provenance.page_numbers == candidate.page_numbers
    assert provenance.section_type == "EXPERIENCE"
    assert provenance.section_index == 4


def test_a_candidate_without_a_section_records_an_absence() -> None:
    candidate = make_candidate(section_type=None, section_index=None)

    provenance = provenance_for_candidate(candidate)

    assert provenance.section_type is None
    assert provenance.section_index is None


def test_the_source_locator_never_carries_the_path_of_the_pdf(tmp_path) -> None:
    """A CV filename usually carries the person's name; the digest does not."""
    pdf_path = tmp_path / "TEST-ONLY-cv.pdf"
    candidate = make_candidate()

    provenance = provenance_for_candidate(candidate)

    assert provenance.source_locator is None
    assert str(pdf_path) not in repr(provenance)
    assert "cv.pdf" not in repr(provenance)


def test_the_bridge_never_takes_a_path_at_all() -> None:
    """There is no argument through which a local path could reach a row."""
    source = code_only(BRIDGE_SOURCE)

    assert "source_locator=None" in source
    assert "pdf_path" not in source
    assert "path" not in source


def test_two_different_cvs_are_two_different_proofs() -> None:
    """Evidence identifies a reading of a document, not just a value."""
    first = make_candidate(cv_sha256="ab" * 32)
    second = make_candidate(cv_sha256="cd" * 32)

    assert provenance_for_candidate(first).resolved_provenance_key() != (
        provenance_for_candidate(second).resolved_provenance_key()
    )


def test_the_same_candidate_always_resolves_to_the_same_proof() -> None:
    assert (
        provenance_for_candidate(make_candidate()).resolved_provenance_key()
        == provenance_for_candidate(make_candidate()).resolved_provenance_key()
    )


# --------------------------------------------------------------------------
# The value is transported, never interpreted
# --------------------------------------------------------------------------


def test_the_bridge_derives_no_business_meaning() -> None:
    """Phase 3.4 has not started, and no shortcut into it exists here."""
    lowered = code_only(BRIDGE_SOURCE).casefold()

    for forbidden in (
        "alias",
        "skill_level",
        "proficiency",
        "employer",
        "institution",
        "canonical_role",
        "confidence",
        "match_score",
        "threshold",
        ".title()",
        ".upper()",
        ".lower()",
        "casefold",
        "strip()",
    ):
        assert forbidden not in lowered, forbidden


def test_the_bridge_accepts_nothing_by_itself() -> None:
    """No path in this module can produce anything but a PROPOSED fact."""
    source = code_only(BRIDGE_SOURCE)

    assert "accept_profile_fact" not in source
    assert "FactStatus.ACCEPTED" not in source
    assert "auto_accept" not in source


def test_the_candidate_package_still_ignores_the_fact_store() -> None:
    """Phase 3.2B stays a reading: the bridge is what crosses, not the package."""
    package = Path("services/digital_twin/cv/candidates")

    for path in sorted(package.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "digital_twin.facts" not in source
        assert "propose_profile_fact" not in source
        assert "ensure_profile_fact_proposal" not in source
        assert "fact_bridge" not in source


def test_this_slice_still_adds_no_migration_of_its_own() -> None:
    """Phase 3.3B reuses the 0007 schema and adds no table of its own.

    `0008` belongs to the Phase 3.4A skill projection, `0009` to the Phase
    3.4B1 structured projection and `0010` to the Phase 3.4B2 one; all three
    read accepted facts and none is part of this bridge. The list is written
    out so a migration added without a decided owner shows up here.
    """
    migrations = sorted(Path("migrations").glob("*.sql"))

    assert [migration.name for migration in migrations] == [
        "0001_opportunity_foundation.sql",
        "0002_deduplication_decisions.sql",
        "0003_deduplication_merges.sql",
        "0004_opportunity_qualifications.sql",
        "0005_source_runs.sql",
        "0006_user_profile_foundation.sql",
        "0007_profile_facts.sql",
        "0008_normalized_profile_skills.sql",
        "0009_structured_profile_experiences_projects.sql",
        "0010_structured_profile_education_certifications_languages.sql",
        "0011_profile_preferences_availability_mobility.sql",
    ]


def test_the_bridge_needs_no_network_and_no_model() -> None:
    lowered = code_only(BRIDGE_SOURCE).casefold()

    for forbidden in ("http", "requests", "openai", "anthropic", "socket", "urllib"):
        assert forbidden not in lowered, forbidden
