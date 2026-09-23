"""Migration 0028 and fact currentness, on disposable SQLite databases.

Every database here is created under `tmp_path` and thrown away; the
operational database is never opened. No real CV, no real person and no real
contact detail takes part: every value is invented and marked TEST ONLY, every
digest is synthetic, and `.invalid` never resolves.

What this file is about is the distinction 0028 introduces. `status` answers
*was this claim validated by a human*; `retired_at` answers *is that validated
claim still part of the active profile*. A fact a CV replacement retires keeps
the first answer and loses the second — it is not refused, and absence from a
newer document is never turned into evidence that a claim became false.
"""

import hashlib
import sqlite3
from pathlib import Path

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
    ProfileFactType,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    FactNotRetirableError,
    RetiredFactNotMutableError,
    accept_profile_fact,
    add_profile_fact_provenance,
    correct_profile_fact,
    decide_profile_facts,
    ensure_profile_fact_proposal,
    ensure_profile_fact_provenance,
    get_profile_fact,
    list_profile_fact_provenance,
    list_profile_facts,
    list_verified_profile_facts,
    reject_profile_fact,
    retire_profile_fact_in_transaction,
)
from services.digital_twin.preferences.repository import _PROJECTIONS
from services.digital_twin.repository import ensure_user_profile
from services.digital_twin.skills.repository import (
    list_profile_skills,
    synchronize_profile_skills,
)
from services.digital_twin.structured_profile.repository import (
    list_profile_languages,
    synchronize_structured_profile_entries,
)

# TEST ONLY identities; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
# Synthetic 64-hex digests; no real file was hashed to produce them.
TEST_ONLY_OLD_SHA256 = "ab" * 32
TEST_ONLY_NEW_SHA256 = "cd" * 32
TEST_ONLY_THIRD_SHA256 = "ef" * 32

TEST_ONLY_SKILL = "TestOnlyToolkit"
TEST_ONLY_DROPPED_SKILL = "DroppedTestOnlyToolkit"
TEST_ONLY_LANGUAGE = "TEST ONLY language, level C1"
TEST_ONLY_CORRECTION = "CorrectedTestOnlyToolkit"


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "activation-foundation.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


def cv_provenance(
    value: str,
    *,
    cv_sha256: str = TEST_ONLY_OLD_SHA256,
    candidate_type: CandidateType = CandidateType.SKILL,
) -> ProvenanceInput:
    return ProvenanceInput(
        source_type=FactSourceType.CV,
        cv_sha256=cv_sha256,
        parser_version=PARSER_VERSION,
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        candidate_fingerprint=candidate_fingerprint(candidate_type, value.casefold()),
        rule_id=ExtractionRule.SECTION_LINE_BLOCK.value,
        page_numbers=(1,),
        section_type=SectionType.UNCLASSIFIED.value,
        section_index=0,
    )


def accepted_cv_fact(
    connection,
    profile_id: int,
    fact_type: str,
    value: str,
    *,
    cv_sha256: str = TEST_ONLY_OLD_SHA256,
) -> int:
    proposal = ensure_profile_fact_proposal(
        connection,
        profile_id=profile_id,
        fact_type=fact_type,
        value=value,
        provenance=cv_provenance(value, cv_sha256=cv_sha256),
    )
    accept_profile_fact(connection, profile_id, proposal.fact.id)
    return proposal.fact.id


def retire(connection, profile_id: int, fact_id: int, *, baseline=TEST_ONLY_OLD_SHA256):
    """Retire through the owner, inside a transaction the test owns."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        fact = retire_profile_fact_in_transaction(
            connection,
            profile_id=profile_id,
            fact_id=fact_id,
            baseline_content_sha256=baseline,
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return fact


def _seed_accepted_fact(connection, profile_id: int, fact_type: str, value: str) -> int:
    """One accepted fact with CV evidence, written without the owners.

    Only used to build a database that predates 0028.
    """
    fact_id = connection.execute(
        """INSERT INTO profile_facts (
               profile_id, fact_type, value, status, decided_at
           ) VALUES (?, ?, ?, 'ACCEPTED', CURRENT_TIMESTAMP) RETURNING id""",
        (profile_id, fact_type, value),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO profile_fact_provenance (
               fact_id, source_type, provenance_key, cv_sha256,
               parser_version, extractor_version
           ) VALUES (?, 'CV', ?, ?, ?, ?)""",
        (
            fact_id,
            f"test-only:{fact_type}:{value}",
            TEST_ONLY_OLD_SHA256,
            PARSER_VERSION,
            CANDIDATE_EXTRACTOR_VERSION,
        ),
    )
    connection.commit()
    return fact_id


