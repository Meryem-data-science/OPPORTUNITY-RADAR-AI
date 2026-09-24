"""Inert CV replacement staging, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away; the
operational database is never opened, and nothing in the package under test can
open it — `test_the_package_cannot_reach_the_operational_database` reads the
source to keep it so. No real CV, no real address and no real personal data
takes part: every candidate is built by hand from invented text marked TEST
ONLY, every digest is synthetic, and `.invalid` never resolves.
"""

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    ExtractedCandidate,
    ExtractionRule,
    StructuredCvExtraction,
    candidate_fingerprint,
)
from services.digital_twin.cv.fact_bridge import provenance_for_candidate
from services.digital_twin.cv.models import PARSER_VERSION, SectionType
from services.digital_twin.cv.replacement import manifest as manifest_module
from services.digital_twin.cv.replacement import planning as planning_module
from services.digital_twin.cv.replacement import repository as repository_module
from services.digital_twin.cv.replacement import staging as staging_module
from services.digital_twin.cv.replacement.manifest import (
    read_manifest,
    verify_manifest,
    write_extraction_with_manifest,
)
from services.digital_twin.cv.replacement.models import (
    ActiveCvDocumentError,
    CvDocumentNotFoundError,
    CvStagingError,
    DecisionNotPermittedError,
    EffectiveReplacementState,
    DecisionRole,
    DifferenceKind,
    DocumentOrigin,
    ExtractionMismatchError,
    ManifestIntegrityError,
    ManifestState,
    OpenReplacementExistsError,
    ReplacementAlreadyActivatedError,
    ReplacementClosedError,
    ReplacementLifecycle,
    ReviewDecision,
    ReviewIncompleteError,
    StaleReviewDecisionError,
    TerminalFactError,
)
from services.digital_twin.cv.replacement.planning import compute_replacement_plan
from services.digital_twin.cv.replacement.repository import (
    cancel_replacement,
    declare_active_cv_document,
    ensure_cv_document,
    ensure_extraction_with_manifest,
    get_active_cv_document,
    get_open_replacement,
    get_replacement,
    list_baseline_cv_facts,
    list_decision_digest_inputs,
    list_staged_decisions,
    open_replacement,
    set_replacement_lifecycle,
)
from services.digital_twin.cv.replacement.review_digest import compute_review_digest
from services.digital_twin.cv.replacement.staging import (
    mark_ready_to_activate,
    mark_ready_to_activate_in_transaction,
    record_review_decision,
    require_complete_current_review,
    review_progress,
)
from services.digital_twin.facts.models import FactSourceType, ProvenanceInput
from services.digital_twin.facts.repository import (
    accept_profile_fact,
    correct_profile_fact,
    ensure_profile_fact_proposal,
    ensure_profile_fact_provenance,
    reject_profile_fact,
)
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_OTHER_EMAIL = "other.student@example.invalid"
# Synthetic 64-hex digests; no real file was hashed to produce them.
TEST_ONLY_OLD_SHA256 = "ab" * 32
TEST_ONLY_NEW_SHA256 = "cd" * 32

TEST_ONLY_NAME = "Alex Test-Only"
TEST_ONLY_SKILL = "TestOnlyToolkit"
TEST_ONLY_SECOND_SKILL = "SecondTestOnlyToolkit"
TEST_ONLY_DROPPED_SKILL = "DroppedTestOnlyToolkit"
TEST_ONLY_CORRECTION = "CorrectedTestOnlyToolkit"


def make_candidate(
    candidate_type: CandidateType,
    raw_text: str,
    *,
    normalized_value: str | None = None,
    rule_id: ExtractionRule = ExtractionRule.SECTION_LINE_BLOCK,
    page_numbers: tuple[int, ...] = (1,),
    section_type: SectionType | None = SectionType.UNCLASSIFIED,
    section_index: int | None = 0,
    cv_sha256: str = TEST_ONLY_NEW_SHA256,
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
    candidates: tuple[ExtractedCandidate, ...],
    *,
    cv_sha256: str = TEST_ONLY_NEW_SHA256,
) -> StructuredCvExtraction:
    return StructuredCvExtraction(
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        parser_version=PARSER_VERSION,
        cv_sha256=cv_sha256,
        candidates=candidates,
        warnings=(),
    )


def new_cv_candidates() -> tuple[ExtractedCandidate, ...]:
    """The new document: the same name, one kept skill, one new skill."""
    return (
        make_candidate(
            CandidateType.NAME_CANDIDATE,
            TEST_ONLY_NAME,
            rule_id=ExtractionRule.HEADER_FIRST_LINE_NAME_SHAPE,
        ),
        make_candidate(CandidateType.SKILL, TEST_ONLY_SKILL),
        make_candidate(CandidateType.SKILL, TEST_ONLY_SECOND_SKILL),
    )


@pytest.fixture
def database_path(tmp_path) -> Path:
    return tmp_path / "cv-staging.db"


@pytest.fixture
def migrated(database_path):
    connection = connect_database(database_path)
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


@pytest.fixture
def other_profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_OTHER_EMAIL).profile_id


def old_cv_provenance(
    value: str, candidate_type: CandidateType = CandidateType.SKILL
) -> ProvenanceInput:
    """Evidence shaped like the old document's, with its own digest."""
    return ProvenanceInput(
        source_type=FactSourceType.CV,
        cv_sha256=TEST_ONLY_OLD_SHA256,
        parser_version=PARSER_VERSION,
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        candidate_fingerprint=candidate_fingerprint(candidate_type, value.casefold()),
        rule_id=ExtractionRule.SECTION_LINE_BLOCK.value,
        page_numbers=(1,),
        section_type=SectionType.UNCLASSIFIED.value,
        section_index=0,
    )


def accept_old_cv_fact(
    connection, profile_id: int, fact_type: str, value: str
) -> int:
    """One accepted fact of the old document, created through the owners."""
    proposal = ensure_profile_fact_proposal(
        connection,
        profile_id=profile_id,
        fact_type=fact_type,
        value=value,
        provenance=old_cv_provenance(value),
    )
    accept_profile_fact(connection, profile_id, proposal.fact.id)
    return proposal.fact.id


def snapshot_twin(connection) -> dict[str, list[tuple]]:
    """Everything a replacement must not touch, as plain rows."""
    tables = (
        "profile_facts",
        "profile_fact_provenance",
        "profile_preferences",
        "profile_availability",
        "profile_mobility",
    )
    captured: dict[str, list[tuple]] = {}
    for table in tables:
        try:
            captured[table] = connection.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
        except sqlite3.OperationalError:  # pragma: no cover - schema drift guard
            captured[table] = []
    return captured


@pytest.fixture
def prepared(migrated, profile_id):
    """An active old document, one accepted fact from it, and a new campaign."""
    declare_active_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_OLD_SHA256,
        origin=DocumentOrigin.LEGACY_DECLARED,
    )
    kept = accept_old_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    dropped = accept_old_cv_fact(
        migrated, profile_id, "SKILL", TEST_ONLY_DROPPED_SKILL
    )
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    outcome = ensure_extraction_with_manifest(
        migrated,
        profile_id=profile_id,
        document_id=document.id,
        extraction=make_extraction(new_cv_candidates()),
    )
    replacement = open_replacement(
        migrated, profile_id=profile_id, extraction_id=outcome.extraction.id
    )
    return {
        "document": document,
        "extraction": outcome.extraction,
        "replacement": replacement,
        "kept_fact_id": kept,
        "dropped_fact_id": dropped,
    }


