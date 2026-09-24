"""Activating a reviewed CV replacement, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away; the
operational database is never opened. No real CV takes part: every candidate is
built by hand from invented text marked TEST ONLY, every digest is synthetic,
and `.invalid` never resolves.

The property almost every test below is about is the same one: an activation
happens whole or not at all. So most of them take a snapshot of the six tables
an activation can touch, make something fail, and assert the snapshot came back
unchanged.
"""

import sqlite3

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
from services.digital_twin.cv.models import PARSER_VERSION, SectionType
from services.digital_twin.cv.replacement.activation import (
    ActiveCvChangedError,
    AlreadyActiveDocumentError,
    HistoricalDocumentReactivationError,
    ReviewChangedError,
    activate_cv_replacement,
)
from services.digital_twin.cv.replacement.models import (
    ContradictoryReviewError,
    CvStagingError,
    DecisionRole,
    DifferenceKind,
    DocumentLifecycle,
    DocumentOrigin,
    EffectiveReplacementState,
    ReplacementLifecycle,
    ReviewDecision,
)
from services.digital_twin.cv.replacement.planning import compute_replacement_plan
from services.digital_twin.cv.replacement.repository import (
    cancel_replacement,
    declare_active_cv_document,
    ensure_cv_document,
    ensure_extraction_with_manifest,
    get_active_cv_document,
    get_cv_document,
    get_replacement,
    open_replacement,
)
from services.digital_twin.cv.replacement.staging import (
    mark_ready_to_activate,
    record_review_decision,
)
from services.digital_twin.facts.models import (
    FactSourceType,
    FactStatus,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    accept_profile_fact,
    ensure_profile_fact_proposal,
    ensure_profile_fact_provenance,
    get_profile_fact,
    list_profile_fact_provenance,
    list_profile_facts,
    list_verified_profile_facts,
)
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
# Synthetic 64-hex digests; no real file was hashed to produce them.
OLD_SHA256 = "ab" * 32
NEW_SHA256 = "cd" * 32
THIRD_SHA256 = "ef" * 32

TEST_ONLY_NAME = "Alex Test-Only"
KEPT_SKILL = "TestOnlyToolkit"
NEW_SKILL = "SecondTestOnlyToolkit"
DROPPED_SKILL = "DroppedTestOnlyToolkit"
CORRECTION = "CorrectedTestOnlyToolkit"

TOUCHED_TABLES = (
    "profile_facts",
    "profile_fact_provenance",
    "profile_cv_documents",
    "profile_cv_replacements",
    "profile_cv_replacement_decisions",
    "recommendation_profile_state",
)


def make_candidate(
    candidate_type: CandidateType, raw_text: str, *, cv_sha256: str = NEW_SHA256
) -> ExtractedCandidate:
    return ExtractedCandidate(
        candidate_type=candidate_type,
        raw_text=raw_text,
        normalized_value=None,
        page_numbers=(1,),
        section_type=SectionType.UNCLASSIFIED,
        section_index=0,
        rule_id=ExtractionRule.SECTION_LINE_BLOCK,
        fingerprint=candidate_fingerprint(candidate_type, raw_text.casefold()),
        cv_sha256=cv_sha256,
        parser_version=PARSER_VERSION,
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
    )


def make_extraction(candidates, *, cv_sha256: str = NEW_SHA256) -> StructuredCvExtraction:
    return StructuredCvExtraction(
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        parser_version=PARSER_VERSION,
        cv_sha256=cv_sha256,
        candidates=tuple(candidates),
        warnings=(),
    )


def new_cv(cv_sha256: str = NEW_SHA256):
    """The replacement document: the kept skill, a new one, and a name."""
    return (
        make_candidate(CandidateType.NAME_CANDIDATE, TEST_ONLY_NAME, cv_sha256=cv_sha256),
        make_candidate(CandidateType.SKILL, KEPT_SKILL, cv_sha256=cv_sha256),
        make_candidate(CandidateType.SKILL, NEW_SKILL, cv_sha256=cv_sha256),
    )


