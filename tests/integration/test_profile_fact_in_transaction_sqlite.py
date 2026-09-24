"""The `*_in_transaction` fact primitives, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away; the
operational database is never opened. No real CV and no real personal data
takes part: every value is invented and marked TEST ONLY, every digest is
synthetic, and `.invalid` never resolves.

What these primitives exist for is one property: several writes that must land
or be undone **together**. An activation creates the facts a person accepted,
refuses the ones they refused, applies the corrections they typed and switches
the CV document — and a database where half of that happened would be a profile
whose facts and whose active CV disagree, with nothing to say which is right.

So each of them is checked twice over: that it does exactly what its public
sibling does, and that a failure anywhere in the batch leaves the database
exactly as it was.
"""

import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    ExtractionRule,
    candidate_fingerprint,
)
from services.digital_twin.cv.models import PARSER_VERSION, SectionType
from services.digital_twin.facts.models import (
    FactSourceType,
    FactStatus,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    AmbiguousFactEvidenceError,
    ConflictingFactEvidenceError,
    InvalidFactTransitionError,
    ProfileFactError,
    ProfileFactNotFoundError,
    RetiredFactNotMutableError,
    accept_profile_fact,
    correct_profile_fact,
    correct_profile_fact_in_transaction,
    decide_profile_facts,
    decide_profile_facts_in_transaction,
    ensure_profile_fact_proposal,
    ensure_profile_fact_proposal_in_transaction,
    ensure_profile_fact_provenance,
    ensure_profile_fact_provenance_in_transaction,
    get_profile_fact,
    list_profile_fact_provenance,
    list_profile_facts,
    reject_profile_fact,
    retire_profile_fact_in_transaction,
)
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
# Synthetic 64-hex digests; no real file was hashed to produce them.
TEST_ONLY_SHA256 = "ab" * 32
TEST_ONLY_OTHER_SHA256 = "cd" * 32

TEST_ONLY_SKILL = "TestOnlyToolkit"
TEST_ONLY_SECOND_SKILL = "SecondTestOnlyToolkit"
TEST_ONLY_CORRECTION = "CorrectedTestOnlyToolkit"


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "in-transaction.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


def cv_provenance(value: str, *, cv_sha256: str = TEST_ONLY_SHA256) -> ProvenanceInput:
    return ProvenanceInput(
        source_type=FactSourceType.CV,
        cv_sha256=cv_sha256,
        parser_version=PARSER_VERSION,
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        candidate_fingerprint=candidate_fingerprint(
            CandidateType.SKILL, value.casefold()
        ),
        rule_id=ExtractionRule.SECTION_LINE_BLOCK.value,
        page_numbers=(1,),
        section_type=SectionType.UNCLASSIFIED.value,
        section_index=0,
    )


def snapshot(connection) -> dict[str, list[tuple]]:
    """Everything a failed batch must leave exactly as it found it."""
    return {
        table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in ("profile_facts", "profile_fact_provenance")
    }


def proposed_fact(connection, profile_id: int, value: str) -> int:
    proposal = ensure_profile_fact_proposal(
        connection,
        profile_id=profile_id,
        fact_type="SKILL",
        value=value,
        provenance=cv_provenance(value),
    )
    return proposal.fact.id


def accepted_fact(connection, profile_id: int, value: str) -> int:
    fact_id = proposed_fact(connection, profile_id, value)
    accept_profile_fact(connection, profile_id, fact_id)
    return fact_id


def status_of(connection, fact_id: int) -> str:
    return connection.execute(
        "SELECT status FROM profile_facts WHERE id = ?", (fact_id,)
    ).fetchone()[0]