# ------------------------------------------------------- ownership at SQLite


def test_a_document_of_another_profile_cannot_own_an_extraction(
    migrated, profile_id, other_profile_id
):
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO profile_cv_extractions (
                   document_id, profile_id, parser_version, extractor_version,
                   attempt_no, candidate_count, manifest_chain_digest, manifest_state
               ) VALUES (?, ?, 'p', 'e', 1, 0, ?, 'COMPLETE')""",
            (document.id, other_profile_id, "ef" * 32),
        )


def test_a_decision_cannot_name_another_profiles_fact(
    migrated, profile_id, other_profile_id, prepared
):
    foreign_fact = accept_old_cv_fact(
        migrated, other_profile_id, "SKILL", TEST_ONLY_SKILL
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO profile_cv_replacement_decisions (
                   replacement_id, profile_id, role, candidate_id, fact_id,
                   difference, decision, fact_status_at_decision, decided_at
               ) VALUES (?, ?, 'EXISTING', NULL, ?, 'ABSENT_FROM_NEW_CV', 'KEEP',
                         'ACCEPTED', CURRENT_TIMESTAMP)""",
            (prepared["replacement"].id, profile_id, foreign_fact),
        )


def test_a_decision_cannot_name_another_profiles_candidate(
    migrated, profile_id, other_profile_id, prepared
):
    foreign_document = ensure_cv_document(
        migrated,
        profile_id=other_profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    foreign = ensure_extraction_with_manifest(
        migrated,
        profile_id=other_profile_id,
        document_id=foreign_document.id,
        extraction=make_extraction(new_cv_candidates()),
    )
    foreign_candidate = read_manifest(
        migrated, profile_id=other_profile_id, extraction_id=foreign.extraction.id
    )[0]
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO profile_cv_replacement_decisions (
                   replacement_id, profile_id, role, candidate_id, fact_id,
                   difference, decision, decided_at
               ) VALUES (?, ?, 'INCOMING', ?, NULL, 'NEW', 'ACCEPT',
                         CURRENT_TIMESTAMP)""",
            (prepared["replacement"].id, profile_id, foreign_candidate.id),
        )


def test_foreign_keys_are_enforced_on_every_test_connection(migrated):
    assert migrated.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_decision_names_exactly_one_target(migrated, profile_id, prepared):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO profile_cv_replacement_decisions (
                   replacement_id, profile_id, role, candidate_id, fact_id,
                   difference, decision, decided_at
               ) VALUES (?, ?, 'INCOMING', NULL, NULL, 'NEW', 'ACCEPT',
                         CURRENT_TIMESTAMP)""",
            (prepared["replacement"].id, profile_id),
        )


def test_a_profile_cannot_hold_two_active_cv_documents(migrated, profile_id):
    declare_active_cv_document(
        migrated, profile_id=profile_id, content_sha256=TEST_ONLY_OLD_SHA256
    )
    with pytest.raises(ActiveCvDocumentError):
        declare_active_cv_document(
            migrated, profile_id=profile_id, content_sha256=TEST_ONLY_NEW_SHA256
        )


def test_declaring_the_same_active_document_twice_is_a_no_op(migrated, profile_id):
    first = declare_active_cv_document(
        migrated, profile_id=profile_id, content_sha256=TEST_ONLY_OLD_SHA256
    )
    again = declare_active_cv_document(
        migrated, profile_id=profile_id, content_sha256=TEST_ONLY_OLD_SHA256
    )
    assert again == first
    assert get_active_cv_document(migrated, profile_id) == first


# ------------------------------------------------------------- the manifest


def test_the_manifest_is_complete_after_a_restart(
    database_path, migrated, profile_id, prepared
):
    """A new connection, no extraction in memory, and the chain still holds."""
    migrated.close()
    reopened = connect_database(database_path)
    try:
        verification = verify_manifest(
            reopened,
            profile_id=profile_id,
            extraction_id=prepared["extraction"].id,
        )
        assert verification.ok
        assert verification.stored_count == 3
        assert verification.stored_chain_digest == verification.expected_chain_digest
    finally:
        reopened.close()


def test_a_missing_candidate_is_detected(migrated, profile_id, prepared):
    migrated.execute(
        "DELETE FROM profile_cv_candidates WHERE extraction_id = ? AND ordinal = 1",
        (prepared["extraction"].id,),
    )
    migrated.commit()
    verification = verify_manifest(
        migrated, profile_id=profile_id, extraction_id=prepared["extraction"].id
    )
    assert not verification.ok
    assert verification.reason == "ORDINAL_GAP"
    assert verification.first_divergent_ordinal == 1


def test_an_altered_candidate_is_detected_at_its_own_position(
    migrated, profile_id, prepared
):
    migrated.execute(
        """UPDATE profile_cv_candidates SET value = ?
            WHERE extraction_id = ? AND ordinal = 2""",
        (TEST_ONLY_CORRECTION, prepared["extraction"].id),
    )
    migrated.commit()
    verification = verify_manifest(
        migrated, profile_id=profile_id, extraction_id=prepared["extraction"].id
    )
    assert not verification.ok
    assert verification.reason == "CHAIN_DIVERGES"
    assert verification.first_divergent_ordinal == 2


