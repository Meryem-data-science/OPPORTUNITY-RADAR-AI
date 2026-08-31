"""The idempotent provenance primitive, on disposable SQLite databases.

`ensure_profile_fact_provenance` is the no-op form of
`add_profile_fact_provenance`: attaching the same proof to the same fact twice
must write nothing the second time rather than raising an `IntegrityError` a
caller would have to interpret. These tests pin that, the three states it
refuses instead of guessing at, and the two evidence-scoped readings a
reconciliation of two extraction campaigns is built on.

Every database here is created under `tmp_path` and thrown away; the real
`.data/` database is never opened. No real CV, no real address and no real
personal data takes part: every value is synthetic and marked TEST ONLY, and
`.invalid` never resolves.
"""

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.facts.models import (
    FactSourceType,
    FactStatus,
    ProfileFactType,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    AmbiguousFactEvidenceError,
    ConflictingFactEvidenceError,
    ProfileFactError,
    ProfileFactNotFoundError,
    accept_profile_fact,
    add_profile_fact_provenance,
    ensure_profile_fact_provenance,
    list_profile_fact_provenance,
    list_profile_facts_by_cv_evidence,
    list_profile_facts_by_evidence,
    propose_profile_fact,
)
from services.digital_twin.repository import ensure_user_profile

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
TEST_ONLY_OTHER_EMAIL = "other.student@example.invalid"
TEST_ONLY_VALUE = "student@example.invalid"
TEST_ONLY_OTHER_VALUE = "TestOnlyToolkit"
# Synthetic 64-hex digests; no real file was hashed to produce them.
TEST_ONLY_SHA256 = "ab" * 32
TEST_ONLY_OTHER_SHA256 = "cd" * 32

OLD_PARSER = "cv-parser-v1"
OLD_EXTRACTOR = "cv-candidates-v1"
NEW_PARSER = "cv-parser-v5"
NEW_EXTRACTOR = "cv-candidates-v6"


def cv_provenance(**overrides) -> ProvenanceInput:
    """Evidence shaped exactly like one Phase 3.2B `ExtractedCandidate`."""
    values = {
        "source_type": FactSourceType.CV,
        "cv_sha256": TEST_ONLY_SHA256,
        "parser_version": OLD_PARSER,
        "extractor_version": OLD_EXTRACTOR,
        "candidate_fingerprint": "0123456789abcdef",
        "rule_id": "EMAIL_PATTERN",
        "page_numbers": (1,),
        "section_type": "UNCLASSIFIED",
        "section_index": 0,
    }
    values.update(overrides)
    return ProvenanceInput(**values)


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "provenance.db")
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
def fact(migrated, profile_id):
    return propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=TEST_ONLY_VALUE,
        normalized_value=TEST_ONLY_VALUE,
        provenance=cv_provenance(),
    )


def test_a_new_proof_is_attached_and_reported_as_created(migrated, profile_id, fact):
    newer = cv_provenance(parser_version=NEW_PARSER, extractor_version=NEW_EXTRACTOR)

    attachment = ensure_profile_fact_provenance(
        migrated, profile_id=profile_id, fact_id=fact.id, provenance=newer
    )

    assert attachment.created is True
    assert attachment.provenance.fact_id == fact.id
    assert attachment.provenance.parser_version == NEW_PARSER
    recorded = list_profile_fact_provenance(migrated, profile_id, fact.id)
    assert len(recorded) == 2


def test_the_same_proof_twice_is_a_no_op(migrated, profile_id, fact):
    newer = cv_provenance(parser_version=NEW_PARSER, extractor_version=NEW_EXTRACTOR)
    first = ensure_profile_fact_provenance(
        migrated, profile_id=profile_id, fact_id=fact.id, provenance=newer
    )

    second = ensure_profile_fact_provenance(
        migrated, profile_id=profile_id, fact_id=fact.id, provenance=newer
    )

    assert second.created is False
    # The same row, not a second one that merely looks alike.
    assert second.provenance.id == first.provenance.id
    assert second.provenance.created_at == first.provenance.created_at
    assert len(list_profile_fact_provenance(migrated, profile_id, fact.id)) == 2