# ------------------------------------------ they refuse to run unsupervised


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda connection, profile_id: ensure_profile_fact_proposal_in_transaction(
                connection,
                profile_id=profile_id,
                fact_type="SKILL",
                value=TEST_ONLY_SKILL,
                provenance=cv_provenance(TEST_ONLY_SKILL),
            ),
            id="propose",
        ),
        pytest.param(
            lambda connection, profile_id: ensure_profile_fact_provenance_in_transaction(
                connection,
                profile_id=profile_id,
                fact_id=1,
                provenance=cv_provenance(TEST_ONLY_SKILL),
            ),
            id="attach-evidence",
        ),
        pytest.param(
            lambda connection, profile_id: decide_profile_facts_in_transaction(
                connection, profile_id, [1], FactStatus.ACCEPTED
            ),
            id="decide",
        ),
        pytest.param(
            lambda connection, profile_id: correct_profile_fact_in_transaction(
                connection, profile_id, 1, value=TEST_ONLY_CORRECTION
            ),
            id="correct",
        ),
    ],
)
def test_a_primitive_refuses_to_run_without_the_callers_transaction(
    migrated, profile_id, call
):
    with pytest.raises(ProfileFactError) as error:
        call(migrated, profile_id)
    assert "transaction" in str(error.value)
    assert migrated.in_transaction is False


def test_the_primitive_asks_for_a_transaction_even_for_an_empty_batch(
    migrated, profile_id
):
    """A caller composing an activation is doing transactional work whether or
    not this particular batch has members."""
    with pytest.raises(ProfileFactError) as error:
        decide_profile_facts_in_transaction(
            migrated, profile_id, [], FactStatus.ACCEPTED
        )
    assert "transaction" in str(error.value)

    migrated.execute("BEGIN IMMEDIATE")
    try:
        assert decide_profile_facts_in_transaction(
            migrated, profile_id, [], FactStatus.ACCEPTED
        ) == ()
    finally:
        migrated.execute("ROLLBACK")


def test_the_public_function_still_decides_an_empty_batch_without_a_transaction(
    migrated, profile_id
):
    """Historical behaviour, unchanged: nothing to decide, nothing opened."""
    assert decide_profile_facts(migrated, profile_id, [], FactStatus.ACCEPTED) == ()
    assert migrated.in_transaction is False


def test_the_public_function_still_refuses_an_absurd_target_on_an_empty_batch(
    migrated, profile_id
):
    """The shortcut is for a *valid* empty batch, not for an unchecked one.

    A caller asking a batch to CORRECT is making the same mistake whether or
    not the batch turned out to have members, and hearing about it only on the
    day it does would be the worst possible moment.
    """
    with pytest.raises(ProfileFactError) as error:
        decide_profile_facts(migrated, profile_id, [], FactStatus.CORRECTED)
    assert "acceptance or a rejection" in str(error.value)
    assert migrated.in_transaction is False


def test_the_public_function_still_refuses_a_repeated_id(migrated, profile_id):
    fact_id = proposed_fact(migrated, profile_id, TEST_ONLY_SKILL)
    with pytest.raises(ProfileFactError) as error:
        decide_profile_facts(
            migrated, profile_id, [fact_id, fact_id], FactStatus.ACCEPTED
        )
    assert "twice" in str(error.value)
    assert migrated.in_transaction is False
    assert status_of(migrated, fact_id) == "PROPOSED"


def test_the_public_function_decides_a_one_shot_iterable(migrated, profile_id):
    """The batch is materialized once, and it is that tuple that is decided.

    A generator consumed by the validation and then handed empty to the
    primitive would report success having decided nothing — the quietest
    possible way for a batch review to lose every answer in it.
    """
    first = proposed_fact(migrated, profile_id, TEST_ONLY_SKILL)
    second = proposed_fact(migrated, profile_id, TEST_ONLY_SECOND_SKILL)

    decided = decide_profile_facts(
        migrated, profile_id, (id_ for id_ in (first, second)), FactStatus.ACCEPTED
    )

    assert tuple(fact.id for fact in decided) == (first, second)
    assert status_of(migrated, first) == "ACCEPTED"
    assert status_of(migrated, second) == "ACCEPTED"


def test_a_one_shot_iterable_repeating_an_id_is_still_refused(migrated, profile_id):
    """The duplicate check reads the materialized batch, not the iterator."""
    fact_id = proposed_fact(migrated, profile_id, TEST_ONLY_SKILL)
    with pytest.raises(ProfileFactError) as error:
        decide_profile_facts(
            migrated,
            profile_id,
            (id_ for id_ in (fact_id, fact_id)),
            FactStatus.ACCEPTED,
        )
    assert "twice" in str(error.value)
    assert status_of(migrated, fact_id) == "PROPOSED"


# --------------------------------------------- they work in a borrowed one


