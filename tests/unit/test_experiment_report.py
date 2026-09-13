"""The two derived views: the error analysis, and the Markdown report.

Neither is authoritative, neither is stored, and neither may invent anything.
The rules with the sharpest edges are that UNJUDGED is never an error, that an
`N_A` stays visible with its reason, and that no baseline, no global score and
no fifth metric appears anywhere.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from evaluation.business_metrics import BusinessMetricUnavailableReason
from evaluation.experiments import (
    CohortProjection,
    ExperimentBindingError,
    ExperimentReport,
    RankingExperimentStatus,
    build_error_analysis,
    build_experiment_report,
    build_experiment_run,
    experiment_run_fingerprint,
    render_experiment_report_markdown,
)
from evaluation.experiments.error_analysis import (
    ERROR_ANALYSIS_EFFECTIVE_K,
    SkillDiagnosticStatus,
)
from evaluation.metrics import EvidenceClass

from tests.unit.experiment_fixtures import (
    OTHER_COUNTRY,
    coverage_of,
    dataset_of,
    experiment_context,
    fully_judged,
    matching,
    qualification,
    ranked_records,
    record,
    recommendation,
    segment,
)


def analysis_of(records=None, **kwargs):
    context = experiment_context(records, **kwargs)
    return build_error_analysis(build_experiment_run(context), context), context


def report_of(records=None, **kwargs):
    context = experiment_context(records, **kwargs)
    run = build_experiment_run(context)
    report = build_experiment_report(run, context)
    return report, render_experiment_report_markdown(report), context


# ====================================================================
# both views full-verify before saying anything
# ====================================================================


def test_the_error_analysis_full_verifies_first() -> None:
    context = experiment_context(ranked_records(size=10, ranked=6))
    run = build_experiment_run(context)
    forged = replace(
        run,
        projections=(
            *run.projections[:4],
            replace(
                run.projections[4],
                included_opportunity_ids=run.projections[4].included_ids[:-1],
            ),
        ),
    )
    forged = replace(forged, run_fingerprint=experiment_run_fingerprint(forged))
    with pytest.raises(Exception):
        build_error_analysis(forged, context)


def test_the_report_full_verifies_first() -> None:
    context = experiment_context(ranked_records(size=10, ranked=6))
    run = build_experiment_run(context)
    other = experiment_context(ranked_records(size=9, ranked=5))
    with pytest.raises(ExperimentBindingError):
        build_experiment_report(run, other)


def test_neither_view_is_stored_or_fingerprinted() -> None:
    """D53/D54: no report fingerprint, no content-addressed store."""
    from evaluation.experiments import ErrorAnalysis

    assert "report_fingerprint" not in ExperimentReport.__dataclass_fields__
    assert "fingerprint" not in ErrorAnalysis.__dataclass_fields__
    import evaluation.experiments.report as module

    assert not hasattr(module, "write_experiment_report")
    assert not hasattr(module, "read_experiment_report")


# ====================================================================
# UNJUDGED is never an error
# ====================================================================


def test_an_unjudged_posting_in_the_top_k_is_not_a_false_positive() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    # Only posting 1 judged; 2..6 of the top K carry no judgement at all.
    analysis, _ = analysis_of(records, coverage=coverage_of({1: 3}, dataset))
    candidates = {
        item.opportunity_id for item in analysis.ranking.false_positive_candidates
    }
    assert candidates == set()
    assert set(analysis.ranking.unjudged_in_top_k) == {2, 3, 4, 5, 6}


def test_an_unjudged_posting_outside_the_top_k_is_not_a_false_negative() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    analysis, _ = analysis_of(records, coverage=coverage_of({1: 3}, dataset))
    assert analysis.ranking.false_negative_candidates == ()


def test_an_unjudged_posting_never_enters_an_inversion() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    analysis, _ = analysis_of(records, coverage=coverage_of({1: 0, 3: 3}, dataset))
    for inversion in analysis.ranking.judged_ranking_inversions:
        assert inversion.higher_ranked_opportunity_id in {1, 3}
        assert inversion.lower_ranked_opportunity_id in {1, 3}


# ====================================================================
# the two candidate lists
# ====================================================================


def test_a_judged_low_grade_in_the_top_k_is_a_false_positive_candidate() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    grades = {index: (3 if index <= 2 else 0) for index in range(1, 11)}
    analysis, _ = analysis_of(records, coverage=coverage_of(grades, dataset))
    candidates = {
        item.opportunity_id: item.relevance_grade
        for item in analysis.ranking.false_positive_candidates
    }
    assert candidates == {3: 0, 4: 0, 5: 0, 6: 0}


@pytest.mark.parametrize("grade", [0, 1])
def test_grades_zero_and_one_in_the_top_k_are_candidates(grade) -> None:
    records = ranked_records(size=8, ranked=4)
    dataset = dataset_of(records)
    grades = {index: grade for index in range(1, 9)}
    analysis, _ = analysis_of(records, coverage=coverage_of(grades, dataset))
    assert {
        item.opportunity_id for item in analysis.ranking.false_positive_candidates
    } == {1, 2, 3, 4}


@pytest.mark.parametrize("grade", [2, 3])
def test_a_relevant_grade_in_the_top_k_is_never_a_candidate(grade) -> None:
    records = ranked_records(size=8, ranked=4)
    dataset = dataset_of(records)
    grades = {index: grade for index in range(1, 9)}
    analysis, _ = analysis_of(records, coverage=coverage_of(grades, dataset))
    assert analysis.ranking.false_positive_candidates == ()


def test_a_relevant_posting_the_run_never_ranked_is_a_false_negative() -> None:
    """The case the whole module exists for: invisible from the output."""
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    grades = {index: (3 if index in (9, 10) else 0) for index in range(1, 11)}
    analysis, _ = analysis_of(records, coverage=coverage_of(grades, dataset))
    candidates = {
        item.opportunity_id: item.rank_position
        for item in analysis.ranking.false_negative_candidates
    }
    assert candidates == {9: None, 10: None}


def test_a_relevant_posting_ranked_below_the_cut_off_is_a_false_negative() -> None:
    records = ranked_records(size=20, ranked=14)
    dataset = dataset_of(records)
    grades = {index: (3 if index == 12 else 0) for index in range(1, 21)}
    analysis, _ = analysis_of(records, coverage=coverage_of(grades, dataset))
    candidates = {
        item.opportunity_id: item.rank_position
        for item in analysis.ranking.false_negative_candidates
    }
    assert candidates == {12: 12}


def test_the_cut_off_is_the_effective_top_ten() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    analysis, _ = analysis_of(records, coverage=fully_judged(dataset))
    assert analysis.ranking.k_requested == ERROR_ANALYSIS_EFFECTIVE_K == 10
    assert analysis.ranking.k_effective == 6
    assert analysis.ranking.ranking_length == 6


# ====================================================================
# ranking inversions
# ====================================================================


def test_a_better_ranked_worse_graded_pair_is_an_inversion() -> None:
    records = ranked_records(size=6, ranked=4)
    dataset = dataset_of(records)
    # Ranked 1,2,3,4 with grades 0,3,0,0: (1,2) is an inversion.
    grades = {1: 0, 2: 3, 3: 0, 4: 0, 5: 0, 6: 0}
    analysis, _ = analysis_of(records, coverage=coverage_of(grades, dataset))
    pairs = {
        (item.higher_ranked_opportunity_id, item.lower_ranked_opportunity_id)
        for item in analysis.ranking.judged_ranking_inversions
    }
    assert (1, 2) in pairs
    assert (2, 3) not in pairs


def test_a_correctly_ordered_ranking_has_no_inversion() -> None:
    records = ranked_records(size=6, ranked=4)
    dataset = dataset_of(records)
    grades = {1: 3, 2: 3, 3: 1, 4: 0, 5: 0, 6: 0}
    analysis, _ = analysis_of(records, coverage=coverage_of(grades, dataset))
    assert analysis.ranking.judged_ranking_inversions == ()


def test_inversions_are_deterministic_and_in_rank_order() -> None:
    records = ranked_records(size=8, ranked=6)
    dataset = dataset_of(records)
    grades = {1: 0, 2: 1, 3: 3, 4: 0, 5: 2, 6: 3, 7: 0, 8: 0}
    first, _ = analysis_of(records, coverage=coverage_of(grades, dataset))
    second, _ = analysis_of(records, coverage=coverage_of(grades, dataset))
    assert (
        first.ranking.judged_ranking_inversions
        == second.ranking.judged_ranking_inversions
    )
    for item in first.ranking.judged_ranking_inversions:
        assert item.higher_rank_position < item.lower_rank_position
        assert item.higher_relevance_grade < item.lower_relevance_grade


def test_the_analysis_computes_no_rate_of_any_kind() -> None:
    """D54: no FP rate, no FN rate, no inversion rate, no Kendall."""
    from evaluation.experiments import (
        ErrorAnalysis,
        FalseNegativeCandidate,
        FalsePositiveCandidate,
        GeoDiagnostics,
        JudgedRankingInversion,
        RankingDiagnostics,
        SourceDiagnostics,
    )

    fields = set()
    for shape in (
        ErrorAnalysis,
        RankingDiagnostics,
        FalsePositiveCandidate,
        FalseNegativeCandidate,
        JudgedRankingInversion,
        GeoDiagnostics,
        SourceDiagnostics,
    ):
        fields |= set(shape.__dataclass_fields__)
    for forbidden in ("rate", "ratio", "kendall", "tau", "score", "precision"):
        assert not any(forbidden in name.lower() for name in fields), forbidden


# ====================================================================
# geography diagnostics — AMBIGUOUS is not UNKNOWN
# ====================================================================


def test_ambiguous_and_unknown_locations_are_separate_lists() -> None:
    records = (
        record(
            1,
            geography_segments=(segment("AMBIGUOUS"),),
            qualification=qualification(),
            matching=matching(),
            recommendation=recommendation(1),
        ),
        record(
            2,
            geography_segments=(segment("UNKNOWN"),),
            qualification=qualification(),
            matching=matching(),
            recommendation=recommendation(2),
        ),
        record(
            3,
            geography_segments=(segment("RESOLVED"),),
            qualification=qualification(),
            matching=matching(),
            recommendation=recommendation(3),
        ),
    )
    analysis, _ = analysis_of(records)
    assert analysis.geo.ambiguous_location_opportunity_ids == (1,)
    assert analysis.geo.unknown_location_opportunity_ids == (2,)
    assert 1 not in analysis.geo.unknown_location_opportunity_ids
    assert 2 not in analysis.geo.ambiguous_location_opportunity_ids


def test_a_record_with_no_segment_is_unknown_and_named_separately() -> None:
    records = (
        record(
            1,
            geography_segments=(),
            qualification=qualification(),
            matching=matching(),
            recommendation=recommendation(1),
        ),
    )
    analysis, _ = analysis_of(records)
    assert analysis.geo.no_geography_segment_opportunity_ids == (1,)
    assert analysis.geo.unknown_location_opportunity_ids == (1,)
    assert analysis.geo.ambiguous_location_opportunity_ids == ()


def test_the_out_of_target_list_is_the_runs_own_excluded_set() -> None:
    records = ranked_records(size=8, ranked=5, out_of_target=(7,))
    analysis, context = analysis_of(records)
    run = build_experiment_run(context)
    geo = run.projection(CohortProjection.GEO_NOT_EXPLICITLY_OUT_OF_TARGET)
    assert analysis.geo.out_of_target_opportunity_ids == (7,)
    assert 7 not in geo.included_ids


def test_an_unavailable_target_states_no_out_of_target_list() -> None:
    """`None`, not an empty list: an empty list would be a finding."""
    analysis, _ = analysis_of(
        ranked_records(size=8, ranked=5), target_country=None
    )
    assert analysis.geo.target_country is None
    assert analysis.geo.out_of_target_opportunity_ids is None


# ====================================================================
# source and skill diagnostics reuse existing truth
# ====================================================================


def test_the_source_diagnostics_report_facts_from_the_frozen_records() -> None:
    analysis, _ = analysis_of(ranked_records(size=6, ranked=4))
    assert analysis.sources.observed_source_ids == ("alpha_board",)
    assert analysis.sources.records_per_source == {"alpha_board": 6}


def test_no_url_audit_reuses_phase_10_4s_own_reason() -> None:
    """D55: no new reason code is invented for a URL."""
    analysis, _ = analysis_of(ranked_records(size=6, ranked=4))
    assert analysis.sources.url_evidence_reason is (
        BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING
    )
    assert analysis.sources.url_outcome_counts == {}


def test_the_skill_diagnostic_is_na_with_the_official_existing_reason() -> None:
    analysis, _ = analysis_of(ranked_records(size=6, ranked=4))
    assert analysis.skills.status is SkillDiagnosticStatus.N_A
    assert analysis.skills.unavailable_reason is (
        BusinessMetricUnavailableReason.OPPORTUNITY_SKILLS_NOT_FROZEN
    )


def test_no_new_skill_reason_code_is_minted() -> None:
    """The name appears only in the docstring that forbids it, never as code.

    Checked structurally rather than by grepping the text, because the module
    deliberately *mentions* the code it refuses to mint — a grep would forbid
    saying so.
    """
    import ast
    import pathlib

    names = {str(item) for item in BusinessMetricUnavailableReason}
    assert "SKILL_LEVEL_EVIDENCE_NOT_FROZEN" not in names

    tree = ast.parse(
        pathlib.Path("evaluation/experiments/error_analysis.py").read_text()
    )
    # Docstring nodes are excluded by identity: `ast.get_docstring` returns the
    # *cleaned* text while the literal node holds the raw one, so comparing the
    # strings would never match.
    docstring_nodes = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", ())
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstring_nodes.add(id(body[0].value))

    identifiers = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]
    assert "SKILL_LEVEL_EVIDENCE_NOT_FROZEN" not in identifiers
    assert not any(
        "SKILL_LEVEL_EVIDENCE_NOT_FROZEN" in literal for literal in literals
    )

    # And this package defines no reason enum of its own for skills at all.
    from evaluation.experiments import error_analysis as module

    assert not hasattr(module, "SkillUnavailableReason")


# ====================================================================
# the evidence identity travels with the judgement-dependent diagnostics
# ====================================================================


def test_the_analysis_names_the_labelset_its_candidates_are_about() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    coverage = fully_judged(dataset)
    analysis, context = analysis_of(records, coverage=coverage)
    run = context.ranking_evaluation_inputs.evaluation_run
    assert analysis.labelset_fingerprint == run.labelset_fingerprint
    assert analysis.label_protocol_version == run.label_protocol_version
    assert analysis.evidence_class is EvidenceClass.DIAGNOSTIC_CALIBRATION


def test_a_snapshot_with_no_ranking_has_no_ranking_diagnostics() -> None:
    analysis, _ = analysis_of(
        ranked_records(size=6, ranked=0), with_ranking=False
    )
    assert analysis.ranking is None
    assert analysis.labelset_fingerprint is None
    assert analysis.evidence_class is None
    # The cohort diagnostics still apply.
    assert analysis.geo.target_country is not None
    assert analysis.skills.status is SkillDiagnosticStatus.N_A


def test_the_analysis_is_deterministic_with_no_sampling() -> None:
    records = ranked_records(size=12, ranked=8)
    dataset = dataset_of(records)
    coverage = fully_judged(dataset)
    first, _ = analysis_of(records, coverage=coverage)
    second, _ = analysis_of(records, coverage=coverage)
    assert first == second


# ====================================================================
# the Markdown report
# ====================================================================


def test_the_report_shows_all_six_sections() -> None:
    _, markdown, _ = report_of(ranked_records(size=10, ranked=6))
    for heading in (
        "## 1. Identities and versions",
        "## 2. Business and data-quality metrics (Phase 10.4)",
        "## 3. Cohort projections",
        "## 4. Projection overlaps",
        "## 5. Observed ranking experiment",
        "## 6. Error analysis",
    ):
        assert heading in markdown


def test_the_report_shows_every_phase_10_4_result() -> None:
    report, markdown, _ = report_of(ranked_records(size=8, ranked=5))
    for result in report.business_metric_run.results:
        assert str(result.key.metric) in markdown


def test_the_report_shows_the_five_projections_and_six_overlaps() -> None:
    _, markdown, _ = report_of(ranked_records(size=8, ranked=5))
    from evaluation.experiments import OVERLAP_PAIR_ORDER, PROJECTION_ORDER

    for projection in PROJECTION_ORDER:
        assert str(projection) in markdown
    for pair in OVERLAP_PAIR_ORDER:
        assert str(pair) in markdown


def test_the_report_shows_the_four_ranking_questions() -> None:
    _, markdown, _ = report_of(ranked_records(size=10, ranked=6))
    for question in (
        "PRECISION_AT_K@5",
        "PRECISION_AT_K@10",
        "RECALL_AT_K@10",
        "NDCG_AT_K@10",
    ):
        assert question in markdown


def test_an_na_projection_is_visible_with_its_reason() -> None:
    _, markdown, _ = report_of(
        ranked_records(size=8, ranked=5), target_country=None
    )
    assert "GEO_NOT_EXPLICITLY_OUT_OF_TARGET" in markdown
    assert "TARGET_COUNTRY_UNAVAILABLE" in markdown
    assert "INPUT_PROJECTION_UNAVAILABLE" in markdown


def test_an_na_ranking_block_is_visible_with_its_reason() -> None:
    _, markdown, _ = report_of(
        ranked_records(size=6, ranked=0), with_ranking=False
    )
    assert "NO_OBSERVED_RECOMMENDATION_RANKING" in markdown
    assert "no observed ordering to evaluate" in markdown


def test_an_na_metric_reason_is_visible() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    _, markdown, _ = report_of(records, coverage=coverage_of({1: 3}, dataset))
    assert "TOP_K_NOT_FULLY_JUDGED" in markdown


def test_the_report_invents_no_baseline_ranking() -> None:
    _, markdown, _ = report_of(ranked_records(size=10, ranked=6))
    lowered = markdown.lower()
    for forbidden in ("baseline", "random ranking", "control ranking"):
        if forbidden == "baseline":
            # The word appears once, in the sentence saying there is none.
            assert "no baseline ranking" in lowered
            assert lowered.count("baseline") == 1
        else:
            assert forbidden not in lowered


def test_the_report_states_no_global_or_quality_score() -> None:
    _, markdown, _ = report_of(ranked_records(size=10, ranked=6))
    lowered = markdown.lower()
    for forbidden in ("overall score", "global score", "quality score"):
        if forbidden == "overall score":
            assert "states no overall score" in lowered
        else:
            assert forbidden not in lowered


def test_the_report_uses_no_causal_vocabulary() -> None:
    _, markdown, _ = report_of(ranked_records(size=10, ranked=6))
    lowered = markdown.lower()
    for forbidden in (
        "conversion",
        "drop-off",
        "dropoff",
        "retention",
        "caused by",
        "improvement",
    ):
        assert forbidden not in lowered


def test_the_report_reports_no_fifth_metric() -> None:
    _, markdown, _ = report_of(ranked_records(size=10, ranked=6))
    for forbidden in ("F1", "MRR", "MAP@", "AUC", "@20", "@1 "):
        assert forbidden not in markdown


def test_rounding_is_display_only(tmp_path) -> None:
    """The stored values keep full precision; only the page rounds."""
    records = (
        record(
            index,
            geography_segments=(segment("RESOLVED"),),
            qualification=qualification(),
            matching=matching(),
            recommendation=recommendation(index) if index <= 1 else None,
        )
        for index in range(1, 4)
    )
    report, markdown, _ = report_of(tuple(records))
    share = report.run.projection(
        CohortProjection.RECOMMENDATION_OBSERVED
    ).cohort_share
    # The stored value is the full quotient.
    assert share == 1 / 3
    assert repr(share).startswith("0.3333333333")
    # The page shows it rounded, and the run is unchanged by rendering.
    assert "0.333333" in markdown
    assert report.run.projection(
        CohortProjection.RECOMMENDATION_OBSERVED
    ).cohort_share == 1 / 3


def test_the_report_carries_the_identities_and_the_evidence_class() -> None:
    report, markdown, _ = report_of(ranked_records(size=10, ranked=6))
    assert report.run.run_fingerprint in markdown
    assert report.run.context.context_fingerprint in markdown
    assert report.run.context.business_metric_run_fingerprint in markdown
    assert "DIAGNOSTIC_CALIBRATION" in markdown


def test_the_report_shows_the_error_analysis_candidates() -> None:
    records = ranked_records(size=10, ranked=6)
    dataset = dataset_of(records)
    grades = {index: (3 if index in (9, 10) else 0) for index in range(1, 11)}
    _, markdown, _ = report_of(records, coverage=coverage_of(grades, dataset))
    assert "False-positive candidates" in markdown
    assert "False-negative candidates" in markdown
    assert "never ranked" in markdown
    assert "unjudged" in markdown.lower()


def test_the_markdown_ends_with_exactly_one_newline() -> None:
    _, markdown, _ = report_of(ranked_records(size=8, ranked=5))
    assert markdown.endswith("\n")
    assert not markdown.endswith("\n\n")


def test_rendering_something_that_is_not_a_report_is_refused() -> None:
    with pytest.raises(TypeError):
        render_experiment_report_markdown({"run": None})