def test_a_duplicated_candidate_is_refused_by_the_schema(
    migrated, profile_id, prepared
):
    row = migrated.execute(
        "SELECT candidate_fingerprint, provenance_key, chain_digest "
        "FROM profile_cv_candidates WHERE extraction_id = ? AND ordinal = 0",
        (prepared["extraction"].id,),
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO profile_cv_candidates (
                   extraction_id, profile_id, ordinal, candidate_type, fact_type,
                   candidate_fingerprint, provenance_key, value, rule_id, chain_digest
               ) VALUES (?, ?, 99, 'SKILL', 'SKILL', ?, ?, 'TEST ONLY', 'r', ?)""",
            (
                prepared["extraction"].id,
                profile_id,
                row[0],
                row[1],
                row[2],
            ),
        )


def test_a_plan_refuses_to_be_built_on_an_unverified_manifest(
    migrated, profile_id, prepared
):
    migrated.execute(
        "DELETE FROM profile_cv_candidates WHERE extraction_id = ? AND ordinal = 0",
        (prepared["extraction"].id,),
    )
    migrated.commit()
    with pytest.raises(ManifestIntegrityError):
        compute_replacement_plan(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
        )


# --------------------------------------------------------- retry semantics


def test_re_uploading_after_a_cancellation_reuses_the_verified_campaign(
    migrated, profile_id, prepared
):
    cancel_replacement(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    outcome = ensure_extraction_with_manifest(
        migrated,
        profile_id=profile_id,
        document_id=prepared["document"].id,
        extraction=make_extraction(new_cv_candidates()),
    )
    assert outcome.created is False
    assert outcome.extraction.id == prepared["extraction"].id
    assert outcome.reused_verification is not None and outcome.reused_verification.ok
    assert (
        migrated.execute(
            "SELECT COUNT(*) FROM profile_cv_candidates WHERE extraction_id = ?",
            (prepared["extraction"].id,),
        ).fetchone()[0]
        == 3
    )
    reopened = open_replacement(
        migrated, profile_id=profile_id, extraction_id=outcome.extraction.id
    )
    assert reopened.id != prepared["replacement"].id


def test_a_corrupt_manifest_is_marked_and_never_reused_or_rewritten(
    migrated, profile_id, prepared
):
    migrated.execute(
        """UPDATE profile_cv_candidates SET value = ?
            WHERE extraction_id = ? AND ordinal = 0""",
        (TEST_ONLY_CORRECTION, prepared["extraction"].id),
    )
    migrated.commit()
    before = migrated.execute(
        "SELECT id, ordinal, value, chain_digest FROM profile_cv_candidates "
        "WHERE extraction_id = ? ORDER BY ordinal",
        (prepared["extraction"].id,),
    ).fetchall()

    outcome = ensure_extraction_with_manifest(
        migrated,
        profile_id=profile_id,
        document_id=prepared["document"].id,
        extraction=make_extraction(new_cv_candidates()),
    )

    assert outcome.created is True
    assert outcome.abandoned_attempt_no == 1
    assert outcome.extraction.attempt_no == 2
    assert outcome.extraction.id != prepared["extraction"].id
    # The corrupt campaign is marked, kept and left exactly as it was.
    assert (
        migrated.execute(
            "SELECT manifest_state FROM profile_cv_extractions WHERE id = ?",
            (prepared["extraction"].id,),
        ).fetchone()[0]
        == ManifestState.CORRUPT.value
    )
    after = migrated.execute(
        "SELECT id, ordinal, value, chain_digest FROM profile_cv_candidates "
        "WHERE extraction_id = ? ORDER BY ordinal",
        (prepared["extraction"].id,),
    ).fetchall()
    assert after == before


def test_a_replacement_cannot_be_opened_over_a_corrupt_manifest(
    migrated, profile_id, prepared
):
    cancel_replacement(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    migrated.execute(
        "UPDATE profile_cv_extractions SET manifest_state = 'CORRUPT', "
        "corrupted_at = CURRENT_TIMESTAMP WHERE id = ?",
        (prepared["extraction"].id,),
    )
    migrated.commit()
    with pytest.raises(Exception) as error:
        open_replacement(
            migrated, profile_id=profile_id, extraction_id=prepared["extraction"].id
        )
    assert "corrupt" in str(error.value)


def test_only_one_replacement_is_open_at_a_time(migrated, profile_id, prepared):
    with pytest.raises(OpenReplacementExistsError):
        open_replacement(
            migrated, profile_id=profile_id, extraction_id=prepared["extraction"].id
        )


# ------------------------------------------------------------- the plan


def test_the_plan_classifies_both_sides(migrated, profile_id, prepared):
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    by_difference = plan.counts_by_difference()
    # The new document: a name nobody held, a skill both hold, a new skill.
    assert by_difference["NEW"] == 2
    # One incoming reading and one existing fact for the shared skill.
    assert by_difference["UNCHANGED_STILL_SUPPORTED"] == 2
    # The dropped skill is UNKNOWN, not a loss.
    assert by_difference["ABSENT_FROM_NEW_CV"] == 1
    incoming = [e for e in plan.entries if e.role is DecisionRole.INCOMING]
    existing = [e for e in plan.entries if e.role is DecisionRole.EXISTING]
    assert len(incoming) == 3
    assert len(existing) == 2


def test_an_identical_reading_is_still_asked(migrated, profile_id, prepared):
    """Text equality attaches no evidence and accepts nothing on its own."""
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    unchanged = [
        entry
        for entry in plan.entries
        if entry.role is DecisionRole.INCOMING
        and entry.difference is DifferenceKind.UNCHANGED_STILL_SUPPORTED
    ]
    assert len(unchanged) == 1
    progress = review_progress(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    assert progress["unanswered"] == len(plan.entries)


def test_a_fact_with_other_evidence_is_protected(migrated, profile_id, prepared):
    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=prepared["dropped_fact_id"],
        provenance=ProvenanceInput(
            source_type=FactSourceType.GITHUB, source_locator="test-only-locator"
        ),
    )
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    entry = next(
        e for e in plan.entries if e.fact_id == prepared["dropped_fact_id"]
    )
    assert entry.difference is DifferenceKind.INDEPENDENTLY_SUPPORTED
    with pytest.raises(DecisionNotPermittedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            fact_id=prepared["dropped_fact_id"],
            decision=ReviewDecision.RETIRE,
        )


def test_a_user_input_replacement_is_protected(migrated, profile_id):
    declare_active_cv_document(
        migrated, profile_id=profile_id, content_sha256=TEST_ONLY_OLD_SHA256
    )
    original = accept_old_cv_fact(
        migrated, profile_id, "SKILL", TEST_ONLY_DROPPED_SKILL
    )
    correction = correct_profile_fact(
        migrated, profile_id, original, value=TEST_ONLY_CORRECTION
    )
    # The replacement carries USER_INPUT evidence; give it the old document's
    # evidence too, so it is part of the baseline and could be offered.
    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=correction.replacement.id,
        provenance=old_cv_provenance(TEST_ONLY_CORRECTION),
    )
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    outcome = ensure_extraction_with_manifest(
        migrated,
        profile_id=profile_id,
        document_id=document.id,
        extraction=make_extraction(new_cv_candidates()),
    )
    replacement = open_replacement(
        migrated, profile_id=profile_id, extraction_id=outcome.extraction.id
    )
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement.id
    )
    entry = next(
        e for e in plan.entries if e.fact_id == correction.replacement.id
    )
    assert entry.difference is DifferenceKind.USER_INPUT_REPLACEMENT
    with pytest.raises(DecisionNotPermittedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement.id,
            fact_id=correction.replacement.id,
            decision=ReviewDecision.RETIRE,
        )


def test_a_forged_difference_label_does_not_authorise_a_retirement(
    migrated, profile_id, prepared
):
    """The decision is checked against the plan the database states now, so a
    row calling a protected fact something else changes nothing."""
    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=prepared["dropped_fact_id"],
        provenance=ProvenanceInput(
            source_type=FactSourceType.USER_INPUT, source_locator="test-only-locator"
        ),
    )
    with pytest.raises(DecisionNotPermittedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            fact_id=prepared["dropped_fact_id"],
            decision=ReviewDecision.RETIRE,
        )
    # And the schema refuses the mislabelled row even written directly.
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """INSERT INTO profile_cv_replacement_decisions (
                   replacement_id, profile_id, role, candidate_id, fact_id,
                   difference, decision, fact_status_at_decision, decided_at
               ) VALUES (?, ?, 'EXISTING', NULL, ?, 'INDEPENDENTLY_SUPPORTED',
                         'RETIRE', 'ACCEPTED', CURRENT_TIMESTAMP)""",
            (prepared["replacement"].id, profile_id, prepared["dropped_fact_id"]),
        )


def test_a_decision_about_something_outside_the_plan_is_refused(
    migrated, profile_id, prepared
):
    with pytest.raises(DecisionNotPermittedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            fact_id=9_999,
            decision=ReviewDecision.KEEP,
        )


# --------------------------------------------------- terminal facts, closed


def test_a_terminal_rejected_reading_fails_closed(migrated, profile_id):
    """The same proof already justifies a refused fact, so the reading cannot
    come back as a fresh proposal — and nothing here pretends otherwise."""
    candidate = make_candidate(CandidateType.SKILL, TEST_ONLY_SECOND_SKILL)
    proposal = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type="SKILL",
        value=candidate.raw_text,
        provenance=provenance_for_candidate(candidate),
    )
    reject_profile_fact(migrated, profile_id, proposal.fact.id)

    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    outcome = ensure_extraction_with_manifest(
        migrated,
        profile_id=profile_id,
        document_id=document.id,
        extraction=make_extraction((candidate,)),
    )
    replacement = open_replacement(
        migrated, profile_id=profile_id, extraction_id=outcome.extraction.id
    )
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement.id
    )
    entry = plan.entries[0]
    assert entry.difference is DifferenceKind.BLOCKED_TERMINAL_REJECTED

    for refused in (ReviewDecision.ACCEPT, ReviewDecision.REJECT):
        with pytest.raises(TerminalFactError):
            record_review_decision(
                migrated,
                profile_id=profile_id,
                replacement_id=replacement.id,
                candidate_id=entry.candidate_id,
                decision=refused,
            )
    skipped = record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement.id,
        candidate_id=entry.candidate_id,
        decision=ReviewDecision.SKIP_BLOCKED,
    )
    assert skipped.decision is ReviewDecision.SKIP_BLOCKED
    # The refused fact is still refused, and no second fact was created.
    assert (
        migrated.execute(
            "SELECT status FROM profile_facts WHERE id = ?", (proposal.fact.id,)
        ).fetchone()[0]
        == "REJECTED"
    )
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0] == 1
    )


def test_skip_blocked_is_refused_for_an_ordinary_reading(
    migrated, profile_id, prepared
):
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    entry = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    with pytest.raises(DecisionNotPermittedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            candidate_id=entry.candidate_id,
            decision=ReviewDecision.SKIP_BLOCKED,
        )


# ------------------------------------------------- deferred review and state


def answer_everything(connection, profile_id: int, replacement_id: int) -> None:
    plan = compute_replacement_plan(
        connection, profile_id=profile_id, replacement_id=replacement_id
    )
    for entry in plan.entries:
        if entry.role is DecisionRole.INCOMING:
            decision = ReviewDecision.ACCEPT
        else:
            decision = ReviewDecision.KEEP
        record_review_decision(
            connection,
            profile_id=profile_id,
            replacement_id=replacement_id,
            candidate_id=entry.candidate_id,
            fact_id=entry.fact_id,
            decision=decision,
        )


def test_an_unanswered_review_never_becomes_ready(migrated, profile_id, prepared):
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    first = plan.entries[0]
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        candidate_id=first.candidate_id,
        fact_id=first.fact_id,
        decision=ReviewDecision.ACCEPT
        if first.role is DecisionRole.INCOMING
        else ReviewDecision.KEEP,
    )
    with pytest.raises(ReviewIncompleteError):
        mark_ready_to_activate(
            migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
        )


def test_a_complete_review_becomes_ready_and_activates_nothing(
    migrated, profile_id, prepared
):
    before = snapshot_twin(migrated)
    answer_everything(migrated, profile_id, prepared["replacement"].id)
    ready = mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    assert ready.lifecycle is ReplacementLifecycle.READY_TO_ACTIVATE
    # Nothing about the person changed, and the old CV is still the active one.
    assert snapshot_twin(migrated) == before
    active = get_active_cv_document(migrated, profile_id)
    assert active is not None and active.content_sha256 == TEST_ONLY_OLD_SHA256


def test_answering_again_reopens_a_ready_review(migrated, profile_id, prepared):
    answer_everything(migrated, profile_id, prepared["replacement"].id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    incoming = next(e for e in plan.entries if e.role is DecisionRole.INCOMING)
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        candidate_id=incoming.candidate_id,
        decision=ReviewDecision.REJECT,
    )
    reopened = migrated.execute(
        "SELECT lifecycle FROM profile_cv_replacements WHERE id = ?",
        (prepared["replacement"].id,),
    ).fetchone()[0]
    assert reopened == ReplacementLifecycle.REVIEWING.value


def test_a_correction_stays_staged(migrated, profile_id, prepared):
    before = snapshot_twin(migrated)
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    entry = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    stored = record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        candidate_id=entry.candidate_id,
        decision=ReviewDecision.CORRECT,
        staged_value=TEST_ONLY_CORRECTION,
    )
    assert stored.decision is ReviewDecision.CORRECT
    assert stored.has_staged_value is True
    # The typed value is kept, and no fact carries it.
    assert (
        migrated.execute(
            "SELECT staged_value FROM profile_cv_replacement_decisions WHERE id = ?",
            (stored.id,),
        ).fetchone()[0]
        == TEST_ONLY_CORRECTION
    )
    assert (
        migrated.execute(
            "SELECT COUNT(*) FROM profile_facts WHERE value = ?",
            (TEST_ONLY_CORRECTION,),
        ).fetchone()[0]
        == 0
    )
    assert snapshot_twin(migrated) == before


def test_a_correction_without_a_value_is_refused(migrated, profile_id, prepared):
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    entry = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    with pytest.raises(DecisionNotPermittedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            candidate_id=entry.candidate_id,
            decision=ReviewDecision.CORRECT,
        )


def test_preparation_review_and_cancellation_leave_the_twin_untouched(
    migrated, profile_id
):
    declare_active_cv_document(
        migrated, profile_id=profile_id, content_sha256=TEST_ONLY_OLD_SHA256
    )
    accept_old_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    accept_old_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_DROPPED_SKILL)
    before = snapshot_twin(migrated)

    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    outcome = ensure_extraction_with_manifest(
        migrated,
        profile_id=profile_id,
        document_id=document.id,
        extraction=make_extraction(new_cv_candidates()),
    )
    replacement = open_replacement(
        migrated, profile_id=profile_id, extraction_id=outcome.extraction.id
    )
    assert snapshot_twin(migrated) == before

    answer_everything(migrated, profile_id, replacement.id)
    assert snapshot_twin(migrated) == before

    cancel_replacement(
        migrated, profile_id=profile_id, replacement_id=replacement.id
    )
    assert snapshot_twin(migrated) == before
    active = get_active_cv_document(migrated, profile_id)
    assert active is not None and active.content_sha256 == TEST_ONLY_OLD_SHA256
    # The answers stay readable; a cancellation forgets nobody's reasoning.
    assert list_staged_decisions(
        migrated, profile_id=profile_id, replacement_id=replacement.id
    )


def test_a_cancelled_replacement_answers_nothing_further(
    migrated, profile_id, prepared
):
    cancel_replacement(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    plan_target = migrated.execute(
        "SELECT id FROM profile_cv_candidates WHERE extraction_id = ? AND ordinal = 0",
        (prepared["extraction"].id,),
    ).fetchone()[0]
    with pytest.raises(ReplacementClosedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            candidate_id=plan_target,
            decision=ReviewDecision.ACCEPT,
        )


# ------------------------------------------------------------ privacy rules


def test_no_summary_carries_cv_text(migrated, profile_id, prepared):
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    entry = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    decision = record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        candidate_id=entry.candidate_id,
        decision=ReviewDecision.CORRECT,
        staged_value=TEST_ONLY_CORRECTION,
    )
    printed = " ".join(
        repr(item)
        for item in (
            plan.summary(),
            decision.summary(),
            prepared["extraction"].summary(),
            prepared["document"].summary(),
            prepared["replacement"].summary(),
            review_progress(
                migrated,
                profile_id=profile_id,
                replacement_id=prepared["replacement"].id,
            ),
            verify_manifest(
                migrated,
                profile_id=profile_id,
                extraction_id=prepared["extraction"].id,
            ).summary(),
        )
    )
    for secret in (
        TEST_ONLY_NAME,
        TEST_ONLY_SKILL,
        TEST_ONLY_SECOND_SKILL,
        TEST_ONLY_DROPPED_SKILL,
        TEST_ONLY_CORRECTION,
    ):
        assert secret not in printed


def test_no_error_message_quotes_a_reading(migrated, profile_id, prepared):
    migrated.execute(
        """UPDATE profile_cv_candidates SET value = ?
            WHERE extraction_id = ? AND ordinal = 0""",
        (TEST_ONLY_CORRECTION, prepared["extraction"].id),
    )
    migrated.commit()
    with pytest.raises(ManifestIntegrityError) as error:
        compute_replacement_plan(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
        )
    message = str(error.value)
    assert TEST_ONLY_CORRECTION not in message
    assert TEST_ONLY_NAME not in message


PACKAGE_MODULES = (
    manifest_module,
    planning_module,
    repository_module,
    staging_module,
)

_FORBIDDEN_WRITES = re.compile(
    r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"(profile_facts|profile_fact_provenance|profile_preferences|"
    r"profile_availability|profile_mobility|profile_skills|skills)\b",
    re.IGNORECASE,
)


def test_the_package_never_writes_a_fact_a_preference_or_a_skill():
    for module in PACKAGE_MODULES:
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert _FORBIDDEN_WRITES.search(source) is None, module.__name__


def test_the_package_cannot_reach_the_operational_database():
    """No configured connection, no settings, no path: these modules can only
    ever touch the connection a caller hands them."""
    for module in PACKAGE_MODULES:
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "load_settings",
            "connect_configured_database",
            "connect_database",
            "opportunity-radar.db",
            "data/",
        ):
            assert forbidden not in source, f"{module.__name__} mentions {forbidden}"


def _referenced_names(module) -> set[str]:
    """Every identifier the module's *code* uses, comments and prose excluded.

    Read from the syntax tree rather than the text, so a docstring explaining
    why an owner is not called cannot be mistaken for a call to it.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def test_the_package_runs_no_downstream_synchronization():
    for module in PACKAGE_MODULES:
        referenced = _referenced_names(module)
        for forbidden in (
            "synchronize_profile_skills",
            "synchronize_structured_profile_entries",
            "synchronize_eligibility",
            "sync_matching",
            "sync_recommendations",
            "sync_priority",
            "sync_portfolio",
            "accept_profile_fact",
            "reject_profile_fact",
            "correct_profile_fact",
            "decide_profile_facts",
            "import_cv_candidates",
            "ensure_profile_fact_proposal",
            "ensure_profile_fact_provenance",
        ):
            assert forbidden not in referenced, f"{module.__name__} calls {forbidden}"


# --------------------------------------------- fixes after independent review


def test_ready_to_activate_cannot_be_set_directly(migrated, profile_id, prepared):
    """The lifecycle setter may not make the claim the review owner makes."""
    with pytest.raises(CvStagingError):
        set_replacement_lifecycle(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            target=ReplacementLifecycle.READY_TO_ACTIVATE,
        )
    assert (
        migrated.execute(
            "SELECT lifecycle FROM profile_cv_replacements WHERE id = ?",
            (prepared["replacement"].id,),
        ).fetchone()[0]
        == ReplacementLifecycle.PREPARED.value
    )


def test_no_public_staging_function_declares_an_incomplete_review_ready(
    migrated, profile_id, prepared
):
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    first = plan.entries[0]
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        candidate_id=first.candidate_id,
        fact_id=first.fact_id,
        decision=ReviewDecision.ACCEPT
        if first.role is DecisionRole.INCOMING
        else ReviewDecision.KEEP,
    )
    with pytest.raises(ReviewIncompleteError):
        mark_ready_to_activate(
            migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
        )
    with pytest.raises(CvStagingError):
        set_replacement_lifecycle(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            target=ReplacementLifecycle.READY_TO_ACTIVATE,
        )
    assert (
        migrated.execute(
            "SELECT lifecycle FROM profile_cv_replacements WHERE id = ?",
            (prepared["replacement"].id,),
        ).fetchone()[0]
        == ReplacementLifecycle.REVIEWING.value
    )


