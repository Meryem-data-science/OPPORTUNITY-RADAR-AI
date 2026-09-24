"""Profile revisions, watermarks and what may be called current.

Every database here is created under `tmp_path` and thrown away; the operational
database is never opened. No real CV takes part: each activation below is a real
`activate_cv_replacement` over a synthetic one-candidate document marked TEST
ONLY, so what is measured is the behaviour of the shipped code rather than of a
hand-written row.

The property under test is one sentence: a result is current only if its phase
and every phase it is derived from have caught up with the active revision, and
a watermark is only ever written by an owner that has just recomputed and
published. The tests come in four groups — the protocol itself, the phase owners
advancing it, the readers deriving staleness from it, and the races.
"""

from datetime import date

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import (
    MATCHING_SELECTION_VERSION,
    list_matching_runs,
    read_current_matching,
    sync_matching,
)
from services.collector.matching.persistence import set_matching_state_empty
from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    ExtractedCandidate,
    ExtractionRule,
    StructuredCvExtraction,
    candidate_fingerprint,
)
from services.digital_twin.cv.models import PARSER_VERSION, SectionType
from services.digital_twin.cv.replacement.activation import activate_cv_replacement
from services.digital_twin.cv.replacement.models import (
    DecisionRole,
    DocumentOrigin,
    ReviewDecision,
)
from services.digital_twin.cv.replacement.planning import compute_replacement_plan
from services.digital_twin.cv.replacement.repository import (
    declare_active_cv_document,
    ensure_cv_document,
    ensure_extraction_with_manifest,
    open_replacement,
)
from services.digital_twin.cv.replacement.staging import (
    mark_ready_to_activate,
    record_review_decision,
)
from services.digital_twin.skills.repository import synchronize_profile_skills
from services.digital_twin.structured_profile.repository import (
    synchronize_structured_profile_entries,
)
from services.eligibility.service import synchronize_eligibility
from services.portfolio import read_current_portfolio, sync_portfolio
from services.portfolio.read_model import PortfolioProfileReadStatus
from services.priority.input_assembly import PriorityReadinessStatus
from services.priority import (
    PriorityProfileReadStatus,
    list_priority_runs,
    read_current_priority,
    sync_priority,
)
from services.profile_revision.watermark import (
    DIRECT_DEPENDENCIES,
    DependencyNotSyncedError,
    ProfileRevisionError,
    SyncPhase,
    WatermarkRegressionError,
    active_profile_revision,
    advance_sync_watermark_if_unchanged,
    advance_sync_watermark_in_transaction,
    phase_is_current,
    read_sync_watermark,
    read_sync_watermarks,
    required_phases,
    stale_phases,
)
from tests.integration.test_priority_persistence_sqlite import priority_fixture

DAY = date(2026, 9, 2)

# TEST ONLY values; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "revision-owner@example.invalid"
BASELINE_SHA256 = "a1" * 32
SECOND_SHA256 = "b2" * 32
THIRD_SHA256 = "c3" * 32
TEST_ONLY_SKILL = "FirstTestOnlyToolkit"
TEST_ONLY_SECOND_SKILL = "SecondTestOnlyToolkit"


# --------------------------------------------------------------- scaffolding


def candidate(value: str, *, cv_sha256: str) -> ExtractedCandidate:
    return ExtractedCandidate(
        candidate_type=CandidateType.SKILL,
        raw_text=value,
        normalized_value=None,
        page_numbers=(1,),
        section_type=SectionType.UNCLASSIFIED,
        section_index=0,
        rule_id=ExtractionRule.SECTION_LINE_BLOCK,
        fingerprint=candidate_fingerprint(CandidateType.SKILL, value.casefold()),
        cv_sha256=cv_sha256,
        parser_version=PARSER_VERSION,
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
    )