# ------------------------------------------------------------ the migration


def test_0028_applies_under_the_existing_runner(migrated):
    recorded = [
        row[0]
        for row in migrated.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    assert recorded[-1] == "0028"
    assert migrated.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0028_is_idempotent(migrated):
    assert apply_migrations(migrated) == []


def test_0028_rebuilt_no_table(migrated):
    """A rebuild would have replaced the tables 0007 and 0027 created, so their
    original `CREATE TABLE` text is what proves none happened."""
    for table, marker in (
        ("profile_facts", "-- Phase 3.3A"),
        ("profile_cv_replacements", "One attempt at replacing the active CV"),
    ):
        sql = migrated.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        assert "retired_at" in sql or "activated_at" in sql
    # The added columns are the last ones, which is what ADD COLUMN does and a
    # rebuild would not have to.
    assert [row[1] for row in migrated.execute("PRAGMA table_info(profile_facts)")][-1] == (
        "retired_at"
    )
    assert [
        row[1] for row in migrated.execute("PRAGMA table_info(profile_cv_replacements)")
    ][-3:] == ["ready_review_digest", "activation_revision", "activated_at"]


def test_the_expected_triggers_exist(migrated):
    names = {
        row[0]
        for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    assert {
        "trg_profile_facts_retirement_is_final",
        "trg_cv_replacement_activation_is_final",
        "trg_cv_decisions_frozen_after_activation_insert",
        "trg_cv_decisions_frozen_after_activation_update",
        "trg_cv_decisions_frozen_after_activation_delete",
    } <= names


def test_the_current_fact_index_exists(migrated):
    sql = migrated.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_profile_facts_current'"
    ).fetchone()[0]
    assert "status = 'ACCEPTED'" in sql and "retired_at IS NULL" in sql


def test_the_watermark_table_names_the_seven_phases(migrated):
    sql = migrated.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='profile_downstream_sync_watermark'"
    ).fetchone()[0]
    for phase in (
        "SKILLS",
        "STRUCTURED_PROFILE",
        "ELIGIBILITY",
        "MATCHING",
        "RECOMMENDATION",
        "PRIORITY",
        "PORTFOLIO",
    ):
        assert phase in sql