def test_marking_ready_requires_the_owner_transaction(migrated, profile_id, prepared):
    """Without a transaction the verification and the write could not land
    together, so the in-transaction entry point refuses to run at all."""
    with pytest.raises(CvStagingError):
        mark_ready_to_activate_in_transaction(
            migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
        )


def test_a_retire_goes_stale_when_the_fact_gains_independent_evidence(
    migrated, profile_id, prepared
):
    """RETIRE on a CV-only fact, then a USER_INPUT proof arrives: fail closed."""
    answer_everything(migrated, profile_id, prepared["replacement"].id)
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        fact_id=prepared["dropped_fact_id"],
        decision=ReviewDecision.RETIRE,
    )
    # Ready at this point, because every entry is answered against its state.
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )

    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=prepared["dropped_fact_id"],
        provenance=ProvenanceInput(
            source_type=FactSourceType.USER_INPUT, source_locator="test-only-locator"
        ),
    )

    progress = review_progress(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    assert progress["unanswered"] == 0
    assert progress["stale_decisions"] == 1
    with pytest.raises(StaleReviewDecisionError):
        mark_ready_to_activate(
            migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
        )
    # And the stale answer can no longer be re-recorded as a retirement.
    with pytest.raises(DecisionNotPermittedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            fact_id=prepared["dropped_fact_id"],
            decision=ReviewDecision.RETIRE,
        )


def test_a_github_proof_also_makes_a_decision_stale(migrated, profile_id, prepared):
    answer_everything(migrated, profile_id, prepared["replacement"].id)
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        fact_id=prepared["dropped_fact_id"],
        decision=ReviewDecision.RETIRE,
    )
    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=prepared["dropped_fact_id"],
        provenance=ProvenanceInput(
            source_type=FactSourceType.GITHUB, source_locator="test-only-locator"
        ),
    )
    with pytest.raises(StaleReviewDecisionError):
        mark_ready_to_activate(
            migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
        )


