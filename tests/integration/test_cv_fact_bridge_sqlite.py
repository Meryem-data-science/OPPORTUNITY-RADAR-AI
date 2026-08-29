"""The Phase 3.3B CV import, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away; the real
`.data/` database is never opened. No real CV, no real address and no real
personal data takes part: every candidate is built by hand from invented text
marked TEST ONLY, and `.invalid` never resolves.
"""

import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.cv import fact_bridge
from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    ExtractedCandidate,
    ExtractionRule,
    StructuredCvExtraction,
    candidate_fingerprint,
)
from services.digital_twin.cv.fact_bridge import (
    import_cv_candidates,
    provenance_for_candidate,
)
from services.digital_twin.cv.models import PARSER_VERSION, SectionType
from services.digital_twin.facts.models import (
    FactSourceType,
    FactStatus,
    ProfileFactType,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    AmbiguousFactEvidenceError,
    ConflictingFactEvidenceError,
    ProfileFactNotFoundError,
    accept_profile_fact,
    correct_profile_fact,
    ensure_profile_fact_proposal,
    get_profile_fact,
    list_profile_fact_provenance,
    list_profile_facts,
    list_verified_profile_facts,
    reject_profile_fact,
)
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_OTHER_EMAIL = "other.student@example.invalid"
# Synthetic 64-hex digests; no real file was hashed to produce them.
TEST_ONLY_SHA256 = "ab" * 32
TEST_ONLY_OTHER_SHA256 = "cd" * 32

TEST_ONLY_NAME = "Alex Test-Only"
TEST_ONLY_CONTACT = "student@example.invalid"
TEST_ONLY_SKILL = "TestOnlyToolkit"
TEST_ONLY_EXPERIENCE = "TEST ONLY analyst at Example Org, 2020-2021"
TEST_ONLY_CORRECTION = "corrected.student@example.invalid"


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
    """One handmade candidate, shaped exactly like the extractor's own."""
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
    candidates: tuple[ExtractedCandidate, ...],
    *,
    cv_sha256: str = TEST_ONLY_SHA256,
) -> StructuredCvExtraction:
    return StructuredCvExtraction(
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        parser_version=PARSER_VERSION,
        cv_sha256=cv_sha256,
        candidates=candidates,
        warnings=(),
    )


def five_candidates(cv_sha256: str = TEST_ONLY_SHA256) -> tuple[ExtractedCandidate, ...]:
    """A small CV: an identity, a contact, an entry and two skills."""
    return (
        make_candidate(
            CandidateType.NAME_CANDIDATE,
            TEST_ONLY_NAME,
            rule_id=ExtractionRule.HEADER_FIRST_LINE_NAME_SHAPE,
            cv_sha256=cv_sha256,
        ),
        make_candidate(
            CandidateType.EMAIL,
            TEST_ONLY_CONTACT,
            normalized_value=TEST_ONLY_CONTACT,
            rule_id=ExtractionRule.EMAIL_PATTERN,
            cv_sha256=cv_sha256,
        ),
        make_candidate(
            CandidateType.EXPERIENCE_ENTRY,
            TEST_ONLY_EXPERIENCE,
            rule_id=ExtractionRule.SECTION_BLANK_LINE_BLOCK,
            page_numbers=(1, 2),
            section_type=SectionType.EXPERIENCE,
            section_index=1,
            cv_sha256=cv_sha256,
        ),
        make_candidate(
            CandidateType.SKILL,
            TEST_ONLY_SKILL,
            rule_id=ExtractionRule.SKILLS_SEPARATED_LIST_LINE,
            page_numbers=(2,),
            section_type=SectionType.SKILLS,
            section_index=2,
            cv_sha256=cv_sha256,
        ),
        make_candidate(
            CandidateType.SKILL,
            "TestOnlyOtherToolkit",
            rule_id=ExtractionRule.SKILLS_SEPARATED_LIST_LINE,
            page_numbers=(2,),
            section_type=SectionType.SKILLS,
            section_index=2,
            cv_sha256=cv_sha256,
        ),
    )


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "bridge.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


@pytest.fixture
def other_profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_OTHER_EMAIL).profile_id


@pytest.fixture
def extraction() -> StructuredCvExtraction:
    return make_extraction(five_candidates())


def _fact_count(connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0])