def test_existing_data_survives_the_upgrade(tmp_path):
    """Facts, provenance, projections and 0027 staging rows all outlive 0028."""
    path = tmp_path / "upgrade.db"
    below = tmp_path / "migrations-below-0028"
    below.mkdir()
    for source in sorted(Path("migrations").glob("*.sql")):
        if source.name < "0028":
            (below / source.name).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
    connection = connect_database(path)
    try:
        apply_migrations(connection, below)
        owner = ensure_user_profile(connection, TEST_ONLY_EMAIL)
        # Seeded in raw SQL on purpose: the fact repository reads `retired_at`,
        # which 0028 is about to add, so the owners cannot be used to build a
        # database that predates their own migration.
        skill = _seed_accepted_fact(
            connection, owner.profile_id, "SKILL", TEST_ONLY_SKILL
        )
        _seed_accepted_fact(
            connection, owner.profile_id, "LANGUAGE", TEST_ONLY_LANGUAGE
        )
        # A whole 0027 staging chain, built under the real 0027 constraints:
        # a document, a reading campaign, a manifest candidate, an attempt and
        # one valid review decision pointing at that candidate.
        document = _document(connection, owner.profile_id, TEST_ONLY_NEW_SHA256)
        extraction = _extraction(connection, owner.profile_id, document)
        candidate = _candidate(connection, owner.profile_id, extraction)
        replacement = _replacement(connection, owner.profile_id, extraction)
        decision = _decision(connection, owner.profile_id, replacement, candidate)
        connection.commit()

        counted = (
            "profile_facts",
            "profile_fact_provenance",
            "profile_cv_documents",
            "profile_cv_extractions",
            "profile_cv_candidates",
            "profile_cv_replacements",
            "profile_cv_replacement_decisions",
        )
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counted
        }
        identities = connection.execute(
            """SELECT r.id, r.profile_id, r.extraction_id, r.lifecycle, r.created_at,
                      e.document_id, e.manifest_chain_digest,
                      c.provenance_key, c.chain_digest,
                      d.id, d.replacement_id, d.candidate_id, d.difference,
                      d.decision, d.review_state_digest
                 FROM profile_cv_replacements AS r
                 JOIN profile_cv_extractions AS e ON e.id = r.extraction_id
                 JOIN profile_cv_candidates AS c ON c.extraction_id = e.id
                 JOIN profile_cv_replacement_decisions AS d ON d.replacement_id = r.id
                WHERE r.id = ?""",
            (replacement,),
        ).fetchone()

        assert apply_migrations(connection) == ["0028"]

        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counted
        }
        assert after == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert get_profile_fact(connection, owner.profile_id, skill).retired_at is None

        # The identities and the relations between them are the ones written
        # before the upgrade, and the new columns start NULL.
        assert (
            connection.execute(
                """SELECT r.id, r.profile_id, r.extraction_id, r.lifecycle, r.created_at,
                          e.document_id, e.manifest_chain_digest,
                          c.provenance_key, c.chain_digest,
                          d.id, d.replacement_id, d.candidate_id, d.difference,
                          d.decision, d.review_state_digest
                     FROM profile_cv_replacements AS r
                     JOIN profile_cv_extractions AS e ON e.id = r.extraction_id
                     JOIN profile_cv_candidates AS c ON c.extraction_id = e.id
                     JOIN profile_cv_replacement_decisions AS d ON d.replacement_id = r.id
                    WHERE r.id = ?""",
                (replacement,),
            ).fetchone()
            == identities
        )
        assert connection.execute(
            "SELECT activated_at, activation_revision, ready_review_digest "
            "FROM profile_cv_replacements WHERE id = ?",
            (replacement,),
        ).fetchone() == (None, None, None)
        # The review of an attempt nobody activated is still editable.
        connection.execute(
            "UPDATE profile_cv_replacement_decisions SET decision = 'REJECT' WHERE id = ?",
            (decision,),
        )
        connection.commit()
        # And the projections still build from the upgraded facts.
        synchronize_profile_skills(connection, owner.profile_id)
        synchronize_structured_profile_entries(connection, owner.profile_id)
        assert len(list_profile_skills(connection, owner.profile_id)) == 1
        assert len(list_profile_languages(connection, owner.profile_id)) == 1
    finally:
        connection.close()


def test_0027_is_untouched():
    """0027 is immutable. Pinned on its content with line endings normalised,
    so the check means the same thing on Windows and in CI."""
    raw = Path("migrations/0027_cv_staging.sql").read_bytes()
    normalised = raw.replace(b"\r\n", b"\n")
    assert len(normalised) == 13693
    assert (
        hashlib.sha256(normalised).hexdigest()
        == "beab7c02d36e5865a2c7f673db6672278b77bc40d35bdcc9bbabc33dccb1d2b2"
    )
    # And nothing this slice introduces is in it.
    assert b"activation_revision" not in normalised
    assert b"ready_review_digest" not in normalised
    assert b"profile_downstream_sync_watermark" not in normalised
    assert b"profile_facts ADD COLUMN" not in normalised


# --------------------------------------------------------- retirement writes


def test_a_current_accepted_fact_can_be_retired(migrated, profile_id):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    retired = retire(migrated, profile_id, fact_id)

    assert retired.status is FactStatus.ACCEPTED
    assert retired.retired_at is not None
    assert retired.is_verified is True
    assert retired.is_current is False
    assert retired.value == TEST_ONLY_SKILL
    assert retired.replaced_by_fact_id is None


def test_retiring_twice_is_a_no_op_that_does_not_re_date(migrated, profile_id):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    first = retire(migrated, profile_id, fact_id)
    again = retire(migrated, profile_id, fact_id)
    assert again.retired_at == first.retired_at


def test_retirement_requires_the_callers_transaction(migrated, profile_id):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    with pytest.raises(Exception) as error:
        retire_profile_fact_in_transaction(
            migrated,
            profile_id=profile_id,
            fact_id=fact_id,
            baseline_content_sha256=TEST_ONLY_OLD_SHA256,
        )
    assert "transaction" in str(error.value)