def test_a_fact_decided_elsewhere_makes_its_answer_stale(
    migrated, profile_id, prepared
):
    answer_everything(migrated, profile_id, prepared["replacement"].id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    # Somebody rejects the fact through the ordinary owner, outside the review.
    reject_profile_fact(migrated, profile_id, prepared["dropped_fact_id"])
    with pytest.raises(ReviewIncompleteError):
        mark_ready_to_activate(
            migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
        )


def test_a_concurrent_cancellation_cannot_slip_between_check_and_write(
    database_path, migrated, profile_id, prepared
):
    """The answer's transaction holds the write lock from the first read."""
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    entry = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    other = connect_database(database_path)
    other.execute("PRAGMA busy_timeout = 100")
    attempts: list[str] = []

    def cancel_from_another_connection() -> None:
        try:
            cancel_replacement(
                other, profile_id=profile_id, replacement_id=prepared["replacement"].id
            )
            attempts.append("cancelled")
        except sqlite3.OperationalError as error:
            attempts.append(type(error).__name__)

    try:
        stored = record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            candidate_id=entry.candidate_id,
            decision=ReviewDecision.ACCEPT,
            after_plan=cancel_from_another_connection,
        )
        assert attempts == ["OperationalError"]
        assert stored.decision is ReviewDecision.ACCEPT
        assert (
            migrated.execute(
                "SELECT lifecycle FROM profile_cv_replacements WHERE id = ?",
                (prepared["replacement"].id,),
            ).fetchone()[0]
            == ReplacementLifecycle.REVIEWING.value
        )
    finally:
        other.close()