def activate_a_replacement(connection, profile_id: int, *, sha256: str, skill: str):
    """Run one real CV activation over a synthetic one-skill document.

    Nothing is hand-written: the document, the manifest, the review and the
    activation all go through the shipped owners, so the revision this produces
    is the revision the product produces.
    """
    if connection.execute(
        "SELECT COUNT(*) FROM profile_cv_documents WHERE profile_id = ?", (profile_id,)
    ).fetchone()[0] == 0:
        declare_active_cv_document(
            connection,
            profile_id=profile_id,
            content_sha256=BASELINE_SHA256,
            origin=DocumentOrigin.LEGACY_DECLARED,
        )
    document = ensure_cv_document(
        connection,
        profile_id=profile_id,
        content_sha256=sha256,
        byte_size=2048,
        page_count=1,
    )
    outcome = ensure_extraction_with_manifest(
        connection,
        profile_id=profile_id,
        document_id=document.id,
        extraction=StructuredCvExtraction(
            extractor_version=CANDIDATE_EXTRACTOR_VERSION,
            parser_version=PARSER_VERSION,
            cv_sha256=sha256,
            candidates=(candidate(skill, cv_sha256=sha256),),
            warnings=(),
        ),
    )
    replacement = open_replacement(
        connection, profile_id=profile_id, extraction_id=outcome.extraction.id
    )
    plan = compute_replacement_plan(
        connection, profile_id=profile_id, replacement_id=replacement.id
    )
    for entry in plan.entries:
        record_review_decision(
            connection,
            profile_id=profile_id,
            replacement_id=replacement.id,
            candidate_id=entry.candidate_id,
            fact_id=entry.fact_id,
            decision=(
                ReviewDecision.ACCEPT
                if entry.role is DecisionRole.INCOMING
                else ReviewDecision.KEEP
            ),
        )
    ready = mark_ready_to_activate(
        connection, profile_id=profile_id, replacement_id=replacement.id
    )
    return activate_cv_replacement(
        connection,
        profile_id=profile_id,
        replacement_id=replacement.id,
        expected_review_digest=ready.ready_review_digest,
    )


def sync_upstream_projections(connection, profile_id: int) -> None:
    """The two phases a CV activation is directly upstream of."""
    synchronize_profile_skills(connection, profile_id)
    synchronize_structured_profile_entries(connection, profile_id)


@pytest.fixture
def plain(tmp_path):
    """A migrated database with one profile and nothing else."""
    from services.digital_twin.repository import ensure_user_profile

    connection = connect_database(tmp_path / "revision.db")
    apply_migrations(connection)
    identity = ensure_user_profile(connection, TEST_ONLY_EMAIL)
    yield connection, identity.profile_id
    connection.close()


# ------------------------------------------------------ the protocol itself


def test_the_phases_are_exactly_the_ones_migration_0028_allows(plain):
    connection, _ = plain
    allowed = {
        row[0]
        for row in connection.execute(
            """SELECT phase FROM (
                   SELECT 'SKILLS' AS phase UNION ALL SELECT 'STRUCTURED_PROFILE'
                   UNION ALL SELECT 'ELIGIBILITY' UNION ALL SELECT 'MATCHING'
                   UNION ALL SELECT 'RECOMMENDATION' UNION ALL SELECT 'PRIORITY'
                   UNION ALL SELECT 'PORTFOLIO')"""
        )
    }
    assert {phase.value for phase in SyncPhase} == allowed
    # And the database agrees: every name inserts, and nothing else does.
    for phase in SyncPhase:
        connection.execute(
            """INSERT INTO profile_downstream_sync_watermark
                   (profile_id, phase, synced_revision) VALUES (1, ?, 0)""",
            (phase.value,),
        )
    connection.rollback()


def test_a_profile_without_an_activation_is_at_revision_zero(plain):
    connection, profile_id = plain
    assert active_profile_revision(connection, profile_id) == 0
    assert dict(read_sync_watermarks(connection, profile_id)) == {
        phase: 0 for phase in SyncPhase
    }
    for phase in SyncPhase:
        assert read_sync_watermark(connection, profile_id, phase) == 0
        assert phase_is_current(connection, profile_id, phase) is True
        assert stale_phases(connection, profile_id, phase) == ()


def test_dependencies_are_closed_over_transitively():
    assert required_phases(SyncPhase.SKILLS) == ()
    assert required_phases(SyncPhase.STRUCTURED_PROFILE) == ()
    assert required_phases(SyncPhase.MATCHING) == (
        SyncPhase.SKILLS,
        SyncPhase.STRUCTURED_PROFILE,
    )
    # Portfolio is built from Priority, which is built from Matching: skills
    # that still describe the previous CV make the Portfolio stale too.
    assert required_phases(SyncPhase.PORTFOLIO) == (
        SyncPhase.ELIGIBILITY,
        SyncPhase.MATCHING,
        SyncPhase.PRIORITY,
        SyncPhase.SKILLS,
        SyncPhase.STRUCTURED_PROFILE,
    )
    assert SyncPhase.ELIGIBILITY in required_phases(SyncPhase.RECOMMENDATION)
    # Priority does not only need the Matching snapshot: it reads the stored
    # eligibility decision of every posting and carries its status and its
    # `input_fingerprint` into `PriorityEligibilitySnapshot`, so a decision left
    # at an earlier revision would be scored as though it still described the
    # person. The closure therefore reaches the two upstream projections too.
    assert required_phases(SyncPhase.PRIORITY) == (
        SyncPhase.ELIGIBILITY,
        SyncPhase.MATCHING,
        SyncPhase.SKILLS,
        SyncPhase.STRUCTURED_PROFILE,
    )
    assert SyncPhase.ELIGIBILITY in required_phases(SyncPhase.PORTFOLIO)
    # No phase depends on itself, directly or through a cycle.
    for phase in SyncPhase:
        assert phase not in required_phases(phase)
    assert set(DIRECT_DEPENDENCIES) == set(SyncPhase)