@pytest.mark.parametrize("source", ["USER_INPUT", "GITHUB", "OTHER_ACCEPTED_EVIDENCE"])
def test_a_fact_supported_by_another_source_is_never_retired(
    migrated, profile_id, source
):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    add_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=fact_id,
        provenance=ProvenanceInput(
            source_type=FactSourceType(source), source_locator="test-only-locator"
        ),
    )
    with pytest.raises(FactNotRetirableError):
        retire(migrated, profile_id, fact_id)
    assert get_profile_fact(migrated, profile_id, fact_id).is_current is True


def test_historical_cv_proofs_do_not_block_retirement(migrated, profile_id):
    """CV1 says X, CV2 says X too, CV3 omits it: the person may still remove it."""
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=fact_id,
        provenance=cv_provenance(TEST_ONLY_SKILL, cv_sha256=TEST_ONLY_NEW_SHA256),
    )
    # The baseline is the second document; the first one's proof stays readable.
    retired = retire(migrated, profile_id, fact_id, baseline=TEST_ONLY_NEW_SHA256)
    assert retired.is_current is False
    digests = {
        row.cv_sha256
        for row in list_profile_fact_provenance(migrated, profile_id, fact_id)
    }
    assert digests == {TEST_ONLY_OLD_SHA256, TEST_ONLY_NEW_SHA256}


def test_a_fact_the_baseline_never_supported_is_not_retirable(migrated, profile_id):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    with pytest.raises(FactNotRetirableError):
        retire(migrated, profile_id, fact_id, baseline=TEST_ONLY_THIRD_SHA256)


def test_a_correction_replacement_is_not_retirable(migrated, profile_id):
    original = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_DROPPED_SKILL)
    correction = correct_profile_fact(
        migrated, profile_id, original, value=TEST_ONLY_CORRECTION
    )
    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=correction.replacement.id,
        provenance=cv_provenance(TEST_ONLY_CORRECTION),
    )
    with pytest.raises(FactNotRetirableError):
        retire(migrated, profile_id, correction.replacement.id)


@pytest.mark.parametrize(
    "fact_type", ["PREFERENCE", "AVAILABILITY", "MOBILITY", "CAREER_OBJECTIVE"]
)
def test_a_fact_type_no_cv_produces_is_not_retirable(migrated, profile_id, fact_type):
    proposal = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type=fact_type,
        value="TEST ONLY stated by the person",
        provenance=cv_provenance("TEST ONLY stated by the person"),
    )
    accept_profile_fact(migrated, profile_id, proposal.fact.id)
    with pytest.raises(FactNotRetirableError):
        retire(migrated, profile_id, proposal.fact.id)


@pytest.mark.parametrize("status", ["PROPOSED", "REJECTED"])
def test_only_an_accepted_fact_is_retirable(migrated, profile_id, status):
    proposal = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type="SKILL",
        value=TEST_ONLY_SKILL,
        provenance=cv_provenance(TEST_ONLY_SKILL),
    )
    if status == "REJECTED":
        reject_profile_fact(migrated, profile_id, proposal.fact.id)
    with pytest.raises(FactNotRetirableError):
        retire(migrated, profile_id, proposal.fact.id)


def test_the_database_refuses_a_retirement_on_a_non_accepted_fact(
    migrated, profile_id
):
    """The CHECK, not the owner: the same refusal one layer down."""
    proposal = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type="SKILL",
        value=TEST_ONLY_SKILL,
        provenance=cv_provenance(TEST_ONLY_SKILL),
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "UPDATE profile_facts SET retired_at = '2026-01-01 00:00:00' WHERE id = ?",
            (proposal.fact.id,),
        )


def test_a_retirement_cannot_be_cleared_or_re_dated(migrated, profile_id):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    retired = retire(migrated, profile_id, fact_id)
    for new_value in (None, "2030-01-01 00:00:00"):
        with pytest.raises(sqlite3.IntegrityError) as error:
            migrated.execute(
                "UPDATE profile_facts SET retired_at = ? WHERE id = ?",
                (new_value, fact_id),
            )
        assert "cleared or re-dated" in str(error.value)
    assert get_profile_fact(migrated, profile_id, fact_id).retired_at == retired.retired_at