def _provenance_count(connection) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM profile_fact_provenance"
        ).fetchone()[0]
    )


# --------------------------------------------------------------------------
# What an import produces
# --------------------------------------------------------------------------


def test_every_candidate_becomes_a_proposal_and_nothing_becomes_verified(
    migrated, profile_id, extraction
) -> None:
    """An extraction is a reading; only a human makes one of them true."""
    result = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    assert result.candidate_count == 5
    assert result.newly_proposed == 5
    assert result.already_imported == 0
    assert [entry.fact.status for entry in result.imported] == [
        FactStatus.PROPOSED
    ] * 5
    assert list_verified_profile_facts(migrated, profile_id) == ()
    assert _fact_count(migrated) == 5
    assert _provenance_count(migrated) == 5


def test_the_mapped_types_are_the_ones_the_candidates_asked_for(
    migrated, profile_id, extraction
) -> None:
    result = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    assert [entry.fact.fact_type for entry in result.imported] == [
        ProfileFactType.NAME.value,
        ProfileFactType.EMAIL.value,
        ProfileFactType.EXPERIENCE.value,
        ProfileFactType.SKILL.value,
        ProfileFactType.SKILL.value,
    ]


def test_the_raw_text_is_stored_verbatim_and_the_normal_form_is_carried_over(
    migrated, profile_id, extraction
) -> None:
    """No alias, no level, no reformulation: what the CV said is what is stored."""
    result = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    for entry in result.imported:
        assert entry.fact.value == entry.candidate.raw_text
        assert entry.fact.normalized_value == entry.candidate.normalized_value
    values = [entry.fact.value for entry in result.imported]
    assert TEST_ONLY_NAME in values
    assert TEST_ONLY_EXPERIENCE in values


def test_the_candidates_are_imported_in_the_extractions_own_order(
    migrated, profile_id, extraction
) -> None:
    result = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    assert [entry.candidate for entry in result.imported] == list(
        extraction.candidates
    )
    assert [entry.fact.id for entry in result.imported] == sorted(
        entry.fact.id for entry in result.imported
    )


def test_the_cv_provenance_is_persisted_field_for_field(
    migrated, profile_id, extraction
) -> None:
    result = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    for entry in result.imported:
        candidate = entry.candidate
        (recorded,) = list_profile_fact_provenance(
            migrated, profile_id, entry.fact.id
        )
        assert recorded.source_type is FactSourceType.CV
        assert recorded.cv_sha256 == candidate.cv_sha256
        assert recorded.parser_version == candidate.parser_version
        assert recorded.extractor_version == candidate.extractor_version
        assert recorded.candidate_fingerprint == candidate.fingerprint
        assert recorded.rule_id == candidate.rule_id.value
        assert recorded.page_numbers == candidate.page_numbers
        assert recorded.section_type == candidate.section_type.value
        assert recorded.section_index == candidate.section_index


def test_no_provenance_row_carries_the_path_of_the_pdf(
    migrated, profile_id, extraction
) -> None:
    """`source_locator` stays NULL: a CV filename usually names the person."""
    import_cv_candidates(migrated, profile_id=profile_id, extraction=extraction)

    locators = [
        row[0]
        for row in migrated.execute("SELECT source_locator FROM profile_fact_provenance")
    ]
    assert locators == [None] * 5


# --------------------------------------------------------------------------
# Importing twice
# --------------------------------------------------------------------------


def test_importing_the_same_extraction_twice_proposes_nothing_new(
    migrated, profile_id, extraction
) -> None:
    first = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )
    second = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    assert (first.newly_proposed, first.already_imported) == (5, 0)
    assert (second.newly_proposed, second.already_imported) == (0, 5)
    assert [entry.fact.id for entry in second.imported] == [
        entry.fact.id for entry in first.imported
    ]
    assert _fact_count(migrated) == 5
    assert _provenance_count(migrated) == 5


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_a_decided_fact_never_comes_back_as_a_fresh_proposal(
    migrated, profile_id, extraction, decision
) -> None:
    """Re-importing must not ask a person to decide again what they decided."""
    first = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )
    decided = first.imported[1].fact.id
    decide = accept_profile_fact if decision == "accept" else reject_profile_fact
    decide(migrated, profile_id, decided)

    second = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    assert second.newly_proposed == 0
    assert _fact_count(migrated) == 5
    entry = second.imported[1]
    assert entry.fact.id == decided
    assert entry.created is False
    assert entry.needs_review is False
    assert entry.fact.status is (
        FactStatus.ACCEPTED if decision == "accept" else FactStatus.REJECTED
    )