def test_advancing_refuses_to_run_outside_the_callers_transaction(plain):
    connection, profile_id = plain
    with pytest.raises(ProfileRevisionError) as error:
        advance_sync_watermark_in_transaction(
            connection, profile_id=profile_id, phase=SyncPhase.SKILLS
        )
    assert "transaction" in str(error.value)


def test_advancing_at_revision_zero_writes_nothing_at_all(plain):
    connection, profile_id = plain
    connection.execute("BEGIN IMMEDIATE")
    try:
        assert (
            advance_sync_watermark_in_transaction(
                connection, profile_id=profile_id, phase=SyncPhase.SKILLS
            )
            == 0
        )
    finally:
        connection.execute("COMMIT")
    assert connection.execute(
        "SELECT COUNT(*) FROM profile_downstream_sync_watermark"
    ).fetchone()[0] == 0


def test_a_watermark_cannot_regress(plain):
    connection, profile_id = plain
    connection.execute(
        """INSERT INTO profile_downstream_sync_watermark
               (profile_id, phase, synced_revision) VALUES (?, 'SKILLS', 5)""",
        (profile_id,),
    )
    connection.commit()
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    assert active_profile_revision(connection, profile_id) == 1
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(WatermarkRegressionError):
            advance_sync_watermark_in_transaction(
                connection, profile_id=profile_id, phase=SyncPhase.SKILLS
            )
    finally:
        connection.execute("ROLLBACK")


def test_advancing_refuses_while_a_dependency_is_behind(plain):
    connection, profile_id = plain
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(DependencyNotSyncedError) as error:
            advance_sync_watermark_in_transaction(
                connection, profile_id=profile_id, phase=SyncPhase.MATCHING
            )
        assert "SKILLS" in str(error.value)
        assert connection.execute(
            "SELECT COUNT(*) FROM profile_downstream_sync_watermark WHERE phase='MATCHING'"
        ).fetchone()[0] == 0
    finally:
        connection.execute("ROLLBACK")


# ------------------------------------------------- an activation invalidates


def test_an_activation_leaves_every_phase_behind(plain):
    connection, profile_id = plain
    result = activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    assert result.activated is True
    assert active_profile_revision(connection, profile_id) == 1
    # The activation itself advances nothing: that is the mechanism.
    assert dict(read_sync_watermarks(connection, profile_id)) == {
        phase: 0 for phase in SyncPhase
    }
    for phase in SyncPhase:
        assert phase_is_current(connection, profile_id, phase) is False


def test_a_second_activation_invalidates_what_the_first_one_repaired(plain):
    connection, profile_id = plain
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)
    assert read_sync_watermark(connection, profile_id, SyncPhase.SKILLS) == 1
    assert phase_is_current(connection, profile_id, SyncPhase.SKILLS) is True

    activate_a_replacement(
        connection, profile_id, sha256=THIRD_SHA256, skill=TEST_ONLY_SECOND_SKILL
    )
    assert active_profile_revision(connection, profile_id) == 2
    # The watermark did not move, so the same number now means "behind".
    assert read_sync_watermark(connection, profile_id, SyncPhase.SKILLS) == 1
    assert phase_is_current(connection, profile_id, SyncPhase.SKILLS) is False
    assert stale_phases(connection, profile_id, SyncPhase.SKILLS) == (SyncPhase.SKILLS,)

    sync_upstream_projections(connection, profile_id)
    assert read_sync_watermark(connection, profile_id, SyncPhase.SKILLS) == 2
    assert phase_is_current(connection, profile_id, SyncPhase.SKILLS) is True


# ------------------------------------------------- the owners advance it


def test_the_upstream_projections_advance_their_own_phase_only(plain):
    connection, profile_id = plain
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    synchronize_profile_skills(connection, profile_id)
    watermarks = read_sync_watermarks(connection, profile_id)
    assert watermarks[SyncPhase.SKILLS] == 1
    assert watermarks[SyncPhase.STRUCTURED_PROFILE] == 0
    assert watermarks[SyncPhase.MATCHING] == 0


