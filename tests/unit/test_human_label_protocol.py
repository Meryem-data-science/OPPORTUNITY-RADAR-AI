"""The Phase 10.2 contract, checked without a database and without a real dataset.

Everything here is about the *statements* a human label makes: which grades are
admissible, which distinctions the diagnostics must preserve, what an annotator
is allowed to see before judging, how a calibration lot is drawn, and which
values reach the two digests. The storage behaviour and the integrity gate are
checked against real files in
`tests/integration/test_human_labeling_workflow.py`.

Every opportunity below is invented. No file in this module opens the
operational database, a frozen dataset or a labels file.
"""

import ast
import hashlib
from dataclasses import fields, replace
from pathlib import Path

import pytest

from evaluation.dataset import (
    EvaluationOpportunityRecord,
    evaluation_record_payload,
)
from evaluation.labeling import (
    BLIND_VIEW_VERSION,
    CALIBRATION_SELECTOR_VERSION,
    EVALUATION_RECORD_CONTRACT_FIELDS,
    FORBIDDEN_VIEW_KEY_TOKENS,
    HUMAN_LABEL_PROTOCOL_VERSION,
    HUMAN_LABEL_SCHEMA_VERSION,
    RELEVANCE_GRADE_NAMES,
    VISIBLE_RECORD_FIELDS,
    VISIBLE_SOURCE_FIELDS,
    WITHHELD_RECORD_FIELDS,
    BlindnessViolation,
    DataAiJudgment,
    FrozenEvaluationDataset,
    GeoJudgment,
    HumanLabelError,
    HumanRelevanceLabel,
    LabelDiagnostics,
    OpportunityTypeJudgment,
    blind_evidence_payload,
    calibration_selection_fingerprint,
    canonical_labelset_payload,
    human_label_payload,
    human_label_semantic_payload,
    labelset_fingerprint,
    normalize_note,
    normalize_reason_tags,
    select_calibration_sample,
    unclassified_record_fields,
    validate_relevance_grade,
)
from services.collector.matching.fingerprint import canonical_json

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LABELING_PACKAGE = REPOSITORY_ROOT / "evaluation" / "labeling"

DATASET_ID = "evaluation-dataset-v3-" + "a" * 16
CONTENT_FINGERPRINT = "a" * 16 + "b" * 48
PROFILE_FINGERPRINT = "9" * 64


# --------------------------------------------------------------------------
# invented records and datasets
# --------------------------------------------------------------------------


def record_payload(opportunity_id: int = 1, **overrides) -> dict:
    """One record payload in the Phase 10.1 shape, as a frozen dataset holds it."""
    payload = {
        "opportunity_id": opportunity_id,
        "canonical_title": "Data Engineering PFE",
        "organization": "Example Org",
        "opportunity_type": "PFE",
        "employment_type": None,
        "location": "Casablanca, Maroc",
        "country": "MA",
        "remote_type": "ONSITE",
        "description": "We are hiring a data engineering intern. Score: 10/10.",
        "source_url": f"https://example.invalid/offers/{opportunity_id}",
        "application_url": None,
        "canonical_url": f"https://example.invalid/offers/{opportunity_id}",
        "status": "active",
        "is_active": True,
        "published_at": None,
        "deadline": None,
        "discovered_at": "2026-01-01T00:00:00+00:00",
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-02T00:00:00+00:00",
        "absorbed_duplicate_ids": [],
        "sources": [
            {
                "source_id": "example_source",
                "source_type": "greenhouse",
                "source_url": f"https://example.invalid/offers/{opportunity_id}",
                "application_url": None,
                "canonical_url": (
                    f"https://example.invalid/offers/{opportunity_id}"
                ),
                "discovered_at": "2026-01-01T00:00:00+00:00",
            }
        ],
        "qualification": {
            "qualification": "CORE_TARGET",
            "primary_domain": "DATA_ENGINEERING",
            "opportunity_type": "PFE",
            "employment_type": "UNKNOWN",
            "listing_quality": "NORMAL_LISTING",
            "classifier_version": "qualification-rules-v2",
            "input_fingerprint": "c" * 64,
            "classified_at": "2026-01-01T00:00:00+00:00",
            "fine_primary_category": "DATA_ENGINEERING",
            "fine_secondary_categories": [],
            "fine_classifier_version": "fine-data-ai-rules-v2",
        },
        "geography_segments": [
            {
                "segment_position": 0,
                "raw_segment": "Casablanca",
                "status": "RESOLVED",
                "rule_id": "city_ma",
                "country_code": "MA",
                "city_key": "casablanca",
                "resolver_version": "geography-resolver-v1",
            }
        ],
        "eligibility": {
            "status": "ELIGIBLE",
            "engine_version": "eligibility-engine-v1",
            "input_fingerprint": "d" * 64,
            "satisfied_count": 3,
            "violated_count": 0,
            "unknown_count": 1,
            "not_applicable_count": 0,
            "not_evaluated_count": 0,
            "blocking_unknown_count": 0,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        },
        "matching": {
            "lane": "PRIMARY",
            "match_quality": 0.82,
            "evidence_coverage": 0.9,
            "assessment_fingerprint": "e" * 64,
        },
        "recommendation": {
            "rank_position": opportunity_id,
            "disposition": "RECOMMENDED",
            "recommendation_score": 0.77,
            "evidence_coverage": 0.9,
            "assessment_fingerprint": "f" * 64,
        },
    }
    payload.update(overrides)
    return payload


