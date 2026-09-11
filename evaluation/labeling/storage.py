"""Where a calibration lot and its human labels live, and how they are written.

Layout, under the project's local data space and outside Git in full:

    data/evaluation/labels/<dataset_id>/
        calibration-selection-<selection_fingerprint[:16]>.json
        labels.jsonl
        labelset-<selection_fingerprint[:16]>.json

**Beside the frozen dataset, not inside it.** Phase 10.1 froze
`data/evaluation/datasets/<dataset_id>/` and its whole contract is that the
directory never changes again. Labels are the opposite kind of artefact: they
grow, one judgement at a time, over days. Writing a growing file into a
directory whose defining property is immutability would make "frozen" a claim
about two of its files rather than about the directory, and the next person to
read it would have to know which two. So the labels get their own root, named
after the dataset they are bound to, and the frozen directory stays untouched.

**Selections are content-addressed.** A selection file is named after its own
digest, so re-deriving the same lot resolves to the same path and writes the
same bytes, while a lot drawn with a different `N` lands beside it instead of
overwriting it. There is no conflict to arbitrate, and an operator who tries
`--sample-size 12` and then `--sample-size 20` keeps both records of what they
did.

**Labels are append-only, and no label is ever silently replaced.** `labels.jsonl`
holds every judgement ever recorded, in the order it was recorded:

    a first judgement          revision 1, no reason required
    a second judgement of an
      already-labelled posting  REFUSED, unless the caller explicitly asks to
                                relabel *and* states why
    an explicit relabel        revision n+1, carrying `relabel_reason` and the
                                revision it supersedes; the earlier row stays

The effective label of an opportunity is its highest revision. Nothing is
deleted, nothing is edited in place, and a correction is therefore an event in
the file rather than a difference between two backups. That is the whole of the
overwrite policy: simple enough to audit by reading the file, and impossible to
trigger by accident, since a relabel needs two extra flags.

**Writes are atomic enough for one machine.** Each write goes to a temporary
file in the same directory and is then `os.replace`d over the target, which is
atomic on POSIX and on Windows for a same-directory rename. An interrupted write
leaves the previous complete file, never a half-written one. There is no lock:
this is a single operator annotating on their own machine, and a distributed
locking scheme would be complexity bought against a risk that does not exist
here. Two concurrent labelling processes on the same file is not a supported
mode, and the append path re-reads the file immediately before writing so the
window is as small as a read-modify-write can be.

**Nothing here is ever committed.** These files hold real opportunity ids and
one real person's judgements about them, so `data/` stays Git-ignored in full,
exactly as Phase 10.1 left it.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .fingerprint import calibration_selection_fingerprint, labelset_fingerprint
from .frozen import FrozenEvaluationDataset
from .schema import (
    HUMAN_LABEL_PROTOCOL_VERSION,
    HUMAN_LABEL_SCHEMA_VERSION,
    RELEVANCE_GRADE_NAMES,
    DataAiJudgment,
    GeoJudgment,
    HumanLabelError,
    HumanRelevanceLabel,
    LabelDiagnostics,
    OpportunityTypeJudgment,
    human_label_payload,
    normalize_note,
    normalize_reason_tags,
    validate_relevance_grade,
)
from .selection import (
    CALIBRATION_SELECTION_SCHEMA_VERSION,
    CalibrationSelection,
    CalibrationSelectionItem,
)

__all__ = [
    "DEFAULT_LABEL_ROOT",
    "LABELS_FILENAME",
    "WRITE_STATUS_CREATED",
    "WRITE_STATUS_UNCHANGED",
    "WRITE_STATUS_UPDATED",
    "LabelWriteResult",
    "LabelsetReport",
    "append_human_label",
    "build_labelset_report",
    "calibration_selection_payload",
    "dataset_label_directory",
    "labelset_manifest_payload",
    "list_calibration_selections",
    "load_calibration_selection",
    "read_label_history",
    "resolve_effective_labels",
    "write_calibration_selection",
    "write_labelset_manifest",
]

#: Relative on purpose, and under `data/` like the frozen datasets and the
#: operational database: local, Git-ignored, and no part of a deployment.
DEFAULT_LABEL_ROOT = Path("data/evaluation/labels")

LABELS_FILENAME = "labels.jsonl"
_SELECTION_PREFIX = "calibration-selection-"
_LABELSET_PREFIX = "labelset-"

WRITE_STATUS_CREATED = "CREATED"
#: The artefact was already on disk and says exactly what this call would have
#: written; nothing was rewritten.
WRITE_STATUS_UNCHANGED = "UNCHANGED"
#: A labelset manifest replaced an earlier one. Only this artefact is ever
#: updated: it reports a round in progress, and the round progresses.
WRITE_STATUS_UPDATED = "UPDATED"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def dataset_label_directory(
    dataset_id: str, root: str | Path = DEFAULT_LABEL_ROOT
) -> Path:
    """Where this dataset's labelling artefacts live."""
    return Path(root) / dataset_id