def test_an_empty_matching_state_is_a_published_result_too(plain):
    """Reached through `sync_matching`, which is what decided it was empty."""
    connection, profile_id = plain
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)

    result = sync_matching(connection, profile_id)

    assert result.state == "EMPTY"
    assert read_sync_watermark(connection, profile_id, SyncPhase.MATCHING) == 1
    assert read_current_matching(connection, profile_id).status == "EMPTY"


def test_a_direct_persistence_call_cannot_claim_the_current_revision(plain):
    """`set_matching_state_empty` publishes, and certifies nothing.

    It receives a decision taken outside its transaction, so it is in no
    position to say that decision describes the profile as it is now. The state
    row is written — its persistence contract is unchanged — and the phase stays
    behind, which is what keeps the reader answering NOT_SYNCED.
    """
    connection, profile_id = plain
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)

    set_matching_state_empty(
        connection, profile_id, selection_version=MATCHING_SELECTION_VERSION
    )

    assert connection.execute(
        "SELECT state FROM matching_profile_state WHERE profile_id=?", (profile_id,)
    ).fetchone()[0] == "EMPTY"
    assert read_sync_watermark(connection, profile_id, SyncPhase.MATCHING) == 0
    assert phase_is_current(connection, profile_id, SyncPhase.MATCHING) is False
    assert read_current_matching(connection, profile_id).status == "NOT_SYNCED"


def test_a_precomputed_batch_cannot_win_the_watermark_after_an_activation(tmp_path):
    """The direct-store counterpart, with a real batch.

    `store_matching_batch` is the historical entry point and still stores. What
    it may not do is let a batch computed before an activation certify the
    revision that activation created — so the phase stays behind until
    `sync_matching` has actually recomputed it.
    """
    from tests.integration.test_recommendation_persistence import (
        SELECTION_VERSION,
        add_opportunity,
        make_matching_batch,
    )
    from services.collector.matching.persistence import store_matching_batch
    from services.digital_twin.repository import ensure_user_profile

    connection = connect_database(tmp_path / "precomputed.db")
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
    ids = tuple(add_opportunity(connection, suffix) for suffix in ("a", "b", "c"))
    connection.commit()
    # Computed now, at revision 0.
    batch = make_matching_batch(profile_id, ids)

    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)

    stored = store_matching_batch(
        connection, profile_id, batch, selection_version=SELECTION_VERSION
    )

    assert stored.run_id is not None  # persistence contract unchanged
    assert read_sync_watermark(connection, profile_id, SyncPhase.MATCHING) == 0
    assert read_current_matching(connection, profile_id).status == "NOT_SYNCED"
    connection.close()


def test_a_failed_publication_rolls_the_watermark_back_with_it(plain):
    connection, profile_id = plain
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)
    before = read_sync_watermark(connection, profile_id, SyncPhase.SKILLS)

    class Injected(RuntimeError):
        pass

    with pytest.raises(Injected):
        synchronize_profile_skills(
            connection, profile_id, after_read=lambda: (_ for _ in ()).throw(Injected())
        )
    assert connection.in_transaction is False
    assert read_sync_watermark(connection, profile_id, SyncPhase.SKILLS) == before


def test_a_watermark_is_never_written_by_a_freshness_check_alone(plain):
    connection, profile_id = plain
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    for _ in range(3):
        for phase in SyncPhase:
            phase_is_current(connection, profile_id, phase)
            stale_phases(connection, profile_id, phase)
            read_sync_watermark(connection, profile_id, phase)
    assert connection.execute(
        "SELECT COUNT(*) FROM profile_downstream_sync_watermark"
    ).fetchone()[0] == 0


# ---------------------------------------------- eligibility, one per posting


def test_eligibility_refuses_to_run_while_its_dependencies_are_behind(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path)
    activate_a_replacement(
        connection, identity.profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    with pytest.raises(Exception) as error:
        synchronize_eligibility(connection, identity.user_id, identity.profile_id)
    assert "SKILLS" in str(error.value) or "STRUCTURED_PROFILE" in str(error.value)
    assert read_sync_watermark(
        connection, identity.profile_id, SyncPhase.ELIGIBILITY
    ) == 0


def test_an_activation_during_the_loop_leaves_eligibility_behind(tmp_path):
    """The corpus then describes two different people, so no claim is made."""
    connection, identity, _, _ = priority_fixture(tmp_path)
    activate_a_replacement(
        connection, identity.profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, identity.profile_id)
    assert advance_sync_watermark_if_unchanged(
        connection,
        profile_id=identity.profile_id,
        phase=SyncPhase.ELIGIBILITY,
        expected_revision=1,
    ) is True
    assert read_sync_watermark(
        connection, identity.profile_id, SyncPhase.ELIGIBILITY
    ) == 1

    # A run that started at revision 1 while the profile has moved to 2.
    activate_a_replacement(
        connection,
        identity.profile_id,
        sha256=THIRD_SHA256,
        skill=TEST_ONLY_SECOND_SKILL,
    )
    assert advance_sync_watermark_if_unchanged(
        connection,
        profile_id=identity.profile_id,
        phase=SyncPhase.ELIGIBILITY,
        expected_revision=1,
    ) is False
    assert read_sync_watermark(
        connection, identity.profile_id, SyncPhase.ELIGIBILITY
    ) == 1


def test_the_final_eligibility_claim_owns_its_transaction(plain):
    connection, profile_id = plain
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ProfileRevisionError):
            advance_sync_watermark_if_unchanged(
                connection,
                profile_id=profile_id,
                phase=SyncPhase.ELIGIBILITY,
                expected_revision=1,
            )
    finally:
        connection.execute("ROLLBACK")