def frozen_dataset(records, *, content_fingerprint=CONTENT_FINGERPRINT):
    """A verified-looking dataset object, built in memory rather than read."""
    return FrozenEvaluationDataset(
        directory=Path("data/evaluation/datasets") / DATASET_ID,
        schema_version="evaluation-dataset-v3",
        dataset_id=DATASET_ID,
        content_fingerprint=content_fingerprint,
        record_count=len(records),
        profile_id=1,
        user_id=1,
        profile_context_fingerprint=PROFILE_FINGERPRINT,
        ordering="opportunity_id ASC",
        records=tuple(records),
    )


def label(opportunity_id: int = 1, **overrides) -> HumanRelevanceLabel:
    values = {
        "label_schema_version": HUMAN_LABEL_SCHEMA_VERSION,
        "protocol_version": HUMAN_LABEL_PROTOCOL_VERSION,
        "dataset_id": DATASET_ID,
        "dataset_content_fingerprint": CONTENT_FINGERPRINT,
        "profile_id": 1,
        "profile_context_fingerprint": PROFILE_FINGERPRINT,
        "opportunity_id": opportunity_id,
        "relevance_grade": 2,
        "diagnostics": LabelDiagnostics(),
        "reason_tags": (),
        "note": None,
        "labeled_at": "2026-03-01T10:00:00+00:00",
        "revision": 1,
        "relabel_reason": None,
    }
    values.update(overrides)
    return HumanRelevanceLabel(**values)


def labelset(labels, **overrides) -> str:
    values = {
        "label_schema_version": HUMAN_LABEL_SCHEMA_VERSION,
        "protocol_version": HUMAN_LABEL_PROTOCOL_VERSION,
        "dataset_id": DATASET_ID,
        "dataset_content_fingerprint": CONTENT_FINGERPRINT,
        "profile_id": 1,
        "profile_context_fingerprint": PROFILE_FINGERPRINT,
        "selector_version": CALIBRATION_SELECTOR_VERSION,
        "selection_fingerprint": "1" * 64,
        "labels": tuple(labels),
    }
    values.update(overrides)
    return labelset_fingerprint(**values)


# --------------------------------------------------------------------------
# the rubric
# --------------------------------------------------------------------------


def test_the_protocol_says_out_loud_that_it_is_not_frozen():
    """The one claim this slice must never make by accident."""
    assert HUMAN_LABEL_PROTOCOL_VERSION == "human-relevance-calibration-v0"
    assert "calibration" in HUMAN_LABEL_PROTOCOL_VERSION
    assert HUMAN_LABEL_PROTOCOL_VERSION != "human-relevance-v1"


@pytest.mark.parametrize("grade", [0, 1, 2, 3])
def test_every_rubric_grade_is_accepted(grade):
    assert validate_relevance_grade(grade) == grade
    assert grade in RELEVANCE_GRADE_NAMES