def old_cv_provenance(value: str, candidate_type=CandidateType.SKILL) -> ProvenanceInput:
    return ProvenanceInput(
        source_type=FactSourceType.CV,
        cv_sha256=OLD_SHA256,
        parser_version=PARSER_VERSION,
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        candidate_fingerprint=candidate_fingerprint(candidate_type, value.casefold()),
        rule_id=ExtractionRule.SECTION_LINE_BLOCK.value,
        page_numbers=(1,),
        section_type=SectionType.UNCLASSIFIED.value,
        section_index=0,
    )


def accept_old_fact(connection, profile_id: int, fact_type: str, value: str) -> int:
    proposal = ensure_profile_fact_proposal(
        connection,
        profile_id=profile_id,
        fact_type=fact_type,
        value=value,
        provenance=old_cv_provenance(value),
    )
    accept_profile_fact(connection, profile_id, proposal.fact.id)
    return proposal.fact.id


def snapshot(connection) -> dict[str, list[tuple]]:
    captured: dict[str, list[tuple]] = {}
    for table in TOUCHED_TABLES:
        captured[table] = connection.execute(
            f"SELECT * FROM {table} ORDER BY 1"
        ).fetchall()
    return captured


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "activation.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


@pytest.fixture
def staged(migrated, profile_id):
    """An active old CV with two accepted skills, and a reviewed replacement."""
    declare_active_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=OLD_SHA256,
        origin=DocumentOrigin.LEGACY_DECLARED,
    )
    kept = accept_old_fact(migrated, profile_id, "SKILL", KEPT_SKILL)
    dropped = accept_old_fact(migrated, profile_id, "SKILL", DROPPED_SKILL)
    document = ensure_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    outcome = ensure_extraction_with_manifest(
        migrated,
        profile_id=profile_id,
        document_id=document.id,
        extraction=make_extraction(new_cv()),
    )
    replacement = open_replacement(
        migrated, profile_id=profile_id, extraction_id=outcome.extraction.id
    )
    return {
        "baseline_document_id": get_active_cv_document(migrated, profile_id).id,
        "document": document,
        "extraction": outcome.extraction,
        "replacement": replacement,
        "kept_fact_id": kept,
        "dropped_fact_id": dropped,
    }


def answer(connection, profile_id: int, replacement_id: int, *, retire_dropped=True):
    """Answer every plan entry: accept the incoming, retire the dropped skill."""
    plan = compute_replacement_plan(
        connection, profile_id=profile_id, replacement_id=replacement_id
    )
    for entry in plan.entries:
        if entry.role is DecisionRole.INCOMING:
            decision = ReviewDecision.ACCEPT
        elif (
            retire_dropped and entry.difference is DifferenceKind.ABSENT_FROM_NEW_CV
        ):
            decision = ReviewDecision.RETIRE
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
    return plan


def ready(connection, profile_id: int, replacement_id: int) -> str:
    replacement = mark_ready_to_activate(
        connection, profile_id=profile_id, replacement_id=replacement_id
    )
    return replacement.ready_review_digest


# ------------------------------------------------------------ nominal path


def test_a_reviewed_replacement_is_applied_whole(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)

    result = activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=token,
    )

    assert result.activated is True
    assert result.activation_revision == 1
    assert result.document_id == staged["document"].id
    assert result.previous_document_id == staged["baseline_document_id"]
    # The name and the new skill were accepted; the kept skill was an unchanged
    # reading, so the new document's evidence went onto the fact that held it.
    assert result.facts_accepted == 2
    assert result.evidence_attached == 1
    assert result.facts_retired == 1
    assert result.facts_rejected == 0
    assert result.facts_corrected == 0