def test_a_fact_can_be_proposed_and_accepted_in_one_transaction(migrated, profile_id):
    migrated.execute("BEGIN IMMEDIATE")
    try:
        proposal = ensure_profile_fact_proposal_in_transaction(
            migrated,
            profile_id=profile_id,
            fact_type="SKILL",
            value=TEST_ONLY_SKILL,
            provenance=cv_provenance(TEST_ONLY_SKILL),
        )
        assert proposal.created is True
        (accepted,) = decide_profile_facts_in_transaction(
            migrated, profile_id, [proposal.fact.id], FactStatus.ACCEPTED
        )
        assert accepted.status is FactStatus.ACCEPTED
        migrated.execute("COMMIT")
    except Exception:
        migrated.execute("ROLLBACK")
        raise

    stored = get_profile_fact(migrated, profile_id, proposal.fact.id)
    assert stored.is_current is True
    assert len(list_profile_fact_provenance(migrated, profile_id, stored.id)) == 1


def test_a_correction_applies_inside_a_borrowed_transaction(migrated, profile_id):
    fact_id = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        correction = correct_profile_fact_in_transaction(
            migrated, profile_id, fact_id, value=TEST_ONLY_CORRECTION
        )
        migrated.execute("COMMIT")
    except Exception:
        migrated.execute("ROLLBACK")
        raise

    assert correction.corrected.status is FactStatus.CORRECTED
    assert correction.corrected.replaced_by_fact_id == correction.replacement.id
    assert correction.replacement.status is FactStatus.ACCEPTED
    # The old value is never overwritten.
    assert correction.corrected.value == TEST_ONLY_SKILL
    sources = {
        row.source_type
        for row in list_profile_fact_provenance(
            migrated, profile_id, correction.replacement.id
        )
    }
    assert sources == {FactSourceType.USER_INPUT}


def test_evidence_attaches_inside_a_borrowed_transaction(migrated, profile_id):
    fact_id = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        attachment = ensure_profile_fact_provenance_in_transaction(
            migrated,
            profile_id=profile_id,
            fact_id=fact_id,
            provenance=cv_provenance(TEST_ONLY_SKILL, cv_sha256=TEST_ONLY_OTHER_SHA256),
        )
        assert attachment.created is True
        # Re-running inside the same transaction writes nothing more.
        again = ensure_profile_fact_provenance_in_transaction(
            migrated,
            profile_id=profile_id,
            fact_id=fact_id,
            provenance=cv_provenance(TEST_ONLY_SKILL, cv_sha256=TEST_ONLY_OTHER_SHA256),
        )
        assert again.created is False
        migrated.execute("COMMIT")
    except Exception:
        migrated.execute("ROLLBACK")
        raise
    assert len(list_profile_fact_provenance(migrated, profile_id, fact_id)) == 2


def test_the_whole_batch_is_undone_when_one_step_fails(migrated, profile_id):
    """The property the primitives exist for: all of it, or none of it."""
    kept = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    before = snapshot(migrated)

    migrated.execute("BEGIN IMMEDIATE")
    try:
        ensure_profile_fact_proposal_in_transaction(
            migrated,
            profile_id=profile_id,
            fact_type="SKILL",
            value=TEST_ONLY_SECOND_SKILL,
            provenance=cv_provenance(TEST_ONLY_SECOND_SKILL),
        )
        correct_profile_fact_in_transaction(
            migrated, profile_id, kept, value=TEST_ONLY_CORRECTION
        )
        # Injected failure, after several writes have already happened.
        raise RuntimeError("TEST ONLY interruption")
    except RuntimeError:
        migrated.execute("ROLLBACK")

    assert snapshot(migrated) == before
    assert get_profile_fact(migrated, profile_id, kept).status is FactStatus.ACCEPTED


def test_a_refusal_partway_undoes_what_came_before_it(migrated, profile_id):
    kept = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    refused = accepted_fact(migrated, profile_id, TEST_ONLY_SECOND_SKILL)
    reject_profile_fact(migrated, profile_id, refused)
    before = snapshot(migrated)

    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(InvalidFactTransitionError):
            # The first id is fine; the second is terminal, and the batch is
            # one act, so neither moves.
            decide_profile_facts_in_transaction(
                migrated, profile_id, [kept, refused], FactStatus.ACCEPTED
            )
        raise RuntimeError("TEST ONLY interruption")
    except RuntimeError:
        migrated.execute("ROLLBACK")

    assert snapshot(migrated) == before