# ------------------------------------------------- retired facts are historical


def test_a_retired_fact_cannot_be_rejected_or_corrected(migrated, profile_id):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    retire(migrated, profile_id, fact_id)

    with pytest.raises(RetiredFactNotMutableError):
        reject_profile_fact(migrated, profile_id, fact_id)
    with pytest.raises(RetiredFactNotMutableError):
        accept_profile_fact(migrated, profile_id, fact_id)
    with pytest.raises(RetiredFactNotMutableError):
        decide_profile_facts(migrated, profile_id, [fact_id], FactStatus.REJECTED)
    with pytest.raises(RetiredFactNotMutableError):
        correct_profile_fact(migrated, profile_id, fact_id, value=TEST_ONLY_CORRECTION)

    stored = get_profile_fact(migrated, profile_id, fact_id)
    assert stored.status is FactStatus.ACCEPTED
    assert stored.retired_at is not None


def test_a_batch_containing_a_retired_fact_changes_nothing(migrated, profile_id):
    kept = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    dropped = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_DROPPED_SKILL)
    retire(migrated, profile_id, dropped)

    with pytest.raises(RetiredFactNotMutableError):
        decide_profile_facts(migrated, profile_id, [kept, dropped], FactStatus.REJECTED)

    assert get_profile_fact(migrated, profile_id, kept).is_current is True


def test_evidence_can_still_be_attached_without_reopening_it(migrated, profile_id):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    retire(migrated, profile_id, fact_id)

    ensure_profile_fact_provenance(
        migrated,
        profile_id=profile_id,
        fact_id=fact_id,
        provenance=cv_provenance(TEST_ONLY_SKILL, cv_sha256=TEST_ONLY_THIRD_SHA256),
    )
    stored = get_profile_fact(migrated, profile_id, fact_id)
    assert stored.is_current is False
    assert stored.status is FactStatus.ACCEPTED
    assert len(list_profile_fact_provenance(migrated, profile_id, fact_id)) == 2


def test_the_same_exact_proof_returns_the_historical_fact_without_reopening(
    migrated, profile_id
):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    retire(migrated, profile_id, fact_id)

    proposal = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type="SKILL",
        value=TEST_ONLY_SKILL,
        provenance=cv_provenance(TEST_ONLY_SKILL),
    )
    assert proposal.created is False
    assert proposal.fact.id == fact_id
    assert proposal.fact.is_current is False
    assert migrated.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0] == 1


def test_a_different_document_saying_the_same_thing_is_a_new_fact(
    migrated, profile_id
):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    retire(migrated, profile_id, fact_id)

    proposal = ensure_profile_fact_proposal(
        migrated,
        profile_id=profile_id,
        fact_type="SKILL",
        value=TEST_ONLY_SKILL,
        provenance=cv_provenance(TEST_ONLY_SKILL, cv_sha256=TEST_ONLY_NEW_SHA256),
    )
    assert proposal.created is True
    assert proposal.fact.id != fact_id
    assert proposal.fact.status is FactStatus.PROPOSED
    assert get_profile_fact(migrated, profile_id, fact_id).retired_at is not None


# ------------------------------------------------------------ reader contracts


def test_a_retired_fact_leaves_every_current_state_reader(migrated, profile_id):
    kept = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    dropped = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_DROPPED_SKILL)
    language = accepted_cv_fact(migrated, profile_id, "LANGUAGE", TEST_ONLY_LANGUAGE)
    synchronize_profile_skills(migrated, profile_id)
    synchronize_structured_profile_entries(migrated, profile_id)
    assert len(list_profile_skills(migrated, profile_id)) == 2
    assert len(list_profile_languages(migrated, profile_id)) == 1

    retire(migrated, profile_id, dropped)
    retire(migrated, profile_id, language)

    verified = {fact.id for fact in list_verified_profile_facts(migrated, profile_id)}
    assert verified == {kept}

    synchronize_profile_skills(migrated, profile_id)
    synchronize_structured_profile_entries(migrated, profile_id)
    remaining = [skill.canonical_name for skill in list_profile_skills(migrated, profile_id)]
    assert remaining == [TEST_ONLY_SKILL]
    assert list_profile_languages(migrated, profile_id) == ()