def test_a_corrected_fact_never_comes_back_as_a_fresh_proposal(
    migrated, profile_id, extraction
) -> None:
    """The CV evidence stays on the corrected row, so the import still finds it."""
    first = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )
    corrected_id = first.imported[1].fact.id
    correction = correct_profile_fact(
        migrated, profile_id, corrected_id, value=TEST_ONLY_CORRECTION
    )

    second = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    assert second.newly_proposed == 0
    # The five imported facts, plus the one replacement the correction wrote.
    assert _fact_count(migrated) == 6
    entry = second.imported[1]
    assert entry.fact.id == corrected_id
    assert entry.fact.status is FactStatus.CORRECTED
    assert entry.fact.value == TEST_ONLY_CONTACT
    assert entry.needs_review is False
    replacement = get_profile_fact(migrated, profile_id, correction.replacement.id)
    assert replacement.value == TEST_ONLY_CORRECTION
    assert replacement.status is FactStatus.ACCEPTED


def test_the_same_value_read_from_another_cv_is_another_proof(
    migrated, profile_id
) -> None:
    """Two documents are two readings; consolidating them is not this slice."""
    import_cv_candidates(
        migrated,
        profile_id=profile_id,
        extraction=make_extraction(five_candidates()),
    )
    second = import_cv_candidates(
        migrated,
        profile_id=profile_id,
        extraction=make_extraction(
            five_candidates(TEST_ONLY_OTHER_SHA256), cv_sha256=TEST_ONLY_OTHER_SHA256
        ),
    )

    assert second.newly_proposed == 5
    assert _fact_count(migrated) == 10


# --------------------------------------------------------------------------
# Interruption and resumption
# --------------------------------------------------------------------------


def test_a_run_that_stops_halfway_leaves_whole_facts_and_resumes_cleanly(
    migrated, profile_id, extraction, monkeypatch
) -> None:
    """Each candidate is its own transaction, so a crash costs no consistency."""
    real = fact_bridge.ensure_profile_fact_proposal
    calls = {"count": 0}

    def fail_on_the_third(connection, **kwargs):
        calls["count"] += 1
        if calls["count"] == 3:
            raise KeyboardInterrupt("TEST ONLY interruption")
        return real(connection, **kwargs)

    monkeypatch.setattr(
        fact_bridge, "ensure_profile_fact_proposal", fail_on_the_third
    )
    with pytest.raises(KeyboardInterrupt):
        import_cv_candidates(migrated, profile_id=profile_id, extraction=extraction)

    # The two candidates that got through are complete, evidence included.
    assert _fact_count(migrated) == 2
    assert _provenance_count(migrated) == 2
    monkeypatch.undo()

    resumed = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    assert (resumed.newly_proposed, resumed.already_imported) == (3, 2)
    assert _fact_count(migrated) == 5
    assert _provenance_count(migrated) == 5


def test_a_failure_inside_one_candidate_leaves_no_half_written_fact(
    migrated, profile_id
) -> None:
    candidate = five_candidates()[0]
    provenance = provenance_for_candidate(candidate)

    def fail() -> None:
        raise sqlite3.OperationalError("TEST ONLY failure")

    with pytest.raises(sqlite3.OperationalError):
        ensure_profile_fact_proposal(
            migrated,
            profile_id=profile_id,
            fact_type=ProfileFactType.NAME,
            value=candidate.raw_text,
            provenance=provenance,
            after_lookup=fail,
        )

    assert _fact_count(migrated) == 0
    assert _provenance_count(migrated) == 0


# --------------------------------------------------------------------------
# Scoping and integrity
# --------------------------------------------------------------------------


def test_one_profiles_import_is_invisible_to_another(
    migrated, profile_id, other_profile_id, extraction
) -> None:
    """The same CV imported for two profiles is two independent sets of facts."""
    mine = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )
    theirs = import_cv_candidates(
        migrated, profile_id=other_profile_id, extraction=extraction
    )

    assert (mine.newly_proposed, theirs.newly_proposed) == (5, 5)
    assert _fact_count(migrated) == 10
    assert {entry.fact.id for entry in mine.imported}.isdisjoint(
        entry.fact.id for entry in theirs.imported
    )
    assert len(list_profile_facts(migrated, profile_id)) == 5
    assert len(list_profile_facts(migrated, other_profile_id)) == 5