def test_a_cancellation_committed_first_stops_the_next_answer(
    database_path, migrated, profile_id, prepared
):
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    entry = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    other = connect_database(database_path)
    try:
        cancel_replacement(
            other, profile_id=profile_id, replacement_id=prepared["replacement"].id
        )
    finally:
        other.close()
    with pytest.raises(ReplacementClosedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=prepared["replacement"].id,
            candidate_id=entry.candidate_id,
            decision=ReviewDecision.ACCEPT,
        )


def test_an_extraction_for_another_document_is_refused_before_any_write(
    migrated, profile_id
):
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    foreign = make_extraction(
        tuple(
            make_candidate(
                candidate.candidate_type,
                candidate.raw_text,
                rule_id=candidate.rule_id,
                cv_sha256=TEST_ONLY_OLD_SHA256,
            )
            for candidate in new_cv_candidates()
        ),
        cv_sha256=TEST_ONLY_OLD_SHA256,
    )
    with pytest.raises(ExtractionMismatchError):
        ensure_extraction_with_manifest(
            migrated,
            profile_id=profile_id,
            document_id=document.id,
            extraction=foreign,
        )
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_cv_extractions").fetchone()[0]
        == 0
    )


def test_a_candidate_from_another_document_is_refused(migrated, profile_id):
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    mixed = make_extraction(
        (
            make_candidate(CandidateType.SKILL, TEST_ONLY_SKILL),
            make_candidate(
                CandidateType.SKILL,
                TEST_ONLY_SECOND_SKILL,
                cv_sha256=TEST_ONLY_OLD_SHA256,
            ),
        )
    )
    with pytest.raises(ExtractionMismatchError) as error:
        ensure_extraction_with_manifest(
            migrated, profile_id=profile_id, document_id=document.id, extraction=mixed
        )
    assert "candidate 1" in str(error.value)
    assert TEST_ONLY_SECOND_SKILL not in str(error.value)
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_cv_candidates").fetchone()[0]
        == 0
    )


def test_a_candidate_read_by_other_versions_is_refused(migrated, profile_id):
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    candidate = make_candidate(CandidateType.SKILL, TEST_ONLY_SKILL)
    other_version = ExtractedCandidate(
        candidate_type=candidate.candidate_type,
        raw_text=candidate.raw_text,
        normalized_value=candidate.normalized_value,
        page_numbers=candidate.page_numbers,
        section_type=candidate.section_type,
        section_index=candidate.section_index,
        rule_id=candidate.rule_id,
        fingerprint=candidate.fingerprint,
        cv_sha256=candidate.cv_sha256,
        parser_version="cv-parser-test-only",
        extractor_version=candidate.extractor_version,
    )
    with pytest.raises(ExtractionMismatchError) as error:
        ensure_extraction_with_manifest(
            migrated,
            profile_id=profile_id,
            document_id=document.id,
            extraction=make_extraction((other_version,)),
        )
    assert "cv-parser-test-only" in str(error.value)
    assert TEST_ONLY_SKILL not in str(error.value)


def test_a_document_of_another_profile_is_refused_before_any_write(
    migrated, profile_id, other_profile_id
):
    foreign_document = ensure_cv_document(
        migrated,
        profile_id=other_profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    with pytest.raises(CvDocumentNotFoundError):
        ensure_extraction_with_manifest(
            migrated,
            profile_id=profile_id,
            document_id=foreign_document.id,
            extraction=make_extraction(new_cv_candidates()),
        )


def test_changed_content_under_the_same_versions_is_refused_not_marked_corrupt(
    migrated, profile_id, prepared
):
    """Same document, same version labels, different readings: the caller is
    wrong, and the stored campaign, which still verifies, stays COMPLETE."""
    changed = make_extraction(
        (
            make_candidate(
                CandidateType.NAME_CANDIDATE,
                TEST_ONLY_NAME,
                rule_id=ExtractionRule.HEADER_FIRST_LINE_NAME_SHAPE,
            ),
            make_candidate(CandidateType.SKILL, TEST_ONLY_SKILL),
            make_candidate(CandidateType.SKILL, TEST_ONLY_CORRECTION),
        )
    )
    with pytest.raises(ExtractionMismatchError):
        ensure_extraction_with_manifest(
            migrated,
            profile_id=profile_id,
            document_id=prepared["document"].id,
            extraction=changed,
        )
    assert (
        migrated.execute(
            "SELECT manifest_state FROM profile_cv_extractions WHERE id = ?",
            (prepared["extraction"].id,),
        ).fetchone()[0]
        == ManifestState.COMPLETE.value
    )
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_cv_extractions").fetchone()[0]
        == 1
    )
    assert verify_manifest(
        migrated, profile_id=profile_id, extraction_id=prepared["extraction"].id
    ).ok


def test_no_dataclass_repr_exposes_cv_text(migrated, profile_id, prepared):
    """A traceback, a log line or a failed assertion prints repr(). None of
    these carry a reading, a normalized value or a staged correction."""
    entries = read_manifest(
        migrated, profile_id=profile_id, extraction_id=prepared["extraction"].id
    )
    baseline = list_baseline_cv_facts(
        migrated, profile_id=profile_id, content_sha256=TEST_ONLY_OLD_SHA256
    )
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    target = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    decision = record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        candidate_id=target.candidate_id,
        decision=ReviewDecision.CORRECT,
        staged_value=TEST_ONLY_CORRECTION,
    )
    printed = " ".join(
        repr(item)
        for item in (
            *entries,
            *baseline,
            plan,
            *plan.entries,
            decision,
            prepared["document"],
            prepared["extraction"],
            prepared["replacement"],
        )
    )
    for secret in (
        TEST_ONLY_NAME,
        TEST_ONLY_SKILL,
        TEST_ONLY_SECOND_SKILL,
        TEST_ONLY_DROPPED_SKILL,
        TEST_ONLY_CORRECTION,
    ):
        assert secret not in printed


# ------------------------------------- lower-level entry points are not holes


def test_the_in_transaction_ready_entry_point_checks_the_review_itself(
    migrated, profile_id, prepared
):
    """An open transaction is not permission. The caller owns the transaction;
    the function still verifies the review before it writes anything."""
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
    )
    first = plan.entries[0]
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        candidate_id=first.candidate_id,
        fact_id=first.fact_id,
        decision=ReviewDecision.ACCEPT
        if first.role is DecisionRole.INCOMING
        else ReviewDecision.KEEP,
    )

    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ReviewIncompleteError):
            mark_ready_to_activate_in_transaction(
                migrated,
                profile_id=profile_id,
                replacement_id=prepared["replacement"].id,
            )
    finally:
        migrated.execute("ROLLBACK")

    assert (
        migrated.execute(
            "SELECT lifecycle FROM profile_cv_replacements WHERE id = ?",
            (prepared["replacement"].id,),
        ).fetchone()[0]
        == ReplacementLifecycle.REVIEWING.value
    )


def test_the_in_transaction_ready_entry_point_refuses_a_stale_review(
    migrated, profile_id, prepared
):
    answer_everything(migrated, profile_id, prepared["replacement"].id)
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=prepared["replacement"].id,
        fact_id=prepared["dropped_fact_id"],
        decision=ReviewDecision.RETIRE,
    )
    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=prepared["dropped_fact_id"],
        provenance=ProvenanceInput(
            source_type=FactSourceType.USER_INPUT, source_locator="test-only-locator"
        ),
    )
    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StaleReviewDecisionError):
            mark_ready_to_activate_in_transaction(
                migrated,
                profile_id=profile_id,
                replacement_id=prepared["replacement"].id,
            )
    finally:
        migrated.execute("ROLLBACK")
    assert (
        migrated.execute(
            "SELECT lifecycle FROM profile_cv_replacements WHERE id = ?",
            (prepared["replacement"].id,),
        ).fetchone()[0]
        == ReplacementLifecycle.REVIEWING.value
    )


