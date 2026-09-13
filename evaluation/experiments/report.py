"""A person-readable view of one verified experiment run. **Derived, not authority.**

`build_experiment_report(run, context)` assembles the view and
`render_experiment_report_markdown(report)` writes it out. Both are downstream of
the run in the strongest sense: nothing here is stored, nothing is fingerprinted,
nothing is content-addressed, and nothing a report says can change what a run
says. The `ExperimentRun` is the authority; this is a way of reading it.

**Always full-verifies first.** A report over an unverified document is the most
dangerous artefact in this package — it is the one somebody quotes — so
`build_experiment_report` recomputes all twelve blocks over the frozen artefacts
before rendering a single line.

## What it shows

1. identities, versions and the evidence class
2. every Phase 10.4 `BusinessMetricRun` result, as that run states them
3. the five projections
4. the six overlaps
5. the four ranking metrics — Precision@5, Precision@10, Recall@10, NDCG@10
6. the error analysis

## What it must never invent

No baseline ranking to compare against. No global score, no quality score, no
computed/`N_A` tally presented as a result. No causal claim — nothing here says
a projection *caused* another, or that a number improved because of anything.
No extra metric: the four questions are the four questions, and a fifth number
in a report is a metric that arrived without a definition, an availability gate
or a place in any identity.

The Phase 10.4 results are shown **as that run states them**. They are not
recomputed here, not copied into results of Phase 10.5's own, and no projection
in section 3 is derived from them.

## `N_A` is shown, with its reason

Every unavailable block appears, named, with the reason code it states. A report
that omitted them would turn twelve statements into however many happened to be
computable, and the reader could not tell the difference between "zero" and "we
could not tell".

## Rounding is display only

Values are rounded **in the rendered text and nowhere else**. The stored
scientific values are never touched: `report.py` formats them, and a reader who
needs the full precision reads the run. Markdown v1 is the whole of it — no PDF,
no HTML, no dashboard, and no report fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evaluation.business_metrics import (
    BusinessMetricRun,
    BusinessMetricStatus,
    business_metric_key_payload,
)
from evaluation.metrics import MetricStatus

from .context import ExperimentRunContext, _verified
from .error_analysis import ErrorAnalysis, build_error_analysis
from .run import verify_experiment_run
from .schema import (
    OverlapStatus,
    ProjectionStatus,
    ExperimentRun,
    RankingExperimentStatus,
)

__all__ = [
    "ExperimentReport",
    "build_experiment_report",
    "render_experiment_report_markdown",
]

#: How many decimals a rate is shown to. **Display only** — see the module
#: docstring. Nothing rounded here is written back to a run, a result or a
#: digest.
_DISPLAY_DECIMALS = 6


@dataclass(frozen=True)
class ExperimentReport:
    """One verified run, its Phase 10.4 run and its error analysis, for reading.

    A **view**: it holds references to the artefacts rather than a restatement
    of them, so there is no second copy of a value that could come to disagree
    with the run. Deliberately no `report_fingerprint` and no identity of its
    own — a derived view that could be cited by digest would start being treated
    as the authority.
    """

    run: ExperimentRun
    business_metric_run: BusinessMetricRun
    error_analysis: ErrorAnalysis


def build_experiment_report(
    run: ExperimentRun, context: ExperimentRunContext
) -> ExperimentReport:
    """Assemble the report view of one run, or refuse.

    Full-verifies the run against its artefacts, then builds the error analysis
    — which full-verifies again, and that redundancy is deliberate: neither
    function may depend on the other having been called first.
    """
    verify_experiment_run(run, context)
    verified = _verified(context)
    return ExperimentReport(
        run=run,
        business_metric_run=verified.business_metric_run,
        error_analysis=build_error_analysis(run, context),
    )


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _number(value: float | None) -> str:
    """One value, rounded for display only, or an em dash for its absence."""
    if value is None:
        return "—"
    return f"{round(float(value), _DISPLAY_DECIMALS)}"


def _status(status: Any, reason: Any) -> str:
    """A status, with its reason when it has one. `N_A` is never bare."""
    if reason is None:
        return f"`{status}`"
    return f"`{status}` / `{reason}`"


def _ids(values: tuple[int, ...] | None, *, limit: int = 40) -> str:
    """A membership, truncated for the page but never silently.

    The count is always stated and the truncation says how many are not shown,
    so a reader is never left believing they have seen the whole list. The run
    holds every id; this is a page.
    """
    if values is None:
        return "*not stated*"
    if not values:
        return "*(none)*"
    if len(values) <= limit:
        return ", ".join(str(item) for item in values)
    shown = ", ".join(str(item) for item in values[:limit])
    return f"{shown} … (+{len(values) - limit} more)"


def _identities(report: ExperimentReport) -> list[str]:
    run = report.run
    binding = run.context
    ranking = run.ranking
    lines = [
        "## 1. Identities and versions",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| dataset | `{binding.dataset_id}` |",
        f"| dataset content | `{binding.dataset_content_fingerprint}` |",
        f"| profile | `{binding.profile_id}` |",
        f"| profile context | `{binding.profile_context_fingerprint}` |",
        f"| business metric run | `{binding.business_metric_run_fingerprint}` |",
        (
            "| ranking evaluation run | "
            + (
                "*none — this snapshot froze no recommendation*"
                if binding.ranking_evaluation_run_fingerprint is None
                else f"`{binding.ranking_evaluation_run_fingerprint}`"
            )
            + " |"
        ),
        f"| experiment context | `{binding.context_fingerprint}` |",
        f"| experiment run | `{run.run_fingerprint}` |",
        f"| run schema | `{run.run_schema_version}` |",
        f"| experiment contract | `{run.contract_version}` |",
        (
            "| evidence class | "
            + (
                "*not stated — the ranking block is unavailable*"
                if ranking.evidence_class is None
                else f"`{ranking.evidence_class}`"
            )
            + " |"
        ),
        (
            "| labelset | "
            + (
                "*not stated*"
                if ranking.labelset_fingerprint is None
                else f"`{ranking.labelset_fingerprint}`"
            )
            + " |"
        ),
        "",
    ]
    provenance = run.provenance
    lines += [
        "Provenance — outside every fingerprint, and informative only:",
        "",
        f"- generated at: {provenance.generated_at or '*not stated*'}",
        f"- dataset directory: {provenance.dataset_directory or '*not stated*'}",
        (
            "- business metric run: "
            f"{provenance.business_metric_run_path or '*not stated*'}"
        ),
        f"- label root: {provenance.label_root or '*not stated*'}",
        (
            "- benchmark records: "
            f"{provenance.benchmark_records_path or '*not stated*'}"
        ),
        "",
    ]
    return lines


def _business_metrics(report: ExperimentReport) -> list[str]:
    """Every Phase 10.4 result, as that run states it. Not recomputed here."""
    run = report.business_metric_run
    lines = [
        "## 2. Business and data-quality metrics (Phase 10.4)",
        "",
        (
            "Stated exactly as the bound `BusinessMetricRun` "
            f"`{run.run_fingerprint}` holds them. Phase 10.5 recomputes none of "
            "these, copies none of them as a result of its own, and derives no "
            "projection from them."
        ),
        "",
        "| metric | status | value |",
        "| --- | --- | --- |",
    ]
    for result in run.results:
        key = business_metric_key_payload(result.key)
        name = str(key["metric"])
        if key["dimension_value"] is not None:
            name += f" / {key['dimension_kind']}={key['dimension_value']}"
        value = (
            _number(result.value)
            if result.status is BusinessMetricStatus.COMPUTED
            else "—"
        )
        lines.append(
            f"| `{name}` | {_status(result.status, result.reason)} | {value} |"
        )
    lines.append("")
    return lines


def _projections(report: ExperimentReport) -> list[str]:
    lines = [
        "## 3. Cohort projections",
        "",
        (
            "Five **independent** projections of the same frozen cohort. Not a "
            "funnel: none of them is a stage of another, and the order below is "
            "the contract's canonical order rather than a sequence of events."
        ),
        "",
        "| projection | status | included | excluded | cohort share |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in report.run.projections:
        computed = result.status is ProjectionStatus.COMPUTED
        lines.append(
            f"| `{result.projection}` | "
            f"{_status(result.status, result.unavailable_reason)} | "
            f"{result.included_count if computed else '—'} | "
            f"{result.excluded_count if computed else '—'} | "
            f"{_number(result.cohort_share) if computed else '—'} |"
        )
    lines.append("")
    for result in report.run.projections:
        lines += [
            f"### {result.projection}",
            "",
        ]
        if result.status is ProjectionStatus.COMPUTED:
            lines += [
                f"- cohort size: {result.cohort_size}",
                f"- included ({result.included_count}): "
                f"{_ids(result.included_opportunity_ids)}",
                "",
            ]
        else:
            lines += [
                (
                    f"- **{result.status} / {result.unavailable_reason}** — no "
                    "membership and no count is stated. An empty included set "
                    "would assert that the pipeline included nobody, which is a "
                    "measurement; this projection could not be measured."
                ),
                f"- cohort size: {result.cohort_size}",
                "",
            ]
    return lines


def _overlaps(report: ExperimentReport) -> list[str]:
    lines = [
        "## 4. Projection overlaps",
        "",
        (
            "The six unordered pairs of the four question projections, each "
            "partitioning the whole frozen cohort into four disjoint sets. Both "
            "directional rates are stated: they are asymmetric, and neither "
            "implies the other."
        ),
        "",
        "| pair | status | both | left only | right only | neither |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in report.run.overlaps:
        computed = result.status is OverlapStatus.COMPUTED
        lines.append(
            f"| `{result.pair}` | "
            f"{_status(result.status, result.unavailable_reason)} | "
            f"{result.both_count if computed else '—'} | "
            f"{result.left_only_count if computed else '—'} | "
            f"{result.right_only_count if computed else '—'} | "
            f"{result.neither_count if computed else '—'} |"
        )
    lines.append("")
    for result in report.run.overlaps:
        lines += [f"### {result.pair}", ""]
        if result.status is not OverlapStatus.COMPUTED:
            lines += [
                (
                    f"- **{result.status} / {result.unavailable_reason}** — one "
                    "of the two projections is itself unavailable, so there is "
                    "no membership to intersect. No partition and no rate is "
                    "stated."
                ),
                "",
            ]
            continue
        lines += [
            f"- left: `{result.left_projection}`",
            f"- right: `{result.right_projection}`",
            f"- both ({result.both_count}): {_ids(result.both_opportunity_ids)}",
            (
                f"- left only ({result.left_only_count}): "
                f"{_ids(result.left_only_opportunity_ids)}"
            ),
            (
                f"- right only ({result.right_only_count}): "
                f"{_ids(result.right_only_opportunity_ids)}"
            ),
            (
                f"- neither ({result.neither_count}): "
                f"{_ids(result.neither_opportunity_ids)}"
            ),
            "",
            "| rate | status | fraction | value |",
            "| --- | --- | --- | --- |",
        ]
        for rate in (result.right_among_left, result.left_among_right):
            lines.append(
                f"| `{rate.name}` | {_status(rate.status, rate.unavailable_reason)} "
                f"| {rate.numerator}/{rate.denominator} | {_number(rate.value)} |"
            )
        lines.append("")
    return lines


def _ranking(report: ExperimentReport) -> list[str]:
    ranking = report.run.ranking
    lines = [
        "## 5. Observed ranking experiment",
        "",
        (
            "The frozen Recommendation ordering, measured against the whole "
            "frozen cohort. There is **no baseline ranking** here and none is "
            "computed anywhere: these four numbers describe the ordering "
            "production actually produced, and nothing else."
        ),
        "",
    ]
    if ranking.status is RankingExperimentStatus.N_A:
        lines += [
            (
                f"**{ranking.status} / {ranking.unavailable_reason}** — this "
                "snapshot froze no recommendation assessment at all, so there is "
                "no observed ordering to evaluate. No metric is reported, and "
                "none is reported as a zero."
            ),
            "",
            (
                "The projection that establishes it: "
                f"`{ranking.recommendation_projection_result_fingerprint}`."
            ),
            "",
        ]
        return lines
    lines += [
        f"- evaluation run: `{ranking.evaluation_run_fingerprint}`",
        f"- universe: `{ranking.evaluation_universe_fingerprint}`",
        f"- ranking: `{ranking.ranking_fingerprint}`",
        f"- labelset: `{ranking.labelset_fingerprint}`",
        f"- evidence class: `{ranking.evidence_class}`",
        "",
        "| question | status | value | K requested | K effective | judged |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in ranking.metric_results:
        result = entry.result
        support = result.support
        lines.append(
            f"| `{entry.metric}@{entry.k_requested}` | "
            f"{_status(result.status, result.reason)} | "
            f"{_number(result.value) if result.status is MetricStatus.COMPUTED else '—'} | "
            f"{support.k_requested} | {support.k_effective} | "
            f"{support.judged_count} |"
        )
    lines.append("")
    for entry in ranking.metric_results:
        result = entry.result
        support = result.support
        lines += [
            f"### {entry.metric}@{entry.k_requested}",
            "",
            f"- status: {_status(result.status, result.reason)}",
        ]
        if result.status is MetricStatus.COMPUTED:
            lines += [
                f"- value: {_number(result.value)}",
                (
                    f"- fraction: {_number(support.numerator)} / "
                    f"{_number(support.denominator)}"
                ),
            ]
        else:
            lines.append(
                "- no value, not even a zero: an unavailable metric has no "
                "number."
            )
        lines += [
            f"- universe size: {support.universe_size}",
            f"- ranking length: {support.ranking_length}",
            f"- unjudged in the effective top K: {_ids(support.unjudged_in_top_k)}",
            "",
        ]
    return lines


def _error_analysis(report: ExperimentReport) -> list[str]:
    analysis = report.error_analysis
    lines = [
        "## 6. Error analysis",
        "",
        (
            "A diagnostic view, derived and not persisted. It names candidate "
            "postings and **counts nothing**: there is no false-positive rate, "
            "no false-negative rate and no inversion rate here, because the four "
            "metrics above are the metrics of this contract."
        ),
        "",
        (
            "An **unjudged** posting is never a candidate. It is a question "
            "nobody answered, which is not a wrong answer."
        ),
        "",
    ]
    if analysis.ranking is None:
        lines += [
            (
                "No ranking diagnostics: this snapshot froze no recommendation, "
                "so there is no ordering to disagree with."
            ),
            "",
        ]
    else:
        diagnostics = analysis.ranking
        lines += [
            "### Ranking disagreements",
            "",
            f"- labelset: `{analysis.labelset_fingerprint}`",
            f"- label protocol: `{analysis.label_protocol_version}`",
            f"- evidence class: `{analysis.evidence_class}`",
            (
                f"- cut-off: K={diagnostics.k_requested}, effective "
                f"{diagnostics.k_effective} over a ranking of "
                f"{diagnostics.ranking_length}"
            ),
            f"- judged postings: {diagnostics.judged_count}",
            (
                "- unjudged in the effective top K: "
                f"{_ids(diagnostics.unjudged_in_top_k)}"
            ),
            "",
            "#### False-positive candidates (judged, in the top K, grade < 2)",
            "",
        ]
        if not diagnostics.false_positive_candidates:
            lines += ["*(none)*", ""]
        else:
            lines += [
                "| opportunity | rank | grade |",
                "| --- | --- | --- |",
            ]
            for item in diagnostics.false_positive_candidates:
                lines.append(
                    f"| {item.opportunity_id} | {item.rank_position} | "
                    f"{item.relevance_grade} |"
                )
            lines.append("")
        lines += [
            (
                "#### False-negative candidates (grade >= 2, outside the top K)"
            ),
            "",
        ]
        if not diagnostics.false_negative_candidates:
            lines += ["*(none)*", ""]
        else:
            lines += [
                "| opportunity | grade | rank |",
                "| --- | --- | --- |",
            ]
            for item in diagnostics.false_negative_candidates:
                rank = (
                    "*never ranked*"
                    if item.rank_position is None
                    else str(item.rank_position)
                )
                lines.append(
                    f"| {item.opportunity_id} | {item.relevance_grade} | {rank} |"
                )
            lines.append("")
        lines += ["#### Judged ranking inversions", ""]
        if not diagnostics.judged_ranking_inversions:
            lines += ["*(none)*", ""]
        else:
            lines += [
                "| better ranked | rank | grade | worse ranked | rank | grade |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for item in diagnostics.judged_ranking_inversions:
                lines.append(
                    f"| {item.higher_ranked_opportunity_id} | "
                    f"{item.higher_rank_position} | "
                    f"{item.higher_relevance_grade} | "
                    f"{item.lower_ranked_opportunity_id} | "
                    f"{item.lower_rank_position} | "
                    f"{item.lower_relevance_grade} |"
                )
            lines.append("")

    geo = analysis.geo
    lines += [
        "### Geography diagnostics",
        "",
        (
            "`AMBIGUOUS` and `UNKNOWN` are kept apart: a resolver that found "
            "several possible places did not fail to find one, and merging them "
            "would hide which of the two the pipeline should fix."
        ),
        "",
        f"- target country: {geo.target_country or '*unavailable*'}",
        (
            "- ambiguous location: "
            f"{_ids(geo.ambiguous_location_opportunity_ids)}"
        ),
        f"- unknown location: {_ids(geo.unknown_location_opportunity_ids)}",
        (
            "- no geography segment at all: "
            f"{_ids(geo.no_geography_segment_opportunity_ids)}"
        ),
        f"- explicitly out of target: {_ids(geo.out_of_target_opportunity_ids)}",
        "",
    ]

    sources = analysis.sources
    lines += [
        "### Source diagnostics",
        "",
        (
            "Facts demonstrable from the Phase 10.1 records and the bound Phase "
            "10.4 evidence only. No reason code is invented here: the URL states "
            "below are Phase 10.4's own vocabulary."
        ),
        "",
        f"- observed sources: {', '.join(sources.observed_source_ids) or '*(none)*'}",
        "",
    ]
    if sources.records_per_source:
        lines += ["| source | records |", "| --- | --- |"]
        for source_id, count in sources.records_per_source.items():
            lines.append(f"| `{source_id}` | {count} |")
        lines.append("")
    lines += [
        f"- records with no source row: {_ids(sources.records_with_no_source_row)}",
        f"- records with no action URL: {_ids(sources.records_with_no_action_url)}",
    ]
    if sources.url_evidence_reason is not None:
        lines.append(
            f"- URL evidence: **`{sources.url_evidence_reason}`** (Phase 10.4's "
            "own reason for this absence)"
        )
    else:
        lines.append(
            "- URL audit outcomes: "
            + ", ".join(
                f"`{outcome}`={count}"
                for outcome, count in sources.url_outcome_counts.items()
            )
        )
        lines.append(
            f"- postings whose action URL the audit found broken: "
            f"{_ids(sources.broken_url_opportunity_ids)}"
        )
    lines.append("")

    skills = analysis.skills
    lines += [
        "### Skill diagnostics",
        "",
        (
            f"**`{skills.status}` / `{skills.unavailable_reason}`** — "
            "per-opportunity skill requirements are not in the Phase 10.1 record "
            "contract, so a skill-level diagnostic has no input. This is Phase "
            "10.4's existing reason for exactly this absence, reused rather than "
            "restated under a new name."
        ),
        "",
    ]
    return lines


def render_experiment_report_markdown(report: ExperimentReport) -> str:
    """The report as Markdown. Rounding here and nowhere else.

    Markdown v1 is the whole deliverable: no PDF, no HTML, no dashboard, no
    report fingerprint and no content-addressed store. A reader who needs full
    precision reads the run, which is the authority.
    """
    if not isinstance(report, ExperimentReport):
        raise TypeError(f"{report!r} is not an experiment report")
    run = report.run
    lines = [
        "# Experiment run report",
        "",
        (
            f"Experiment run `{run.run_fingerprint}` over dataset "
            f"`{run.context.dataset_id}`."
        ),
        "",
        (
            "A **derived view**. The `ExperimentRun` is the authority; this "
            "document is a way of reading it, is not stored, carries no "
            "identity of its own, and states no overall score — a run is twelve "
            "separate statements about one cohort, and a single figure "
            "summarising them would be a number nobody could re-derive."
        ),
        "",
        (
            "Every value shown is rounded for display only. The stored "
            "scientific values are untouched."
        ),
        "",
    ]
    lines += _identities(report)
    lines += _business_metrics(report)
    lines += _projections(report)
    lines += _overlaps(report)
    lines += _ranking(report)
    lines += _error_analysis(report)
    return "\n".join(lines).rstrip("\n") + "\n"