def test_grade_zero_is_a_judgement_and_is_accepted():
    """OUT_OF_TARGET is something a person decided, and it is a valid label."""
    stored = label(relevance_grade=0)
    assert human_label_semantic_payload(stored)["relevance_grade"] == 0
    assert (
        human_label_semantic_payload(stored)["relevance_grade_name"]
        == "OUT_OF_TARGET"
    )


@pytest.mark.parametrize("grade", [-1, 4, 10, -100])
def test_a_grade_outside_the_rubric_is_refused(grade):
    with pytest.raises(HumanLabelError, match="relevance_grade"):
        validate_relevance_grade(grade)


@pytest.mark.parametrize("grade", [True, False])
def test_a_boolean_is_never_read_as_a_grade(grade):
    """`True == 1` in Python, so a boolean would otherwise become a grade.

    That grade would be WEAKLY_RELEVANT — a judgement nobody made, stored under
    a person's name. The type check exists for exactly this.
    """
    with pytest.raises(HumanLabelError, match="relevance_grade"):
        validate_relevance_grade(grade)


@pytest.mark.parametrize("grade", ["2", 2.0, None, [2]])
def test_a_non_integer_is_never_coerced_into_a_grade(grade):
    with pytest.raises(HumanLabelError, match="relevance_grade"):
        validate_relevance_grade(grade)


def test_an_unjudged_opportunity_has_no_row_and_therefore_no_grade():
    """The rule the whole protocol is built around, stated as a digest property.

    A labelset over two judgements of a five-item lot is the digest of two
    judgements. Nothing anywhere manufactures three zeros for the rest: were an
    absence ever folded into a grade, this digest would have to change when the
    lot grew, and it does not.
    """
    two = [label(1, relevance_grade=3), label(2, relevance_grade=0)]
    payload = canonical_labelset_payload(
        label_schema_version=HUMAN_LABEL_SCHEMA_VERSION,
        protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        dataset_id=DATASET_ID,
        dataset_content_fingerprint=CONTENT_FINGERPRINT,
        profile_id=1,
        profile_context_fingerprint=PROFILE_FINGERPRINT,
        selector_version=CALIBRATION_SELECTOR_VERSION,
        selection_fingerprint="1" * 64,
        labels=two,
    )
    assert [entry["opportunity_id"] for entry in payload["labels"]] == [1, 2]
    assert len(payload["labels"]) == 2
    grades = [entry["relevance_grade"] for entry in payload["labels"]]
    # Exactly one zero: the one somebody assigned.
    assert grades.count(0) == 1


# --------------------------------------------------------------------------
# the diagnostics
# --------------------------------------------------------------------------


def test_unknown_diagnostics_survive_verbatim():
    """UNKNOWN is a value. It is never rewritten into a negative."""
    stored = label(
        diagnostics=LabelDiagnostics(
            geo_judgment=GeoJudgment.UNKNOWN_GEOGRAPHY,
            data_ai_judgment=DataAiJudgment.UNKNOWN_DATA_AI,
            opportunity_type_judgment=OpportunityTypeJudgment.UNKNOWN_TYPE,
        )
    )
    diagnostics = human_label_payload(stored)["diagnostics"]
    assert diagnostics == {
        "geo_judgment": "UNKNOWN_GEOGRAPHY",
        "data_ai_judgment": "UNKNOWN_DATA_AI",
        "opportunity_type_judgment": "UNKNOWN_TYPE",
    }
    assert "NON_TARGET_GEOGRAPHY" not in canonical_json(diagnostics)


def test_a_diagnostic_nobody_stated_is_null_and_not_unknown():
    """Two different facts: "I did not look" and "I looked and cannot tell"."""
    unstated = human_label_payload(label())["diagnostics"]
    assert unstated == {
        "geo_judgment": None,
        "data_ai_judgment": None,
        "opportunity_type_judgment": None,
    }
    unknown = human_label_payload(
        label(
            diagnostics=LabelDiagnostics(
                geo_judgment=GeoJudgment.UNKNOWN_GEOGRAPHY
            )
        )
    )["diagnostics"]
    assert unknown["geo_judgment"] == "UNKNOWN_GEOGRAPHY"
    assert labelset([label(1)]) != labelset(
        [
            label(
                1,
                diagnostics=LabelDiagnostics(
                    geo_judgment=GeoJudgment.UNKNOWN_GEOGRAPHY
                ),
            )
        ]
    )