def test_no_repository_statement_writes_the_ready_lifecycle():
    """One writer for that lifecycle, and it is the one that verifies.

    The check is on the write itself, not on the word: prose explaining why a
    module does not set a lifecycle is not a module that sets it.
    """
    write = re.compile(
        r"SET\s+lifecycle\s*=\s*'READY_TO_ACTIVATE'", re.IGNORECASE
    )
    for module in (repository_module, planning_module, manifest_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert write.search(source) is None, module.__name__
    staging_source = Path(staging_module.__file__).read_text(encoding="utf-8")
    assert len(write.findall(staging_source)) == 1


def test_writing_a_manifest_directly_checks_the_document_owner(
    migrated, profile_id, other_profile_id
):
    foreign_document = ensure_cv_document(
        migrated,
        profile_id=other_profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    with pytest.raises(CvDocumentNotFoundError):
        write_extraction_with_manifest(
            migrated,
            profile_id=profile_id,
            document_id=foreign_document.id,
            extraction=make_extraction(new_cv_candidates()),
            attempt_no=1,
        )
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_cv_extractions").fetchone()[0]
        == 0
    )
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_cv_candidates").fetchone()[0]
        == 0
    )


def test_writing_a_manifest_directly_checks_the_document_digest(
    migrated, profile_id
):
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    other_document_extraction = make_extraction(
        tuple(
            make_candidate(
                candidate.candidate_type,
                candidate.raw_text,
                rule_id=candidate.rule_id,
                cv_sha256=TEST_ONLY_OLD_SHA256,
            )
            for candidate in new_cv_candidates()
        ),
        cv_sha256=TEST_ONLY_OLD_SHA256,
    )
    with pytest.raises(ExtractionMismatchError):
        write_extraction_with_manifest(
            migrated,
            profile_id=profile_id,
            document_id=document.id,
            extraction=other_document_extraction,
            attempt_no=1,
        )
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_cv_extractions").fetchone()[0]
        == 0
    )


def test_writing_a_manifest_directly_checks_every_candidate(migrated, profile_id):
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    mixed = make_extraction(
        (
            make_candidate(CandidateType.SKILL, TEST_ONLY_SKILL),
            make_candidate(
                CandidateType.SKILL,
                TEST_ONLY_SECOND_SKILL,
                cv_sha256=TEST_ONLY_OLD_SHA256,
            ),
        )
    )
    with pytest.raises(ExtractionMismatchError) as error:
        write_extraction_with_manifest(
            migrated,
            profile_id=profile_id,
            document_id=document.id,
            extraction=mixed,
            attempt_no=1,
        )
    assert "candidate 1" in str(error.value)
    assert TEST_ONLY_SECOND_SKILL not in str(error.value)
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_cv_candidates").fetchone()[0]
        == 0
    )


def test_writing_a_manifest_directly_checks_candidate_versions(migrated, profile_id):
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=TEST_ONLY_NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    candidate = make_candidate(CandidateType.SKILL, TEST_ONLY_SKILL)
    other_version = ExtractedCandidate(
        candidate_type=candidate.candidate_type,
        raw_text=candidate.raw_text,
        normalized_value=candidate.normalized_value,
        page_numbers=candidate.page_numbers,
        section_type=candidate.section_type,
        section_index=candidate.section_index,
        rule_id=candidate.rule_id,
        fingerprint=candidate.fingerprint,
        cv_sha256=candidate.cv_sha256,
        parser_version=candidate.parser_version,
        extractor_version="cv-candidates-test-only",
    )
    with pytest.raises(ExtractionMismatchError) as error:
        write_extraction_with_manifest(
            migrated,
            profile_id=profile_id,
            document_id=document.id,
            extraction=make_extraction((other_version,)),
            attempt_no=1,
        )
    assert "cv-candidates-test-only" in str(error.value)
    assert TEST_ONLY_SKILL not in str(error.value)


def test_a_direct_manifest_write_preserves_an_existing_campaign(
    migrated, profile_id, prepared
):
    """A refused direct write leaves the stored campaign exactly as it was."""
    before = migrated.execute(
        "SELECT id, ordinal, value, chain_digest FROM profile_cv_candidates "
        "WHERE extraction_id = ? ORDER BY ordinal",
        (prepared["extraction"].id,),
    ).fetchall()
    mismatched = make_extraction(
        (
            make_candidate(
                CandidateType.SKILL, TEST_ONLY_SKILL, cv_sha256=TEST_ONLY_OLD_SHA256
            ),
        ),
        cv_sha256=TEST_ONLY_OLD_SHA256,
    )
    with pytest.raises(ExtractionMismatchError):
        write_extraction_with_manifest(
            migrated,
            profile_id=profile_id,
            document_id=prepared["document"].id,
            extraction=mismatched,
            attempt_no=2,
        )
    after = migrated.execute(
        "SELECT id, ordinal, value, chain_digest FROM profile_cv_candidates "
        "WHERE extraction_id = ? ORDER BY ordinal",
        (prepared["extraction"].id,),
    ).fetchall()
    assert after == before
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_cv_extractions").fetchone()[0]
        == 1
    )
    assert (
        migrated.execute(
            "SELECT manifest_state FROM profile_cv_extractions WHERE id = ?",
            (prepared["extraction"].id,),
        ).fetchone()[0]
        == ManifestState.COMPLETE.value
    )


# ---------------------------------------- B1b-B1: review token and activation
#
# Activation itself is B1b-B2 and does not exist yet, so the tests below write
# the 0028 activation columns directly. That is the point: what they check is
# that every review and cancellation path refuses an activated attempt whoever
# activated it, not that a particular function did.


TEST_ONLY_REVIEW_TOKEN = "ff" * 32


def _stored_token(connection, replacement_id: int) -> str | None:
    return connection.execute(
        "SELECT ready_review_digest FROM profile_cv_replacements WHERE id = ?",
        (replacement_id,),
    ).fetchone()[0]