def test_an_unchanged_reading_gains_evidence_and_creates_no_second_fact(
    migrated, profile_id, staged
):
    replacement_id = staged["replacement"].id
    before = len(list_profile_facts(migrated, profile_id))
    answer(migrated, profile_id, replacement_id)
    activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=ready(migrated, profile_id, replacement_id),
    )

    kept = get_profile_fact(migrated, profile_id, staged["kept_fact_id"])
    assert kept.is_current is True
    digests = {
        row.cv_sha256
        for row in list_profile_fact_provenance(
            migrated, profile_id, staged["kept_fact_id"]
        )
    }
    assert digests == {OLD_SHA256, NEW_SHA256}
    # Two facts created, for the name and the new skill. The kept reading did
    # not become a third.
    assert len(list_profile_facts(migrated, profile_id)) == before + 2
    values = {fact.value for fact in list_verified_profile_facts(migrated, profile_id)}
    assert values == {TEST_ONLY_NAME, KEPT_SKILL, NEW_SKILL}


def test_a_retired_fact_leaves_the_current_profile_and_stays_verified(
    migrated, profile_id, staged
):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=ready(migrated, profile_id, replacement_id),
    )

    dropped = get_profile_fact(migrated, profile_id, staged["dropped_fact_id"])
    assert dropped.status is FactStatus.ACCEPTED
    assert dropped.retired_at is not None
    assert dropped.is_current is False
    assert dropped.value == DROPPED_SKILL


def test_a_refused_reading_is_recorded_as_refused(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    for entry in plan.entries:
        if entry.role is DecisionRole.INCOMING:
            decision = (
                ReviewDecision.REJECT
                if entry.difference is DifferenceKind.NEW
                else ReviewDecision.ACCEPT
            )
        else:
            decision = ReviewDecision.KEEP
        record_review_decision(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            candidate_id=entry.candidate_id,
            fact_id=entry.fact_id,
            decision=decision,
        )
    result = activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=ready(migrated, profile_id, replacement_id),
    )
    assert result.facts_rejected == 2
    refused = [
        fact
        for fact in list_profile_facts(migrated, profile_id)
        if fact.status is FactStatus.REJECTED
    ]
    assert {fact.value for fact in refused} == {TEST_ONLY_NAME, NEW_SKILL}


def test_a_correction_is_applied_as_a_correction(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    for entry in plan.entries:
        if entry.role is not DecisionRole.INCOMING:
            record_review_decision(
                migrated,
                profile_id=profile_id,
                replacement_id=replacement_id,
                fact_id=entry.fact_id,
                decision=ReviewDecision.KEEP,
            )
            continue
        if entry.difference is DifferenceKind.NEW and entry.fact_type == "SKILL":
            record_review_decision(
                migrated,
                profile_id=profile_id,
                replacement_id=replacement_id,
                candidate_id=entry.candidate_id,
                decision=ReviewDecision.CORRECT,
                staged_value=CORRECTION,
            )
        else:
            record_review_decision(
                migrated,
                profile_id=profile_id,
                replacement_id=replacement_id,
                candidate_id=entry.candidate_id,
                decision=ReviewDecision.ACCEPT,
            )
    result = activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=ready(migrated, profile_id, replacement_id),
    )
    assert result.facts_corrected == 1
    corrected = [
        fact
        for fact in list_profile_facts(migrated, profile_id)
        if fact.status is FactStatus.CORRECTED
    ]
    assert len(corrected) == 1
    assert corrected[0].value == NEW_SKILL
    replacement_fact = get_profile_fact(
        migrated, profile_id, corrected[0].replaced_by_fact_id
    )
    assert replacement_fact.value == CORRECTION
    assert replacement_fact.is_current is True
    assert {
        row.source_type
        for row in list_profile_fact_provenance(
            migrated, profile_id, replacement_fact.id
        )
    } == {FactSourceType.USER_INPUT}


def test_the_documents_switch_together(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=ready(migrated, profile_id, replacement_id),
    )

    active = get_active_cv_document(migrated, profile_id)
    assert active.id == staged["document"].id
    previous = get_cv_document(
        migrated, profile_id=profile_id, document_id=staged["baseline_document_id"]
    )
    assert previous.lifecycle is DocumentLifecycle.HISTORICAL
    assert (
        migrated.execute(
            "SELECT COUNT(*) FROM profile_cv_documents WHERE lifecycle = 'ACTIVE'"
        ).fetchone()[0]
        == 1
    )