# ------------------------------------------------------- the readers gate


def test_matching_priority_and_portfolio_stop_being_current(tmp_path):
    connection, identity, _, arguments = priority_fixture(tmp_path)
    profile_id = identity.profile_id
    sync_priority(connection, profile_id, DAY)
    sync_portfolio(connection, profile_id)
    assert read_current_matching(connection, profile_id).status == "READY"
    assert read_current_priority(connection, profile_id).status is (
        PriorityProfileReadStatus.READY
    )
    assert read_current_portfolio(connection, profile_id).status is (
        PortfolioProfileReadStatus.READY
    )

    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )

    matching = read_current_matching(connection, profile_id)
    priority = read_current_priority(connection, profile_id)
    portfolio = read_current_portfolio(connection, profile_id)
    assert matching.status == "NOT_SYNCED"
    assert priority.status is PriorityProfileReadStatus.NOT_SYNCED
    assert portfolio.status is PortfolioProfileReadStatus.NOT_SYNCED
    for view in (matching, priority, portfolio):
        assert view.current_run_id is None
        assert view.current_run is None
        # The real history is kept: this is a derived answer, not a deletion.
        assert view.history_count >= 1


def test_the_stored_rows_and_runs_are_left_exactly_as_they_were(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path)
    profile_id = identity.profile_id
    sync_priority(connection, profile_id, DAY)
    sync_portfolio(connection, profile_id)
    tables = (
        "matching_runs",
        "matching_profile_state",
        "priority_runs",
        "priority_profile_state",
        "portfolio_runs",
        "portfolio_profile_state",
    )
    before = {
        table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in tables
    }

    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    read_current_matching(connection, profile_id)
    read_current_priority(connection, profile_id)
    read_current_portfolio(connection, profile_id)

    after = {
        table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in tables
    }
    assert after == before


def test_no_historical_run_is_marked_current_once_the_revision_moved(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path)
    profile_id = identity.profile_id
    sync_priority(connection, profile_id, DAY)
    assert any(run.is_current for run in list_matching_runs(connection, profile_id))
    assert any(run.is_current for run in list_priority_runs(connection, profile_id))

    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )

    matching_runs = list_matching_runs(connection, profile_id)
    priority_runs = list_priority_runs(connection, profile_id)
    # Still listed, every one of them, and none of them current.
    assert matching_runs and priority_runs
    assert not any(run.is_current for run in matching_runs)
    assert not any(run.is_current for run in priority_runs)


def test_a_dependency_still_behind_keeps_the_dependent_phase_stale(tmp_path):
    connection, identity, _, _ = priority_fixture(tmp_path)
    profile_id = identity.profile_id
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)
    sync_matching(connection, profile_id)
    assert read_sync_watermark(connection, profile_id, SyncPhase.MATCHING) == 1
    assert phase_is_current(connection, profile_id, SyncPhase.MATCHING) is True
    assert read_current_matching(connection, profile_id).status != "NOT_SYNCED"
    # Priority has not been recomputed, so it is still not presentable.
    assert read_current_priority(connection, profile_id).status is (
        PriorityProfileReadStatus.NOT_SYNCED
    )
    # And it cannot be, until the eligibility decisions it scores catch up too.
    advance_sync_watermark_if_unchanged(
        connection,
        profile_id=profile_id,
        phase=SyncPhase.ELIGIBILITY,
        expected_revision=1,
    )
    sync_priority(connection, profile_id, DAY)
    assert read_current_priority(connection, profile_id).status is (
        PriorityProfileReadStatus.READY
    )
    # And the Portfolio, built on Priority, is the last one to catch up.
    assert read_current_portfolio(connection, profile_id).status is (
        PortfolioProfileReadStatus.NOT_SYNCED
    )
    sync_portfolio(connection, profile_id)
    assert read_current_portfolio(connection, profile_id).status is (
        PortfolioProfileReadStatus.READY
    )