def _atomic_write(path: Path, text: str) -> None:
    """Write the whole file, or leave the previous one entirely intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise HumanLabelError(f"cannot write {path}: {error}") from error


# --------------------------------------------------------------------------
# the calibration selection artefact
# --------------------------------------------------------------------------


def calibration_selection_payload(
    selection: CalibrationSelection, *, generated_at: str | None = None
) -> dict[str, Any]:
    """The selection as it is written to disk.

    `generated_at` is provenance and is deliberately outside the digest, exactly
    as Phase 10.1 keeps its own `generated_at` outside the dataset digest: a lot
    re-derived tomorrow is the same lot.

    `items[].stratum` records the system outputs that placed each posting in the
    lot. It is written because a draw nobody can audit is a draw nobody should
    trust — and it is the reason this file must not be put in front of an
    annotator who has not yet judged the postings it names.
    """
    return {
        "selection_schema_version": selection.selection_schema_version,
        "selector_version": selection.selector_version,
        "protocol_version": selection.protocol_version,
        "dataset_id": selection.dataset_id,
        "dataset_content_fingerprint": selection.dataset_content_fingerprint,
        "profile_id": selection.profile_id,
        "profile_context_fingerprint": selection.profile_context_fingerprint,
        "requested_sample_size": selection.requested_sample_size,
        "effective_sample_size": selection.effective_sample_size,
        "generated_at": _utc_now() if generated_at is None else generated_at,
        "selection_fingerprint": selection.selection_fingerprint,
        "items": [
            {
                "position": item.position,
                "opportunity_id": item.opportunity_id,
                "stratum": dict(item.stratum),
                "diversity_key": dict(item.diversity_key),
            }
            for item in selection.items
        ],
    }


def _selection_path(directory: Path, selection_fingerprint: str) -> Path:
    return directory / f"{_SELECTION_PREFIX}{selection_fingerprint[:16]}.json"


@dataclass(frozen=True)
class LabelWriteResult:
    """What a write actually did, and where."""

    path: Path
    status: str


def write_calibration_selection(
    selection: CalibrationSelection, root: str | Path = DEFAULT_LABEL_ROOT
) -> LabelWriteResult:
    """Store a lot, or confirm the identical one already stored.

    A file whose name is derived from the digest can only be re-derived, never
    conflicted with — but it can still have been *edited*, so an existing file
    is compared field by field against what this call would have written, with
    `generated_at` allowed to differ and nothing else.
    """
    directory = dataset_label_directory(selection.dataset_id, root)
    path = _selection_path(directory, selection.selection_fingerprint)
    payload = calibration_selection_payload(selection)

    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise HumanLabelError(
                f"the calibration selection at {path} cannot be read: {error}"
            ) from error
        if not isinstance(stored, Mapping):
            raise HumanLabelError(f"{path} is not a JSON object")
        comparable_stored = {
            key: value for key, value in stored.items() if key != "generated_at"
        }
        comparable_new = {
            key: value for key, value in payload.items() if key != "generated_at"
        }
        if canonical_json(comparable_stored) != canonical_json(comparable_new):
            raise HumanLabelError(
                f"a different calibration selection is already stored at "
                f"{path} under the same fingerprint; refusing to overwrite it"
            )
        return LabelWriteResult(path=path, status=WRITE_STATUS_UNCHANGED)

    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return LabelWriteResult(path=path, status=WRITE_STATUS_CREATED)


def list_calibration_selections(
    dataset_id: str, root: str | Path = DEFAULT_LABEL_ROOT
) -> tuple[Path, ...]:
    directory = dataset_label_directory(dataset_id, root)
    if not directory.is_dir():
        return ()
    return tuple(sorted(directory.glob(f"{_SELECTION_PREFIX}*.json")))


def load_calibration_selection(path: str | Path) -> CalibrationSelection:
    """Read a stored lot back, refusing one this build does not understand.

    The stored `selection_fingerprint` is recomputed rather than believed: a
    file that was edited keeps whatever digest it was written with, and a lot
    identified by a digest it no longer matches would bind a set of judgements
    to a sample that never existed.
    """
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HumanLabelError(
            f"the calibration selection at {path} cannot be read: {error}"
        ) from error
    if not isinstance(payload, Mapping):
        raise HumanLabelError(f"{path} is not a JSON object")
    schema_version = payload.get("selection_schema_version")
    if schema_version != CALIBRATION_SELECTION_SCHEMA_VERSION:
        raise HumanLabelError(
            f"{path} states selection schema {schema_version!r}; this build "
            f"reads {CALIBRATION_SELECTION_SCHEMA_VERSION!r}"
        )
    items_payload = payload.get("items")
    if not isinstance(items_payload, Sequence):
        raise HumanLabelError(f"{path} states no items")
    items = tuple(
        CalibrationSelectionItem(
            position=int(item["position"]),
            opportunity_id=int(item["opportunity_id"]),
            stratum=dict(item.get("stratum") or {}),
            diversity_key=dict(item.get("diversity_key") or {}),
        )
        for item in items_payload
    )
    selection = CalibrationSelection(
        selection_schema_version=str(schema_version),
        selector_version=str(payload["selector_version"]),
        protocol_version=str(payload["protocol_version"]),
        dataset_id=str(payload["dataset_id"]),
        dataset_content_fingerprint=str(payload["dataset_content_fingerprint"]),
        profile_id=int(payload["profile_id"]),
        profile_context_fingerprint=str(payload["profile_context_fingerprint"]),
        requested_sample_size=int(payload["requested_sample_size"]),
        effective_sample_size=int(payload["effective_sample_size"]),
        items=items,
        selection_fingerprint=str(payload["selection_fingerprint"]),
    )
    recomputed = calibration_selection_fingerprint(
        selector_version=selection.selector_version,
        dataset_id=selection.dataset_id,
        dataset_content_fingerprint=selection.dataset_content_fingerprint,
        sample_size=selection.effective_sample_size,
        selected_opportunity_ids=selection.opportunity_ids,
    )
    if recomputed != selection.selection_fingerprint:
        raise HumanLabelError(
            f"{path} claims selection fingerprint "
            f"{selection.selection_fingerprint} but its contents digest to "
            f"{recomputed}; refusing an edited selection"
        )
    return selection


# --------------------------------------------------------------------------
# the labels themselves
# --------------------------------------------------------------------------


def _labels_path(directory: Path) -> Path:
    return directory / LABELS_FILENAME


def _parse_label(payload: Mapping[str, Any], *, path: Path, line: int) -> HumanRelevanceLabel:
    schema_version = payload.get("label_schema_version")
    if schema_version != HUMAN_LABEL_SCHEMA_VERSION:
        raise HumanLabelError(
            f"{path} line {line} states label schema {schema_version!r}; "
            f"this build reads {HUMAN_LABEL_SCHEMA_VERSION!r}"
        )

    def _enum(name: str, enum_class):
        raw = payload.get("diagnostics", {}).get(name)
        if raw is None:
            return None
        try:
            return enum_class(raw)
        except ValueError as error:
            raise HumanLabelError(
                f"{path} line {line} states unknown {name} {raw!r}"
            ) from error

    diagnostics = LabelDiagnostics(
        geo_judgment=_enum("geo_judgment", GeoJudgment),
        data_ai_judgment=_enum("data_ai_judgment", DataAiJudgment),
        opportunity_type_judgment=_enum(
            "opportunity_type_judgment", OpportunityTypeJudgment
        ),
    )
    return HumanRelevanceLabel(
        label_schema_version=str(schema_version),
        protocol_version=str(payload["protocol_version"]),
        dataset_id=str(payload["dataset_id"]),
        dataset_content_fingerprint=str(payload["dataset_content_fingerprint"]),
        profile_id=int(payload["profile_id"]),
        profile_context_fingerprint=str(payload["profile_context_fingerprint"]),
        opportunity_id=int(payload["opportunity_id"]),
        relevance_grade=validate_relevance_grade(payload["relevance_grade"]),
        diagnostics=diagnostics,
        reason_tags=normalize_reason_tags(payload.get("reason_tags")),
        note=normalize_note(payload.get("note")),
        labeled_at=str(payload["labeled_at"]),
        revision=int(payload.get("revision", 1)),
        relabel_reason=payload.get("relabel_reason"),
    )


def read_label_history(
    dataset_id: str, root: str | Path = DEFAULT_LABEL_ROOT
) -> tuple[HumanRelevanceLabel, ...]:
    """Every judgement ever recorded for this dataset, in file order.

    Includes superseded revisions: this is the audit trail, not the current
    state. `resolve_effective_labels` turns it into the latter.
    """
    path = _labels_path(dataset_label_directory(dataset_id, root))
    if not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HumanLabelError(f"cannot read {path}: {error}") from error

    labels: list[HumanRelevanceLabel] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise HumanLabelError(
                f"{path} line {number} is blank; refusing a damaged label file"
            )
        try:
            payload = json.loads(line)
        except ValueError as error:
            raise HumanLabelError(
                f"{path} line {number} is not valid JSON: {error}"
            ) from error
        if not isinstance(payload, Mapping):
            raise HumanLabelError(f"{path} line {number} is not a JSON object")
        labels.append(_parse_label(payload, path=path, line=number))
    return tuple(labels)


def resolve_effective_labels(
    history: Iterable[HumanRelevanceLabel],
) -> dict[int, HumanRelevanceLabel]:
    """The current judgement of each opportunity: its highest revision.

    Two rows sharing an opportunity *and* a revision is corruption, not a
    conflict to resolve by preferring one: it means two different judgements
    both claim to be the same revision, and nothing here can know which one the
    person meant.
    """
    effective: dict[int, HumanRelevanceLabel] = {}
    for label in history:
        current = effective.get(label.opportunity_id)
        if current is None:
            effective[label.opportunity_id] = label
            continue
        if label.revision == current.revision:
            raise HumanLabelError(
                f"opportunity {label.opportunity_id} has two labels at "
                f"revision {label.revision}; the label file is corrupt"
            )
        if label.revision > current.revision:
            effective[label.opportunity_id] = label
    return effective


def append_human_label(
    dataset: FrozenEvaluationDataset,
    *,
    opportunity_id: int,
    relevance_grade: Any,
    diagnostics: LabelDiagnostics | None = None,
    reason_tags: Iterable[str] | None = None,
    note: str | None = None,
    relabel: bool = False,
    relabel_reason: str | None = None,
    labeled_at: str | None = None,
    root: str | Path = DEFAULT_LABEL_ROOT,
) -> HumanRelevanceLabel:
    """Record one judgement, refusing anything that would overwrite another.

    Every binding is checked against the verified dataset rather than trusted
    from the caller: the opportunity must be *in* this dataset, and the label
    carries this dataset's id, its content fingerprint, its profile id and its
    profile context fingerprint. A label whose bindings were supplied by hand
    could otherwise attach a judgement to a snapshot it was never made against.

    A second judgement of an already-judged opportunity is refused unless
    `relabel=True` **and** a `relabel_reason` is given. The refusal names the
    grade already on file, so an operator who hit the wrong key learns what they
    were about to replace.
    """
    if not dataset.contains(opportunity_id):
        raise HumanLabelError(
            f"opportunity {opportunity_id} is not in dataset "
            f"{dataset.dataset_id}; a judgement can only be recorded against "
            "the frozen evidence it was made from"
        )
    grade = validate_relevance_grade(relevance_grade)
    tags = normalize_reason_tags(reason_tags)
    cleaned_note = normalize_note(note)
    reason = normalize_note(relabel_reason)
    if relabel and not reason:
        raise HumanLabelError(
            "a relabel must state why: pass an explicit relabel reason"
        )
    if reason and not relabel:
        raise HumanLabelError(
            "a relabel reason was given without asking for a relabel"
        )

    directory = dataset_label_directory(dataset.dataset_id, root)
    history = read_label_history(dataset.dataset_id, root)
    effective = resolve_effective_labels(history)
    previous = effective.get(opportunity_id)

    if previous is not None and not relabel:
        raise HumanLabelError(
            f"opportunity {opportunity_id} is already labelled "
            f"{previous.relevance_grade} "
            f"({RELEVANCE_GRADE_NAMES[previous.relevance_grade]}) at revision "
            f"{previous.revision}; recording a different judgement requires an "
            "explicit relabel with a stated reason"
        )
    if previous is None and relabel:
        raise HumanLabelError(
            f"opportunity {opportunity_id} has no label to correct"
        )

    label = HumanRelevanceLabel(
        label_schema_version=HUMAN_LABEL_SCHEMA_VERSION,
        protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        opportunity_id=opportunity_id,
        relevance_grade=grade,
        diagnostics=diagnostics or LabelDiagnostics(),
        reason_tags=tags,
        note=cleaned_note,
        labeled_at=_utc_now() if labeled_at is None else labeled_at,
        revision=1 if previous is None else previous.revision + 1,
        relabel_reason=reason,
    )

    lines = [canonical_json(human_label_payload(item)) for item in history]
    lines.append(canonical_json(human_label_payload(label)))
    _atomic_write(_labels_path(directory), "\n".join(lines) + "\n")
    return label


# --------------------------------------------------------------------------
# progress and the labelset manifest
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelsetReport:
    """Where a calibration round stands, and what its judgements digest to.

    `unjudged_opportunity_ids` is a first-class part of the report and is never
    counted as a grade. A round with 4 judgements out of 12 has four labels and
    eight unanswered questions — not four labels and eight zeros — and the
    grade distribution below sums to the former.
    """

    dataset_id: str
    dataset_content_fingerprint: str
    profile_id: int
    profile_context_fingerprint: str
    label_schema_version: str
    protocol_version: str
    selector_version: str
    selection_fingerprint: str
    selected_count: int
    judged_count: int
    unjudged_count: int
    unjudged_opportunity_ids: tuple[int, ...]
    judged_outside_selection: tuple[int, ...]
    grade_distribution: Mapping[str, int]
    revision_count: int
    labelset_fingerprint: str


def build_labelset_report(
    dataset: FrozenEvaluationDataset,
    selection: CalibrationSelection,
    root: str | Path = DEFAULT_LABEL_ROOT,
) -> LabelsetReport:
    """Read the labels of one dataset and report the state of one calibration lot.

    The labelset digest covers the judgements **of the selected postings**: a lot
    is the question being asked, and a judgement recorded outside it belongs to
    a different question. Such judgements are not discarded — they are reported
    under `judged_outside_selection` so nothing goes missing silently — but they
    do not enter this lot's digest.
    """
    if selection.dataset_id != dataset.dataset_id:
        raise HumanLabelError(
            f"the selection belongs to dataset {selection.dataset_id}, not "
            f"{dataset.dataset_id}"
        )
    if selection.dataset_content_fingerprint != dataset.content_fingerprint:
        raise HumanLabelError(
            "the selection was drawn from a different content fingerprint than "
            "the dataset it is being reported against"
        )

    history = read_label_history(dataset.dataset_id, root)
    effective = resolve_effective_labels(history)
    for label in effective.values():
        _assert_bindings(label, dataset)

    selected = selection.opportunity_ids
    selected_set = set(selected)
    in_lot = [
        effective[opportunity_id]
        for opportunity_id in selected
        if opportunity_id in effective
    ]
    unjudged = tuple(
        opportunity_id
        for opportunity_id in selected
        if opportunity_id not in effective
    )
    outside = tuple(
        sorted(
            opportunity_id
            for opportunity_id in effective
            if opportunity_id not in selected_set
        )
    )

    distribution = {name: 0 for name in RELEVANCE_GRADE_NAMES.values()}
    for label in in_lot:
        distribution[RELEVANCE_GRADE_NAMES[label.relevance_grade]] += 1

    fingerprint = labelset_fingerprint(
        label_schema_version=HUMAN_LABEL_SCHEMA_VERSION,
        protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        selector_version=selection.selector_version,
        selection_fingerprint=selection.selection_fingerprint,
        labels=in_lot,
    )
    return LabelsetReport(
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        label_schema_version=HUMAN_LABEL_SCHEMA_VERSION,
        protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        selector_version=selection.selector_version,
        selection_fingerprint=selection.selection_fingerprint,
        selected_count=len(selected),
        judged_count=len(in_lot),
        unjudged_count=len(unjudged),
        unjudged_opportunity_ids=unjudged,
        judged_outside_selection=outside,
        grade_distribution=distribution,
        revision_count=len(history),
        labelset_fingerprint=fingerprint,
    )


def _assert_bindings(
    label: HumanRelevanceLabel, dataset: FrozenEvaluationDataset
) -> None:
    """Refuse a stored label that was made against something else.

    Three separate mismatches, three separate messages, because they mean three
    different things: a different dataset, the same dataset id with different
    contents, and the same contents judged for a different profile state.
    """
    if label.dataset_id != dataset.dataset_id:
        raise HumanLabelError(
            f"label for opportunity {label.opportunity_id} belongs to dataset "
            f"{label.dataset_id}, not {dataset.dataset_id}"
        )
    if label.dataset_content_fingerprint != dataset.content_fingerprint:
        raise HumanLabelError(
            f"label for opportunity {label.opportunity_id} was made against "
            f"content fingerprint {label.dataset_content_fingerprint}, not "
            f"{dataset.content_fingerprint}"
        )
    if label.profile_context_fingerprint != dataset.profile_context_fingerprint:
        raise HumanLabelError(
            f"label for opportunity {label.opportunity_id} was made against "
            "a different profile context fingerprint "
            f"({label.profile_context_fingerprint}, not "
            f"{dataset.profile_context_fingerprint})"
        )
    if label.profile_id != dataset.profile_id:
        raise HumanLabelError(
            f"label for opportunity {label.opportunity_id} was made for "
            f"profile {label.profile_id}, not {dataset.profile_id}"
        )


def labelset_manifest_payload(
    report: LabelsetReport, *, generated_at: str | None = None
) -> dict[str, Any]:
    """The labelset manifest as it is written to disk.

    `generated_at` is provenance and sits outside `labelset_fingerprint`, which
    is computed from the judgements alone. A round re-reported an hour later
    reports the same digest.
    """
    return {
        "label_schema_version": report.label_schema_version,
        "protocol_version": report.protocol_version,
        "protocol_status": (
            "CALIBRATION — this protocol is not frozen; a human-relevance-v1 "
            "can only be declared after a real calibration round has been "
            "annotated and its ambiguous cases reviewed"
        ),
        "dataset_id": report.dataset_id,
        "dataset_content_fingerprint": report.dataset_content_fingerprint,
        "profile_id": report.profile_id,
        "profile_context_fingerprint": report.profile_context_fingerprint,
        "selector_version": report.selector_version,
        "selection_fingerprint": report.selection_fingerprint,
        "generated_at": _utc_now() if generated_at is None else generated_at,
        "selected_count": report.selected_count,
        "judged_count": report.judged_count,
        "unjudged_count": report.unjudged_count,
        "unjudged_opportunity_ids": list(report.unjudged_opportunity_ids),
        "judged_outside_selection": list(report.judged_outside_selection),
        "grade_distribution": dict(report.grade_distribution),
        "recorded_label_rows": report.revision_count,
        "labelset_fingerprint": report.labelset_fingerprint,
    }


def write_labelset_manifest(
    report: LabelsetReport, root: str | Path = DEFAULT_LABEL_ROOT
) -> LabelWriteResult:
    """Write the current state of one calibration lot.

    Unlike a selection or a dataset, this file *is* expected to change: it is a
    snapshot of a round in progress, and the round progresses. It is rewritten
    atomically under a name derived from the selection it reports on, so two
    lots never overwrite each other's manifest.
    """
    directory = dataset_label_directory(report.dataset_id, root)
    path = (
        directory
        / f"{_LABELSET_PREFIX}{report.selection_fingerprint[:16]}.json"
    )
    existed = path.exists()
    _atomic_write(
        path,
        json.dumps(
            labelset_manifest_payload(report),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return LabelWriteResult(
        path=path,
        status=WRITE_STATUS_UPDATED if existed else WRITE_STATUS_CREATED,
    )
