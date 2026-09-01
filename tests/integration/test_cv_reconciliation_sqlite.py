"""Phase 3.3C: reconciling two CV extraction campaigns, on disposable SQLite.

The scenario every test here builds is the shape of the real one, in miniature
and entirely invented:

* an old campaign read the document and a human decided about all of it — most
  of it accepted, one reading corrected by hand;
* a newer campaign reads the same file, produces most of those readings word
  for word, reclassifies two of them under another type, and replaces two
  badly cut blocks with one grouped block.

What the reconciliation has to do with that: reuse the unchanged facts rather
than duplicating them, raise the changed ones as proposals nobody has answered,
and — only once a human has answered them — retire the old readings they
replaced, by rejecting them and never by deleting them.

Every database here is created under `tmp_path` and thrown away; the real
`.data/` database is never opened, and no real CV is ever parsed. Every value
is synthetic and marked TEST ONLY, and `.invalid` never resolves.
"""

import hashlib
import json
from dataclasses import replace

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.cv.candidates.models import (
    CandidateType,
    ExtractedCandidate,
    ExtractionRule,
    StructuredCvExtraction,
)
from services.digital_twin.cv.fact_bridge import import_cv_candidates
from services.digital_twin.cv.models import SectionType
from services.digital_twin.cv.reconciliation import (
    ALREADY_REJECTED,
    TERMINAL_CORRECTED,
    UNDECIDED,
    AmbiguousHistoricalReadingError,
    CvReconciliationVersionError,
    UnresolvedChangedReadingError,
    compute_cv_reconciliation_plan,
    finalize_cv_fact_reconciliation,
    prepare_cv_fact_reconciliation,
)
from services.digital_twin.facts.models import FactStatus, ProfileFactType
from services.digital_twin.facts.repository import (
    accept_profile_fact,
    correct_profile_fact,
    get_profile_fact,
    list_profile_fact_provenance,
    list_profile_facts,
    list_verified_profile_facts,
    reject_profile_fact,
)
from services.digital_twin.repository import ensure_user_profile
from services.digital_twin.skills.repository import (
    list_profile_skills,
    synchronize_profile_skills,
)

# TEST ONLY identity; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "student@example.invalid"
# A synthetic 64-hex digest; no real file was hashed to produce it.
TEST_ONLY_SHA256 = "ab" * 32

OLD_PARSER = "cv-parser-v1"
OLD_EXTRACTOR = "cv-candidates-v1"
NEW_PARSER = "cv-parser-v5"
NEW_EXTRACTOR = "cv-candidates-v7"

# TEST ONLY CV content. Every line below is invented and describes nobody.
NAME = "Alex Test-Only"
EMAIL = "student@example.invalid"
DEGREE = "TEST ONLY degree, Example School, 2019-2021"
CORRECTED_DEGREE = "TEST ONLY degree (corrected by hand), Example School"
ANALYST_ROLE = "TEST ONLY analyst, Example Org, 2021-2022"
TOOLKIT_PROJECT = "TEST ONLY toolkit build"
DASHBOARD_PROJECT = "TEST ONLY dashboard build"
FRAGMENT_ONE = "TEST ONLY intern, Example Lab"
FRAGMENT_TWO = "2022-2023 | Example Lab | Paris"
GROUPED_ROLE = f"{FRAGMENT_ONE}\n{FRAGMENT_TWO}"
FIRST_SKILL = "TestOnlyToolkit"
SECOND_SKILL = "TestOnlyLang"

#: Everything a summary, a report or a log line must never carry.
CV_CONTENT = (
    NAME,
    EMAIL,
    DEGREE,
    CORRECTED_DEGREE,
    ANALYST_ROLE,
    TOOLKIT_PROJECT,
    DASHBOARD_PROJECT,
    FRAGMENT_ONE,
    FRAGMENT_TWO,
    FIRST_SKILL,
    SECOND_SKILL,
)

#: The shape of the miniature scenario, asserted rather than assumed.
OLD_FACTS = 8
NEW_CANDIDATES = 7
UNCHANGED = 4
CHANGED = 3
SUPERSEDED = 4


