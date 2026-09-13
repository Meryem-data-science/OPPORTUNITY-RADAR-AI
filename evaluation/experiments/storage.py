"""Where an experiment run lands on disk, and why it never moves again.

One file per run, under the project's local data space:

    data/evaluation/experiment_runs/<run_fingerprint>/run.json

**One authoritative document.** Not a manifest beside a results file, and not
one file per block: a run is a single indivisible statement — these twelve
blocks, over this assembly of frozen artefacts, under these definitions — and
splitting it across files creates a state in which some of them exist. The
directory is named after `run_fingerprint`, so the store is content-addressed:
the same experiment computed twice resolves to the same directory, and two
different experiments can never collide there.

**Nothing here touches the operational database**, makes a request, or reads a
human label. It writes one JSON file and reads it back.

## Immutable, and verified rather than trusted

    directory absent        write it, report CREATED
    present and identical   write nothing, report UNCHANGED, and return the run
                            that is actually on disk
    present and different   raise, and change nothing

"Identical" is decided by the contract's own **identity projection** — the
canonical D49 payload `run_fingerprint` is taken over — and only after *both*
runs have been fully structurally verified, which recomputes every one of the
twelve child digests and each run's own. Neither the stored `run_fingerprint`
nor the file's bytes decide it: the first is a claim a document makes about
itself and an edited file can keep it, and the second carries provenance that
identity deliberately excludes.

That last point is the lesson Phase 10.4 had to learn twice, and it is why this
comparison is a projection rather than "the document minus its provenance
block": two runs of the identical experiment read from checkouts at different
paths, or generated an hour apart, are **one run** — UNCHANGED, and not a byte
rewritten.

The run returned on UNCHANGED is the one **read back from disk**, with its
original provenance. A caller handed its own in-memory run instead would learn
nothing about what the store holds.

There is no `CONFLICT` status. A divergent document is a hard error: a status
that is reported and then ignored is how a frozen artefact gets overwritten.

## Verified before any official I/O

`write_experiment_run` fully verifies the run **and its context** before it
prepares a single byte. An artefact that would not survive being read back must
not be written in the first place, and a run whose memberships are not the ones
its artefacts imply must never reach a content-addressed store where its
fingerprint becomes its name.

## Publication is atomic and cannot lose a race

The document is rendered to a temporary file **outside** the destination, on the
same filesystem, fsynced, and then linked into place with `os.link` —
deliberately **not** `os.replace`. Both are atomic; only one of them refuses an
existing destination. `os.replace` silently overwrites, so the guard it needs is
a prior `exists()` check, and between that check and the call there is a window
in which another writer can publish. Losing that race means destroying a frozen
run, which is the one thing this store exists to make impossible.

Afterwards the temporary name is dropped **best effort**, by a helper that
cannot raise. Nothing between a successful publication and the CREATED return
may fail: once `run.json` exists it is complete, and a failure to tidy up a
stray dotfile must never be reported as a failure to write.

## Strict reading, and no repair

Every parser below requires its object to carry **exactly** the keys the
corresponding payload function writes. A missing key is a statement this build
needs and does not have; an unexpected one is a statement it cannot interpret,
and dropping it silently would let a document written under a wider contract be
read as though it had been written under this one. There is no repair, no
migration and no overwrite anywhere in this module.
"""

from __future__ import annotations

import json
import os
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from evaluation.metrics import (
    EvidenceClass,
    MetricName,
    MetricResult,
    MetricStatus,
    MetricSupport,
    MetricUnavailableReason,
)

from .fingerprint import canonical_experiment_run_payload
from .run import verify_experiment_run, verify_experiment_run_structure
from .schema import (
    CohortProjection,
    DirectionalRate,
    DirectionalRateName,
    ExperimentBindingError,
    ExperimentContractError,
    ExperimentRun,
    ExperimentRunContextBinding,
    ExperimentRunProvenance,
    OverlapPair,
    OverlapResult,
    OverlapStatus,
    OverlapUnavailableReason,
    ProjectionResult,
    ProjectionStatus,
    ProjectionUnavailableReason,
    RankingExperimentResult,
    RankingExperimentStatus,
    RankingExperimentUnavailableReason,
    RankingMetricEntry,
    experiment_run_payload,
    validate_experiment_fingerprint,
)