def test_priority_refuses_to_publish_on_a_matching_that_is_behind(tmp_path):
    """Through Priority's own readiness contract, not through a second rule.

    Priority assembles its inputs from `read_current_matching`. That reader now
    reports NOT_SYNCED while Matching is behind, so the assembly is INCOMPLETE
    and the existing owner rolls back and publishes nothing — no new refusal had
    to be invented for it, and the watermark it never earned is never written.
    """
    connection, identity, _, _ = priority_fixture(tmp_path)
    profile_id = identity.profile_id
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)
    before = connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0]

    result = sync_priority(connection, profile_id, DAY)

    assert result.status is PriorityReadinessStatus.INCOMPLETE
    assert result.persisted is False
    assert result.run_id is None
    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT COUNT(*) FROM priority_runs"
    ).fetchone()[0] == before
    assert read_sync_watermark(connection, profile_id, SyncPhase.PRIORITY) == 0




# ------------------------------------------------------- the recommendation


def test_a_stored_ready_recommendation_that_an_activation_overtook(tmp_path):
    """The defence-in-depth branch, in the vocabulary the reader already has.

    An activation publishes INCOMPLETE itself, so a READY row that is behind
    means some path wrote one without going through the persistence owner. The
    reader answers the same thing the activation would have — INCOMPLETE, with
    the pending-synchronization issue — and leaves the stored row untouched for
    the audit to see.
    """
    from tests.integration.test_recommendation_persistence import (
        Fixture,
        SELECTION_VERSION,
        add_opportunity,
        make_matching_batch,
    )
    from services.collector.matching.persistence import store_matching_batch
    from services.digital_twin.repository import ensure_user_profile
    from services.recommendation.read_model import (
        list_recommendation_runs,
        read_current_recommendation,
    )

    connection = connect_database(tmp_path / "recommendation-revision.db")
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
    ids = tuple(add_opportunity(connection, suffix) for suffix in ("a", "b", "c"))
    connection.commit()
    matching = store_matching_batch(
        connection,
        profile_id,
        make_matching_batch(profile_id, ids),
        selection_version=SELECTION_VERSION,
    )
    fixture = Fixture(connection, profile_id, ids, matching)
    stored = fixture.store()
    assert read_current_recommendation(connection, profile_id).status == "READY"

    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    # Put the READY row back, as a path bypassing the owner would leave it.
    connection.execute(
        """UPDATE recommendation_profile_state
              SET state='READY', current_run_id=?, readiness_issues_json='[]'
            WHERE profile_id=?""",
        (stored.run_id, profile_id),
    )
    connection.commit()

    view = read_current_recommendation(connection, profile_id)
    assert view.status == "INCOMPLETE"
    assert view.current_run_id is None
    assert view.current_run is None
    assert view.history_count == 1
    assert [issue.code.value for issue in view.readiness_issues] == [
        "PROFILE_CV_ACTIVATION_PENDING_SYNC"
    ]
    # The history is still listed, and none of it is current.
    runs = list_recommendation_runs(connection, profile_id)
    assert len(runs) == 1 and runs[0].is_current is False
    # And the stored row was not rewritten by the read.
    assert connection.execute(
        "SELECT state, current_run_id FROM recommendation_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone() == ("READY", stored.run_id)
    connection.close()


# -------------------------------------------------------------- the races


def test_an_activation_cannot_interleave_with_a_publication(tmp_path):
    """The publication holds the write lock the activation also needs.

    Both own `BEGIN IMMEDIATE`, so there is no instant between a phase reading
    the revision and writing its watermark in which an activation can commit.
    The probe below runs from a second connection, inside the first one's
    transaction, and is refused.
    """
    import sqlite3

    from services.digital_twin.repository import ensure_user_profile

    path = tmp_path / "race.db"
    connection = connect_database(path)
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )

    other = connect_database(path)
    other.execute("PRAGMA busy_timeout=200")
    refusals: list[Exception] = []

    def interleave() -> None:
        try:
            activate_a_replacement(
                other, profile_id, sha256=THIRD_SHA256, skill=TEST_ONLY_SECOND_SKILL
            )
        except sqlite3.OperationalError as error:  # pragma: no branch
            refusals.append(error)

    synchronize_profile_skills(connection, profile_id, after_read=interleave)

    assert refusals, "a second connection wrote while the publication was open"
    assert "locked" in str(refusals[0]).lower() or "busy" in str(refusals[0]).lower()
    # The publication landed, for the revision that was live throughout.
    assert active_profile_revision(connection, profile_id) == 1
    assert read_sync_watermark(connection, profile_id, SyncPhase.SKILLS) == 1
    other.close()
    connection.close()