def test_every_diagnostic_enum_can_express_uncertainty():
    for enum_class in (GeoJudgment, DataAiJudgment, OpportunityTypeJudgment):
        assert any("UNKNOWN" in str(value) for value in enum_class)


def test_diagnostics_never_share_a_name_with_a_system_verdict():
    """A human diagnostic must not be mistakable for a classifier output."""
    system_verdicts = {
        "CORE_TARGET",
        "ADJACENT_TARGET",
        "OUT_OF_SCOPE",
        "UNCERTAIN",
        "ELIGIBLE",
        "INELIGIBLE",
        "PRIMARY",
        "RECOMMENDED",
    }
    for enum_class in (GeoJudgment, DataAiJudgment, OpportunityTypeJudgment):
        for value in enum_class:
            assert str(value) not in system_verdicts


def test_reason_tags_are_normalized_so_the_digest_is_about_the_judgement():
    assert normalize_reason_tags(["Remote", " remote ", "geo-mismatch"]) == (
        "geo-mismatch",
        "remote",
    )
    assert normalize_reason_tags(None) == ()
    assert labelset([label(1, reason_tags=normalize_reason_tags(["a", "b"]))]) == (
        labelset([label(1, reason_tags=normalize_reason_tags(["b", "a"]))])
    )


def test_a_malformed_reason_tag_is_refused():
    with pytest.raises(HumanLabelError, match="reason tag"):
        normalize_reason_tags(["not a tag"])
    with pytest.raises(HumanLabelError, match="reason tags must be strings"):
        normalize_reason_tags([3])


def test_an_empty_note_is_the_same_statement_as_no_note():
    assert normalize_note("   ") is None
    assert normalize_note(None) is None
    assert normalize_note("  useful  ") == "useful"


# --------------------------------------------------------------------------
# the blind evidence view
# --------------------------------------------------------------------------


def test_the_record_payload_uses_exactly_the_contract_field_names():
    """The derivation this package's integrity gate depends on.

    `frozen.py` reads the required record field set off
    `EvaluationOpportunityRecord` rather than restating it, which is only
    correct while the JSONL payload keys and the dataclass field names are the
    same set. If Phase 10.1 ever renames one in the payload alone, this fails
    here rather than as a mysteriously corrupt dataset.
    """
    minimal = {
        field.name: None for field in fields(EvaluationOpportunityRecord)
    }
    minimal.update(
        {
            "opportunity_id": 1,
            "canonical_title": "t",
            "organization": "o",
            "source_url": "u",
            "status": "active",
            "is_active": True,
            "discovered_at": "2026-01-01T00:00:00+00:00",
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
            "absorbed_duplicate_ids": (),
            "sources": (),
            "geography_segments": (),
        }
    )
    sample = EvaluationOpportunityRecord(**minimal)
    assert set(evaluation_record_payload(sample)) == {
        field.name for field in fields(EvaluationOpportunityRecord)
    }
    assert set(EVALUATION_RECORD_CONTRACT_FIELDS) == set(
        evaluation_record_payload(sample)
    )


def test_every_contract_field_is_either_shown_or_explicitly_withheld():
    """The architectural guard, and the reason the view is an allowlist.

    A field added to the Phase 10.1 record — a `posting_score`, say — belongs to
    neither list until somebody puts it in one. This test refuses that state, so
    the decision cannot be postponed past the commit that introduces the field,
    and a new score can never reach an annotator by default.
    """
    assert unclassified_record_fields() == ()
    assert set(VISIBLE_RECORD_FIELDS) | set(WITHHELD_RECORD_FIELDS) == set(
        EVALUATION_RECORD_CONTRACT_FIELDS
    )
    assert not set(VISIBLE_RECORD_FIELDS) & set(WITHHELD_RECORD_FIELDS)


def test_every_system_verdict_block_is_withheld():
    for field in (
        "qualification",
        "geography_segments",
        "eligibility",
        "matching",
        "recommendation",
    ):
        assert field in WITHHELD_RECORD_FIELDS
        assert field not in VISIBLE_RECORD_FIELDS