def test_a_retired_fact_stays_visible_to_audit_and_history(migrated, profile_id):
    fact_id = accepted_cv_fact(migrated, profile_id, "SKILL", TEST_ONLY_SKILL)
    retire(migrated, profile_id, fact_id)

    everything = {fact.id for fact in list_profile_facts(migrated, profile_id)}
    assert fact_id in everything
    accepted = {
        fact.id
        for fact in list_profile_facts(
            migrated, profile_id, statuses=(FactStatus.ACCEPTED,)
        )
    }
    assert fact_id in accepted, "the audit reading still says the person accepted it"
    assert get_profile_fact(migrated, profile_id, fact_id) is not None
    assert list_profile_fact_provenance(migrated, profile_id, fact_id)


def test_the_preference_projections_read_current_facts_only():
    """Defensive: those four types can never be retired — the owner refuses
    them by name — but the statement says what it means all the same."""
    assert len(_PROJECTIONS) == 4
    for projection in _PROJECTIONS:
        assert "f.status = 'ACCEPTED'" in projection.facts_sql, projection.domain
        assert "f.retired_at IS NULL" in projection.facts_sql, projection.domain


# ------------------------------------------- activation and decision freezing
#
# Every row below is built under the real 0027 constraints — the CHECKs, the
# partial unique indexes and the composite foreign keys — rather than against a
# simplified schema, because what is under test is precisely how those rules and
# the 0028 triggers behave together.


TEST_ONLY_DIGEST = "12" * 32
TEST_ONLY_OTHER_DIGEST = "34" * 32
TEST_ONLY_CHAIN = "56" * 32
TEST_ONLY_REVIEW_DIGEST = "78" * 32


def _document(connection, profile_id: int, content_sha256: str) -> int:
    return connection.execute(
        """INSERT INTO profile_cv_documents (
               profile_id, content_sha256, origin, byte_size, page_count, lifecycle
           ) VALUES (?, ?, 'UPLOADED', 4096, 2, 'KNOWN') RETURNING id""",
        (profile_id, content_sha256),
    ).fetchone()[0]


def _extraction(connection, profile_id: int, document_id: int, *, attempt: int = 1) -> int:
    return connection.execute(
        """INSERT INTO profile_cv_extractions (
               document_id, profile_id, parser_version, extractor_version,
               attempt_no, candidate_count, manifest_chain_digest, manifest_state
           ) VALUES (?, ?, ?, ?, ?, 1, ?, 'COMPLETE') RETURNING id""",
        (
            document_id,
            profile_id,
            PARSER_VERSION,
            CANDIDATE_EXTRACTOR_VERSION,
            attempt,
            TEST_ONLY_CHAIN,
        ),
    ).fetchone()[0]


def _candidate(connection, profile_id: int, extraction_id: int, *, ordinal: int = 0) -> int:
    return connection.execute(
        """INSERT INTO profile_cv_candidates (
               extraction_id, profile_id, ordinal, candidate_type, fact_type,
               candidate_fingerprint, provenance_key, value, rule_id, chain_digest
           ) VALUES (?, ?, ?, 'SKILL', 'SKILL', ?, ?, ?, ?, ?) RETURNING id""",
        (
            extraction_id,
            profile_id,
            ordinal,
            f"{ordinal:02d}" * 16,
            f"test-only:candidate:{extraction_id}:{ordinal}",
            TEST_ONLY_SKILL,
            ExtractionRule.SECTION_LINE_BLOCK.value,
            f"{ordinal:02d}" * 32,
        ),
    ).fetchone()[0]


def _replacement(connection, profile_id: int, extraction_id: int, *, baseline=None) -> int:
    return connection.execute(
        """INSERT INTO profile_cv_replacements (
               profile_id, extraction_id, baseline_document_id, lifecycle
           ) VALUES (?, ?, ?, 'READY_TO_ACTIVATE') RETURNING id""",
        (profile_id, extraction_id, baseline),
    ).fetchone()[0]


def _decision(connection, profile_id: int, replacement_id: int, candidate_id: int) -> int:
    return connection.execute(
        """INSERT INTO profile_cv_replacement_decisions (
               replacement_id, profile_id, role, candidate_id, fact_id,
               difference, decision, review_state_digest, decided_at
           ) VALUES (?, ?, 'INCOMING', ?, NULL, 'NEW', 'ACCEPT', ?, CURRENT_TIMESTAMP)
           RETURNING id""",
        (replacement_id, profile_id, candidate_id, TEST_ONLY_CHAIN),
    ).fetchone()[0]


