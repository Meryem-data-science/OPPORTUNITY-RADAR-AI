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
    require_supported_protocol_version,
    validate_relevance_grade,
)
from .selection import (
    CALIBRATION_SELECTION_SCHEMA_VERSION,
    SUPPORTED_SELECTOR_VERSIONS,
    CalibrationSelection,
    CalibrationSelectionItem,
    assert_selection_bindings,
)

__all__ = [
    "DEFAULT_LABEL_ROOT",
    "LABELS_FILENAME",
    "WRITE_STATUS_CREATED",
    "WRITE_STATUS_UNCHANGED",
    "WRITE_STATUS_UPDATED",
    "LabelWriteResult",
    "LabelFile",
    "LabelsetReport",
    "append_human_label",
    "build_labelset_report",
    "calibration_selection_payload",
    "dataset_label_directory",
    "labelset_manifest_payload",
    "list_calibration_selections",
    "load_calibration_selection",
    "read_label_file",
    "read_label_history",
    "read_validated_label_file",
    "read_validated_label_history",
    "resolve_effective_labels",
    "validate_label_history",
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
    require_supported_protocol_version(
        payload.get("protocol_version"), subject=str(path)
    )
    selector_version = payload.get("selector_version")
    if selector_version not in SUPPORTED_SELECTOR_VERSIONS:
        raise HumanLabelError(
            f"{path} was drawn by selector {selector_version!r}; this build "
            f"implements {list(SUPPORTED_SELECTOR_VERSIONS)} and has no "
            "compatibility policy for another"
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


#: The exact key set of a stored label row. A row with a key more or a key less
#: is not a row this writer produced, and is refused rather than read around.
_LABEL_ROW_KEYS: frozenset[str] = frozenset(
    {
        "label_schema_version",
        "protocol_version",
        "dataset_id",
        "dataset_content_fingerprint",
        "profile_id",
        "profile_context_fingerprint",
        "opportunity_id",
        "relevance_grade",
        "relevance_grade_name",
        "diagnostics",
        "reason_tags",
        "note",
        "labeled_at",
        "revision",
        "relabel_reason",
    }
)

_DIAGNOSTIC_ENUMS: tuple[tuple[str, type], ...] = (
    ("geo_judgment", GeoJudgment),
    ("data_ai_judgment", DataAiJudgment),
    ("opportunity_type_judgment", OpportunityTypeJudgment),
)


def _stored_text(value: Any, *, where: str, field: str) -> str:
    """A field that must already be a non-empty JSON string."""
    if not isinstance(value, str) or not value:
        raise HumanLabelError(
            f"{where} states {field} {value!r}; a non-empty string was expected"
        )
    return value


def _stored_positive_integer(value: Any, *, where: str, field: str) -> int:
    """A field that must already be a positive JSON integer.

    `bool` is rejected before `int` because `isinstance(True, int)` is true in
    Python: `"revision": true` would otherwise be read as revision 1, and
    `"profile_id": true` as profile 1. A float is rejected too — `1.0` is not
    how this writer spells 1, and turning it into one would be the repair this
    function exists to refuse.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise HumanLabelError(
            f"{where} states {field} {value!r} ({type(value).__name__}); an "
            "integer was expected and no value is converted into one here"
        )
    if value <= 0:
        raise HumanLabelError(f"{where} states {field} {value}, which is not positive")
    return value


def _stored_optional_text(value: Any, *, where: str, field: str) -> str | None:
    """A field that must already be a string or JSON `null`, in canonical form.

    `normalize_note` is used as a *predicate*, never as a repair: if the stored
    value is not already what normalization would produce — an untrimmed note,
    an empty string standing in for `null` — the row is refused. Reading it as
    its normalized form would silently rewrite somebody's record on the next
    append.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise HumanLabelError(
            f"{where} states {field} {value!r} ({type(value).__name__}); a "
            "string or null was expected"
        )
    if normalize_note(value) != value:
        raise HumanLabelError(
            f"{where} states {field} {value!r}, which is not the canonical form "
            "this writer produces; refusing to normalize a stored judgement"
        )
    return value


def _stored_diagnostics(value: Any, *, where: str) -> LabelDiagnostics:
    """The diagnostics block, which must already be an object of the right shape."""
    if not isinstance(value, Mapping):
        raise HumanLabelError(
            f"{where} states diagnostics {value!r} ({type(value).__name__}); "
            "an object was expected"
        )
    expected = {name for name, _ in _DIAGNOSTIC_ENUMS}
    if set(value) != expected:
        missing = ", ".join(sorted(expected - set(value)))
        unexpected = ", ".join(sorted(set(value) - expected))
        raise HumanLabelError(
            f"{where} states a diagnostics block that is not the contract's "
            f"(missing: {missing or 'none'}; unexpected: {unexpected or 'none'})"
        )
    resolved: dict[str, Any] = {}
    for name, enum_class in _DIAGNOSTIC_ENUMS:
        raw = value[name]
        if raw is None:
            resolved[name] = None
            continue
        if not isinstance(raw, str):
            raise HumanLabelError(
                f"{where} states {name} {raw!r} ({type(raw).__name__}); a "
                "string or null was expected"
            )
        try:
            resolved[name] = enum_class(raw)
        except ValueError as error:
            raise HumanLabelError(
                f"{where} states unknown {name} {raw!r}"
            ) from error
    return LabelDiagnostics(**resolved)


def _stored_reason_tags(value: Any, *, where: str) -> tuple[str, ...]:
    """The tags, which must already be a list of strings in canonical form.

    Canonical means what `normalize_reason_tags` would have produced: lowercase,
    trimmed, de-duplicated and sorted. As with the note, normalization is the
    predicate and never the fix — a row holding `["Remote", "remote"]` is a row
    this writer did not write, and reading it as `["remote"]` would rewrite it.
    """
    if not isinstance(value, list):
        raise HumanLabelError(
            f"{where} states reason_tags {value!r} ({type(value).__name__}); "
            "a list was expected"
        )
    for tag in value:
        if not isinstance(tag, str):
            raise HumanLabelError(
                f"{where} states reason tag {tag!r} ({type(tag).__name__}); "
                "a string was expected"
            )
    normalized = normalize_reason_tags(value)
    if list(normalized) != value:
        raise HumanLabelError(
            f"{where} states reason_tags {value!r}, which is not the canonical "
            f"form this writer produces ({list(normalized)!r}); refusing to "
            "normalize a stored judgement"
        )
    return normalized


def _stored_timestamp(value: Any, *, where: str) -> str:
    """`labeled_at`, which must already be a string holding a real timestamp."""
    text = _stored_text(value, where=where, field="labeled_at")
    try:
        datetime.fromisoformat(text)
    except ValueError as error:
        raise HumanLabelError(
            f"{where} states labeled_at {text!r}, which is not an ISO-8601 "
            "timestamp"
        ) from error
    return text


def _parse_label(
    payload: Mapping[str, Any], *, path: Path, line: int
) -> HumanRelevanceLabel:
    """Read one stored row strictly, or refuse it. Never repair it.

    This function is the audit trail's boundary, and it is deliberately
    unhelpful. Nothing here coerces: no `int(...)` over a string, no `str(...)`
    over a number, no normalization of a tag list or a note that was not already
    canonical. The reason is not fussiness about types — it is that
    `append_human_label` used to rebuild the whole file from the objects this
    function returns, so any value quietly converted on the way in became a
    *rewritten stored row* on the way out. A file that somebody had edited, or
    that an older writer had produced, would be silently "repaired" by the act
    of recording an unrelated new judgement, and an append-only audit trail that
    edits its own history is not one.

    Two layers, in order:

    1. every field is checked against the raw JSON type the contract states,
       with `bool` refused wherever an integer is expected and the canonical
       forms of `reason_tags` and `note` required rather than produced;
    2. the resulting object is re-serialized through `human_label_payload` and
       compared, as canonical JSON, against the row that was read. Anything the
       first layer did not think of shows up here as a mismatch — a
       `relevance_grade_name` contradicting its grade, a stray key, a value
       whose spelling differs from the writer's.

    Layer 2 is the one that makes this safe going forward: it does not need to
    know what a future field is in order to refuse a row that does not round
    trip. The comparison is over *structures*, not bytes, so whitespace and key
    order in the file are irrelevant — only what the row says.
    """
    where = f"{path} line {line}"
    if set(payload) != _LABEL_ROW_KEYS:
        missing = ", ".join(sorted(_LABEL_ROW_KEYS - set(payload)))
        unexpected = ", ".join(sorted(set(payload) - _LABEL_ROW_KEYS))
        raise HumanLabelError(
            f"{where} is not a label row of this schema "
            f"(missing: {missing or 'none'}; unexpected: {unexpected or 'none'})"
        )

    schema_version = payload["label_schema_version"]
    if schema_version != HUMAN_LABEL_SCHEMA_VERSION:
        raise HumanLabelError(
            f"{where} states label schema {schema_version!r}; "
            f"this build reads {HUMAN_LABEL_SCHEMA_VERSION!r}"
        )
    # The shape being readable is not the same fact as the judgement being
    # interpretable. A row written under a future `human-relevance-v1` would
    # parse perfectly here — same fields, same types — and would then be
    # digested as though its grade answered this rubric's question. It does not,
    # so it stops here, unconverted and unrewritten.
    protocol_version = require_supported_protocol_version(
        payload["protocol_version"], subject=where
    )

    grade = validate_relevance_grade(payload["relevance_grade"])
    grade_name = payload["relevance_grade_name"]
    if grade_name != RELEVANCE_GRADE_NAMES[grade]:
        # Refused, never reconciled. The row states two things about one
        # judgement and they disagree; picking the number over the name would be
        # this code deciding what somebody meant.
        raise HumanLabelError(
            f"{where} states relevance_grade {grade} and "
            f"relevance_grade_name {grade_name!r}, which contradict each other "
            f"({RELEVANCE_GRADE_NAMES[grade]!r} was due); refusing to reconcile "
            "a stored judgement"
        )

    label = HumanRelevanceLabel(
        label_schema_version=schema_version,
        protocol_version=protocol_version,
        dataset_id=_stored_text(
            payload["dataset_id"], where=where, field="dataset_id"
        ),
        dataset_content_fingerprint=_stored_text(
            payload["dataset_content_fingerprint"],
            where=where,
            field="dataset_content_fingerprint",
        ),
        profile_id=_stored_positive_integer(
            payload["profile_id"], where=where, field="profile_id"
        ),
        profile_context_fingerprint=_stored_text(
            payload["profile_context_fingerprint"],
            where=where,
            field="profile_context_fingerprint",
        ),
        opportunity_id=_stored_positive_integer(
            payload["opportunity_id"], where=where, field="opportunity_id"
        ),
        relevance_grade=grade,
        diagnostics=_stored_diagnostics(payload["diagnostics"], where=where),
        reason_tags=_stored_reason_tags(payload["reason_tags"], where=where),
        note=_stored_optional_text(payload["note"], where=where, field="note"),
        labeled_at=_stored_timestamp(payload["labeled_at"], where=where),
        revision=_stored_positive_integer(
            payload["revision"], where=where, field="revision"
        ),
        relabel_reason=_stored_optional_text(
            payload["relabel_reason"], where=where, field="relabel_reason"
        ),
    )

    round_trip = human_label_payload(label)
    if canonical_json(round_trip) != canonical_json(payload):
        differing = sorted(
            key
            for key in _LABEL_ROW_KEYS
            if canonical_json(payload.get(key)) != canonical_json(round_trip.get(key))
        )
        raise HumanLabelError(
            f"{where} is not in the canonical form this writer produces; "
            f"{', '.join(differing)} would change if it were rewritten, and a "
            "stored judgement is never rewritten"
        )
    return label


@dataclass(frozen=True)
class LabelFile:
    """A labels file as it is on disk, and as it was understood.

    `lines` holds the stored rows **verbatim**, and it is the reason this type
    exists: an append writes those exact strings back and adds one, so recording
    a new judgement cannot alter a single character of an older one. The parsed
    `labels` are for reasoning about; the lines are what is preserved.
    """

    path: Path
    lines: tuple[str, ...]
    labels: tuple[HumanRelevanceLabel, ...]


def read_label_file(
    dataset_id: str, root: str | Path = DEFAULT_LABEL_ROOT
) -> LabelFile:
    """Read and strictly parse the labels file, keeping its rows verbatim."""
    path = _labels_path(dataset_label_directory(dataset_id, root))
    if not path.is_file():
        return LabelFile(path=path, lines=(), labels=())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HumanLabelError(f"cannot read {path}: {error}") from error

    lines: list[str] = []
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
        lines.append(line)
    return LabelFile(path=path, lines=tuple(lines), labels=tuple(labels))


def read_label_history(
    dataset_id: str, root: str | Path = DEFAULT_LABEL_ROOT
) -> tuple[HumanRelevanceLabel, ...]:
    """Every judgement ever recorded for this dataset, in file order.

    Includes superseded revisions: this is the audit trail, not the current
    state. `resolve_effective_labels` turns it into the latter.
    """
    return read_label_file(dataset_id, root).labels


def validate_label_history(
    history: Sequence[HumanRelevanceLabel], dataset: FrozenEvaluationDataset
) -> None:
    """Refuse a labels file that is not entirely about this dataset, in order.

    Called **before** a new judgement is resolved or written, and before a
    labelset is reported, because the alternative is worse than it looks: a
    file holding one row bound to another snapshot, or a revision chain with a
    hole in it, would otherwise quietly keep that row and gain a valid one
    behind it. The file would then be half-trustworthy, which for an audit trail
    means untrustworthy — and the corruption would be discovered, if ever, by
    whoever later tried to explain a number computed from it.

    So a bad row stops the write. Nothing is dropped, repaired, renumbered or
    quarantined: this code does not get to decide which of somebody's recorded
    judgements were real.

    Checked per row: the four bindings (dataset id, content fingerprint, profile
    id, profile context fingerprint) and that the opportunity is actually in the
    frozen dataset. Checked per opportunity, in file order: revisions run
    `1, 2, 3, ...` with no gap and no repeat, revision 1 claims no relabel
    reason, and every later revision states one.
    """
    revisions: dict[int, int] = {}
    for position, label in enumerate(history, start=1):
        _assert_bindings(label, dataset)
        if not dataset.contains(label.opportunity_id):
            raise HumanLabelError(
                f"label row {position} judges opportunity "
                f"{label.opportunity_id}, which is not in dataset "
                f"{dataset.dataset_id}"
            )
        if isinstance(label.revision, bool) or not isinstance(label.revision, int):
            raise HumanLabelError(
                f"label row {position} states a non-integer revision "
                f"{label.revision!r}"
            )
        expected = revisions.get(label.opportunity_id, 0) + 1
        if label.revision != expected:
            raise HumanLabelError(
                f"label row {position} states revision {label.revision} for "
                f"opportunity {label.opportunity_id}, where revision "
                f"{expected} was due; the label file is corrupt"
            )
        revisions[label.opportunity_id] = label.revision
        if label.revision == 1 and label.relabel_reason:
            raise HumanLabelError(
                f"label row {position} is a first judgement of opportunity "
                f"{label.opportunity_id} but states a relabel reason"
            )
        if label.revision > 1 and not label.relabel_reason:
            raise HumanLabelError(
                f"label row {position} corrects opportunity "
                f"{label.opportunity_id} without stating why"
            )


def read_validated_label_file(
    dataset: FrozenEvaluationDataset, root: str | Path = DEFAULT_LABEL_ROOT
) -> LabelFile:
    """The labels file of this dataset, refused whole if any row is wrong.

    The one way the rest of this package reads labels. Reading and validating
    are one call so that no path can accidentally take the unchecked one.
    """
    stored = read_label_file(dataset.dataset_id, root)
    validate_label_history(stored.labels, dataset)
    return stored


def read_validated_label_history(
    dataset: FrozenEvaluationDataset, root: str | Path = DEFAULT_LABEL_ROOT
) -> tuple[HumanRelevanceLabel, ...]:
    """The validated audit trail of this dataset, without its raw rows."""
    return read_validated_label_file(dataset, root).labels


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
    # Read and validate the whole file first. A judgement appended behind a row
    # that does not belong here would make the corruption permanent and give it
    # company.
    stored = read_validated_label_file(dataset, root)
    history = stored.labels
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

    # The stored rows are written back **verbatim**, not re-serialized from the
    # objects they were parsed into. Strict parsing already guarantees the two
    # would agree, so this is belt and braces — but it is the belt that makes
    # "append-only" a property of the bytes rather than a property of a proof.
    # A temporary file and `os.replace` still do the writing: append-only here
    # is an audit guarantee about content, not a claim about syscalls.
    lines = [*stored.lines, canonical_json(human_label_payload(label))]
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
    assert_selection_bindings(selection, dataset)
    history = read_validated_label_history(dataset, root)
    effective = resolve_effective_labels(history)

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