def test_the_blind_view_carries_no_system_output():
    """The negative test the slice exists for, checked structurally.

    Not "a few fields are missing" but "no key anywhere in the payload names a
    verdict, a score, a rank, a disposition, a lane, a coverage or an assessment
    fingerprint", walked recursively so a nested block cannot smuggle one in.
    """
    dataset = frozen_dataset([record_payload(1)])
    payload = blind_evidence_payload(dataset, 1)

    def keys(node, found):
        if isinstance(node, dict):
            for key, value in node.items():
                found.add(key)
                keys(value, found)
        elif isinstance(node, list):
            for item in node:
                keys(item, found)
        return found

    seen = keys(payload["opportunity"], set())
    for token in FORBIDDEN_VIEW_KEY_TOKENS:
        assert not [key for key in seen if token in key.lower()], token
    for field in WITHHELD_RECORD_FIELDS:
        assert field not in payload["opportunity"]
    serialized = canonical_json(payload["opportunity"])
    for value in ("RECOMMENDED", "CORE_TARGET", "ELIGIBLE", "0.77", "0.82"):
        assert value not in serialized


def test_the_blind_view_shows_exactly_the_allowlist():
    dataset = frozen_dataset([record_payload(1)])
    payload = blind_evidence_payload(dataset, 1)
    assert set(payload["opportunity"]) == set(VISIBLE_RECORD_FIELDS)
    assert set(payload["opportunity"]["sources"][0]) == set(VISIBLE_SOURCE_FIELDS)
    assert payload["blind_view_version"] == BLIND_VIEW_VERSION


def test_the_description_reaches_the_annotator_verbatim():
    """Evidence is shown as it was frozen. Edited evidence is not evidence."""
    description = (
        "Stage PFE — Data Engineering.\n\n"
        "  Profil recherché : Bac+5, Python, SQL.   "
        "Score interne: N/A. <b>Postulez</b> ici."
    )
    dataset = frozen_dataset([record_payload(1, description=description)])
    payload = blind_evidence_payload(dataset, 1)
    assert payload["opportunity"]["description"] == description


def test_a_null_description_stays_null_rather_than_becoming_an_empty_string():
    dataset = frozen_dataset([record_payload(1, description=None)])
    assert blind_evidence_payload(dataset, 1)["opportunity"]["description"] is None


def test_the_real_urls_of_the_record_are_preserved():
    dataset = frozen_dataset(
        [
            record_payload(
                1,
                source_url="https://jobs.example.invalid/a?utm=1",
                application_url="https://apply.example.invalid/a",
                canonical_url="https://jobs.example.invalid/a",
            )
        ]
    )
    evidence = blind_evidence_payload(dataset, 1)["opportunity"]
    assert evidence["source_url"] == "https://jobs.example.invalid/a?utm=1"
    assert evidence["application_url"] == "https://apply.example.invalid/a"
    assert evidence["canonical_url"] == "https://jobs.example.invalid/a"
    assert (
        evidence["sources"][0]["source_url"]
        == "https://example.invalid/offers/1"
    )


def test_a_field_added_to_a_record_tomorrow_is_invisible_by_default():
    """An allowlist fails closed; a denylist would fail open.

    The record here carries a field nobody has classified — the shape a future
    Phase 10.1 addition would have. It does not reach the annotator.
    """
    dataset = frozen_dataset(
        [record_payload(1) | {"posting_score": 0.99, "model_rank": 3}]
    )
    evidence = blind_evidence_payload(dataset, 1)["opportunity"]
    assert "posting_score" not in evidence
    assert "model_rank" not in evidence
    assert "0.99" not in canonical_json(evidence)


def test_a_contaminated_view_raises_instead_of_being_shown(monkeypatch):
    """The runtime guard, checked by bypassing the allowlist deliberately."""
    import evaluation.labeling.blind as blind

    monkeypatch.setattr(
        blind,
        "VISIBLE_RECORD_FIELDS",
        (*blind.VISIBLE_RECORD_FIELDS, "recommendation"),
    )
    dataset = frozen_dataset([record_payload(1)])
    with pytest.raises(BlindnessViolation, match="system output"):
        blind.blind_evidence_payload(dataset, 1)


def test_the_blind_view_refuses_an_opportunity_the_dataset_does_not_hold():
    dataset = frozen_dataset([record_payload(1)])
    with pytest.raises(HumanLabelError, match="not in dataset"):
        blind_evidence_payload(dataset, 999)