def test_the_attempt_is_activated_without_moving_its_stored_lifecycle(
    migrated, profile_id, staged
):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=ready(migrated, profile_id, replacement_id),
    )

    stored = get_replacement(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    assert stored.effective_state is EffectiveReplacementState.ACTIVATED
    assert stored.lifecycle.value == "READY_TO_ACTIVATE"
    assert stored.activation_revision == 1
    assert stored.activated_at is not None
    assert (
        migrated.execute(
            "SELECT closed_at FROM profile_cv_replacements WHERE id = ?",
            (replacement_id,),
        ).fetchone()[0]
        is None
    )


# ------------------------------------------------- the recommendation closes


def test_the_recommendation_is_published_incomplete(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=ready(migrated, profile_id, replacement_id),
    )

    state, current_run_id, issues = migrated.execute(
        """SELECT state, current_run_id, readiness_issues_json
             FROM recommendation_profile_state WHERE profile_id = ?""",
        (profile_id,),
    ).fetchone()
    assert state == "INCOMPLETE"
    assert current_run_id is None
    assert "PROFILE_CV_ACTIVATION_PENDING_SYNC" in issues


def test_no_stored_recommendation_run_is_deleted(migrated, profile_id, staged):
    """History stays readable; only the pointer that named one as current goes."""
    replacement_id = staged["replacement"].id
    before = migrated.execute(
        "SELECT COUNT(*) FROM recommendation_runs"
    ).fetchone()[0]
    answer(migrated, profile_id, replacement_id)
    activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=ready(migrated, profile_id, replacement_id),
    )
    assert (
        migrated.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0]
        == before
    )


# ------------------------------------------------------------ the refusals


def test_a_borrowed_transaction_is_refused(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(CvStagingError) as error:
            activate_cv_replacement(
                migrated,
                profile_id=profile_id,
                replacement_id=replacement_id,
                expected_review_digest=token,
            )
        assert "borrows none" in str(error.value)
    finally:
        migrated.execute("ROLLBACK")


@pytest.mark.parametrize("token", ["", "not-a-digest", "0" * 63, "zz" * 32])
def test_a_token_that_is_not_a_digest_is_refused(
    migrated, profile_id, staged, token
):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    ready(migrated, profile_id, replacement_id)
    before = snapshot(migrated)
    with pytest.raises(ReviewChangedError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
        )
    assert snapshot(migrated) == before


def test_another_reviews_token_is_refused(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    ready(migrated, profile_id, replacement_id)
    before = snapshot(migrated)
    with pytest.raises(ReviewChangedError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest="ab" * 32,
        )
    assert snapshot(migrated) == before


def test_a_changed_review_is_refused_before_anything_else(
    migrated, profile_id, staged
):
    """Answering again puts the attempt back under review, and an activation
    only ever applies a review someone declared ready."""
    replacement_id = staged["replacement"].id
    plan = answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)

    incoming = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        candidate_id=incoming.candidate_id,
        decision=ReviewDecision.REJECT,
    )
    before = snapshot(migrated)
    with pytest.raises(CvStagingError) as error:
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
        )
    assert "REVIEWING" in str(error.value)
    assert snapshot(migrated) == before


def test_the_token_of_a_superseded_review_is_refused(migrated, profile_id, staged):
    """The property the token exists for: the screen confirmed one review, the
    person changed an answer and declared it ready again, and the activation
    still carries the token of the review nobody confirmed twice."""
    replacement_id = staged["replacement"].id
    plan = answer(migrated, profile_id, replacement_id)
    stale_token = ready(migrated, profile_id, replacement_id)

    incoming = next(e for e in plan.entries if e.difference is DifferenceKind.NEW)
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        candidate_id=incoming.candidate_id,
        decision=ReviewDecision.REJECT,
    )
    fresh_token = ready(migrated, profile_id, replacement_id)
    assert fresh_token != stale_token

    before = snapshot(migrated)
    with pytest.raises(ReviewChangedError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=stale_token,
        )
    assert snapshot(migrated) == before

    # The review that *was* confirmed still applies.
    result = activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=fresh_token,
    )
    assert result.activated is True