# ----------------------------------- the business rules are the ones that were


def test_a_retired_fact_is_still_refused_by_every_primitive(migrated, profile_id):
    fact_id = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        retire_profile_fact_in_transaction(
            migrated,
            profile_id=profile_id,
            fact_id=fact_id,
            baseline_content_sha256=TEST_ONLY_SHA256,
        )
        migrated.execute("COMMIT")
    except Exception:
        migrated.execute("ROLLBACK")
        raise

    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RetiredFactNotMutableError):
            decide_profile_facts_in_transaction(
                migrated, profile_id, [fact_id], FactStatus.REJECTED
            )
        with pytest.raises(RetiredFactNotMutableError):
            correct_profile_fact_in_transaction(
                migrated, profile_id, fact_id, value=TEST_ONLY_CORRECTION
            )
        # Evidence may still be attached, and does not make it current again.
        ensure_profile_fact_provenance_in_transaction(
            migrated,
            profile_id=profile_id,
            fact_id=fact_id,
            provenance=cv_provenance(TEST_ONLY_SKILL, cv_sha256=TEST_ONLY_OTHER_SHA256),
        )
        migrated.execute("COMMIT")
    except Exception:
        migrated.execute("ROLLBACK")
        raise
    stored = get_profile_fact(migrated, profile_id, fact_id)
    assert stored.is_current is False
    assert stored.status is FactStatus.ACCEPTED


def test_a_terminal_fact_is_still_refused(migrated, profile_id):
    fact_id = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    reject_profile_fact(migrated, profile_id, fact_id)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(InvalidFactTransitionError):
            decide_profile_facts_in_transaction(
                migrated, profile_id, [fact_id], FactStatus.ACCEPTED
            )
        with pytest.raises(InvalidFactTransitionError):
            correct_profile_fact_in_transaction(
                migrated, profile_id, fact_id, value=TEST_ONLY_CORRECTION
            )
    finally:
        migrated.execute("ROLLBACK")


def test_a_retirement_is_not_a_batch_decision(migrated, profile_id):
    """`RETIRED` is not a status, and retirement has its own owner."""
    fact_id = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ProfileFactError):
            decide_profile_facts_in_transaction(
                migrated, profile_id, [fact_id], FactStatus.CORRECTED
            )
    finally:
        migrated.execute("ROLLBACK")


def test_a_repeated_id_is_still_a_caller_mistake(migrated, profile_id):
    fact_id = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ProfileFactError):
            decide_profile_facts_in_transaction(
                migrated, profile_id, [fact_id, fact_id], FactStatus.REJECTED
            )
    finally:
        migrated.execute("ROLLBACK")


def test_another_profiles_fact_is_still_out_of_reach(migrated, profile_id):
    other = ensure_user_profile(migrated, "other.student@example.invalid").profile_id
    fact_id = accepted_fact(migrated, other, TEST_ONLY_SKILL)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ProfileFactNotFoundError):
            decide_profile_facts_in_transaction(
                migrated, profile_id, [fact_id], FactStatus.REJECTED
            )
        with pytest.raises(ProfileFactNotFoundError):
            ensure_profile_fact_provenance_in_transaction(
                migrated,
                profile_id=profile_id,
                fact_id=fact_id,
                provenance=cv_provenance(TEST_ONLY_SKILL),
            )
    finally:
        migrated.execute("ROLLBACK")


def test_one_proof_cannot_be_moved_onto_another_fact(migrated, profile_id):
    first = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    second = accepted_fact(migrated, profile_id, TEST_ONLY_SECOND_SKILL)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ConflictingFactEvidenceError):
            ensure_profile_fact_provenance_in_transaction(
                migrated,
                profile_id=profile_id,
                fact_id=second,
                provenance=cv_provenance(TEST_ONLY_SKILL),
            )
    finally:
        migrated.execute("ROLLBACK")
    assert get_profile_fact(migrated, profile_id, first) is not None