def test_the_proof_a_fact_was_created_with_is_already_present(
    migrated, profile_id, fact
):
    attachment = ensure_profile_fact_provenance(
        migrated, profile_id=profile_id, fact_id=fact.id, provenance=cv_provenance()
    )

    assert attachment.created is False
    assert len(list_profile_fact_provenance(migrated, profile_id, fact.id)) == 1


def test_attaching_evidence_decides_nothing(migrated, profile_id, fact):
    accept_profile_fact(migrated, profile_id, fact.id)
    newer = cv_provenance(parser_version=NEW_PARSER, extractor_version=NEW_EXTRACTOR)

    ensure_profile_fact_provenance(
        migrated, profile_id=profile_id, fact_id=fact.id, provenance=newer
    )

    still = list_profile_facts_by_evidence(
        migrated, profile_id, newer.resolved_provenance_key()
    )
    assert [entry.status for entry in still] == [FactStatus.ACCEPTED]
    assert still[0].value == TEST_ONLY_VALUE


def test_a_proof_already_justifying_another_fact_is_refused(
    migrated, profile_id, fact
):
    """The proof identifies one reading; it must not be moved onto another fact."""
    other = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.SKILL,
        value=TEST_ONLY_OTHER_VALUE,
        provenance=cv_provenance(provenance_key="TEST-ONLY-other-proof"),
    )
    contested = cv_provenance(provenance_key="TEST-ONLY-other-proof")

    with pytest.raises(ConflictingFactEvidenceError):
        ensure_profile_fact_provenance(
            migrated, profile_id=profile_id, fact_id=fact.id, provenance=contested
        )

    assert len(list_profile_fact_provenance(migrated, profile_id, fact.id)) == 1
    assert len(list_profile_fact_provenance(migrated, profile_id, other.id)) == 1


def test_a_proof_shared_by_two_facts_is_reported_as_ambiguous(
    migrated, profile_id, fact
):
    """`UNIQUE (fact_id, provenance_key)` cannot stop this; the code must."""
    other = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.SKILL,
        value=TEST_ONLY_OTHER_VALUE,
        provenance=cv_provenance(provenance_key="TEST-ONLY-shared"),
    )
    add_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=fact.id,
        provenance=cv_provenance(provenance_key="TEST-ONLY-shared"),
    )
    third = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.PHONE,
        value="+33 0 00 00 00 00",
        provenance=cv_provenance(provenance_key="TEST-ONLY-third"),
    )

    with pytest.raises(AmbiguousFactEvidenceError):
        ensure_profile_fact_provenance(
            migrated,
            profile_id=profile_id,
            fact_id=third.id,
            provenance=cv_provenance(provenance_key="TEST-ONLY-shared"),
        )

    assert len(list_profile_fact_provenance(migrated, profile_id, third.id)) == 1
    assert len(list_profile_fact_provenance(migrated, profile_id, other.id)) == 1


def test_a_fact_of_another_profile_is_reported_as_missing(
    migrated, profile_id, other_profile_id, fact
):
    with pytest.raises(ProfileFactNotFoundError):
        ensure_profile_fact_provenance(
            migrated,
            profile_id=other_profile_id,
            fact_id=fact.id,
            provenance=cv_provenance(
                parser_version=NEW_PARSER, extractor_version=NEW_EXTRACTOR
            ),
        )

    assert len(list_profile_fact_provenance(migrated, profile_id, fact.id)) == 1