def test_an_incomplete_review_is_refused(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    plan = compute_replacement_plan(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    first = plan.entries[0]
    record_review_decision(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        candidate_id=first.candidate_id,
        fact_id=first.fact_id,
        decision=ReviewDecision.ACCEPT
        if first.role is DecisionRole.INCOMING
        else ReviewDecision.KEEP,
    )
    before = snapshot(migrated)
    with pytest.raises(CvStagingError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest="ab" * 32,
        )
    assert snapshot(migrated) == before


def test_a_review_that_went_stale_is_refused(migrated, profile_id, staged):
    """A fact gained evidence no CV produced after the person answered."""
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=staged["dropped_fact_id"],
        provenance=ProvenanceInput(
            source_type=FactSourceType.USER_INPUT, source_locator="test-only-locator"
        ),
    )
    before = snapshot(migrated)
    with pytest.raises(CvStagingError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
        )
    assert snapshot(migrated) == before
    assert get_profile_fact(
        migrated, profile_id, staged["dropped_fact_id"]
    ).is_current is True


def test_a_changed_baseline_is_refused(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    # Somebody retired the active CV behind this attempt's back.
    migrated.execute(
        """UPDATE profile_cv_documents
              SET lifecycle = 'HISTORICAL', retired_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
        (staged["baseline_document_id"],),
    )
    migrated.commit()
    before = snapshot(migrated)
    with pytest.raises(ActiveCvChangedError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
        )
    assert snapshot(migrated) == before


def test_a_target_activated_behind_the_attempts_back_is_refused(
    migrated, profile_id, staged
):
    """Whoever switched the CV, the baseline this attempt was opened against is
    no longer the active one, and that is caught before anything is written."""
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    migrated.execute(
        """UPDATE profile_cv_documents
              SET lifecycle = 'HISTORICAL', retired_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
        (staged["baseline_document_id"],),
    )
    migrated.execute(
        """UPDATE profile_cv_documents
              SET lifecycle = 'ACTIVE', activated_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
        (staged["document"].id,),
    )
    migrated.commit()
    before = snapshot(migrated)
    with pytest.raises(ActiveCvChangedError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
        )
    assert snapshot(migrated) == before


def test_replacing_the_active_document_with_itself_is_refused(migrated, profile_id):
    """The one shape in which the target really is the active CV: an attempt
    whose reading campaign is over the document already in force."""
    declare_active_cv_document(
        migrated,
        profile_id=profile_id,
        content_sha256=OLD_SHA256,
        origin=DocumentOrigin.LEGACY_DECLARED,
    )
    accept_old_fact(migrated, profile_id, "SKILL", KEPT_SKILL)
    active = get_active_cv_document(migrated, profile_id)
    outcome = ensure_extraction_with_manifest(
        migrated,
        profile_id=profile_id,
        document_id=active.id,
        extraction=make_extraction(new_cv(OLD_SHA256), cv_sha256=OLD_SHA256),
    )
    replacement = open_replacement(
        migrated, profile_id=profile_id, extraction_id=outcome.extraction.id
    )
    answer(migrated, profile_id, replacement.id, retire_dropped=False)
    token = ready(migrated, profile_id, replacement.id)

    before = snapshot(migrated)
    with pytest.raises(AlreadyActiveDocumentError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement.id,
            expected_review_digest=token,
        )
    assert snapshot(migrated) == before


def test_a_historical_target_is_refused_by_name(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    migrated.execute(
        """UPDATE profile_cv_documents
              SET lifecycle = 'HISTORICAL',
                  activated_at = CURRENT_TIMESTAMP,
                  retired_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
        (staged["document"].id,),
    )
    migrated.commit()
    before = snapshot(migrated)
    with pytest.raises(HistoricalDocumentReactivationError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
        )
    assert snapshot(migrated) == before


def test_a_cancelled_attempt_is_refused(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    cancel_replacement(migrated, profile_id=profile_id, replacement_id=replacement_id)
    before = snapshot(migrated)
    with pytest.raises(CvStagingError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
        )
    assert snapshot(migrated) == before


# --------------------------------------------------- idempotence and failure


def test_replaying_an_activation_writes_nothing(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=token,
    )
    after_first = snapshot(migrated)

    again = activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=token,
    )

    assert again.activated is False
    assert again.activation_revision == 1
    assert snapshot(migrated) == after_first


def test_a_replay_does_not_undo_a_later_synchronization(migrated, profile_id, staged):
    """The state a sync published afterwards survives a repeated activation."""
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=token,
    )
    migrated.execute(
        "UPDATE recommendation_profile_state SET readiness_issues_json = ? "
        "WHERE profile_id = ?",
        ('[{"code":"MATCHING_NOT_READY","message":"TEST ONLY","opportunity_id":null}]',
         profile_id),
    )
    migrated.commit()

    activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=token,
    )
    issues = migrated.execute(
        "SELECT readiness_issues_json FROM recommendation_profile_state WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()[0]
    assert "MATCHING_NOT_READY" in issues


@pytest.mark.parametrize("seam", ["after_decisions", "after_documents"])
def test_a_failure_partway_undoes_everything(migrated, profile_id, staged, seam):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    before = snapshot(migrated)

    def explode():
        raise RuntimeError("TEST ONLY interruption")

    with pytest.raises(RuntimeError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
            **{seam: explode},
        )

    assert snapshot(migrated) == before
    assert get_active_cv_document(migrated, profile_id).id == staged[
        "baseline_document_id"
    ]
    assert get_replacement(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    ).is_activated is False


def test_a_concurrent_connection_cannot_interleave(
    tmp_path, migrated, profile_id, staged
):
    """The activation holds the write lock from its first read."""
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)

    other = connect_database(tmp_path / "activation.db")
    other.execute("PRAGMA busy_timeout = 100")
    attempts: list[str] = []

    def cancel_from_elsewhere():
        try:
            cancel_replacement(
                other, profile_id=profile_id, replacement_id=replacement_id
            )
            attempts.append("cancelled")
        except sqlite3.OperationalError as error:
            attempts.append(type(error).__name__)

    try:
        result = activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
            after_decisions=cancel_from_elsewhere,
        )
        assert attempts == ["OperationalError"]
        assert result.activated is True
    finally:
        other.close()