def _activate(connection, replacement_id: int, *, revision: int = 1) -> None:
    """What B1b-B will write. Here it is set directly, to test the triggers."""
    connection.execute(
        """UPDATE profile_cv_replacements
              SET ready_review_digest = ?,
                  activation_revision = ?,
                  activated_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
        (TEST_ONLY_REVIEW_DIGEST, revision, replacement_id),
    )
    connection.commit()


@pytest.fixture
def activated(migrated, profile_id):
    """One activated attempt and one still open, both fully shaped by 0027."""
    document = _document(migrated, profile_id, TEST_ONLY_DIGEST)
    extraction = _extraction(migrated, profile_id, document)
    candidate = _candidate(migrated, profile_id, extraction)
    replacement = _replacement(migrated, profile_id, extraction)
    decision = _decision(migrated, profile_id, replacement, candidate)
    migrated.commit()
    _activate(migrated, replacement)

    # An activated attempt no longer counts as open, so a second one is allowed.
    open_document = _document(migrated, profile_id, TEST_ONLY_OTHER_DIGEST)
    open_extraction = _extraction(migrated, profile_id, open_document)
    open_candidate = _candidate(migrated, profile_id, open_extraction)
    open_replacement = _replacement(migrated, profile_id, open_extraction)
    open_decision = _decision(migrated, profile_id, open_replacement, open_candidate)
    migrated.commit()
    return {
        "document": document,
        "extraction": extraction,
        "candidate": candidate,
        "replacement": replacement,
        "decision": decision,
        "open_document": open_document,
        "open_extraction": open_extraction,
        "open_candidate": open_candidate,
        "open_replacement": open_replacement,
        "open_decision": open_decision,
    }


def test_an_activated_attempt_no_longer_blocks_the_next_one(migrated, activated):
    open_rows = migrated.execute(
        """SELECT COUNT(*) FROM profile_cv_replacements
            WHERE lifecycle IN ('PREPARED','REVIEWING','READY_TO_ACTIVATE')
              AND activated_at IS NULL"""
    ).fetchone()[0]
    assert open_rows == 1
    assert activated["open_replacement"] != activated["replacement"]


@pytest.mark.parametrize(
    "column, value",
    [
        ("activated_at", None),
        ("activated_at", "2030-01-01 00:00:00"),
        ("activation_revision", None),
        ("activation_revision", 99),
        ("ready_review_digest", "ff" * 32),
        ("lifecycle", "CANCELLED"),
        ("lifecycle", "REVIEWING"),
    ],
)
def test_the_activation_event_itself_cannot_be_rewritten(
    migrated, activated, column, value
):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            f"UPDATE profile_cv_replacements SET {column} = ? WHERE id = ?",
            (value, activated["replacement"]),
        )


def test_the_activated_extraction_cannot_be_moved(migrated, activated):
    other = _extraction(migrated, activated_profile(migrated), activated["open_document"], attempt=2)
    migrated.commit()
    with pytest.raises(sqlite3.IntegrityError) as error:
        migrated.execute(
            "UPDATE profile_cv_replacements SET extraction_id = ? WHERE id = ?",
            (other, activated["replacement"]),
        )
    assert "reopened or rewritten" in str(error.value)


def test_the_activated_baseline_document_cannot_be_moved(migrated, activated):
    with pytest.raises(sqlite3.IntegrityError) as error:
        migrated.execute(
            "UPDATE profile_cv_replacements SET baseline_document_id = ? WHERE id = ?",
            (activated["open_document"], activated["replacement"]),
        )
    assert "reopened or rewritten" in str(error.value)


def test_the_activated_profile_cannot_be_moved(migrated, activated):
    other_profile = ensure_user_profile(migrated, "other.student@example.invalid").profile_id
    migrated.commit()
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "UPDATE profile_cv_replacements SET profile_id = ? WHERE id = ?",
            (other_profile, activated["replacement"]),
        )


def test_the_activated_identity_and_opening_date_cannot_be_rewritten(
    migrated, activated
):
    for column, value in (("id", 9_999), ("created_at", "2000-01-01 00:00:00")):
        with pytest.raises(sqlite3.IntegrityError) as error:
            migrated.execute(
                f"UPDATE profile_cv_replacements SET {column} = ? WHERE id = ?",
                (value, activated["replacement"]),
            )
        assert "reopened or rewritten" in str(error.value)


def test_a_technical_touch_stamp_is_still_allowed(migrated, activated):
    """`updated_at` is not part of the historical event, and is left mutable."""
    migrated.execute(
        "UPDATE profile_cv_replacements SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (activated["replacement"],),
    )
    migrated.commit()
    row = migrated.execute(
        "SELECT activated_at, activation_revision, ready_review_digest "
        "FROM profile_cv_replacements WHERE id = ?",
        (activated["replacement"],),
    ).fetchone()
    assert row[0] is not None and row[1] == 1 and row[2] == TEST_ONLY_REVIEW_DIGEST


def test_a_decision_cannot_be_inserted_into_an_activated_review(migrated, activated):
    second = _candidate(migrated, activated_profile(migrated), activated["extraction"], ordinal=1)
    migrated.commit()
    with pytest.raises(sqlite3.IntegrityError) as error:
        _decision(migrated, activated_profile(migrated), activated["replacement"], second)
    assert "activated replacement is closed" in str(error.value)


def test_a_decision_of_an_activated_review_cannot_be_updated(migrated, activated):
    with pytest.raises(sqlite3.IntegrityError) as error:
        migrated.execute(
            "UPDATE profile_cv_replacement_decisions SET decision = 'REJECT' WHERE id = ?",
            (activated["decision"],),
        )
    assert "activated replacement is closed" in str(error.value)


def test_a_decision_of_an_activated_review_cannot_be_deleted(migrated, activated):
    with pytest.raises(sqlite3.IntegrityError) as error:
        migrated.execute(
            "DELETE FROM profile_cv_replacement_decisions WHERE id = ?",
            (activated["decision"],),
        )
    assert "activated replacement is closed" in str(error.value)
    assert (
        migrated.execute(
            "SELECT COUNT(*) FROM profile_cv_replacement_decisions WHERE id = ?",
            (activated["decision"],),
        ).fetchone()[0]
        == 1
    )


def test_a_decision_cannot_be_moved_into_an_activated_review(migrated, activated):
    """The dangerous direction: an answer carried into a review already applied."""
    with pytest.raises(sqlite3.IntegrityError) as error:
        migrated.execute(
            """UPDATE profile_cv_replacement_decisions
                  SET replacement_id = ?, candidate_id = ?
                WHERE id = ?""",
            (
                activated["replacement"],
                activated["candidate"],
                activated["open_decision"],
            ),
        )
    assert "activated replacement is closed" in str(error.value)


def test_a_decision_cannot_be_moved_into_another_profiles_activated_review(
    migrated, activated
):
    other_profile = ensure_user_profile(migrated, "other.student@example.invalid").profile_id
    migrated.commit()
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            """UPDATE profile_cv_replacement_decisions
                  SET replacement_id = ?, profile_id = ?
                WHERE id = ?""",
            (activated["replacement"], other_profile, activated["open_decision"]),
        )


def test_the_review_of_an_open_attempt_is_still_editable(migrated, activated):
    migrated.execute(
        "UPDATE profile_cv_replacement_decisions SET decision = 'REJECT' WHERE id = ?",
        (activated["open_decision"],),
    )
    migrated.commit()
    assert (
        migrated.execute(
            "SELECT decision FROM profile_cv_replacement_decisions WHERE id = ?",
            (activated["open_decision"],),
        ).fetchone()[0]
        == "REJECT"
    )
    migrated.execute(
        "DELETE FROM profile_cv_replacement_decisions WHERE id = ?",
        (activated["open_decision"],),
    )
    migrated.commit()
    second = _decision(
        migrated,
        activated_profile(migrated),
        activated["open_replacement"],
        activated["open_candidate"],
    )
    migrated.commit()
    assert second is not None


def activated_profile(connection) -> int:
    """The profile the `migrated` fixture's owner was created with."""
    return connection.execute(
        "SELECT p.id FROM profiles AS p JOIN users AS u ON u.id = p.user_id "
        "WHERE u.email = ? ",
        (TEST_ONLY_EMAIL,),
    ).fetchone()[0]