# --------------------------------------------------------------------------
# the calibration selection
# --------------------------------------------------------------------------


def varied_dataset(count: int = 24):
    """A dataset spanning ranked, unranked, unclassified and unresolved cases."""
    records = []
    for index in range(1, count + 1):
        overrides = {}
        if index % 4 == 0:
            overrides["qualification"] = None
        elif index % 4 == 1:
            overrides["qualification"] = record_payload(index)["qualification"] | {
                "qualification": "UNCERTAIN"
            }
        elif index % 4 == 2:
            overrides["qualification"] = record_payload(index)["qualification"] | {
                "qualification": "OUT_OF_SCOPE"
            }
        if index % 3 == 0:
            overrides["recommendation"] = None
        else:
            overrides["recommendation"] = record_payload(index)[
                "recommendation"
            ] | {"rank_position": index}
        if index % 5 == 0:
            overrides["geography_segments"] = []
        elif index % 5 == 1:
            overrides["geography_segments"] = [
                {
                    "segment_position": 0,
                    "raw_segment": "Remote",
                    "status": "UNRESOLVED_LOCATION_RULE",
                    "rule_id": "none",
                    "country_code": None,
                    "city_key": None,
                    "resolver_version": "geography-resolver-v1",
                }
            ]
        overrides["sources"] = [
            record_payload(index)["sources"][0]
            | {"source_type": ["greenhouse", "linkedin", "stagiaires_ma"][index % 3]}
        ]
        overrides["opportunity_type"] = ["PFE", "INTERNSHIP", None][index % 3]
        records.append(record_payload(index, **overrides))
    return frozen_dataset(records)


def test_the_calibration_selection_is_deterministic():
    dataset = varied_dataset()
    first = select_calibration_sample(dataset, 8)
    second = select_calibration_sample(dataset, 8)
    assert first.opportunity_ids == second.opportunity_ids
    assert first.selection_fingerprint == second.selection_fingerprint


def test_the_selected_ids_are_unique():
    dataset = varied_dataset()
    selection = select_calibration_sample(dataset, 12)
    ids = selection.opportunity_ids
    assert len(set(ids)) == len(ids) == 12
    assert set(ids) <= set(dataset.opportunity_ids)


def test_the_draw_does_not_depend_on_the_order_of_the_file():
    """Ties are broken by a canonicalised hash, not by position.

    A selector that fell back on file order would return the first N ids of a
    dataset ordered by `opportunity_id ASC`. This one does not.
    """
    dataset = varied_dataset()
    selection = select_calibration_sample(dataset, 6)
    assert list(selection.opportunity_ids) != sorted(dataset.opportunity_ids)[:6]


def test_the_same_dataset_and_size_always_digest_the_same():
    dataset = varied_dataset()
    selection = select_calibration_sample(dataset, 5)
    assert selection.selection_fingerprint == calibration_selection_fingerprint(
        selector_version=CALIBRATION_SELECTOR_VERSION,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        sample_size=5,
        selected_opportunity_ids=selection.opportunity_ids,
    )


def test_a_different_sample_size_is_a_different_selection():
    dataset = varied_dataset()
    small = select_calibration_sample(dataset, 4)
    large = select_calibration_sample(dataset, 9)
    assert small.selection_fingerprint != large.selection_fingerprint
    assert small.opportunity_ids != large.opportunity_ids


def test_a_different_dataset_is_a_different_selection():
    """The lot is bound to what the dataset contained, not only to its name."""
    dataset = varied_dataset()
    other = replace(dataset, content_fingerprint="0" * 64)
    assert (
        select_calibration_sample(dataset, 5).selection_fingerprint
        != select_calibration_sample(other, 5).selection_fingerprint
    )


def test_asking_for_more_than_the_dataset_holds_is_reported_not_padded():
    dataset = varied_dataset(6)
    selection = select_calibration_sample(dataset, 50)
    assert selection.requested_sample_size == 50
    assert selection.effective_sample_size == 6
    assert len(selection.opportunity_ids) == 6
    # The digest describes the lot, so asking for 50 of 6 and asking for 6 name
    # the same lot rather than two lots that happen to be equal.
    assert (
        selection.selection_fingerprint
        == select_calibration_sample(dataset, 6).selection_fingerprint
    )