def fingerprint(
    extractor_version: str, candidate_type: CandidateType, text: str
) -> str:
    """The extractor's own fingerprint formula, for a campaign of any version.

    Written out here rather than imported, because the shipped helper is pinned
    to the version this checkout produces and these tests need both campaigns.
    """
    payload = "\x1f".join((extractor_version, candidate_type.value, text.casefold()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_candidate(
    candidate_type: CandidateType,
    raw_text: str,
    *,
    extractor_version: str,
    parser_version: str,
    rule_id: ExtractionRule = ExtractionRule.SECTION_BLANK_LINE_BLOCK,
    page_numbers: tuple[int, ...] = (1,),
    section_type: SectionType | None = SectionType.EXPERIENCE,
    section_index: int | None = 0,
    normalized_value: str | None = None,
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
        fingerprint=fingerprint(extractor_version, candidate_type, raw_text),
        cv_sha256=TEST_ONLY_SHA256,
        parser_version=parser_version,
        extractor_version=extractor_version,
    )


def make_extraction(
    candidates: tuple[ExtractedCandidate, ...],
    *,
    parser_version: str,
    extractor_version: str,
) -> StructuredCvExtraction:
    return StructuredCvExtraction(
        extractor_version=extractor_version,
        parser_version=parser_version,
        cv_sha256=TEST_ONLY_SHA256,
        candidates=candidates,
        warnings=(),
    )


def old_extraction(
    *extra: ExtractedCandidate,
) -> StructuredCvExtraction:
    """What the older parser and extractor read: four entries, cut badly."""

    def old(candidate_type: CandidateType, text: str, **overrides):
        return make_candidate(
            candidate_type,
            text,
            extractor_version=OLD_EXTRACTOR,
            parser_version=OLD_PARSER,
            **overrides,
        )

    return make_extraction(
        (
            old(
                CandidateType.NAME_CANDIDATE,
                NAME,
                rule_id=ExtractionRule.HEADER_FIRST_LINE_NAME_SHAPE,
                section_type=SectionType.UNCLASSIFIED,
            ),
            old(
                CandidateType.EDUCATION_ENTRY,
                DEGREE,
                section_type=SectionType.EDUCATION,
            ),
            old(CandidateType.EXPERIENCE_ENTRY, ANALYST_ROLE),
            # Read as experience by the old rules; the newer ones classify the
            # very same text as a project.
            old(CandidateType.EXPERIENCE_ENTRY, TOOLKIT_PROJECT),
            old(CandidateType.EXPERIENCE_ENTRY, DASHBOARD_PROJECT),
            # One role the old boundaries cut in two.
            old(CandidateType.EXPERIENCE_ENTRY, FRAGMENT_ONE),
            old(CandidateType.EXPERIENCE_ENTRY, FRAGMENT_TWO),
            old(
                CandidateType.SKILL,
                FIRST_SKILL,
                rule_id=ExtractionRule.SKILLS_SEPARATED_LIST_LINE,
                section_type=SectionType.SKILLS,
            ),
        )
        + extra,
        parser_version=OLD_PARSER,
        extractor_version=OLD_EXTRACTOR,
    )


def new_extraction(*extra: ExtractedCandidate) -> StructuredCvExtraction:
    """What this checkout reads: the same words, two of them reclassified."""

    def new(candidate_type: CandidateType, text: str, **overrides):
        return make_candidate(
            candidate_type,
            text,
            extractor_version=NEW_EXTRACTOR,
            parser_version=NEW_PARSER,
            **overrides,
        )

    return make_extraction(
        (
            new(
                CandidateType.NAME_CANDIDATE,
                NAME,
                rule_id=ExtractionRule.HEADER_FIRST_LINE_NAME_SHAPE,
                section_type=SectionType.UNCLASSIFIED,
            ),
            new(
                CandidateType.EDUCATION_ENTRY,
                DEGREE,
                section_type=SectionType.EDUCATION,
            ),
            new(CandidateType.EXPERIENCE_ENTRY, ANALYST_ROLE),
            new(
                CandidateType.PROJECT_ENTRY,
                TOOLKIT_PROJECT,
                section_type=SectionType.PROJECTS,
            ),
            new(
                CandidateType.PROJECT_ENTRY,
                DASHBOARD_PROJECT,
                section_type=SectionType.PROJECTS,
            ),
            new(
                CandidateType.EXPERIENCE_ENTRY,
                GROUPED_ROLE,
                rule_id=ExtractionRule.EXPERIENCE_PIPE_DELIMITED_BLOCK,
            ),
            new(
                CandidateType.SKILL,
                FIRST_SKILL,
                rule_id=ExtractionRule.SKILLS_SEPARATED_LIST_LINE,
                section_type=SectionType.SKILLS,
            ),
        )
        + extra,
        parser_version=NEW_PARSER,
        extractor_version=NEW_EXTRACTOR,
    )


@pytest.fixture
def migrated(tmp_path):
    connection = connect_database(tmp_path / "reconciliation.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def profile_id(migrated) -> int:
    return ensure_user_profile(migrated, TEST_ONLY_EMAIL).profile_id


def fact_for(connection, profile_id: int, fact_type, value: str):
    """The one fact of this profile holding that exact reading. Never two."""
    stored = fact_type.value if hasattr(fact_type, "value") else fact_type
    found = [
        fact
        for fact in list_profile_facts(connection, profile_id)
        if fact.fact_type == stored and fact.value == value
    ]
    assert len(found) == 1, f"expected one {stored} reading, found {len(found)}"
    return found[0]


def seed_old_campaign(
    connection,
    profile_id: int,
    *,
    extraction=None,
    leave_undecided: tuple[str, ...] = (),
):
    """Import the old campaign and decide it the way a human would have.

    Everything is accepted except what the caller names, and the education
    reading is corrected by hand, so the scenario carries the one case that
    must survive untouched: a `CORRECTED` fact among the unchanged readings.
    """
    result = import_cv_candidates(
        connection,
        profile_id=profile_id,
        extraction=old_extraction() if extraction is None else extraction,
    )
    for entry in result.imported:
        if entry.fact.value in leave_undecided:
            continue
        accept_profile_fact(connection, profile_id, entry.fact.id)
    correction = correct_profile_fact(
        connection,
        profile_id,
        fact_for(connection, profile_id, ProfileFactType.EDUCATION, DEGREE).id,
        value=CORRECTED_DEGREE,
    )
    return result, correction


@pytest.fixture
def seeded(migrated, profile_id):
    return seed_old_campaign(migrated, profile_id)


def reconcile_plan(connection, profile_id: int, extraction=None):
    return compute_cv_reconciliation_plan(
        connection,
        profile_id=profile_id,
        extraction=new_extraction() if extraction is None else extraction,
        old_parser_version=OLD_PARSER,
        old_extractor_version=OLD_EXTRACTOR,
    )


def prepare(connection, profile_id: int, extraction=None):
    return prepare_cv_fact_reconciliation(
        connection,
        profile_id=profile_id,
        extraction=new_extraction() if extraction is None else extraction,
        old_parser_version=OLD_PARSER,
        old_extractor_version=OLD_EXTRACTOR,
    )


def finalize(connection, profile_id: int, extraction=None, **kwargs):
    return finalize_cv_fact_reconciliation(
        connection,
        profile_id=profile_id,
        extraction=new_extraction() if extraction is None else extraction,
        old_parser_version=OLD_PARSER,
        old_extractor_version=OLD_EXTRACTOR,
        **kwargs,
    )


def answer_every_changed_reading(connection, profile_id: int, preparation) -> None:
    """Do by hand what the review CLI would do: accept each new reading."""
    for entry in preparation.proposed:
        accept_profile_fact(connection, profile_id, entry.fact.id)


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def test_the_plan_splits_the_new_campaign_in_three(migrated, profile_id, seeded):
    plan = reconcile_plan(migrated, profile_id)

    assert plan.historical_facts == OLD_FACTS
    assert plan.new_candidates == NEW_CANDIDATES
    assert len(plan.unchanged) == UNCHANGED
    assert len(plan.changed) == CHANGED
    assert len(plan.superseded) == SUPERSEDED
    assert plan.changed_by_fact_type() == {"EXPERIENCE": 1, "PROJECT": 2}
    assert plan.superseded_by_fact_type() == {"EXPERIENCE": 4}


def test_the_plan_writes_nothing(migrated, profile_id, seeded):
    before = list_profile_facts(migrated, profile_id)

    reconcile_plan(migrated, profile_id)

    assert list_profile_facts(migrated, profile_id) == before


def test_every_old_fact_is_either_reused_or_superseded(migrated, profile_id, seeded):
    plan = reconcile_plan(migrated, profile_id)

    reused = {entry.fact.id for entry in plan.unchanged}
    retired = {entry.fact.id for entry in plan.superseded}
    assert reused.isdisjoint(retired)
    assert len(reused | retired) == plan.historical_facts


def test_reconciling_a_campaign_with_itself_is_refused(migrated, profile_id, seeded):
    with pytest.raises(CvReconciliationVersionError):
        compute_cv_reconciliation_plan(
            migrated,
            profile_id=profile_id,
            extraction=new_extraction(),
            old_parser_version=NEW_PARSER,
            old_extractor_version=NEW_EXTRACTOR,
        )


def test_an_ambiguous_old_reading_is_refused_before_anything_is_written(
    migrated, profile_id
):
    """Two old facts holding one reading: the code says so instead of choosing."""
    twin = make_candidate(
        CandidateType.EXPERIENCE_ENTRY,
        ANALYST_ROLE,
        extractor_version=OLD_EXTRACTOR,
        parser_version=OLD_PARSER,
        # A different place in the document, so it is a different proof and the
        # ordinary import creates a second fact holding the same reading.
        page_numbers=(2,),
        section_index=1,
    )
    seed_old_campaign(migrated, profile_id, extraction=old_extraction(twin))
    before = list_profile_facts(migrated, profile_id)

    with pytest.raises(AmbiguousHistoricalReadingError):
        prepare(migrated, profile_id)

    assert list_profile_facts(migrated, profile_id) == before


def new_extraction_reading(before: str, after: str) -> StructuredCvExtraction:
    """The newer campaign, with one reading rewritten to `after`."""
    base = new_extraction()
    candidates = tuple(
        replace(
            candidate,
            raw_text=after,
            fingerprint=fingerprint(NEW_EXTRACTOR, candidate.candidate_type, after),
        )
        if candidate.raw_text == before
        else candidate
        for candidate in base.candidates
    )
    return make_extraction(
        candidates, parser_version=NEW_PARSER, extractor_version=NEW_EXTRACTOR
    )


@pytest.mark.parametrize(
    "rewritten",
    [
        ANALYST_ROLE.upper(),
        ANALYST_ROLE.casefold(),
        ANALYST_ROLE + " ",
        " " + ANALYST_ROLE,
        ANALYST_ROLE.replace(", ", ",  "),
        ANALYST_ROLE[:-1],
    ],
    ids=["upper", "lower", "trailing", "leading", "inner", "truncated"],
)
def test_a_reading_that_is_not_byte_identical_is_a_new_reading(
    migrated, profile_id, seeded, rewritten
):
    """No case folding, no trimming, no collapsing, no prefix rule: exact only.

    Every one of these would compare equal under some "obvious" normalization,
    and every one of them is a different thing to say about the person. They go
    to a human as proposals, and the old reading they no longer match becomes
    superseded rather than being quietly re-confirmed.
    """
    plan = reconcile_plan(
        migrated, profile_id, new_extraction_reading(ANALYST_ROLE, rewritten)
    )

    assert len(plan.unchanged) == UNCHANGED - 1
    assert len(plan.changed) == CHANGED + 1
    assert len(plan.superseded) == SUPERSEDED + 1
    assert ANALYST_ROLE in [entry.fact.value for entry in plan.superseded]


def test_where_a_reading_sits_in_the_document_takes_no_part_in_its_identity(
    migrated, profile_id, seeded
):
    """The same words under the same type are the same reading, moved or not."""
    base = new_extraction()
    moved = make_extraction(
        tuple(
            replace(
                candidate,
                page_numbers=(2,),
                section_index=3,
                rule_id=ExtractionRule.SECTION_BULLET_BLOCK,
            )
            if candidate.raw_text == ANALYST_ROLE
            else candidate
            for candidate in base.candidates
        ),
        parser_version=NEW_PARSER,
        extractor_version=NEW_EXTRACTOR,
    )

    plan = reconcile_plan(migrated, profile_id, moved)

    assert len(plan.unchanged) == UNCHANGED
    assert len(plan.changed) == CHANGED


# --------------------------------------------------------------------------
# Prepare
# --------------------------------------------------------------------------


def test_prepare_reuses_the_unchanged_facts_instead_of_duplicating_them(
    migrated, profile_id, seeded
):
    before = {fact.id for fact in list_profile_facts(migrated, profile_id)}

    preparation = prepare(migrated, profile_id)

    after = {fact.id for fact in list_profile_facts(migrated, profile_id)}
    # Exactly one new fact per changed reading, and not one per candidate.
    assert len(after - before) == CHANGED
    assert preparation.summary()["new_candidates"] == NEW_CANDIDATES
    assert preparation.summary()["unchanged_candidates"] == UNCHANGED
    assert preparation.provenance_attached == UNCHANGED
    assert preparation.newly_proposed == CHANGED
    for entry in preparation.attached:
        assert entry.reading.fact.id in before


def test_an_unchanged_reading_keeps_its_id_value_and_decision(
    migrated, profile_id, seeded
):
    original = fact_for(migrated, profile_id, ProfileFactType.NAME, NAME)

    prepare(migrated, profile_id)

    reused = get_profile_fact(migrated, profile_id, original.id)
    assert reused.value == original.value
    assert reused.normalized_value == original.normalized_value
    assert reused.status is FactStatus.ACCEPTED
    assert reused.decided_at == original.decided_at


def test_an_unchanged_reading_gains_the_new_campaigns_provenance(
    migrated, profile_id, seeded
):
    original = fact_for(migrated, profile_id, ProfileFactType.NAME, NAME)

    prepare(migrated, profile_id)

    recorded = list_profile_fact_provenance(migrated, profile_id, original.id)
    campaigns = {
        (entry.parser_version, entry.extractor_version) for entry in recorded
    }
    # Both readings of the document are on the same fact, side by side.
    assert campaigns == {(OLD_PARSER, OLD_EXTRACTOR), (NEW_PARSER, NEW_EXTRACTOR)}


def test_a_corrected_reading_stays_corrected_and_its_replacement_is_untouched(
    migrated, profile_id, seeded
):
    _, correction = seeded
    before_replacement = get_profile_fact(
        migrated, profile_id, correction.replacement.id
    )

    prepare(migrated, profile_id)

    corrected = get_profile_fact(migrated, profile_id, correction.corrected.id)
    assert corrected.status is FactStatus.CORRECTED
    assert corrected.value == DEGREE
    assert corrected.replaced_by_fact_id == correction.replacement.id
    # The person's own statement is not re-read, re-accepted or re-dated.
    assert get_profile_fact(migrated, profile_id, correction.replacement.id) == (
        before_replacement
    )
    # And it did gain the new campaign's evidence, on the corrected fact only.
    assert len(list_profile_fact_provenance(migrated, profile_id, corrected.id)) == 2
    assert (
        len(
            list_profile_fact_provenance(
                migrated, profile_id, correction.replacement.id
            )
        )
        == 1
    )


def test_a_type_change_becomes_a_new_proposal(migrated, profile_id, seeded):
    old_reading = fact_for(
        migrated, profile_id, ProfileFactType.EXPERIENCE, TOOLKIT_PROJECT
    )

    preparation = prepare(migrated, profile_id)

    proposed = [
        entry for entry in preparation.proposed if entry.fact.value == TOOLKIT_PROJECT
    ]
    assert len(proposed) == 1
    assert proposed[0].fact.fact_type == ProfileFactType.PROJECT.value
    assert proposed[0].fact.status is FactStatus.PROPOSED
    assert proposed[0].fact.id != old_reading.id
    # The old reading is untouched by prepare: only finalize retires it.
    assert (
        get_profile_fact(migrated, profile_id, old_reading.id).status
        is FactStatus.ACCEPTED
    )


def test_a_boundary_change_becomes_a_new_proposal(migrated, profile_id, seeded):
    preparation = prepare(migrated, profile_id)

    proposed = [
        entry for entry in preparation.proposed if entry.fact.value == GROUPED_ROLE
    ]
    assert len(proposed) == 1
    assert proposed[0].fact.fact_type == ProfileFactType.EXPERIENCE.value
    assert proposed[0].fact.status is FactStatus.PROPOSED
    for fragment in (FRAGMENT_ONE, FRAGMENT_TWO):
        old_fragment = fact_for(
            migrated, profile_id, ProfileFactType.EXPERIENCE, fragment
        )
        assert old_fragment.status is FactStatus.ACCEPTED


def test_a_block_the_layout_rule_grouped_is_still_only_a_proposal(
    migrated, profile_id, seeded
):
    """A `SECTION_LAYOUT_CONTINUATION_BLOCK` reading answers to the same rules.

    The rule that cut a block takes no part in whether two readings are the same
    reading — only the profile, the document, the campaign, the type and the
    text do — so a block the newer parser grouped from the page's layout arrives
    exactly like any other regrouping: as a `PROPOSED` fact a human answers,
    with the accepted fragments it would replace left untouched until they do.
    """
    grouped = make_candidate(
        CandidateType.EDUCATION_ENTRY,
        f"{FRAGMENT_ONE}\n{FRAGMENT_TWO}",
        extractor_version=NEW_EXTRACTOR,
        parser_version=NEW_PARSER,
        rule_id=ExtractionRule.SECTION_LAYOUT_CONTINUATION_BLOCK,
        section_type=SectionType.EDUCATION,
    )

    preparation = prepare_cv_fact_reconciliation(
        migrated,
        profile_id=profile_id,
        extraction=new_extraction(grouped),
        old_parser_version=OLD_PARSER,
        old_extractor_version=OLD_EXTRACTOR,
    )

    proposed = [
        entry
        for entry in preparation.proposed
        if entry.fact.fact_type == ProfileFactType.EDUCATION.value
        and entry.fact.value == GROUPED_ROLE
    ]
    assert len(proposed) == 1
    assert proposed[0].fact.status is FactStatus.PROPOSED
    assert proposed[0].fact.decided_at is None
    # Nothing the old campaign read was replaced, rewritten or deleted.
    for fragment in (FRAGMENT_ONE, FRAGMENT_TWO):
        assert (
            fact_for(
                migrated, profile_id, ProfileFactType.EXPERIENCE, fragment
            ).status
            is FactStatus.ACCEPTED
        )


def test_prepare_accepts_nothing(migrated, profile_id, seeded):
    preparation = prepare(migrated, profile_id)

    assert len(preparation.pending_review) == CHANGED
    for entry in preparation.proposed:
        assert entry.fact.status is FactStatus.PROPOSED
        assert entry.fact.decided_at is None


def test_prepare_rejects_nothing(migrated, profile_id, seeded):
    prepare(migrated, profile_id)

    assert not [
        fact
        for fact in list_profile_facts(migrated, profile_id)
        if fact.status is FactStatus.REJECTED
    ]


def test_prepare_twice_duplicates_nothing(migrated, profile_id, seeded):
    first = prepare(migrated, profile_id)
    facts_after_first = list_profile_facts(migrated, profile_id)

    second = prepare(migrated, profile_id)

    assert list_profile_facts(migrated, profile_id) == facts_after_first
    assert second.provenance_attached == 0
    assert second.provenance_already_present == UNCHANGED
    assert second.newly_proposed == 0
    assert second.already_proposed == CHANGED
    # The same facts, not new ones that happen to look alike.
    assert [entry.fact.id for entry in second.proposed] == [
        entry.fact.id for entry in first.proposed
    ]


def test_prepare_after_a_decision_does_not_ask_again(migrated, profile_id, seeded):
    first = prepare(migrated, profile_id)
    accept_profile_fact(migrated, profile_id, first.proposed[0].fact.id)

    second = prepare(migrated, profile_id)

    assert second.proposed[0].fact.status is FactStatus.ACCEPTED
    assert len(second.pending_review) == CHANGED - 1


def test_the_preparation_summary_carries_no_cv_content(migrated, profile_id, seeded):
    rendered = json.dumps(prepare(migrated, profile_id).summary())

    for value in CV_CONTENT:
        assert value not in rendered
    assert "PROJECT" in rendered


def test_the_plan_entry_summaries_carry_no_cv_content(migrated, profile_id, seeded):
    plan = reconcile_plan(migrated, profile_id)
    entries = (
        [entry.summary() for entry in plan.unchanged]
        + [entry.summary() for entry in plan.changed]
        + [entry.summary() for entry in plan.superseded]
    )

    rendered = json.dumps(entries)
    for value in CV_CONTENT:
        assert value not in rendered


# --------------------------------------------------------------------------
# Finalize: the refusals
# --------------------------------------------------------------------------


def test_finalize_refuses_while_a_new_reading_is_still_proposed(
    migrated, profile_id, seeded
):
    prepare(migrated, profile_id)
    before = list_profile_facts(migrated, profile_id)

    with pytest.raises(UnresolvedChangedReadingError) as raised:
        finalize(migrated, profile_id)

    assert len(raised.value.unresolved) == CHANGED
    assert {entry["reason"] for entry in raised.value.unresolved} == {"PROPOSED"}
    assert list_profile_facts(migrated, profile_id) == before


def test_finalize_refuses_when_one_new_reading_was_rejected(
    migrated, profile_id, seeded
):
    preparation = prepare(migrated, profile_id)
    answer_every_changed_reading(migrated, profile_id, preparation)
    reject_profile_fact(migrated, profile_id, preparation.proposed[0].fact.id)
    before = list_profile_facts(migrated, profile_id)

    with pytest.raises(UnresolvedChangedReadingError) as raised:
        finalize(migrated, profile_id)

    assert [entry["reason"] for entry in raised.value.unresolved] == ["REJECTED"]
    assert list_profile_facts(migrated, profile_id) == before


def test_finalize_refuses_when_a_new_reading_has_never_been_prepared(
    migrated, profile_id, seeded
):
    """No prepare, no evidence, no fact: there is nothing confirming anything."""
    with pytest.raises(UnresolvedChangedReadingError) as raised:
        finalize(migrated, profile_id)

    assert {entry["reason"] for entry in raised.value.unresolved} == {"MISSING"}


def test_finalize_refuses_a_corrected_new_reading_whose_replacement_is_not_accepted(
    migrated, profile_id, seeded
):
    preparation = prepare(migrated, profile_id)
    answer_every_changed_reading(migrated, profile_id, preparation)
    correction = correct_profile_fact(
        migrated,
        profile_id,
        preparation.proposed[0].fact.id,
        value="TEST ONLY corrected project",
    )
    reject_profile_fact(migrated, profile_id, correction.replacement.id)
    before = list_profile_facts(migrated, profile_id)

    with pytest.raises(UnresolvedChangedReadingError) as raised:
        finalize(migrated, profile_id)

    assert [entry["reason"] for entry in raised.value.unresolved] == [
        "CORRECTED_REPLACEMENT_NOT_ACCEPTED"
    ]
    assert list_profile_facts(migrated, profile_id) == before


def test_the_refusal_carries_no_cv_content(migrated, profile_id, seeded):
    prepare(migrated, profile_id)

    with pytest.raises(UnresolvedChangedReadingError) as raised:
        finalize(migrated, profile_id)

    rendered = json.dumps(list(raised.value.unresolved)) + str(raised.value)
    for value in CV_CONTENT:
        assert value not in rendered


# --------------------------------------------------------------------------
# Finalize: the retirement
# --------------------------------------------------------------------------


@pytest.fixture
def answered(migrated, profile_id, seeded):
    """The state finalize is meant to run in: every new reading confirmed."""
    preparation = prepare(migrated, profile_id)
    answer_every_changed_reading(migrated, profile_id, preparation)
    return preparation


def test_finalize_rejects_the_superseded_old_readings(
    migrated, profile_id, answered
):
    finalization = finalize(migrated, profile_id)

    assert finalization.newly_rejected == SUPERSEDED
    assert len(finalization.resolved) == CHANGED
    for text in (TOOLKIT_PROJECT, DASHBOARD_PROJECT, FRAGMENT_ONE, FRAGMENT_TWO):
        retired = fact_for(migrated, profile_id, ProfileFactType.EXPERIENCE, text)
        assert retired.status is FactStatus.REJECTED


def test_finalize_leaves_the_unchanged_readings_alone(migrated, profile_id, answered):
    kept = fact_for(migrated, profile_id, ProfileFactType.EXPERIENCE, ANALYST_ROLE)

    finalize(migrated, profile_id)

    assert (
        get_profile_fact(migrated, profile_id, kept.id).status is FactStatus.ACCEPTED
    )
    assert (
        get_profile_fact(migrated, profile_id, kept.id).value == ANALYST_ROLE
    )


def test_finalize_deletes_nothing(migrated, profile_id, answered):
    before = [fact.id for fact in list_profile_facts(migrated, profile_id)]
    provenance_before = {
        fact_id: len(list_profile_fact_provenance(migrated, profile_id, fact_id))
        for fact_id in before
    }

    finalize(migrated, profile_id)

    after = [fact.id for fact in list_profile_facts(migrated, profile_id)]
    assert after == before
    for fact_id in after:
        assert (
            len(list_profile_fact_provenance(migrated, profile_id, fact_id))
            == provenance_before[fact_id]
        )


def test_a_retired_reading_keeps_its_value_and_its_own_provenance(
    migrated, profile_id, answered
):
    retired = fact_for(
        migrated, profile_id, ProfileFactType.EXPERIENCE, TOOLKIT_PROJECT
    )

    finalize(migrated, profile_id)

    after = get_profile_fact(migrated, profile_id, retired.id)
    assert after.value == TOOLKIT_PROJECT
    assert after.normalized_value == retired.normalized_value
    assert after.fact_type == ProfileFactType.EXPERIENCE.value
    campaigns = {
        (entry.parser_version, entry.extractor_version)
        for entry in list_profile_fact_provenance(migrated, profile_id, retired.id)
    }
    # The old campaign's proof is still there: the history stays auditable.
    assert campaigns == {(OLD_PARSER, OLD_EXTRACTOR)}


def test_a_second_finalize_changes_nothing(migrated, profile_id, answered):
    first = finalize(migrated, profile_id)
    facts_after_first = list_profile_facts(migrated, profile_id)

    second = finalize(migrated, profile_id)

    assert first.changed_anything is True
    assert second.changed_anything is False
    assert second.newly_rejected == 0
    assert second.already_rejected == SUPERSEDED
    assert list_profile_facts(migrated, profile_id) == facts_after_first


def test_finalize_never_mutates_a_terminal_corrected_old_reading(
    migrated, profile_id, answered
):
    retired = fact_for(
        migrated, profile_id, ProfileFactType.EXPERIENCE, DASHBOARD_PROJECT
    )
    correction = correct_profile_fact(
        migrated, profile_id, retired.id, value="TEST ONLY corrected old reading"
    )
    before = get_profile_fact(migrated, profile_id, correction.corrected.id)

    finalization = finalize(migrated, profile_id)

    assert finalization.left_terminal_corrected == 1
    assert finalization.newly_rejected == SUPERSEDED - 1
    assert get_profile_fact(migrated, profile_id, retired.id) == before


def test_finalize_leaves_an_old_reading_nobody_decided_undecided(migrated, profile_id):
    """Turning an unanswered proposal into a refusal would be a decision."""
    seed_old_campaign(migrated, profile_id, leave_undecided=(FRAGMENT_TWO,))
    preparation = prepare(migrated, profile_id)
    answer_every_changed_reading(migrated, profile_id, preparation)

    finalization = finalize(migrated, profile_id)

    assert finalization.left_undecided == 1
    assert finalization.newly_rejected == SUPERSEDED - 1
    undecided = fact_for(
        migrated, profile_id, ProfileFactType.EXPERIENCE, FRAGMENT_TWO
    )
    assert undecided.status is FactStatus.PROPOSED


def test_finalize_reports_why_each_old_reading_was_left_alone(
    migrated, profile_id, answered
):
    finalize(migrated, profile_id)

    outcomes = finalize(migrated, profile_id).outcomes
    assert {entry.left_alone for entry in outcomes} == {ALREADY_REJECTED}
    assert TERMINAL_CORRECTED != ALREADY_REJECTED != UNDECIDED


def test_a_failure_before_the_first_rejection_leaves_every_old_reading_alone(
    migrated, profile_id, answered
):
    before = list_profile_facts(migrated, profile_id)

    def explode() -> None:
        raise RuntimeError("TEST ONLY interruption")

    with pytest.raises(RuntimeError):
        finalize(migrated, profile_id, after_precheck=explode)

    assert list_profile_facts(migrated, profile_id) == before


def test_an_interrupted_finalize_is_resumed_by_running_it_again(
    migrated, profile_id, answered
):
    """Each rejection is its own transaction, so a partial run is restartable."""
    plan = reconcile_plan(migrated, profile_id)
    reject_profile_fact(migrated, profile_id, plan.superseded[0].fact.id)

    finalization = finalize(migrated, profile_id)

    assert finalization.newly_rejected == SUPERSEDED - 1
    assert finalization.already_rejected == 1
    for entry in plan.superseded:
        assert (
            get_profile_fact(migrated, profile_id, entry.fact.id).status
            is FactStatus.REJECTED
        )


def test_the_finalization_summary_carries_no_cv_content(
    migrated, profile_id, answered
):
    rendered = json.dumps(finalize(migrated, profile_id).summary())

    for value in CV_CONTENT:
        assert value not in rendered


def test_the_outcome_summaries_carry_no_cv_content(migrated, profile_id, answered):
    finalization = finalize(migrated, profile_id)

    rendered = json.dumps(
        [entry.summary() for entry in finalization.outcomes]
        + [entry.summary() for entry in finalization.resolved]
    )
    for value in CV_CONTENT:
        assert value not in rendered


# --------------------------------------------------------------------------
# Phase 3.4A non-regression
# --------------------------------------------------------------------------


def test_the_verified_skill_facts_are_the_same_facts_afterwards(
    migrated, profile_id, seeded
):
    """Reconciling non-SKILL readings must not move a single skill fact."""
    before = list_verified_profile_facts(
        migrated, profile_id, fact_type=ProfileFactType.SKILL
    )
    assert [fact.value for fact in before] == [FIRST_SKILL]

    preparation = prepare(migrated, profile_id)
    answer_every_changed_reading(migrated, profile_id, preparation)
    finalize(migrated, profile_id)

    after = list_verified_profile_facts(
        migrated, profile_id, fact_type=ProfileFactType.SKILL
    )
    assert after == before


def test_the_skill_projection_stays_equal_to_itself_across_a_reconciliation(
    migrated, profile_id, seeded
):
    """The Phase 3.4A projection is unchanged, and nothing here ran it."""
    first = synchronize_profile_skills(migrated, profile_id)
    projected = list_profile_skills(migrated, profile_id)
    assert first.verified_skill_facts == 1

    preparation = prepare(migrated, profile_id)
    answer_every_changed_reading(migrated, profile_id, preparation)
    finalize(migrated, profile_id)

    # Read before re-synchronizing: the reconciliation must not have touched
    # the projection by itself.
    assert list_profile_skills(migrated, profile_id) == projected
    second = synchronize_profile_skills(migrated, profile_id)
    assert second.changed is False
    assert second.verified_skill_facts == first.verified_skill_facts
    assert list_profile_skills(migrated, profile_id) == projected