def test_an_activation_committed_first_leaves_the_next_publication_behind(tmp_path):
    """The other order: the activation wins, and the phase must catch up again."""
    connection, identity, _, _ = priority_fixture(tmp_path)
    profile_id = identity.profile_id
    sync_upstream_projections(connection, profile_id)
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    # Skills were synchronized for revision 0 and the profile is now at 1.
    assert read_sync_watermark(connection, profile_id, SyncPhase.SKILLS) == 0
    assert phase_is_current(connection, profile_id, SyncPhase.SKILLS) is False
    synchronize_profile_skills(connection, profile_id)
    assert read_sync_watermark(connection, profile_id, SyncPhase.SKILLS) == 1


def test_priority_cannot_be_published_while_eligibility_is_behind(tmp_path):
    """Matching at N is not enough: Priority also scores stored decisions.

    `assemble_priority_inputs` reads `read_eligibility` for every posting and
    carries its status and `input_fingerprint` into the assessment, so a corpus
    left at N-1 would be scored as though it still described the person. With
    Matching current and Eligibility behind, the publication must fail whole.

    Eligibility is advanced here through the very primitive
    `synchronize_eligibility` calls at the end of its loop: this fixture has no
    Phase 3.5 requirement corpus, so the full run cannot execute against it, and
    what is under test is the watermark rule rather than the eligibility engine.
    """
    connection, identity, _, _ = priority_fixture(tmp_path)
    profile_id = identity.profile_id
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)
    sync_matching(connection, profile_id)

    watermarks = read_sync_watermarks(connection, profile_id)
    assert watermarks[SyncPhase.MATCHING] == 1
    assert watermarks[SyncPhase.ELIGIBILITY] == 0
    before = connection.execute("SELECT COUNT(*) FROM priority_runs").fetchone()[0]

    with pytest.raises(DependencyNotSyncedError) as error:
        sync_priority(connection, profile_id, DAY)
    assert "ELIGIBILITY" in str(error.value)

    # Nothing published, nothing claimed, nothing left open.
    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT COUNT(*) FROM priority_runs"
    ).fetchone()[0] == before
    assert read_sync_watermark(connection, profile_id, SyncPhase.PRIORITY) == 0
    assert phase_is_current(connection, profile_id, SyncPhase.PRIORITY) is False
    assert read_current_priority(connection, profile_id).status is (
        PriorityProfileReadStatus.NOT_SYNCED
    )

    # Eligibility catches up, and the same call now goes through.
    assert advance_sync_watermark_if_unchanged(
        connection,
        profile_id=profile_id,
        phase=SyncPhase.ELIGIBILITY,
        expected_revision=1,
    ) is True

    result = sync_priority(connection, profile_id, DAY)

    assert result.persisted is True
    assert read_sync_watermark(connection, profile_id, SyncPhase.PRIORITY) == 1
    assert phase_is_current(connection, profile_id, SyncPhase.PRIORITY) is True
    assert read_current_priority(connection, profile_id).status is (
        PriorityProfileReadStatus.READY
    )


def test_a_matching_batch_computed_before_an_activation_never_becomes_current(
    tmp_path,
):
    """The race the persistence split exists to close.

    a) the batch is computed while the profile is at revision N;
    b) a CV activation takes the profile to N+1 before it is published;
    c) skills and the structured profile catch up with N+1;
    d) the old batch must not become the current Matching of N+1, and must not
       advance the watermark to it.

    Step (c) is what makes this worth a test of its own: with the upstream
    projections at N+1, a watermark written at persist time would have found
    every dependency satisfied and certified a snapshot computed for N.
    """
    from tests.integration.test_recommendation_persistence import (
        SELECTION_VERSION,
        add_opportunity,
        make_matching_batch,
    )
    from services.collector.matching.persistence import store_matching_batch
    from services.digital_twin.repository import ensure_user_profile

    connection = connect_database(tmp_path / "matching-race.db")
    apply_migrations(connection)
    profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
    ids = tuple(add_opportunity(connection, suffix) for suffix in ("a", "b", "c"))
    connection.commit()

    # (a) computed at revision 0.
    batch = make_matching_batch(profile_id, ids)
    # (b) and (c).
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)
    assert read_sync_watermark(connection, profile_id, SyncPhase.SKILLS) == 1
    assert read_sync_watermark(
        connection, profile_id, SyncPhase.STRUCTURED_PROFILE
    ) == 1

    stale_run = store_matching_batch(
        connection, profile_id, batch, selection_version=SELECTION_VERSION
    )

    # (d) it is stored history, and it is not the current Matching.
    assert read_sync_watermark(connection, profile_id, SyncPhase.MATCHING) == 0
    assert read_current_matching(connection, profile_id).status == "NOT_SYNCED"
    assert not any(
        run.is_current for run in list_matching_runs(connection, profile_id)
    )

    # Only a real recomputation earns the revision, and it produces its own run.
    sync_matching(connection, profile_id)
    current = read_current_matching(connection, profile_id)
    assert read_sync_watermark(connection, profile_id, SyncPhase.MATCHING) == 1
    assert current.status in ("READY", "EMPTY")
    assert current.current_run_id != stale_run.run_id
    connection.close()