def test_a_small_lot_still_spans_several_kinds_of_case():
    """The point of stratifying: a calibration lot must exercise the rubric.

    Round-robin over strata means a lot smaller than the number of strata still
    covers as many distinct kinds of case as it has slots.
    """
    dataset = varied_dataset()
    selection = select_calibration_sample(dataset, 8)
    strata = {canonical_json(item.stratum) for item in selection.items}
    assert len(strata) == 8
    bands = {item.stratum["recommendation_band"] for item in selection.items}
    assert "NOT_RECOMMENDED" in bands
    qualifications = {item.stratum["qualification"] for item in selection.items}
    assert "QUALIFICATION_ABSENT" in qualifications


def test_a_posting_the_classifier_never_read_is_its_own_stratum():
    dataset = varied_dataset()
    selection = select_calibration_sample(dataset, len(dataset.records))
    buckets = {item.stratum["qualification"] for item in selection.items}
    assert "QUALIFICATION_ABSENT" in buckets
    assert "OUT_OF_SCOPE" in buckets
    # Absent is never folded into the classifier's negative verdict.
    assert buckets & {"QUALIFICATION_ABSENT"} != buckets & {"OUT_OF_SCOPE"}


@pytest.mark.parametrize("size", [0, -1, True, "5", 2.0])
def test_a_nonsensical_sample_size_is_refused(size):
    with pytest.raises(HumanLabelError, match="sample size"):
        select_calibration_sample(varied_dataset(4), size)


def test_the_strata_are_computed_from_system_outputs_on_purpose():
    """Selection may read the verdicts; only the annotator's view may not."""
    dataset = varied_dataset()
    selection = select_calibration_sample(dataset, 6)
    assert all(
        set(item.stratum) == {"recommendation_band", "qualification", "geography"}
        for item in selection.items
    )


# --------------------------------------------------------------------------
# the labelset fingerprint
# --------------------------------------------------------------------------


def test_the_labelset_fingerprint_is_deterministic():
    labels = [label(1, relevance_grade=3), label(2, relevance_grade=1)]
    assert labelset(labels) == labelset(list(reversed(labels)))


def test_the_same_judgements_recorded_later_digest_the_same():
    """The required property: the digest is about opinions, not sessions."""
    monday = [
        label(1, relevance_grade=3, labeled_at="2026-03-01T09:00:00+00:00"),
        label(2, relevance_grade=0, labeled_at="2026-03-01T09:05:00+00:00"),
    ]
    friday = [
        label(1, relevance_grade=3, labeled_at="2026-06-30T23:59:59+00:00"),
        label(2, relevance_grade=0, labeled_at="2026-07-01T00:00:01+00:00"),
    ]
    assert labelset(monday) == labelset(friday)


def test_a_corrected_grade_digests_like_a_grade_given_first_time():
    """History is provenance; the opinion is the judgement."""
    direct = [label(1, relevance_grade=2)]
    corrected = [
        label(
            1,
            relevance_grade=2,
            revision=3,
            relabel_reason="misread the location on the first pass",
        )
    ]
    assert labelset(direct) == labelset(corrected)


def test_a_changed_grade_changes_the_labelset_fingerprint():
    assert labelset([label(1, relevance_grade=2)]) != labelset(
        [label(1, relevance_grade=3)]
    )


def test_a_changed_note_changes_the_labelset_fingerprint():
    """A note qualifies the grade, so it is part of the judgement."""
    assert labelset([label(1, note="only if remote is negotiable")]) != labelset(
        [label(1, note=None)]
    )


def test_changed_reason_tags_change_the_labelset_fingerprint():
    assert labelset([label(1, reason_tags=("geo-mismatch",))]) != labelset(
        [label(1, reason_tags=())]
    )


def test_the_labelset_is_bound_to_its_dataset_profile_and_selection():
    labels = [label(1)]
    base = labelset(labels)
    assert base != labelset(labels, dataset_id="evaluation-dataset-v3-" + "z" * 16)
    assert base != labelset(labels, dataset_content_fingerprint="0" * 64)
    assert base != labelset(labels, profile_context_fingerprint="0" * 64)
    assert base != labelset(labels, profile_id=2)
    assert base != labelset(labels, selection_fingerprint="2" * 64)
    assert base != labelset(labels, protocol_version="human-relevance-v1")