# ---------------------------------------------------------------- privacy


def test_no_summary_or_error_carries_a_reading(migrated, profile_id, staged):
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    result = activate_cv_replacement(
        migrated,
        profile_id=profile_id,
        replacement_id=replacement_id,
        expected_review_digest=token,
    )
    printed = repr(result.summary()) + repr(result.replacement.summary())
    for secret in (TEST_ONLY_NAME, KEPT_SKILL, NEW_SKILL, DROPPED_SKILL, CORRECTION):
        assert secret not in printed

    with pytest.raises(CvStagingError) as error:
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest="zz" * 32,
        )
    for secret in (TEST_ONLY_NAME, KEPT_SKILL, NEW_SKILL, DROPPED_SKILL):
        assert secret not in str(error.value)


# ---------------------------------------- one reading, two opposite answers


def answer_contradicting_the_kept_skill(
    connection, profile_id: int, replacement_id: int, *, retire_first: bool
):
    """Accept the kept skill from the new CV and retire the fact that holds it.

    `KEPT_SKILL` reaches the review twice — as a candidate of the new document
    and as the baseline fact carrying exactly that reading — so both entries are
    UNCHANGED_STILL_SUPPORTED and each answer is legal on its own. Recording
    them in either order produces a review that says the same claim is both
    confirmed and gone.
    """
    plan = compute_replacement_plan(
        connection, profile_id=profile_id, replacement_id=replacement_id
    )
    entries = list(plan.entries)
    kept = [
        entry
        for entry in entries
        if entry.difference is DifferenceKind.UNCHANGED_STILL_SUPPORTED
    ]
    assert len(kept) == 2, "the fixture is meant to put one reading on both sides"
    if retire_first:
        entries.sort(key=lambda entry: entry.role is DecisionRole.INCOMING)
    else:
        entries.sort(key=lambda entry: entry.role is DecisionRole.EXISTING)
    for entry in entries:
        if entry.role is DecisionRole.INCOMING:
            decision = ReviewDecision.ACCEPT
        elif entry.difference is DifferenceKind.UNCHANGED_STILL_SUPPORTED:
            decision = ReviewDecision.RETIRE
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
    return plan