def test_importing_for_a_profile_that_does_not_exist_writes_nothing(
    migrated, extraction
) -> None:
    with pytest.raises(ProfileFactNotFoundError):
        import_cv_candidates(migrated, profile_id=999, extraction=extraction)

    assert _fact_count(migrated) == 0
    assert _provenance_count(migrated) == 0


def test_one_proof_shared_by_two_facts_is_refused_rather_than_guessed_at(
    migrated, profile_id
) -> None:
    """Choosing between them would silently decide which reading counts."""
    candidate = five_candidates()[1]
    provenance = provenance_for_candidate(candidate)
    first = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=candidate.raw_text,
        provenance=provenance,
    )
    # A second fact claiming the same proof; `UNIQUE (fact_id, provenance_key)`
    # cannot stop this, because the fact id differs.
    duplicate = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=candidate.raw_text,
        provenance=ProvenanceInput(
            source_type=FactSourceType.CV, provenance_key="TEST-ONLY-other-key"
        ),
    )
    migrated.execute(
        "UPDATE profile_fact_provenance SET provenance_key = ? WHERE fact_id = ?",
        (provenance.resolved_provenance_key(), duplicate.fact.id),
    )
    migrated.commit()

    with pytest.raises(AmbiguousFactEvidenceError):
        ensure_profile_fact_proposal(
            migrated,
            profile_id=profile_id,
            fact_type=ProfileFactType.EMAIL,
            value=candidate.raw_text,
            provenance=provenance,
        )

    assert first.fact.id != duplicate.fact.id
    assert _fact_count(migrated) == 2


def test_a_proof_cannot_change_the_kind_of_fact_it_justifies(
    migrated, profile_id
) -> None:
    candidate = five_candidates()[1]
    provenance = provenance_for_candidate(candidate)
    ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=candidate.raw_text,
        provenance=provenance,
    )

    with pytest.raises(ConflictingFactEvidenceError):
        ensure_profile_fact_proposal(
            migrated,
            profile_id=profile_id,
            fact_type=ProfileFactType.SKILL,
            value=candidate.raw_text,
            provenance=provenance,
        )

    assert _fact_count(migrated) == 1


def test_the_same_proof_under_another_profile_is_not_reused(
    migrated, profile_id, other_profile_id
) -> None:
    """A provenance key is unique per fact, so scoping must be done by join."""
    candidate = five_candidates()[1]
    provenance = provenance_for_candidate(candidate)
    mine = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=candidate.raw_text,
        provenance=provenance,
    )
    theirs = ensure_profile_fact_proposal(
        migrated,
        profile_id=other_profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=candidate.raw_text,
        provenance=provenance,
    )

    assert (mine.created, theirs.created) == (True, True)
    assert mine.fact.id != theirs.fact.id


# --------------------------------------------------------------------------
# The result is reportable without quoting the CV
# --------------------------------------------------------------------------


def test_the_result_summary_never_quotes_the_cv(
    migrated, profile_id, extraction
) -> None:
    result = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    rendered = repr(result.summary()) + repr(
        [entry.summary() for entry in result.imported]
    )
    for secret in (
        TEST_ONLY_NAME,
        TEST_ONLY_CONTACT,
        TEST_ONLY_SKILL,
        TEST_ONLY_EXPERIENCE,
    ):
        assert secret not in rendered
    assert result.summary()["candidates"] == 5
    assert result.summary()["counts_by_fact_type"] == {
        "EMAIL": 1,
        "EXPERIENCE": 1,
        "NAME": 1,
        "SKILL": 2,
    }


def test_pending_review_holds_only_what_nobody_decided(
    migrated, profile_id, extraction
) -> None:
    result = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )
    accept_profile_fact(migrated, profile_id, result.imported[0].fact.id)
    reject_profile_fact(migrated, profile_id, result.imported[1].fact.id)

    reimported = import_cv_candidates(
        migrated, profile_id=profile_id, extraction=extraction
    )

    assert len(reimported.pending_review) == 3
    assert all(entry.needs_review for entry in reimported.pending_review)