__all__ = [
    "DEFAULT_EXPERIMENT_RUN_ROOT",
    "RUN_FILENAME",
    "ExperimentRunStorageResult",
    "ExperimentRunWriteStatus",
    "read_experiment_run",
    "write_experiment_run",
]

#: Under `data/`, which is outside Git: an experiment run is derived from a
#: frozen snapshot of real postings and is not source.
DEFAULT_EXPERIMENT_RUN_ROOT = Path("data/evaluation/experiment_runs")

#: The one document. There is deliberately no second file.
RUN_FILENAME = "run.json"


class ExperimentRunWriteStatus(StrEnum):
    """What a write actually did. Two members, and there is no third.

    There is no `OVERWRITTEN`, no `REPAIRED` and no `CONFLICT`: a stored run is
    immutable, so every other outcome is a refusal that raises rather than a
    status that is reported and then ignored.
    """

    #: This call froze the run.
    CREATED = "CREATED"
    #: The run was already stored — by this call's experiment or an earlier
    #: identical one — and was verified rather than rewritten.
    UNCHANGED = "UNCHANGED"


class ExperimentRunStorageResult:
    """Where a run lives, what this call did, and what is actually stored.

    `run` is the **persisted artefact**, not necessarily the one that was passed
    in. On UNCHANGED it is the run read back from disk, carrying the provenance
    it was originally frozen with — so a caller learns what the store holds
    rather than being handed its own object back with a status attached.
    """

    __slots__ = ("directory", "run_file", "status", "run")

    def __init__(
        self,
        *,
        directory: Path,
        run_file: Path,
        status: ExperimentRunWriteStatus,
        run: ExperimentRun,
    ) -> None:
        self.directory = directory
        self.run_file = run_file
        self.status = status
        self.run = run

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"ExperimentRunStorageResult(directory={self.directory!r}, "
            f"status={self.status!r})"
        )


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _render(run: ExperimentRun) -> str:
    """The exact bytes of `run.json`, as text.

    Indented because a person reads this file; sorted keys and a fixed separator
    make that indentation just as reproducible as the compact canonical form.
    The formatting is not in any digest. One trailing newline, so the file ends
    the way a text file ends.
    """
    return (
        json.dumps(
            experiment_run_payload(run),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _semantic_identity(run: ExperimentRun) -> str:
    """What "the same run" means: the contract's own identity projection.

    The canonical D49 payload — the one `experiment_run_fingerprint` digests —
    and **not** the stored document with a block deleted from it. See the module
    docstring on why that difference is load-bearing rather than pedantic.

    Comparing the identity projection is not a weakening. Both runs have been
    fully structurally verified before this is called, which recomputes all
    twelve child digests and each run's own, so neither is a document making
    unchecked claims about itself. Once that holds, what the contract says two
    runs must agree on to be the same run is exactly this.
    """
    return canonical_json(canonical_experiment_run_payload(run))


# --------------------------------------------------------------------------
# strict reading
# --------------------------------------------------------------------------


def _object(value: Any, *, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExperimentBindingError(
            f"{subject} is not a JSON object: {value!r} ({type(value).__name__})"
        )
    return value


def _fields(
    value: Any, required: tuple[str, ...], *, subject: str
) -> dict[str, Any]:
    """Exactly these keys, no more and no fewer. The strict-key rule."""
    payload = _object(value, subject=subject)
    stated = set(payload)
    expected = set(required)
    if stated != expected:
        missing = sorted(expected - stated)
        unexpected = sorted(stated - expected)
        raise ExperimentBindingError(
            f"{subject} does not carry the fields of this contract (missing "
            f"{missing}, unexpected {unexpected}); refusing to read a document "
            "of another shape"
        )
    return payload


def _sequence(value: Any, *, subject: str) -> list[Any]:
    if not isinstance(value, list):
        raise ExperimentBindingError(
            f"{subject} is not a JSON array: {value!r} ({type(value).__name__})"
        )
    return value


def _member(enum: Any, value: Any, *, subject: str) -> Any:
    """One member of a closed vocabulary, or a refusal. Never a coercion."""
    try:
        return enum(value)
    except (ValueError, TypeError) as error:
        raise ExperimentContractError(
            f"{subject} is {value!r}, which is not a member of "
            f"{enum.__name__}; this build knows {[str(item) for item in enum]}"
        ) from error


def _optional_member(enum: Any, value: Any, *, subject: str) -> Any:
    return None if value is None else _member(enum, value, subject=subject)


def _optional_ids(value: Any, *, subject: str) -> tuple[int, ...] | None:
    """A membership, or `None` for its absence. `null` is not `[]` here."""
    if value is None:
        return None
    return tuple(_sequence(value, subject=subject))


_PROJECTION_FIELDS = (
    "result_schema_version",
    "contract_version",
    "projection",
    "status",
    "unavailable_reason",
    "dataset_id",
    "dataset_content_fingerprint",
    "cohort_size",
    "included_opportunity_ids",
    "included_count",
    "excluded_count",
    "cohort_share",
    "result_fingerprint",
)


def _parse_projection(value: Any, position: int) -> ProjectionResult:
    subject = f"projection {position} of the stored run"
    payload = _fields(value, _PROJECTION_FIELDS, subject=subject)
    return ProjectionResult(
        result_schema_version=payload["result_schema_version"],
        contract_version=payload["contract_version"],
        projection=_member(
            CohortProjection,
            payload["projection"],
            subject=f"the projection of {subject}",
        ),
        status=_member(
            ProjectionStatus, payload["status"], subject=f"the status of {subject}"
        ),
        unavailable_reason=_optional_member(
            ProjectionUnavailableReason,
            payload["unavailable_reason"],
            subject=f"the unavailable reason of {subject}",
        ),
        dataset_id=payload["dataset_id"],
        dataset_content_fingerprint=payload["dataset_content_fingerprint"],
        cohort_size=payload["cohort_size"],
        included_opportunity_ids=_optional_ids(
            payload["included_opportunity_ids"],
            subject=f"the membership of {subject}",
        ),
        included_count=payload["included_count"],
        excluded_count=payload["excluded_count"],
        cohort_share=payload["cohort_share"],
        # Read as stated. Every verifier recomputes it before believing it: a
        # stored digest is a claim a document makes about itself, never
        # evidence.
        result_fingerprint=payload["result_fingerprint"],
    )


_OVERLAP_FIELDS = (
    "result_schema_version",
    "contract_version",
    "pair",
    "status",
    "unavailable_reason",
    "dataset_id",
    "dataset_content_fingerprint",
    "cohort_size",
    "left_projection",
    "right_projection",
    "left_projection_result_fingerprint",
    "right_projection_result_fingerprint",
    "both_opportunity_ids",
    "left_only_opportunity_ids",
    "right_only_opportunity_ids",
    "neither_opportunity_ids",
    "both_count",
    "left_only_count",
    "right_only_count",
    "neither_count",
    "right_among_left",
    "left_among_right",
    "result_fingerprint",
)

_RATE_FIELDS = (
    "name",
    "status",
    "unavailable_reason",
    "numerator",
    "denominator",
    "value",
)


def _parse_rate(value: Any, *, subject: str) -> DirectionalRate | None:
    if value is None:
        return None
    payload = _fields(value, _RATE_FIELDS, subject=subject)
    return DirectionalRate(
        name=_member(
            DirectionalRateName, payload["name"], subject=f"the name of {subject}"
        ),
        status=_member(
            OverlapStatus, payload["status"], subject=f"the status of {subject}"
        ),
        unavailable_reason=_optional_member(
            OverlapUnavailableReason,
            payload["unavailable_reason"],
            subject=f"the unavailable reason of {subject}",
        ),
        numerator=payload["numerator"],
        denominator=payload["denominator"],
        value=payload["value"],
    )


def _parse_overlap(value: Any, position: int) -> OverlapResult:
    subject = f"overlap {position} of the stored run"
    payload = _fields(value, _OVERLAP_FIELDS, subject=subject)
    return OverlapResult(
        result_schema_version=payload["result_schema_version"],
        contract_version=payload["contract_version"],
        pair=_member(
            OverlapPair, payload["pair"], subject=f"the pair of {subject}"
        ),
        status=_member(
            OverlapStatus, payload["status"], subject=f"the status of {subject}"
        ),
        unavailable_reason=_optional_member(
            OverlapUnavailableReason,
            payload["unavailable_reason"],
            subject=f"the unavailable reason of {subject}",
        ),
        dataset_id=payload["dataset_id"],
        dataset_content_fingerprint=payload["dataset_content_fingerprint"],
        cohort_size=payload["cohort_size"],
        left_projection=_member(
            CohortProjection,
            payload["left_projection"],
            subject=f"the left projection of {subject}",
        ),
        right_projection=_member(
            CohortProjection,
            payload["right_projection"],
            subject=f"the right projection of {subject}",
        ),
        left_projection_result_fingerprint=payload[
            "left_projection_result_fingerprint"
        ],
        right_projection_result_fingerprint=payload[
            "right_projection_result_fingerprint"
        ],
        both_opportunity_ids=_optional_ids(
            payload["both_opportunity_ids"], subject=f"the `both` of {subject}"
        ),
        left_only_opportunity_ids=_optional_ids(
            payload["left_only_opportunity_ids"],
            subject=f"the `left_only` of {subject}",
        ),
        right_only_opportunity_ids=_optional_ids(
            payload["right_only_opportunity_ids"],
            subject=f"the `right_only` of {subject}",
        ),
        neither_opportunity_ids=_optional_ids(
            payload["neither_opportunity_ids"],
            subject=f"the `neither` of {subject}",
        ),
        both_count=payload["both_count"],
        left_only_count=payload["left_only_count"],
        right_only_count=payload["right_only_count"],
        neither_count=payload["neither_count"],
        right_among_left=_parse_rate(
            payload["right_among_left"],
            subject=f"the `right among left` rate of {subject}",
        ),
        left_among_right=_parse_rate(
            payload["left_among_right"],
            subject=f"the `left among right` rate of {subject}",
        ),
        result_fingerprint=payload["result_fingerprint"],
    )


_SUPPORT_FIELDS = (
    "k_requested",
    "k_effective",
    "ranking_length",
    "universe_size",
    "judged_count",
    "judged_in_top_k",
    "unjudged_in_top_k",
    "unjudged_in_universe",
    "relevant_count",
    "numerator",
    "denominator",
)

_METRIC_RESULT_FIELDS = ("metric", "status", "value", "reason", "support")
_ENTRY_FIELDS = ("metric", "k_requested", "result")


def _parse_metric_result(value: Any, *, subject: str) -> MetricResult:
    """One Phase 10.3 metric result, in Phase 10.3's own shape.

    Parsed back into Phase 10.3's dataclasses rather than into a shape of this
    package's own: the contract says the entry holds the **exact**
    `MetricResult`, so reading it as anything else would make the stored
    document and the computed one two different types that merely agree.
    """
    payload = _fields(value, _METRIC_RESULT_FIELDS, subject=subject)
    support_payload = _fields(
        payload["support"], _SUPPORT_FIELDS, subject=f"the support of {subject}"
    )
    support = MetricSupport(
        k_requested=support_payload["k_requested"],
        k_effective=support_payload["k_effective"],
        ranking_length=support_payload["ranking_length"],
        universe_size=support_payload["universe_size"],
        judged_count=support_payload["judged_count"],
        judged_in_top_k=support_payload["judged_in_top_k"],
        unjudged_in_top_k=tuple(
            _sequence(
                support_payload["unjudged_in_top_k"],
                subject=f"the unjudged top-K ids of {subject}",
            )
        ),
        unjudged_in_universe=tuple(
            _sequence(
                support_payload["unjudged_in_universe"],
                subject=f"the unjudged universe ids of {subject}",
            )
        ),
        relevant_count=support_payload["relevant_count"],
        numerator=support_payload["numerator"],
        denominator=support_payload["denominator"],
    )
    return MetricResult(
        metric=_member(
            MetricName, payload["metric"], subject=f"the metric of {subject}"
        ),
        status=_member(
            MetricStatus, payload["status"], subject=f"the status of {subject}"
        ),
        support=support,
        value=payload["value"],
        reason=_optional_member(
            MetricUnavailableReason,
            payload["reason"],
            subject=f"the reason of {subject}",
        ),
    )


_RANKING_FIELDS = (
    "result_schema_version",
    "contract_version",
    "status",
    "unavailable_reason",
    "dataset_id",
    "dataset_content_fingerprint",
    "profile_id",
    "profile_context_fingerprint",
    "recommendation_projection_result_fingerprint",
    "evaluation_run_fingerprint",
    "evaluation_universe_fingerprint",
    "ranking_fingerprint",
    "labelset_fingerprint",
    "evidence_class",
    "metric_results",
    "result_fingerprint",
)


def _parse_ranking(value: Any) -> RankingExperimentResult:
    subject = "the ranking block of the stored run"
    payload = _fields(value, _RANKING_FIELDS, subject=subject)
    entries: list[RankingMetricEntry] = []
    for position, item in enumerate(
        _sequence(payload["metric_results"], subject=f"the entries of {subject}"),
        start=1,
    ):
        entry_subject = f"ranking entry {position}"
        entry = _fields(item, _ENTRY_FIELDS, subject=entry_subject)
        entries.append(
            RankingMetricEntry(
                metric=_member(
                    MetricName,
                    entry["metric"],
                    subject=f"the metric of {entry_subject}",
                ),
                k_requested=entry["k_requested"],
                result=_parse_metric_result(
                    entry["result"], subject=f"the result of {entry_subject}"
                ),
            )
        )
    return RankingExperimentResult(
        result_schema_version=payload["result_schema_version"],
        contract_version=payload["contract_version"],
        status=_member(
            RankingExperimentStatus,
            payload["status"],
            subject=f"the status of {subject}",
        ),
        unavailable_reason=_optional_member(
            RankingExperimentUnavailableReason,
            payload["unavailable_reason"],
            subject=f"the unavailable reason of {subject}",
        ),
        dataset_id=payload["dataset_id"],
        dataset_content_fingerprint=payload["dataset_content_fingerprint"],
        profile_id=payload["profile_id"],
        profile_context_fingerprint=payload["profile_context_fingerprint"],
        recommendation_projection_result_fingerprint=payload[
            "recommendation_projection_result_fingerprint"
        ],
        evaluation_run_fingerprint=payload["evaluation_run_fingerprint"],
        evaluation_universe_fingerprint=payload["evaluation_universe_fingerprint"],
        ranking_fingerprint=payload["ranking_fingerprint"],
        labelset_fingerprint=payload["labelset_fingerprint"],
        evidence_class=_optional_member(
            EvidenceClass,
            payload["evidence_class"],
            subject=f"the evidence class of {subject}",
        ),
        metric_results=tuple(entries),
        result_fingerprint=payload["result_fingerprint"],
    )


_CONTEXT_FIELDS = (
    "run_context_schema_version",
    "experiment_contract_version",
    "dataset_id",
    "dataset_content_fingerprint",
    "profile_id",
    "profile_context_fingerprint",
    "business_metric_run_fingerprint",
    "ranking_evaluation_run_fingerprint",
    "context_fingerprint",
)


def _parse_context(value: Any) -> ExperimentRunContextBinding:
    payload = _fields(
        value, _CONTEXT_FIELDS, subject="the context binding of the stored run"
    )
    return ExperimentRunContextBinding(
        run_context_schema_version=payload["run_context_schema_version"],
        experiment_contract_version=payload["experiment_contract_version"],
        dataset_id=payload["dataset_id"],
        dataset_content_fingerprint=payload["dataset_content_fingerprint"],
        profile_id=payload["profile_id"],
        profile_context_fingerprint=payload["profile_context_fingerprint"],
        business_metric_run_fingerprint=payload[
            "business_metric_run_fingerprint"
        ],
        ranking_evaluation_run_fingerprint=payload[
            "ranking_evaluation_run_fingerprint"
        ],
        context_fingerprint=payload["context_fingerprint"],
    )


_PROVENANCE_FIELDS = (
    "generated_at",
    "dataset_directory",
    "business_metric_run_path",
    "label_root",
    "benchmark_records_path",
)

_RUN_FIELDS = (
    "run_schema_version",
    "contract_version",
    "context",
    "projections",
    "overlaps",
    "ranking",
    "run_fingerprint",
    "provenance",
)


def _parse_run(value: Any) -> ExperimentRun:
    payload = _fields(value, _RUN_FIELDS, subject="the stored experiment run")
    provenance = _fields(
        payload["provenance"],
        _PROVENANCE_FIELDS,
        subject="the provenance of the stored run",
    )
    return ExperimentRun(
        run_schema_version=payload["run_schema_version"],
        contract_version=payload["contract_version"],
        context=_parse_context(payload["context"]),
        projections=tuple(
            _parse_projection(item, position)
            for position, item in enumerate(
                _sequence(
                    payload["projections"],
                    subject="the projections of the stored run",
                ),
                start=1,
            )
        ),
        overlaps=tuple(
            _parse_overlap(item, position)
            for position, item in enumerate(
                _sequence(
                    payload["overlaps"], subject="the overlaps of the stored run"
                ),
                start=1,
            )
        ),
        ranking=_parse_ranking(payload["ranking"]),
        run_fingerprint=payload["run_fingerprint"],
        provenance=ExperimentRunProvenance(
            generated_at=provenance["generated_at"],
            dataset_directory=provenance["dataset_directory"],
            business_metric_run_path=provenance["business_metric_run_path"],
            label_root=provenance["label_root"],
            benchmark_records_path=provenance["benchmark_records_path"],
        ),
    )


def _load_document(run_file: Path, directory: Path) -> Any:
    """Read and decode `run.json`, or refuse the directory. No repair."""
    if not run_file.is_file():
        raise ExperimentBindingError(
            f"an experiment run directory exists at {directory} and holds no "
            f"{RUN_FILENAME}; it is incomplete, and this store neither repairs "
            "nor overwrites a run — remove the directory to recompute it"
        )
    try:
        text = run_file.read_text(encoding="utf-8")
    except OSError as error:
        raise ExperimentBindingError(
            f"cannot read the experiment run at {run_file}: {error}"
        ) from error
    except ValueError as error:
        raise ExperimentBindingError(
            f"the experiment run at {run_file} is not valid UTF-8"
        ) from error
    try:
        return json.loads(text)
    except ValueError as error:
        raise ExperimentBindingError(
            f"the experiment run at {run_file} is not valid JSON: {error}"
        ) from error


def _read_verified_run(directory: Path, run_file: Path) -> ExperimentRun:
    """Parse a stored run and re-establish everything it claims about itself.

    The structural verifier is what makes the stored digests worthless as
    assertions: it recomputes every one of them — the context binding's, all
    twelve blocks', and the run's own — re-derives each overlap's partitions
    from the run's own projections, and refuses anything that does not survive.
    A hand-edited `run_fingerprint` fails here, and so does an edited membership
    whose fingerprint was left alone.

    The directory's own name is held against the run as well: a document moved
    into another directory is filed under an identity that is not its own, and
    this store addresses runs by content.
    """
    run = _parse_run(_load_document(run_file, directory))
    verify_experiment_run_structure(run)
    if run.run_fingerprint != directory.name:
        raise ExperimentBindingError(
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

    `os.link` and deliberately **not** `os.replace` — see the module docstring.
    The link either creates the name or raises `FileExistsError`, decided by the
    filesystem under its own lock, so the check-then-act window `os.replace`
    would need simply does not exist.

    The temporary file is fully written and fsynced before this is called, so the
    name that appears is a complete document from the instant it exists — there
    is no moment at which `run.json` is half a run. Afterwards both paths name
    one inode, and the caller drops the temporary one through `_discard`.

    Portable to the platforms this project targets: `os.link` is implemented on
    POSIX and, on Windows, via `CreateHardLinkW` on NTFS. Both paths live under
    the same root and therefore the same filesystem, which hard links require.
    """
    try:
        os.link(temporary, run_file)
    except FileExistsError as error:
        raise ExperimentBindingError(
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

    What it must never do is turn a successful publication into a failure.

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


def write_experiment_run(
    run: ExperimentRun,
    context: Any,
    root: str | Path = DEFAULT_EXPERIMENT_RUN_ROOT,
) -> ExperimentRunStorageResult:
    """Freeze a run if it is new, verify it if it is not, never overwrite.

    The run is **fully** verified against its context before anything is
    prepared — the structure, every digest, and all twelve blocks recomputed
    over the frozen artefacts. `context` is required rather than optional for
    that reason: an artefact whose memberships are not the ones its records
    imply must never reach a content-addressed store where its fingerprint
    becomes its name, and a structural check alone cannot establish that.

    The document's bytes are rendered and written to a temporary file **outside**
    the official destination, on the same filesystem, and only then published:
    the final directory is created exclusively, and the complete file is linked
    onto a path that does not exist. A failure at any point leaves no readable
    run — at worst an empty directory, which `_read_verified_run` refuses rather
    than trusts.

    An existing directory is never written to. It is read, parsed, fully
    verified against the same context and compared semantically; identical
    content reports UNCHANGED without touching a byte, and anything else raises.
    """
    # Before any I/O. The order is the guarantee.
    verify_experiment_run(run, context)
    root_path = Path(root)
    directory = root_path / run.run_fingerprint
    run_file = directory / RUN_FILENAME

    if directory.exists():
        stored = _read_verified_run(directory, run_file)
        # The stored run is held to the same full verification, against the same
        # artefacts: a document that parses and is internally consistent is not
        # thereby a measurement of this snapshot.
        verify_experiment_run(stored, context)
        if _semantic_identity(stored) != _semantic_identity(run):
            # A backstop rather than a routine outcome, and worth saying why:
            # both runs have been verified, so each one's fingerprint has been
            # recomputed from its content, and the stored run's was additionally
            # required to equal this directory's name — which is this run's
            # fingerprint. Two identity projections that differ here would mean
            # two different contents digesting to one SHA-256. It raises rather
            # than assuming, because the alternative is overwriting a frozen run
            # on the strength of an assumption.
            raise ExperimentBindingError(
                f"a different experiment run is already stored at {directory}; "
                f"it states fingerprint {stored.run_fingerprint} and this one "
                f"states {run.run_fingerprint}, and their canonical identities "
                "do not agree — refusing to overwrite a frozen run"
            )
        # Identical experiment. A provenance that differs — another moment,
        # another directory — is not a difference in what was measured, so
        # nothing is rewritten and the caller is told what is actually stored.
        return ExperimentRunStorageResult(
            directory=directory,
            run_file=run_file,
            status=ExperimentRunWriteStatus.UNCHANGED,
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
        # inode the official path now also holds, and that is housekeeping
        # rather than part of the write. It is done in `finally`, by a helper
        # that cannot raise, so there is no statement between here and the
        # CREATED return that could fail.
    except FileExistsError as error:
        raise ExperimentBindingError(
            f"an experiment run appeared at {directory} while this one was being "
            "written; refusing to publish over it"
        ) from error
    except OSError as error:
        raise ExperimentBindingError(
            f"cannot write the experiment run to {directory}: {error}"
        ) from error
    finally:
        _discard(temporary)

    return ExperimentRunStorageResult(
        directory=directory,
        run_file=run_file,
        status=ExperimentRunWriteStatus.CREATED,
        run=run,
    )


def read_experiment_run(
    run_fingerprint: str,
    root: str | Path = DEFAULT_EXPERIMENT_RUN_ROOT,
) -> ExperimentRun:
    """Read one stored run by its fingerprint, re-establishing everything.

    The requested fingerprint, the directory's name, the document's own
    `run_fingerprint` and the digest recomputed from its content must all be the
    same string. Three of those four are claims somebody could have written; the
    fourth is computed here, and it is the one that decides.

    This performs the **structural** verification only, and does not pretend
    otherwise: it establishes that the document is a well-formed run whose every
    digest survives recomputation and whose overlaps agree with its own
    projections. Whether its memberships are the memberships the frozen records
    imply is `verify_experiment_run`'s question, and answering it needs the
    artefacts, which this function is not given.
    """
    fingerprint = validate_experiment_fingerprint(
        run_fingerprint, subject="the requested experiment run fingerprint"
    )
    directory = Path(root) / fingerprint
    if not directory.is_dir():
        raise ExperimentBindingError(
            f"no experiment run is stored at {directory}"
        )
    run = _read_verified_run(directory, directory / RUN_FILENAME)
    if run.run_fingerprint != fingerprint:
        raise ExperimentBindingError(
            f"the run stored at {directory} states fingerprint "
            f"{run.run_fingerprint} and {fingerprint} was requested"
        )
    return run