def test_sync_matching_owns_its_transaction_and_borrows_none(plain):
    connection, profile_id = plain
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(Exception) as error:
            sync_matching(connection, profile_id)
        assert "transaction" in str(error.value)
    finally:
        connection.execute("ROLLBACK")


def test_a_dry_run_matching_writes_nothing_and_claims_nothing(plain):
    connection, profile_id = plain
    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )
    sync_upstream_projections(connection, profile_id)
    before = connection.execute(
        "SELECT COUNT(*) FROM profile_downstream_sync_watermark"
    ).fetchone()[0]

    result = sync_matching(connection, profile_id, persist=False)

    assert result.persisted is False
    assert connection.in_transaction is False
    assert read_sync_watermark(connection, profile_id, SyncPhase.MATCHING) == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM profile_downstream_sync_watermark"
    ).fetchone()[0] == before


# ------------------------------------------------ portfolio on stale priority


def test_portfolio_reports_a_stale_priority_instead_of_asserting(tmp_path):
    """A stale dependency is a business answer, never an AssertionError.

    The stored Priority run is intact, so its audit still passes; the reader
    refuses to present it because the revision moved. Portfolio must read that
    derived state, not the audit, and come out INCOMPLETE.
    """
    from services.portfolio.input_assembly import (
        PortfolioAssemblyIssueCode,
        PortfolioAssemblyStatus,
        assemble_portfolio_inputs,
    )

    connection, identity, _, _ = priority_fixture(tmp_path)
    profile_id = identity.profile_id
    sync_priority(connection, profile_id, DAY)
    sync_portfolio(connection, profile_id)
    runs_before = connection.execute(
        "SELECT COUNT(*) FROM portfolio_runs WHERE profile_id=?", (profile_id,)
    ).fetchone()[0]

    activate_a_replacement(
        connection, profile_id, sha256=SECOND_SHA256, skill=TEST_ONLY_SKILL
    )

    assembly = assemble_portfolio_inputs(connection, profile_id)
    assert assembly.status is PortfolioAssemblyStatus.INCOMPLETE
    assert [issue.code for issue in assembly.issues] == [
        PortfolioAssemblyIssueCode.PRIORITY_NOT_SYNCED
    ]

    result = sync_portfolio(connection, profile_id)

    assert result.status is PortfolioAssemblyStatus.INCOMPLETE
    assert result.persisted is False
    assert result.run_id is None
    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT COUNT(*) FROM portfolio_runs WHERE profile_id=?", (profile_id,)
    ).fetchone()[0] == runs_before
    assert read_sync_watermark(connection, profile_id, SyncPhase.PORTFOLIO) == 0
    assert read_current_portfolio(connection, profile_id).status is (
        PortfolioProfileReadStatus.NOT_SYNCED
    )


def test_an_unreadable_priority_state_is_reported_not_raised(tmp_path):
    """The other way to have no current run, and it must not become a crash.

    Priority runs with no state row cannot be described, so
    `read_current_priority` refuses. That is corruption rather than staleness;
    Portfolio must still come out INCOMPLETE, and the audit is what names it.
    """
    from services.portfolio.input_assembly import (
        PortfolioAssemblyIssueCode,
        PortfolioAssemblyStatus,
        assemble_portfolio_inputs,
    )

    connection, identity, _, _ = priority_fixture(tmp_path)
    profile_id = identity.profile_id
    sync_priority(connection, profile_id, DAY)
    connection.execute(
        "DELETE FROM priority_profile_state WHERE profile_id=?", (profile_id,)
    )
    connection.commit()

    assembly = assemble_portfolio_inputs(connection, profile_id)

    assert assembly.status is PortfolioAssemblyStatus.INCOMPLETE
    assert [issue.code for issue in assembly.issues] == [
        PortfolioAssemblyIssueCode.PRIORITY_AUDIT_CORRUPT
    ]
