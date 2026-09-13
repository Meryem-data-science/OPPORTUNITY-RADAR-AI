"""Where a business metric run lands on disk, and why it never moves again.

One file per run, under the project's local data space:

    data/evaluation/business_metric_runs/<run_fingerprint>/run.json

**One authoritative document.** Not a manifest beside a results file, and not
JSONL: a run is a single indivisible statement — these metrics, over this
evidence, under these rules — and splitting it across two files creates a state
in which one of them exists. The directory is named after `run_fingerprint`, so
the store is content-addressed: the same measurement computed twice resolves to
the same directory, and two different measurements can never collide there.

**Nothing here touches the operational database**, makes a request, or reads a
human label. It writes one JSON file and reads it back.

## Immutable, and verified rather than trusted

    directory absent        write it, report CREATED
    present and identical   write nothing, report UNCHANGED, and return the run
                            that is actually on disk
    present and different   raise, and change nothing

"Identical" is decided by the contract's own **identity projection** — the
canonical D9 payload `run_fingerprint` is taken over — and only after *both* runs
have been fully structurally verified, which recomputes every nested digest and
each run's own. Neither the stored `run_fingerprint` nor the file's bytes decide
it: the first is a claim a document makes about itself and an edited file can
keep it, and the second carries provenance that identity deliberately excludes.
So two runs of the same measurement differing only in when they were generated,
which directory the dataset was read from, or **where a nested source map or
benchmark file happened to live** are one run: UNCHANGED, and not a byte
rewritten.

The run returned on UNCHANGED is the one **read back from disk**, with its
original provenance. A caller that was handed its own in-memory run instead would
be told "unchanged" while holding a different `generated_at` from the artefact
that is actually stored.

There is no repair, no overwrite and no deletion. An incomplete directory,
unreadable JSON, an unknown schema version, a nested artefact whose digest does
not survive recomputation, or content that diverges from this run: each raises
and leaves the directory exactly as it was.

A partial write cannot be mistaken for a run, and neither can a lost race. The
document is written to a temporary file, fsynced, and then published by a **hard
link**, which is atomic *and* fails when the destination already exists — unlike
a rename, which would need a prior `exists()` check and could be overtaken
between the check and the call.

So a crash leaves one of exactly three states, and none of them is a half-written
run: no directory at all; an empty directory, which the next read refuses rather
than trusts; or — once the link has returned — a **complete, valid run** whose
temporary name simply has not been cleaned up yet. That third state is a stray
hidden `.tmp` file beside the run directories, read by nothing, and the run in it
is frozen and correct. Removing that name is best-effort housekeeping: it cannot
fail the write, and it never touches `run.json`.

## The bytes are stable

UTF-8, `ensure_ascii=False`, two-space indent, sorted keys, one trailing newline.
Two runs of the same measurement produce files that are identical byte for byte
and `diff` says nothing. The formatting reaches no fingerprint: `run_fingerprint`
is taken over the run's canonical payload and is **not** the SHA-256 of this
file, which also carries the provenance the identity deliberately excludes.

## Reading is strict

`read_business_metric_run` reconstructs closed dataclasses field by field. Every
object must carry **exactly** its expected keys — a missing one is refused, and so
is an unexpected one, because a key this build does not know is a statement it
cannot interpret and silently dropping it would read a future document as though
it were this one. No value is coerced, no version is accepted but the current
one, no stored digest is believed, and `pickle` appears nowhere.

**Nothing written here is ever committed.** These runs describe real opportunity
data, so `data/` is Git-ignored in full.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .fingerprint import canonical_business_metric_run_payload
from .run import verify_business_metric_run_structure
from .schema import (
    BusinessEvidenceClass,
    BusinessMetricBindingError,
    BusinessMetricContractError,
    BusinessMetricDimension,
    BusinessMetricKey,
    BusinessMetricName,
    BusinessMetricResult,
    BusinessMetricRun,
    BusinessMetricRunContext,
    BusinessMetricRunProvenance,
    BusinessMetricStatus,
    BusinessMetricSupport,
    BusinessMetricUnavailableReason,
    BusinessMetricUniverse,
    BenchmarkBinding,
    DataAiQualificationBreakdown,
    DeclaredSourceEntry,
    DeclaredSourceUniverseEvidence,
    DedupEvidence,
    FreshnessAvailabilityWitness,
    FreshnessBinding,
    FreshnessBucket,
    FreshnessBucketCount,
    FreshnessBucketDistribution,
    FrozenCohortBinding,
    ListingQualityBreakdown,
    OpportunityTypeBreakdown,
    ProfileTargetBindingEvidence,
    TargetVerdictBreakdown,
    UnknownLocationDiagnostics,
    UrlAuditBinding,
    UrlAuditInconclusiveReason,
    UrlAuditObservation,
    UrlAuditOutcome,
    business_metric_run_payload,
    validate_fingerprint,
)

__all__ = [
    "DEFAULT_BUSINESS_METRIC_RUN_ROOT",
    "RUN_FILENAME",
    "BusinessMetricRunStorageResult",
    "BusinessMetricRunWriteStatus",
    "read_business_metric_run",
    "write_business_metric_run",
]

#: Relative on purpose, and under `data/` beside the frozen datasets these runs
#: measure: both are local, both are ignored by Git, and neither belongs to a
#: deployment.
DEFAULT_BUSINESS_METRIC_RUN_ROOT = Path("data/evaluation/business_metric_runs")

#: The one document. There is deliberately no second file.
RUN_FILENAME = "run.json"


class BusinessMetricRunWriteStatus(StrEnum):
    """What a write actually did. Two members, and there is no third.

    There is no `OVERWRITTEN` and no `REPAIRED`: a stored run is immutable, so
    every other outcome is a refusal that raises rather than a status that is
    reported and then ignored.
    """

    #: This call froze the run.
    CREATED = "CREATED"
    #: The run was already stored — by this call's measurement or an earlier
    #: identical one — and was verified rather than rewritten.
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True)
class BusinessMetricRunStorageResult:
    """Where a run lives, what this call did, and what is actually stored.

    `run` is the **persisted artefact**, not necessarily the one that was passed
    in. On UNCHANGED it is the run read back from disk, carrying the provenance
    it was originally frozen with — so a caller learns what the store holds
    rather than being handed its own object back with a status attached.
    """

    directory: Path
    run_file: Path
    status: BusinessMetricRunWriteStatus
    run: BusinessMetricRun


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _render(run: BusinessMetricRun) -> str:
    """The exact bytes of `run.json`, as text.

    Indented because a person reads this file; sorted keys and a fixed separator
    make that indentation just as reproducible as the compact canonical form. The
    formatting is not in any digest.
    """
    return (
        json.dumps(
            business_metric_run_payload(run),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _semantic_identity(run: BusinessMetricRun) -> str:
    """What "the same run" means: the contract's own identity projection.

    The canonical D9 payload — the one `business_metric_run_fingerprint` digests
    — and **not** the stored document with a block deleted from it. An earlier
    version compared the full persisted document minus `run.provenance`, which
    was wrong in a way that only showed up on nested artefacts: a
    `DeclaredSourceUniverseEvidence` and a `BenchmarkBinding` each carry a
    `provenance_path`, deliberately outside their own digests because a path says
    where a file was read rather than what it says. Two runs of the identical
    measurement, one made from a checkout at another path, therefore produced the
    same `run_fingerprint` and two different "semantic documents" — and the
    second was refused as divergent.

    Comparing the identity projection instead is not a weakening. Both runs have
    been **fully structurally verified** before this is called, which recomputes
    every nested digest and the run's own, so neither is a document making
    unchecked claims about itself. Once that holds, what the contract says two
    runs must agree on to be the same run is exactly this.
    """
    return canonical_json(canonical_business_metric_run_payload(run))


# --------------------------------------------------------------------------
# strict reading
# --------------------------------------------------------------------------
#
# Every parser below requires its object to carry **exactly** the keys the
# corresponding payload function writes. A missing key is a statement this build
# needs and does not have; an unexpected one is a statement it cannot interpret,
# and dropping it silently would let a document written under a wider contract be
# read as though it had been written under this one.


def _object(value: Any, *, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessMetricBindingError(
            f"{subject} is not a JSON object: {value!r} ({type(value).__name__})"
        )
    return value


def _fields(
    value: Any, required: tuple[str, ...], *, subject: str
) -> dict[str, Any]:
    payload = _object(value, subject=subject)
    stated = set(payload)
    expected = set(required)
    if stated != expected:
        missing = sorted(expected - stated)
        unexpected = sorted(stated - expected)
        raise BusinessMetricBindingError(
            f"{subject} does not carry the fields of this contract (missing "
            f"{missing}, unexpected {unexpected}); refusing to read a document "
            "of another shape"
        )
    return payload


def _sequence(value: Any, *, subject: str) -> list[Any]:
    if not isinstance(value, list):
        raise BusinessMetricBindingError(
            f"{subject} is not a JSON array: {value!r} ({type(value).__name__})"
        )
    return value


def _member(enum: Any, value: Any, *, subject: str) -> Any:
    """One member of a closed vocabulary, or a refusal. Never a coercion."""
    try:
        return enum(value)
    except (ValueError, TypeError) as error:
        raise BusinessMetricContractError(
            f"{subject} is {value!r}, which is not a member of "
            f"{enum.__name__}; this build knows "
            f"{[str(item) for item in enum]}"
        ) from error


def _optional_member(enum: Any, value: Any, *, subject: str) -> Any:
    return None if value is None else _member(enum, value, subject=subject)


_COHORT_FIELDS = (
    "binding_version",
    "dataset_id",
    "dataset_fingerprint",
    "cohort_size",
    "profile_id",
    "profile_fingerprint",
    "expected_action_urls",
    "absorbed_duplicate_total",
    "records_with_absorbed_duplicates",
    "declared_merged_duplicate_count",
    "dataset_generated_at",
    "freshness_as_of_date",
    "binding_fingerprint",
)


def _parse_cohort_binding(value: Any) -> FrozenCohortBinding:
    payload = _fields(value, _COHORT_FIELDS, subject="the frozen cohort binding")
    urls = _sequence(
        payload["expected_action_urls"],
        subject="the cohort binding's expected action URLs",
    )
    return FrozenCohortBinding(
        binding_version=payload["binding_version"],
        dataset_id=payload["dataset_id"],
        dataset_fingerprint=payload["dataset_fingerprint"],
        cohort_size=payload["cohort_size"],
        profile_id=payload["profile_id"],
        profile_fingerprint=payload["profile_fingerprint"],
        dataset_generated_at=payload["dataset_generated_at"],
        freshness_as_of_date=payload["freshness_as_of_date"],
        expected_action_urls=tuple(urls),
        absorbed_duplicate_total=payload["absorbed_duplicate_total"],
        records_with_absorbed_duplicates=payload[
            "records_with_absorbed_duplicates"
        ],
        declared_merged_duplicate_count=payload["declared_merged_duplicate_count"],
        binding_fingerprint=payload["binding_fingerprint"],
    )


_TARGET_FIELDS = (
    "binding_version",
    "profile_id",
    "profile_fingerprint",
    "country_code",
    "rule_id",
    "binding_fingerprint",
)


def _parse_profile_target_binding(value: Any) -> ProfileTargetBindingEvidence:
    payload = _fields(value, _TARGET_FIELDS, subject="the profile target binding")
    return ProfileTargetBindingEvidence(
        binding_version=payload["binding_version"],
        profile_id=payload["profile_id"],
        profile_fingerprint=payload["profile_fingerprint"],
        country_code=payload["country_code"],
        rule_id=payload["rule_id"],
        binding_fingerprint=payload["binding_fingerprint"],
    )


_DECLARED_ENTRY_FIELDS = (
    "source_id",
    "integration_status",
    "coverage_role",
    "priority",
    "country",
    "production_source_id",
)

_DECLARED_UNIVERSE_FIELDS = (
    "binding_version",
    "map_name",
    "map_version",
    "scope_country",
    "entries",
    "source_map_fingerprint",
    "git_commit",
    "content_fingerprint",
    "provenance_path",
)


def _parse_declared_source_universe(value: Any) -> DeclaredSourceUniverseEvidence:
    payload = _fields(
        value, _DECLARED_UNIVERSE_FIELDS, subject="the declared source universe"
    )
    entries = []
    for index, item in enumerate(
        _sequence(payload["entries"], subject="the declared source entries"),
        start=1,
    ):
        entry = _fields(
            item,
            _DECLARED_ENTRY_FIELDS,
            subject=f"declared source entry {index}",
        )
        entries.append(
            DeclaredSourceEntry(
                source_id=entry["source_id"],
                integration_status=entry["integration_status"],
                coverage_role=entry["coverage_role"],
                priority=entry["priority"],
                country=entry["country"],
                production_source_id=entry["production_source_id"],
            )
        )
    return DeclaredSourceUniverseEvidence(
        binding_version=payload["binding_version"],
        map_name=payload["map_name"],
        map_version=payload["map_version"],
        scope_country=payload["scope_country"],
        entries=tuple(entries),
        source_map_fingerprint=payload["source_map_fingerprint"],
        git_commit=payload["git_commit"],
        content_fingerprint=payload["content_fingerprint"],
        provenance_path=payload["provenance_path"],
    )


_OBSERVATION_FIELDS = (
    "requested_url",
    "attempted",
    "outcome",
    "reason",
    "final_url",
    "status_code",
    "redirect_chain",
    "audited_at",
)

_URL_AUDIT_FIELDS = (
    "binding_version",
    "protocol_version",
    "audit_policy_version",
    "dataset_id",
    "dataset_fingerprint",
    "audit_started_at",
    "observations",
    "declared_universe_size",
    "binding_fingerprint",
)


def _parse_url_audit_binding(value: Any) -> UrlAuditBinding:
    payload = _fields(value, _URL_AUDIT_FIELDS, subject="the URL audit binding")
    observations = []
    for index, item in enumerate(
        _sequence(payload["observations"], subject="the URL audit observations"),
        start=1,
    ):
        subject = f"URL audit observation {index}"
        observation = _fields(item, _OBSERVATION_FIELDS, subject=subject)
        chain = _sequence(
            observation["redirect_chain"],
            subject=f"the redirect chain of {subject}",
        )
        observations.append(
            UrlAuditObservation(
                requested_url=observation["requested_url"],
                attempted=observation["attempted"],
                outcome=_member(
                    UrlAuditOutcome,
                    observation["outcome"],
                    subject=f"the outcome of {subject}",
                ),
                reason=_optional_member(
                    UrlAuditInconclusiveReason,
                    observation["reason"],
                    subject=f"the reason of {subject}",
                ),
                final_url=observation["final_url"],
                status_code=observation["status_code"],
                redirect_chain=tuple(chain),
                audited_at=observation["audited_at"],
            )
        )
    return UrlAuditBinding(
        binding_version=payload["binding_version"],
        protocol_version=payload["protocol_version"],
        audit_policy_version=payload["audit_policy_version"],
        dataset_id=payload["dataset_id"],
        dataset_fingerprint=payload["dataset_fingerprint"],
        audit_started_at=payload["audit_started_at"],
        observations=tuple(observations),
        declared_universe_size=payload["declared_universe_size"],
        binding_fingerprint=payload["binding_fingerprint"],
    )


_DEDUP_FIELDS = (
    "evidence_version",
    "dataset_id",
    "dataset_fingerprint",
    "cohort_size",
    "absorbed_duplicate_total",
    "declared_merged_duplicate_count",
    "records_with_absorbed_duplicates",
    "evidence_fingerprint",
)


def _parse_dedup_evidence(value: Any) -> DedupEvidence:
    payload = _fields(value, _DEDUP_FIELDS, subject="the deduplication evidence")
    return DedupEvidence(
        evidence_version=payload["evidence_version"],
        dataset_id=payload["dataset_id"],
        dataset_fingerprint=payload["dataset_fingerprint"],
        cohort_size=payload["cohort_size"],
        absorbed_duplicate_total=payload["absorbed_duplicate_total"],
        declared_merged_duplicate_count=payload["declared_merged_duplicate_count"],
        records_with_absorbed_duplicates=payload[
            "records_with_absorbed_duplicates"
        ],
        evidence_fingerprint=payload["evidence_fingerprint"],
    )


_BENCHMARK_FIELDS = (
    "binding_version",
    "benchmark_name",
    "benchmark_version",
    "scope_country",
    "status",
    "record_count",
    "target_minimum_rows",
    "evaluation_ready",
    "records_fingerprint",
    "content_fingerprint",
    "provenance_path",
)


def _parse_benchmark_binding(value: Any) -> BenchmarkBinding:
    payload = _fields(value, _BENCHMARK_FIELDS, subject="the benchmark binding")
    return BenchmarkBinding(
        binding_version=payload["binding_version"],
        benchmark_name=payload["benchmark_name"],
        benchmark_version=payload["benchmark_version"],
        scope_country=payload["scope_country"],
        status=payload["status"],
        record_count=payload["record_count"],
        target_minimum_rows=payload["target_minimum_rows"],
        evaluation_ready=payload["evaluation_ready"],
        content_fingerprint=payload["content_fingerprint"],
        records_fingerprint=payload["records_fingerprint"],
        provenance_path=payload["provenance_path"],
    )


_FRESHNESS_FIELDS = (
    "binding_version",
    "as_of_date",
    "policy_version",
    "cohort_binding_fingerprint",
    "binding_fingerprint",
)


def _parse_freshness_binding(value: Any) -> FreshnessBinding:
    payload = _fields(value, _FRESHNESS_FIELDS, subject="the freshness binding")
    return FreshnessBinding(
        binding_version=payload["binding_version"],
        as_of_date=payload["as_of_date"],
        policy_version=payload["policy_version"],
        cohort_binding_fingerprint=payload["cohort_binding_fingerprint"],
        binding_fingerprint=payload["binding_fingerprint"],
    )


_RUN_CONTEXT_FIELDS = (
    "run_context_schema_version",
    "contract_version",
    "frozen_cohort_binding",
    "run_context_fingerprint",
    "profile_target_binding",
    "declared_source_universe",
    "url_audit_binding",
    "dedup_evidence",
    "benchmark_binding",
    "freshness_binding",
)


def _parse_run_context(value: Any) -> BusinessMetricRunContext:
    payload = _fields(value, _RUN_CONTEXT_FIELDS, subject="the run context")
    optional = {
        "profile_target_binding": _parse_profile_target_binding,
        "declared_source_universe": _parse_declared_source_universe,
        "url_audit_binding": _parse_url_audit_binding,
        "dedup_evidence": _parse_dedup_evidence,
        "benchmark_binding": _parse_benchmark_binding,
        "freshness_binding": _parse_freshness_binding,
    }
    members = {
        name: (None if payload[name] is None else parse(payload[name]))
        for name, parse in optional.items()
    }
    return BusinessMetricRunContext(
        run_context_schema_version=payload["run_context_schema_version"],
        contract_version=payload["contract_version"],
        frozen_cohort_binding=_parse_cohort_binding(
            payload["frozen_cohort_binding"]
        ),
        run_context_fingerprint=payload["run_context_fingerprint"],
        **members,
    )


_KEY_FIELDS = ("metric", "dimension_kind", "dimension_value")


def _parse_key(value: Any) -> BusinessMetricKey:
    payload = _fields(value, _KEY_FIELDS, subject="a business metric key")
    return BusinessMetricKey(
        metric=_member(
            BusinessMetricName, payload["metric"], subject="a business metric name"
        ),
        dimension_kind=_member(
            BusinessMetricDimension,
            payload["dimension_kind"],
            subject="a business metric dimension",
        ),
        dimension_value=payload["dimension_value"],
    )


_DISTRIBUTION_FIELDS = ("entries", "universe_size")
_BUCKET_FIELDS = ("bucket", "count")
_TARGET_VERDICT_FIELDS = (
    "match_count",
    "out_of_target_count",
    "unknown_target_verdict_count",
)
_DATA_AI_FIELDS = (
    "core_target_count",
    "adjacent_target_count",
    "out_of_scope_count",
    "uncertain_count",
    "unclassified_count",
)
_TYPE_FIELDS = (
    "pfe_count",
    "internship_count",
    "apprenticeship_count",
    "graduate_count",
    "job_count",
    "unknown_type_count",
    "unclassified_type_count",
)
_QUALITY_FIELDS = (
    "normal_listing_count",
    "possible_non_job_page_count",
    "insufficient_content_count",
    "unclassified_count",
)
_LOCATION_FIELDS = (
    "ambiguous_location_count",
    "no_geography_segments_count",
    "unknown_segment_record_count",
)
_WITNESS_FIELDS = ("known_date_count",)

_SUPPORT_FIELDS = (
    "numerator",
    "denominator",
    "universe_size",
    "freshness_distribution",
    "target_verdict_breakdown",
    "data_ai_breakdown",
    "opportunity_type_breakdown",
    "listing_quality_breakdown",
    "unknown_location_diagnostics",
    "freshness_availability_witness",
)


def _parse_freshness_distribution(value: Any) -> FreshnessBucketDistribution:
    payload = _fields(
        value, _DISTRIBUTION_FIELDS, subject="the freshness distribution"
    )
    entries = []
    for index, item in enumerate(
        _sequence(payload["entries"], subject="the freshness bucket counts"),
        start=1,
    ):
        subject = f"freshness bucket count {index}"
        entry = _fields(item, _BUCKET_FIELDS, subject=subject)
        entries.append(
            FreshnessBucketCount(
                bucket=_member(
                    FreshnessBucket,
                    entry["bucket"],
                    subject=f"the bucket of {subject}",
                ),
                count=entry["count"],
            )
        )
    return FreshnessBucketDistribution(
        entries=tuple(entries), universe_size=payload["universe_size"]
    )


def _parse_support(value: Any) -> BusinessMetricSupport:
    payload = _fields(value, _SUPPORT_FIELDS, subject="a support block")

    def block(name: str, fields: tuple[str, ...], factory: Any) -> Any:
        stated = payload[name]
        if stated is None:
            return None
        return factory(**_fields(stated, fields, subject=f"the {name}"))

    return BusinessMetricSupport(
        numerator=payload["numerator"],
        denominator=payload["denominator"],
        universe_size=payload["universe_size"],
        freshness_distribution=(
            None
            if payload["freshness_distribution"] is None
            else _parse_freshness_distribution(payload["freshness_distribution"])
        ),
        target_verdict_breakdown=block(
            "target_verdict_breakdown", _TARGET_VERDICT_FIELDS, TargetVerdictBreakdown
        ),
        data_ai_breakdown=block(
            "data_ai_breakdown", _DATA_AI_FIELDS, DataAiQualificationBreakdown
        ),
        opportunity_type_breakdown=block(
            "opportunity_type_breakdown", _TYPE_FIELDS, OpportunityTypeBreakdown
        ),
        listing_quality_breakdown=block(
            "listing_quality_breakdown", _QUALITY_FIELDS, ListingQualityBreakdown
        ),
        unknown_location_diagnostics=block(
            "unknown_location_diagnostics",
            _LOCATION_FIELDS,
            UnknownLocationDiagnostics,
        ),
        freshness_availability_witness=block(
            "freshness_availability_witness",
            _WITNESS_FIELDS,
            FreshnessAvailabilityWitness,
        ),
    )


_RESULT_FIELDS = (
    "result_schema_version",
    "contract_version",
    "key",
    "status",
    "value",
    "support",
    "universe_kind",
    "reason",
    "evidence_class",
    "scope_fingerprint",
    "evidence_fingerprint",
    "result_fingerprint",
)


def _parse_result(value: Any, position: int) -> BusinessMetricResult:
    payload = _fields(value, _RESULT_FIELDS, subject=f"result {position}")
    return BusinessMetricResult(
        key=_parse_key(payload["key"]),
        status=_member(
            BusinessMetricStatus,
            payload["status"],
            subject=f"the status of result {position}",
        ),
        universe_kind=_member(
            BusinessMetricUniverse,
            payload["universe_kind"],
            subject=f"the universe of result {position}",
        ),
        evidence_class=_member(
            BusinessEvidenceClass,
            payload["evidence_class"],
            subject=f"the evidence class of result {position}",
        ),
        support=_parse_support(payload["support"]),
        scope_fingerprint=payload["scope_fingerprint"],
        evidence_fingerprint=payload["evidence_fingerprint"],
        result_fingerprint=payload["result_fingerprint"],
        contract_version=payload["contract_version"],
        result_schema_version=payload["result_schema_version"],
        value=payload["value"],
        reason=_optional_member(
            BusinessMetricUnavailableReason,
            payload["reason"],
            subject=f"the reason of result {position}",
        ),
    )


_PROVENANCE_FIELDS = (
    "generated_at",
    "dataset_directory",
    "benchmark_records_path",
)

_RUN_FIELDS = (
    "run_schema_version",
    "contract_version",
    "context",
    "results",
    "run_fingerprint",
    "provenance",
)


def _parse_run(value: Any) -> BusinessMetricRun:
    """Reconstruct a run from its document. Strict, and trusting nothing.

    The versions are checked by `validate_business_metric_run_structure` and the
    digests by `verify_business_metric_run_structure`, both of which the caller
    runs immediately after this; what happens here is the reconstruction itself,
    which refuses anything whose shape is not exactly this contract's.
    """
    payload = _fields(value, _RUN_FIELDS, subject="the stored run")
    provenance = _fields(
        payload["provenance"], _PROVENANCE_FIELDS, subject="the run provenance"
    )
    results = [
        _parse_result(item, position)
        for position, item in enumerate(
            _sequence(payload["results"], subject="the run's results"), start=1
        )
    ]
    return BusinessMetricRun(
        run_schema_version=payload["run_schema_version"],
        contract_version=payload["contract_version"],
        context=_parse_run_context(payload["context"]),
        results=tuple(results),
        run_fingerprint=payload["run_fingerprint"],
        provenance=BusinessMetricRunProvenance(
            generated_at=provenance["generated_at"],
            dataset_directory=provenance["dataset_directory"],
            benchmark_records_path=provenance["benchmark_records_path"],
        ),
    )


def _load_document(run_file: Path, directory: Path) -> Any:
    """Read and decode `run.json`, or refuse the directory. No repair."""
    if not run_file.is_file():
        raise BusinessMetricBindingError(
            f"a business metric run directory exists at {directory} and holds no "
            f"{RUN_FILENAME}; it is incomplete, and this store neither repairs "
            "nor overwrites a run — remove the directory to recompute it"
        )
    try:
        text = run_file.read_text(encoding="utf-8")
    except OSError as error:
        raise BusinessMetricBindingError(
            f"cannot read the business metric run at {run_file}: {error}"
        ) from error
    except ValueError as error:
        raise BusinessMetricBindingError(
            f"the business metric run at {run_file} is not valid UTF-8"
        ) from error
    try:
        return json.loads(text)
    except ValueError as error:
        raise BusinessMetricBindingError(
            f"the business metric run at {run_file} is not valid JSON: {error}"
        ) from error


def _read_verified_run(directory: Path, run_file: Path) -> BusinessMetricRun:
    """Parse a stored run and re-establish everything it claims about itself.

    The structural verifier is what makes the stored digests worthless as
    assertions: it recomputes every one of them — each nested binding's, each
    result's and the run's own — and refuses any that does not survive. A
    hand-edited `run_fingerprint` fails here, and so does an edited value whose
    fingerprint was left alone.

    The directory's own name is held against the run as well: a document moved
    into another directory is filed under an identity that is not its own, and
    this store addresses runs by content.
    """
    run = _parse_run(_load_document(run_file, directory))
    verify_business_metric_run_structure(run)
    if run.run_fingerprint != directory.name:
        raise BusinessMetricBindingError(
            f"the run stored at {directory} states fingerprint "
            f"{run.run_fingerprint}; a run is stored under the directory its own "
            "content names, and this one is not"
        )
    return run


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def _publish(temporary: Path, run_file: Path) -> None:
    """Move a complete document into its official place, or refuse. **Atomic.**

    `os.link` and deliberately **not** `os.replace`. Both are atomic; only one of
    them refuses an existing destination. `os.replace` silently overwrites, so
    the guard it needs is a prior `exists()` check — and between that check and
    the call there is a window in which another writer can publish. Losing that
    race means destroying a frozen run, which is the one thing this store exists
    to make impossible, so the check-then-act pair is replaced by a single
    primitive that cannot lose it: the link either creates the name or raises
    `FileExistsError`, decided by the filesystem under its own lock.

    The temporary file is fully written and fsynced before this is called, so the
    name that appears is a complete document from the instant it exists — there
    is no moment at which `run.json` is half a run. Afterwards both paths name
    one inode, and the caller drops the temporary one through `_discard` — best
    effort, because by then the run is already frozen and a failure to tidy up
    must not be reported as a failure to write.

    Portable to the platforms this project targets: `os.link` is implemented on
    POSIX and, on Windows, via `CreateHardLinkW` on NTFS. Both paths live under
    the same root and therefore the same filesystem, which hard links require.
    """
    try:
        os.link(temporary, run_file)
    except FileExistsError as error:
        raise BusinessMetricBindingError(
            f"{run_file} already exists; refusing to publish over a frozen run. "
            "The official document is written by a link that fails when the "
            "destination is taken, so this call lost a race rather than winning "
            "one it should not have entered"
        ) from error


def _discard(temporary: Path | None) -> None:
    """Drop the temporary name for a document, best effort. **Never raises.**

    Deliberately silent about its own failure, and the asymmetry is the point.
    Before publication the temporary file is the only copy and its removal is
    cleanup after an error that is already being raised — masking that error with
    a second one would report the wrong cause. After publication it is one of two
    names for an inode `run.json` also holds, so failing to remove it costs a
    stray dotfile and nothing else: the run is frozen, complete and readable
    either way.

    What it must never do is turn a successful publication into a failure. An
    earlier version unlinked inside the `try`, where an `OSError` from the
    cleanup was caught by the publication's own handler and re-raised as "cannot
    write the business metric run" — telling the caller nothing had been stored
    while `run.json` sat there, correct and complete.

    Nothing reads the name this removes: readers open `<fingerprint>/run.json`
    and temporary files are hidden, suffixed `.tmp`, and live beside the run
    directories rather than inside one.
    """
    if temporary is None:
        return
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        # Housekeeping only. The caller's outcome — CREATED, or the error being
        # raised through this `finally` — is already decided and stands.
        pass


def write_business_metric_run(
    run: BusinessMetricRun,
    root: str | Path = DEFAULT_BUSINESS_METRIC_RUN_ROOT,
) -> BusinessMetricRunStorageResult:
    """Freeze a run if it is new, verify it if it is not, never overwrite.

    The run is verified **before** anything is prepared — structure, every nested
    digest and its own — because an artefact that would not survive being read
    back must not be written in the first place.

    The document's bytes are rendered and written to a temporary file **outside**
    the official destination, on the same filesystem, and only then published: the
    final directory is created exclusively, and the complete file is moved into
    it by an atomic rename onto a path that does not exist. A failure at any
    point leaves no readable run — at worst an empty directory, which
    `_read_verified_run` refuses rather than trusts.

    An existing directory is never written to. It is read, parsed, verified and
    compared semantically; identical content reports UNCHANGED without touching a
    byte, and anything else raises.
    """
    verify_business_metric_run_structure(run)
    root_path = Path(root)
    directory = root_path / run.run_fingerprint
    run_file = directory / RUN_FILENAME

    if directory.exists():
        stored = _read_verified_run(directory, run_file)
        if _semantic_identity(stored) != _semantic_identity(run):
            # A backstop rather than a routine outcome, and worth saying why:
            # both runs have been verified, so each one's fingerprint has been
            # recomputed from its content, and the stored run's was additionally
            # required to equal this directory's name — which is this run's
            # fingerprint. Two identity projections that differ here would mean
            # two different contents digesting to one SHA-256. It raises rather
            # than assuming, because the alternative is overwriting a frozen run
            # on the strength of an assumption.
            raise BusinessMetricBindingError(
                f"a different business metric run is already stored at "
                f"{directory}; it states fingerprint {stored.run_fingerprint} and "
                f"this one states {run.run_fingerprint}, and their canonical "
                "identities do not agree — refusing to overwrite a frozen run"
            )
        # Identical measurement. A provenance that differs — another moment,
        # another directory — is not a difference in what was measured, so
        # nothing is rewritten and the caller is told what is actually stored.
        return BusinessMetricRunStorageResult(
            directory=directory,
            run_file=run_file,
            status=BusinessMetricRunWriteStatus.UNCHANGED,
            run=stored,
        )

    document = _render(run)
    temporary: Path | None = None
    try:
        root_path.mkdir(parents=True, exist_ok=True)
        handle, raw = tempfile.mkstemp(
            dir=root_path, prefix=f".{run.run_fingerprint[:16]}-", suffix=".tmp"
        )
        temporary = Path(raw)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        # Exclusive: two writers racing to freeze the same run cannot both
        # believe they created it, and neither can land on a directory somebody
        # else is publishing into.
        directory.mkdir(parents=False, exist_ok=False)
        _publish(temporary, run_file)
        # **The run is frozen from here.** `_publish` returned, so `run.json`
        # exists, complete, under its own fingerprint. Nothing below may undo
        # that: the only step left is discarding the temporary *name* for an
        # inode the official path now also holds, and that is housekeeping rather
        # than part of the write. It is done in `finally`, by a helper that
        # cannot raise, so there is no statement between here and the CREATED
        # return that could fail.
    except FileExistsError as error:
        raise BusinessMetricBindingError(
            f"a business metric run appeared at {directory} while this one was "
            "being written; refusing to publish over it"
        ) from error
    except OSError as error:
        raise BusinessMetricBindingError(
            f"cannot write the business metric run to {directory}: {error}"
        ) from error
    finally:
        _discard(temporary)

    return BusinessMetricRunStorageResult(
        directory=directory,
        run_file=run_file,
        status=BusinessMetricRunWriteStatus.CREATED,
        run=run,
    )


def read_business_metric_run(
    run_fingerprint: str,
    root: str | Path = DEFAULT_BUSINESS_METRIC_RUN_ROOT,
) -> BusinessMetricRun:
    """Read one stored run by its fingerprint, re-establishing everything.

    The requested fingerprint, the directory's name, the document's own
    `run_fingerprint` and the digest recomputed from its content must all be the
    same string. Three of those four are claims somebody could have written; the
    fourth is computed here, and it is the one that decides.

    This performs the **structural** verification only, and does not pretend
    otherwise: it establishes that the document is a well-formed run whose every
    digest survives recomputation. Whether its numbers are the numbers the
    metrics yield over the frozen snapshot is `verify_business_metric_run`'s
    question, and answering it needs the records, which this function is not
    given.
    """
    fingerprint = validate_fingerprint(
        run_fingerprint, subject="the requested business metric run fingerprint"
    )
    directory = Path(root) / fingerprint
    if not directory.is_dir():
        raise BusinessMetricBindingError(
            f"no business metric run is stored at {directory}"
        )
    run = _read_verified_run(directory, directory / RUN_FILENAME)
    if run.run_fingerprint != fingerprint:
        raise BusinessMetricBindingError(
            f"the run stored at {directory} states fingerprint "
            f"{run.run_fingerprint} and {fingerprint} was requested"
        )
    return run