@pytest.mark.parametrize("retire_first", [False, True], ids=["accept-first", "retire-first"])
def test_a_review_cannot_accept_and_retire_one_reading(
    migrated, profile_id, staged, retire_first
):
    """Neither order of recording produces a review anyone may act on.

    Each answer is legal when it is given, so nothing refuses them one at a
    time. The contradiction only exists across the whole review, which is where
    it is caught — and it is caught at the moment the review is declared ready,
    on the screen where both answers were given.
    """
    replacement_id = staged["replacement"].id
    answer_contradicting_the_kept_skill(
        migrated, profile_id, replacement_id, retire_first=retire_first
    )
    before = snapshot(migrated)

    with pytest.raises(ContradictoryReviewError) as error:
        mark_ready_to_activate(
            migrated, profile_id=profile_id, replacement_id=replacement_id
        )

    # Both targets are named, and neither answer is preferred over the other.
    message = str(error.value)
    assert str(staged["kept_fact_id"]) in message
    assert "accepted" in message and "retired" in message
    assert snapshot(migrated) == before
    replacement = get_replacement(
        migrated, profile_id=profile_id, replacement_id=replacement_id
    )
    assert replacement.lifecycle is not ReplacementLifecycle.READY_TO_ACTIVATE
    assert replacement.ready_review_digest is None


def test_a_contradictory_review_never_reaches_an_activation(
    migrated, profile_id, staged
):
    """And if one is put there anyway, the activation refuses it too.

    The review is declared ready while it is coherent, and the stored answer is
    then edited out of band — the one way a contradiction can sit behind a
    valid token. The activation asks the same shared validator, so it refuses
    before it writes anything, and no fact and no document moves.
    """
    replacement_id = staged["replacement"].id
    answer(migrated, profile_id, replacement_id)
    token = ready(migrated, profile_id, replacement_id)
    # The kept skill was answered KEEP. Flipping the stored row to RETIRE
    # leaves every per-entry rule satisfied: the contradiction is only visible
    # across the review as a whole.
    migrated.execute(
        """UPDATE profile_cv_replacement_decisions
              SET decision = 'RETIRE'
            WHERE replacement_id = ? AND profile_id = ? AND fact_id = ?""",
        (replacement_id, profile_id, staged["kept_fact_id"]),
    )
    migrated.commit()
    before = snapshot(migrated)

    with pytest.raises(ContradictoryReviewError):
        activate_cv_replacement(
            migrated,
            profile_id=profile_id,
            replacement_id=replacement_id,
            expected_review_digest=token,
        )

    assert migrated.in_transaction is False
    assert snapshot(migrated) == before
    # Nothing was accepted, nothing was retired, and the CV did not change.
    assert get_profile_fact(migrated, profile_id, staged["kept_fact_id"]).is_current
    assert get_active_cv_document(migrated, profile_id).id == staged[
        "baseline_document_id"
    ]
    assert get_cv_document(
        migrated, profile_id=profile_id, document_id=staged["document"].id
    ).lifecycle is DocumentLifecycle.KNOWN