def _force_activated(connection, replacement_id: int, *, revision: int = 1) -> None:
    """Set the 0028 activation columns by hand, as B1b-B2 will."""
    token = _stored_token(connection, replacement_id) or TEST_ONLY_REVIEW_TOKEN
    connection.execute(
        """UPDATE profile_cv_replacements
              SET ready_review_digest = ?,
                  activation_revision = ?,
                  activated_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
        (token, revision, replacement_id),
    )
    connection.commit()


def test_declaring_a_review_ready_stores_its_token(migrated, profile_id, prepared):
    replacement_id = prepared["replacement"].id
    assert _stored_token(migrated, replacement_id) is None

    answer_everything(migrated, profile_id, replacement_id)
    ready = mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )

    stored = _stored_token(migrated, replacement_id)
    assert stored is not None and len(stored) == 64
    assert ready.ready_review_digest == stored
    assert ready.effective_state is EffectiveReplacementState.READY_TO_ACTIVATE
    assert ready.is_activated is False


def test_the_stored_token_is_the_one_the_review_computes(migrated, profile_id, prepared):
    replacement_id = prepared["replacement"].id
    answer_everything(migrated, profile_id, replacement_id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    assert _stored_token(migrated, replacement_id) == compute_review_digest(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        extraction_id=prepared["extraction"].id,
    )


def test_changing_a_decision_clears_the_token(migrated, profile_id, prepared):
    replacement_id = prepared["replacement"].id
    answer_everything(migrated, profile_id, replacement_id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    assert _stored_token(migrated, replacement_id) is not None

    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    incoming = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        candidate_id=incoming.candidate_id,
        decision=ReviewDecision.REJECT,
    )

    assert _stored_token(migrated, replacement_id) is None
    assert (
        migrated.execute(
            "SELECT lifecycle FROM profile_cv_replacements WHERE id = ?",
            (replacement_id,),
        ).fetchone()[0]
        == ReplacementLifecycle.REVIEWING.value
    )


def test_a_new_token_is_issued_after_the_review_changes(migrated, profile_id, prepared):
    replacement_id = prepared["replacement"].id
    answer_everything(migrated, profile_id, replacement_id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    first = _stored_token(migrated, replacement_id)

    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    incoming = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        candidate_id=incoming.candidate_id,
        decision=ReviewDecision.REJECT,
    )
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    second = _stored_token(migrated, replacement_id)

    assert second is not None
    assert second != first


def test_a_decision_taken_while_reviewing_also_clears_a_stale_token(
    migrated, profile_id, prepared
):
    """The clearing is not a side effect of the lifecycle move: it happens even
    when the attempt is already REVIEWING."""
    replacement_id = prepared["replacement"].id
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    first_entry = plan.entries[0]
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        candidate_id=first_entry.candidate_id,
        fact_id=first_entry.fact_id,
        decision=ReviewDecision.ACCEPT
        if first_entry.role is DecisionRole.INCOMING
        else ReviewDecision.KEEP,
    )
    # REVIEWING, and a token planted by hand as a previous ready state would.
    migrated.execute(
        "UPDATE profile_cv_replacements SET ready_review_digest = ? WHERE id = ?",
        (TEST_ONLY_REVIEW_TOKEN, replacement_id),
    )
    migrated.commit()

    second_entry = plan.entries[1]
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        candidate_id=second_entry.candidate_id,
        fact_id=second_entry.fact_id,
        decision=ReviewDecision.ACCEPT
        if second_entry.role is DecisionRole.INCOMING
        else ReviewDecision.KEEP,
    )
    assert _stored_token(migrated, replacement_id) is None


def test_an_incomplete_review_is_still_refused_and_stores_no_token(
    migrated, profile_id, prepared
):
    replacement_id = prepared["replacement"].id
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    entry = plan.entries[0]
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        candidate_id=entry.candidate_id,
        fact_id=entry.fact_id,
        decision=ReviewDecision.ACCEPT
        if entry.role is DecisionRole.INCOMING
        else ReviewDecision.KEEP,
    )
    with pytest.raises(ReviewIncompleteError):
        mark_ready_to_activate(
            migrated, profile_id=profile_id, replacement_id=replacement_id
        )
    assert _stored_token(migrated, replacement_id) is None


def test_the_shared_validator_requires_the_callers_transaction(
    migrated, profile_id, prepared
):
    with pytest.raises(CvStagingError):
        require_complete_current_review(
            migrated, profile_id=profile_id, replacement_id=prepared["replacement"].id
        )


def test_the_shared_validator_returns_the_plan_it_verified(
    migrated, profile_id, prepared
):
    replacement_id = prepared["replacement"].id
    answer_everything(migrated, profile_id, replacement_id)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        plan = require_complete_current_review(
            migrated, profile_id=profile_id, replacement_id=replacement_id
        )
    finally:
        migrated.execute("ROLLBACK")
    assert plan.replacement_id == replacement_id
    assert plan.extraction_id == prepared["extraction"].id


# --------------------------------------- an activated attempt is out of reach


def test_an_activated_attempt_reads_as_activated(migrated, profile_id, prepared):
    replacement_id = prepared["replacement"].id
    answer_everything(migrated, profile_id, replacement_id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    _force_activated(migrated, replacement_id)

    stored = get_replacement(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    assert stored.is_activated is True
    assert stored.effective_state is EffectiveReplacementState.ACTIVATED
    assert stored.is_open is False
    # The storage column is untouched, and the summary says both.
    assert stored.lifecycle is ReplacementLifecycle.READY_TO_ACTIVATE
    assert stored.summary()["effective_state"] == "ACTIVATED"
    assert stored.activation_revision == 1


def test_an_activated_attempt_answers_no_further_decision(
    migrated, profile_id, prepared
):
    replacement_id = prepared["replacement"].id
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    entry = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    answer_everything(migrated, profile_id, replacement_id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    _force_activated(migrated, replacement_id)

    with pytest.raises(ReplacementAlreadyActivatedError):
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            candidate_id=entry.candidate_id,
            decision=ReviewDecision.REJECT,
        )


def test_an_activated_attempt_cannot_be_cancelled(migrated, profile_id, prepared):
    replacement_id = prepared["replacement"].id
    answer_everything(migrated, profile_id, replacement_id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    _force_activated(migrated, replacement_id)

    with pytest.raises(ReplacementAlreadyActivatedError):
        cancel_replacement(
            migrated, profile_id=profile_id, replacement_id=replacement_id
        )
    stored = get_replacement(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    assert stored.effective_state is EffectiveReplacementState.ACTIVATED


def test_an_activated_attempt_cannot_be_reopened_for_review(
    migrated, profile_id, prepared
):
    replacement_id = prepared["replacement"].id
    answer_everything(migrated, profile_id, replacement_id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    _force_activated(migrated, replacement_id)

    with pytest.raises(ReplacementAlreadyActivatedError):
        set_replacement_lifecycle(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            target=ReplacementLifecycle.REVIEWING,
        )
    with pytest.raises(ReplacementAlreadyActivatedError):
        mark_ready_to_activate(
            migrated, profile_id=profile_id, replacement_id=replacement_id
        )


def test_an_activated_attempt_is_not_an_open_replacement(
    migrated, profile_id, prepared
):
    replacement_id = prepared["replacement"].id
    answer_everything(migrated, profile_id, replacement_id)
    mark_ready_to_activate(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    _force_activated(migrated, replacement_id)

    assert get_open_replacement(migrated, profile_id) is None
    # And the next attempt can be opened over another campaign.
    other_document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256="19" * 32,
        byte_size=4096,
        page_count=2,
    )
    other_candidates = tuple(
        make_candidate(
            candidate.candidate_type,
            candidate.raw_text,
            rule_id=candidate.rule_id,
            cv_sha256="19" * 32,
        )
        for candidate in new_cv_candidates()
    )
    other = ensure_extraction_with_manifest(
        migrated,
        profile_id=profile_id,
        document_id=other_document.id,
        extraction=make_extraction(other_candidates, cv_sha256="19" * 32),
    )
    reopened = open_replacement(
        migrated, profile_id=profile_id, extraction_id=other.extraction.id
    )
    assert reopened.id != replacement_id
    assert reopened.effective_state is EffectiveReplacementState.PREPARED


def test_no_summary_or_error_carries_the_staged_value(migrated, profile_id, prepared):
    replacement_id = prepared["replacement"].id
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    entry = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        candidate_id=entry.candidate_id,
        decision=ReviewDecision.CORRECT,
        staged_value=TEST_ONLY_CORRECTION,
    )
    digest = compute_review_digest(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        extraction_id=prepared["extraction"].id,
    )
    inputs = list_decision_digest_inputs(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    printed = " ".join(
        [digest, repr(inputs), repr(get_replacement(
            migrated, profile_id=profile_id, replacement_id=replacement_id
        ).summary())]
    )
    assert TEST_ONLY_CORRECTION not in printed