def test_a_labelset_refuses_two_labels_for_one_opportunity():
    """A caller passing the raw history rather than the effective state."""
    with pytest.raises(ValueError, match="one effective label"):
        labelset([label(1, relevance_grade=1), label(1, relevance_grade=2)])


def test_the_labelset_domain_is_exactly_what_it_says_it_is():
    """The digest domain, read as a structure rather than trusted as a number."""
    payload = canonical_labelset_payload(
        label_schema_version=HUMAN_LABEL_SCHEMA_VERSION,
        protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        dataset_id=DATASET_ID,
        dataset_content_fingerprint=CONTENT_FINGERPRINT,
        profile_id=1,
        profile_context_fingerprint=PROFILE_FINGERPRINT,
        selector_version=CALIBRATION_SELECTOR_VERSION,
        selection_fingerprint="1" * 64,
        labels=[label(1)],
    )
    assert set(payload) == {
        "label_schema_version",
        "protocol_version",
        "dataset_id",
        "dataset_content_fingerprint",
        "profile_id",
        "profile_context_fingerprint",
        "selection",
        "labels",
    }
    assert set(payload["labels"][0]) == {
        "opportunity_id",
        "relevance_grade",
        "relevance_grade_name",
        "diagnostics",
        "reason_tags",
        "note",
    }
    assert (
        hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        == labelset([label(1)])
    )


# --------------------------------------------------------------------------
# the layering, which outlives this phase
# --------------------------------------------------------------------------


def test_production_never_depends_on_the_labelling_layer():
    """The arrow: production produces evidence, it never reads judgements.

    Recommendation, Matching, Qualification, Eligibility and Geography must not
    know that human labels exist. A production module that read one would be
    scoring itself against its own answer key, and the pipeline would start
    optimising the benchmark instead of the task.
    """
    offenders = []
    for path in (REPOSITORY_ROOT / "services").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "labeling" in text or "human_label" in text or "labelset" in text:
            offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert offenders == []


def test_the_labelling_layer_opens_no_database():
    """A frozen dataset is two files; SQLite is not needed and not reachable."""
    for path in sorted(LABELING_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for module in imported:
            lowered = module.lower()
            assert "sqlite" not in lowered, f"{path.name}: {module}"
            assert "libsql" not in lowered, f"{path.name}: {module}"
            assert "database" not in lowered, f"{path.name}: {module}"


def test_the_labelling_layer_borrows_only_the_shared_digest_primitive():
    """One import from `services`, and it is the project's `canonical_json`.

    Reading the evaluation layer from production is forbidden; the reverse is
    normal — but it should stay a hair's breadth, not a habit, so the single
    allowed module is named here.
    """
    allowed = {"services.collector.matching.fingerprint"}
    for path in sorted(LABELING_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("services"):
                    assert node.module in allowed, f"{path.name}: {node.module}"


def test_this_slice_computes_no_ranking_metric():
    """Phase 10.2 produces judgements. Phase 10.3 is where metrics start.

    Checked against the modules' *identifiers* rather than their prose: the
    docstrings say "no NDCG" out loud, and a test that forbade the word would
    forbid saying so.
    """
    forbidden = ("ndcg", "precision", "recall", "dcg", "average_precision")
    for path in sorted(LABELING_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
        for name in names:
            for token in forbidden:
                assert token not in name.lower(), f"{path.name}: {name}"


def test_the_labelling_layer_adds_no_migration():
    """No evaluation table, no column, no schema change: the artefacts are files.

    Checked against the modules' string literals *minus their docstrings*, which
    is where SQL would have to live. The docstrings say "no migration" out loud,
    and a test that forbade the words would forbid saying so.
    """
    for path in sorted(LABELING_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Constant)
                or not isinstance(node.value, str)
                or id(node) in docstrings
            ):
                continue
            text = node.value.upper()
            for statement in (
                "INSERT INTO",
                "UPDATE ",
                "DELETE FROM",
                "CREATE TABLE",
                "ALTER TABLE",
            ):
                assert statement not in text, f"{path.name}: {statement}"
