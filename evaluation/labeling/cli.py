"""The local, blind-by-construction labelling CLI — `evaluation.labeling`.

    python -m evaluation.labeling.cli verify   --dataset-dir DIR
    python -m evaluation.labeling.cli select   --dataset-dir DIR --sample-size N
    python -m evaluation.labeling.cli show     --dataset-dir DIR --next
    python -m evaluation.labeling.cli label    --dataset-dir DIR \
        --opportunity-id ID --grade 0..3 [diagnostics] [--reason-tag T]...
    python -m evaluation.labeling.cli progress --dataset-dir DIR
    python -m evaluation.labeling.cli fingerprint --dataset-dir DIR

Six explicit commands rather than one interactive prompt loop. An interactive
session would be pleasanter to use and much harder to test, and the property
this slice must defend — that the annotator never sees a system verdict before
judging — is defended by tests. A command that can be run, captured and asserted
on is worth more here than a nicer prompt.

Every command starts by verifying the frozen dataset. There is no `--force`, no
`--skip-verify` and no fast path: a judgement made against an unverified dataset
is the failure this slice exists to prevent, so the gate is not optional and
cannot be argued with from the command line.

`show` prints the blind evidence view and nothing else. It does not print the
posting's stratum, its rank, its qualification or any other output of the
pipeline, and it will not do so even for a posting the operator has already
judged: the same command is used throughout a session, and a flag that revealed
verdicts "only afterwards" would be one mistyped invocation away from
contaminating the next judgement. The verdicts stay in the frozen dataset, where
the later metrics slice reads them.

`select` prints the drawn ids and the *aggregate* stratum counts. The per-item
strata are written to the selection file — a draw nobody can audit is a draw
nobody should trust — but they are printed only under `--reveal-strata`, which
exists for reviewing a draw after annotation and not before it.

Output is JSON on stdout, exit code 0 on success and 2 on a refusal, exactly
like the Phase 10.1 dataset CLI next door.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .blind import blind_evidence_payload
from .frozen import FrozenEvaluationDataset, read_frozen_dataset
from .schema import (
    HUMAN_LABEL_PROTOCOL_VERSION,
    HUMAN_LABEL_SCHEMA_VERSION,
    RELEVANCE_RUBRIC,
    DataAiJudgment,
    GeoJudgment,
    HumanLabelError,
    LabelDiagnostics,
    OpportunityTypeJudgment,
)
from .selection import (
    CalibrationSelection,
    assert_selection_bindings,
    select_calibration_sample,
)
from .storage import (
    DEFAULT_LABEL_ROOT,
    append_human_label,
    build_labelset_report,
    calibration_selection_payload,
    labelset_manifest_payload,
    list_calibration_selections,
    load_calibration_selection,
    read_label_history,
    resolve_effective_labels,
    write_calibration_selection,
    write_labelset_manifest,
)

__all__ = ["main", "parse_args"]

#: Printed by `verify` and `progress` so nobody has to read this package's
#: source to learn that the rubric is still a draft.
_PROTOCOL_STATUS = (
    "CALIBRATION — this protocol is not frozen. A human-relevance-v1 can only "
    "be declared after a real calibration round has been annotated and its "
    "ambiguous cases reviewed."
)


def _grade(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "grade must be an integer in 0..3"
        ) from error
    if parsed not in (0, 1, 2, 3):
        raise argparse.ArgumentTypeError("grade must be one of 0, 1, 2, 3")
    return parsed


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset-dir",
        required=True,
        help="the frozen Phase 10.1 dataset directory to label against",
    )
    parser.add_argument(
        "--labels-root",
        default=str(DEFAULT_LABEL_ROOT),
        help="where labelling artefacts are stored (never inside the dataset)",
    )


def _add_selection_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--selection",
        default=None,
        help=(
            "the calibration selection to work against, as a stored file path "
            "or a selection fingerprint prefix; optional when exactly one "
            "selection is stored for this dataset"
        ),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.labeling.cli",
        description=(
            "Human relevance labelling against a frozen evaluation dataset "
            f"({HUMAN_LABEL_PROTOCOL_VERSION})."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser(
        "verify", help="verify a frozen dataset and report what it says"
    )
    _add_common(verify)

    select = commands.add_parser(
        "select", help="draw a deterministic calibration lot"
    )
    _add_common(select)
    select.add_argument("--sample-size", required=True, type=_positive_integer)
    select.add_argument(
        "--dry-run",
        action="store_true",
        help="draw and report the lot without writing any file",
    )
    select.add_argument(
        "--reveal-strata",
        action="store_true",
        help=(
            "print the per-opportunity strata; for reviewing a draw AFTER "
            "annotation, never before"
        ),
    )

    show = commands.add_parser(
        "show", help="print the blind evidence view of one opportunity"
    )
    _add_common(show)
    _add_selection_option(show)
    target = show.add_mutually_exclusive_group(required=True)
    target.add_argument("--opportunity-id", type=_positive_integer)
    target.add_argument(
        "--next",
        action="store_true",
        dest="next_unjudged",
        help="the first unjudged opportunity of the calibration lot",
    )

    label = commands.add_parser("label", help="record one human judgement")
    _add_common(label)
    label.add_argument("--opportunity-id", required=True, type=_positive_integer)
    label.add_argument("--grade", required=True, type=_grade)
    label.add_argument(
        "--geo-judgment", choices=[str(value) for value in GeoJudgment]
    )
    label.add_argument(
        "--data-ai-judgment", choices=[str(value) for value in DataAiJudgment]
    )
    label.add_argument(
        "--opportunity-type-judgment",
        choices=[str(value) for value in OpportunityTypeJudgment],
    )
    label.add_argument("--reason-tag", action="append", dest="reason_tags")
    label.add_argument("--note")
    label.add_argument(
        "--relabel",
        action="store_true",
        help="correct an existing judgement; requires --relabel-reason",
    )
    label.add_argument("--relabel-reason")

    progress = commands.add_parser(
        "progress", help="report judged / unjudged state of a calibration lot"
    )
    _add_common(progress)
    _add_selection_option(progress)

    fingerprint = commands.add_parser(
        "fingerprint", help="print the current labelset fingerprint"
    )
    _add_common(fingerprint)
    _add_selection_option(fingerprint)
    fingerprint.add_argument(
        "--write-manifest",
        action="store_true",
        help="also write the labelset manifest beside the labels",
    )

    return parser.parse_args(argv)


def _dataset_identity(dataset: FrozenEvaluationDataset) -> dict[str, Any]:
    return {
        "dataset_id": dataset.dataset_id,
        "schema_version": dataset.schema_version,
        "content_fingerprint": dataset.content_fingerprint,
        "record_count": dataset.record_count,
        "profile_id": dataset.profile_id,
        "profile_context_fingerprint": dataset.profile_context_fingerprint,
    }


def _resolve_selection(
    dataset: FrozenEvaluationDataset, argument: str | None, root: str
) -> CalibrationSelection:
    """Find the lot a command should work against, verify it, or refuse to guess.

    One stored selection and no argument is unambiguous, so it is allowed. More
    than one is a question only the operator can answer, and answering it here —
    by taking the newest, say — would silently bind a set of judgements to a lot
    they were not drawn for.

    Whatever is found goes through `assert_selection_bindings` before it is
    returned, so a `--selection` pointing at another dataset's or another
    profile's lot is refused here — while it is still an argument — and not
    several steps later by whichever command happened to notice. Every path that
    uses a stored selection goes through this function, which is why the check
    lives in it.
    """
    selection = _load_selection(dataset, argument, root)
    assert_selection_bindings(selection, dataset)
    return selection


def _load_selection(
    dataset: FrozenEvaluationDataset, argument: str | None, root: str
) -> CalibrationSelection:
    """Locate the stored lot named by `--selection`, or the only one there is."""
    if argument:
        candidate = Path(argument)
        if candidate.is_file():
            return load_calibration_selection(candidate)
        matches = [
            path
            for path in list_calibration_selections(dataset.dataset_id, root)
            if argument in path.name
        ]
        if not matches:
            raise HumanLabelError(
                f"no stored calibration selection matches {argument!r}"
            )
        if len(matches) > 1:
            raise HumanLabelError(
                f"{argument!r} matches several stored selections: "
                + ", ".join(path.name for path in matches)
            )
        return load_calibration_selection(matches[0])

    stored = list_calibration_selections(dataset.dataset_id, root)
    if not stored:
        raise HumanLabelError(
            f"no calibration selection is stored for dataset "
            f"{dataset.dataset_id}; draw one with `select --sample-size N`"
        )
    if len(stored) > 1:
        raise HumanLabelError(
            "several calibration selections are stored for dataset "
            f"{dataset.dataset_id}; name one with --selection: "
            + ", ".join(path.name for path in stored)
        )
    return load_calibration_selection(stored[0])


def _verify(args: argparse.Namespace) -> dict[str, Any]:
    dataset = read_frozen_dataset(args.dataset_dir)
    payload = _dataset_identity(dataset)
    payload.update(
        {
            "integrity": "VERIFIED",
            "directory": str(dataset.directory),
            "ordering": dataset.ordering,
            "label_schema_version": HUMAN_LABEL_SCHEMA_VERSION,
            "protocol_version": HUMAN_LABEL_PROTOCOL_VERSION,
            "protocol_status": _PROTOCOL_STATUS,
        }
    )
    return payload


def _select(args: argparse.Namespace) -> dict[str, Any]:
    dataset = read_frozen_dataset(args.dataset_dir)
    selection = select_calibration_sample(dataset, args.sample_size)
    strata = Counter(canonical_json(item.stratum) for item in selection.items)
    payload: dict[str, Any] = {
        "dataset_id": selection.dataset_id,
        "dataset_content_fingerprint": selection.dataset_content_fingerprint,
        "selector_version": selection.selector_version,
        "protocol_version": selection.protocol_version,
        "requested_sample_size": selection.requested_sample_size,
        "effective_sample_size": selection.effective_sample_size,
        "selection_fingerprint": selection.selection_fingerprint,
        "selected_opportunity_ids": list(selection.opportunity_ids),
        # Aggregate only: how varied the lot is, without saying which posting
        # sits in which stratum.
        "stratum_counts": {
            key: count for key, count in sorted(strata.items())
        },
        "written": not args.dry_run,
    }
    if args.reveal_strata:
        payload["items"] = calibration_selection_payload(selection)["items"]
    if not args.dry_run:
        result = write_calibration_selection(selection, args.labels_root)
        payload["write_status"] = result.status
        payload["path"] = str(result.path)
    return payload


def _show(args: argparse.Namespace) -> dict[str, Any]:
    dataset = read_frozen_dataset(args.dataset_dir)
    if args.next_unjudged:
        selection = _resolve_selection(dataset, args.selection, args.labels_root)
        effective = resolve_effective_labels(
            read_label_history(dataset.dataset_id, args.labels_root)
        )
        remaining = [
            opportunity_id
            for opportunity_id in selection.opportunity_ids
            if opportunity_id not in effective
        ]
        if not remaining:
            return {
                "dataset_id": dataset.dataset_id,
                "selection_fingerprint": selection.selection_fingerprint,
                "note": "every opportunity in this calibration lot is judged",
                "unjudged_count": 0,
            }
        opportunity_id = remaining[0]
    else:
        opportunity_id = args.opportunity_id

    payload = blind_evidence_payload(dataset, opportunity_id)
    payload["rubric"] = [
        {"grade": grade, "name": name, "definition": definition}
        for grade, name, definition in RELEVANCE_RUBRIC
    ]
    payload["protocol_version"] = HUMAN_LABEL_PROTOCOL_VERSION
    payload["unjudged_is_not_zero"] = (
        "If you cannot judge this opportunity, record nothing. An unjudged "
        "opportunity has no label; it is never a 0."
    )
    return payload


def _label(args: argparse.Namespace) -> dict[str, Any]:
    dataset = read_frozen_dataset(args.dataset_dir)
    diagnostics = LabelDiagnostics(
        geo_judgment=(
            GeoJudgment(args.geo_judgment) if args.geo_judgment else None
        ),
        data_ai_judgment=(
            DataAiJudgment(args.data_ai_judgment)
            if args.data_ai_judgment
            else None
        ),
        opportunity_type_judgment=(
            OpportunityTypeJudgment(args.opportunity_type_judgment)
            if args.opportunity_type_judgment
            else None
        ),
    )
    label = append_human_label(
        dataset,
        opportunity_id=args.opportunity_id,
        relevance_grade=args.grade,
        diagnostics=diagnostics,
        reason_tags=args.reason_tags,
        note=args.note,
        relabel=args.relabel,
        relabel_reason=args.relabel_reason,
        root=args.labels_root,
    )
    return {
        "recorded": True,
        "dataset_id": label.dataset_id,
        "opportunity_id": label.opportunity_id,
        "relevance_grade": label.relevance_grade,
        "revision": label.revision,
        "relabel_reason": label.relabel_reason,
        "labeled_at": label.labeled_at,
        "protocol_version": label.protocol_version,
    }


def _progress(args: argparse.Namespace) -> dict[str, Any]:
    dataset = read_frozen_dataset(args.dataset_dir)
    selection = _resolve_selection(dataset, args.selection, args.labels_root)
    report = build_labelset_report(dataset, selection, args.labels_root)
    payload = labelset_manifest_payload(report)
    payload["protocol_status"] = _PROTOCOL_STATUS
    payload["note"] = (
        "unjudged opportunities carry no grade and are not counted as 0"
    )
    return payload


def _fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    dataset = read_frozen_dataset(args.dataset_dir)
    selection = _resolve_selection(dataset, args.selection, args.labels_root)
    report = build_labelset_report(dataset, selection, args.labels_root)
    payload: dict[str, Any] = {
        "dataset_id": report.dataset_id,
        "label_schema_version": report.label_schema_version,
        "protocol_version": report.protocol_version,
        "selector_version": report.selector_version,
        "selection_fingerprint": report.selection_fingerprint,
        "judged_count": report.judged_count,
        "unjudged_count": report.unjudged_count,
        "labelset_fingerprint": report.labelset_fingerprint,
    }
    if args.write_manifest:
        result = write_labelset_manifest(report, args.labels_root)
        payload["write_status"] = result.status
        payload["path"] = str(result.path)
    return payload


_COMMANDS = {
    "verify": _verify,
    "select": _select,
    "show": _show,
    "label": _label,
    "progress": _progress,
    "fingerprint": _fingerprint,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    handler = _COMMANDS[args.command]
    try:
        payload = handler(args)
    except HumanLabelError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