def test_the_same_proof_returns_the_known_fact_without_creating_a_second(
    migrated, profile_id
):
    fact_id = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        proposal = ensure_profile_fact_proposal_in_transaction(
            migrated,
            profile_id=profile_id,
            fact_type="SKILL",
            value=TEST_ONLY_SKILL,
            provenance=cv_provenance(TEST_ONLY_SKILL),
        )
        migrated.execute("COMMIT")
    except Exception:
        migrated.execute("ROLLBACK")
        raise
    assert proposal.created is False
    assert proposal.fact.id == fact_id
    assert len(list_profile_facts(migrated, profile_id)) == 1


def test_a_proof_justifying_two_facts_is_reported_not_guessed(migrated, profile_id):
    """A database already inconsistent is said to be, never resolved by picking."""
    first = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    second = accepted_fact(migrated, profile_id, TEST_ONLY_SECOND_SKILL)
    forged = cv_provenance(TEST_ONLY_SKILL).resolved_provenance_key()
    migrated.execute(
        "UPDATE profile_fact_provenance SET provenance_key = ? WHERE fact_id = ?",
        (forged, second),
    )
    migrated.commit()

    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(AmbiguousFactEvidenceError):
            ensure_profile_fact_proposal_in_transaction(
                migrated,
                profile_id=profile_id,
                fact_type="SKILL",
                value=TEST_ONLY_SKILL,
                provenance=cv_provenance(TEST_ONLY_SKILL),
            )
    finally:
        migrated.execute("ROLLBACK")
    assert get_profile_fact(migrated, profile_id, first) is not None


# ------------------------------- the public functions still own what they own


def test_the_public_functions_still_open_and_commit_their_own_transaction(
    migrated, profile_id
):
    proposal = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type="SKILL",
        value=TEST_ONLY_SKILL,
        provenance=cv_provenance(TEST_ONLY_SKILL),
    )
    assert migrated.in_transaction is False
    (accepted,) = decide_profile_facts(
        migrated, profile_id, [proposal.fact.id], FactStatus.ACCEPTED
    )
    assert migrated.in_transaction is False
    attachment = ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=accepted.id,
        provenance=cv_provenance(TEST_ONLY_SKILL, cv_sha256=TEST_ONLY_OTHER_SHA256),
    )
    assert migrated.in_transaction is False
    correction = correct_profile_fact(
        migrated, profile_id, accepted.id, value=TEST_ONLY_CORRECTION
    )
    assert migrated.in_transaction is False

    assert proposal.created is True
    assert attachment.created is True
    assert correction.replacement.status is FactStatus.ACCEPTED
    # And a fresh connection sees it all, so it really was committed.
    assert (
        migrated.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0] == 2
    )


def test_a_public_function_still_rolls_its_own_failure_back(migrated, profile_id):
    fact_id = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    reject_profile_fact(migrated, profile_id, fact_id)
    before = snapshot(migrated)

    with pytest.raises(InvalidFactTransitionError):
        correct_profile_fact(
            migrated, profile_id, fact_id, value=TEST_ONLY_CORRECTION
        )

    assert migrated.in_transaction is False
    assert snapshot(migrated) == before


def test_a_public_function_called_inside_a_transaction_still_refuses(
    migrated, profile_id
):
    """Unchanged behaviour: it owns its transaction, so it cannot borrow one."""
    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            ensure_profile_fact_proposal(
                migrated,
                profile_id=profile_id,
                fact_type="SKILL",
                value=TEST_ONLY_SKILL,
                provenance=cv_provenance(TEST_ONLY_SKILL),
            )
    finally:
        if migrated.in_transaction:
            migrated.execute("ROLLBACK")


# ---------------------------------------------------------------- privacy


def test_no_error_message_carries_a_reading(migrated, profile_id):
    fact_id = accepted_fact(migrated, profile_id, TEST_ONLY_SKILL)
    reject_profile_fact(migrated, profile_id, fact_id)
    migrated.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(InvalidFactTransitionError) as error:
            correct_profile_fact_in_transaction(
                migrated, profile_id, fact_id, value=TEST_ONLY_CORRECTION
            )
        message = str(error.value)
        assert TEST_ONLY_CORRECTION not in message
        assert TEST_ONLY_SKILL not in message
    finally:
        migrated.execute("ROLLBACK")