def test_a_failure_after_the_lookup_writes_nothing(migrated, profile_id, fact):
    """The whole call is one transaction, so an interruption leaves no row."""
    newer = cv_provenance(parser_version=NEW_PARSER, extractor_version=NEW_EXTRACTOR)

    def explode() -> None:
        raise RuntimeError("TEST ONLY interruption")

    with pytest.raises(RuntimeError):
        ensure_profile_fact_provenance(
            migrated,
            profile_id=profile_id,
            fact_id=fact.id,
            provenance=newer,
            after_lookup=explode,
        )

    assert len(list_profile_fact_provenance(migrated, profile_id, fact.id)) == 1
    assert (
        list_profile_facts_by_evidence(
            migrated, profile_id, newer.resolved_provenance_key()
        )
        == ()
    )
    # The connection is usable afterwards: the transaction was rolled back, not
    # left open.
    ensure_profile_fact_provenance(
        migrated, profile_id=profile_id, fact_id=fact.id, provenance=newer
    )


def test_something_other_than_a_provenance_input_is_refused(
    migrated, profile_id, fact
):
    with pytest.raises(ProfileFactError):
        ensure_profile_fact_provenance(
            migrated,
            profile_id=profile_id,
            fact_id=fact.id,
            provenance={"source_type": "CV"},
        )


def test_facts_are_read_back_by_the_campaign_that_produced_them(
    migrated, profile_id, fact
):
    newer = cv_provenance(parser_version=NEW_PARSER, extractor_version=NEW_EXTRACTOR)
    ensure_profile_fact_provenance(
        migrated, profile_id=profile_id, fact_id=fact.id, provenance=newer
    )

    old_campaign = list_profile_facts_by_cv_evidence(
        migrated,
        profile_id,
        cv_sha256=TEST_ONLY_SHA256,
        parser_version=OLD_PARSER,
        extractor_version=OLD_EXTRACTOR,
    )
    new_campaign = list_profile_facts_by_cv_evidence(
        migrated,
        profile_id,
        cv_sha256=TEST_ONLY_SHA256,
        parser_version=NEW_PARSER,
        extractor_version=NEW_EXTRACTOR,
    )

    # One fact, two campaigns: it belongs to both, and to neither twice.
    assert [entry.id for entry in old_campaign] == [fact.id]
    assert [entry.id for entry in new_campaign] == [fact.id]


def test_another_document_is_another_campaign(migrated, profile_id, fact):
    assert (
        list_profile_facts_by_cv_evidence(
            migrated,
            profile_id,
            cv_sha256=TEST_ONLY_OTHER_SHA256,
            parser_version=OLD_PARSER,
            extractor_version=OLD_EXTRACTOR,
        )
        == ()
    )


def test_a_campaign_reading_never_crosses_profiles(
    migrated, profile_id, other_profile_id, fact
):
    assert (
        list_profile_facts_by_cv_evidence(
            migrated,
            other_profile_id,
            cv_sha256=TEST_ONLY_SHA256,
            parser_version=OLD_PARSER,
            extractor_version=OLD_EXTRACTOR,
        )
        == ()
    )
    assert (
        list_profile_facts_by_evidence(
            migrated, other_profile_id, cv_provenance().resolved_provenance_key()
        )
        == ()
    )


def test_a_persons_own_correction_is_not_read_as_a_document_reading(
    migrated, profile_id
):
    """`source_type = 'CV'` is in the statement, so `USER_INPUT` stays invisible."""
    typed = propose_profile_fact(
        migrated,
        profile_id=profile_id,
        fact_type=ProfileFactType.EMAIL,
        value=TEST_ONLY_VALUE,
        provenance=ProvenanceInput(
            source_type=FactSourceType.USER_INPUT,
            cv_sha256=TEST_ONLY_SHA256,
            parser_version=OLD_PARSER,
            extractor_version=OLD_EXTRACTOR,
        ),
    )

    read_back = list_profile_facts_by_cv_evidence(
        migrated,
        profile_id,
        cv_sha256=TEST_ONLY_SHA256,
        parser_version=OLD_PARSER,
        extractor_version=OLD_EXTRACTOR,
    )

    assert typed.id not in [entry.id for entry in read_back]
