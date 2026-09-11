"""The versioned vocabulary of a human relevance judgement — Phase 10.2.

This module is the *contract* of a human label and nothing else: the rubric a
judgement is expressed in, the diagnostics that can accompany it, the validation
that decides whether a judgement is well formed, and the one canonical payload
every consumer reads a label through. It opens no file, computes no digest and
knows nothing about a dataset on disk.

Three rules shape everything below, and two of them are inherited from Phase
10.1 on purpose.

* **UNJUDGED is not a zero.** An opportunity nobody has judged has *no label
  row*. There is no implicit grade, no default, no "assume out of target". A
  zero is a statement a person made; an absence is the absence of a person.
  Nothing in this package ever manufactures the former out of the latter.
* **UNKNOWN is a value, not an absence.** Each diagnostic enum carries its own
  explicit unknown member, and a diagnostic that is `None` means the annotator
  said nothing about that axis at all. Those are two different facts — "I looked
  and I cannot tell" and "I did not look" — and folding either into a negative
  would delete exactly the cases a calibration round exists to find.
* **The payload judged is the payload fingerprinted.** `human_label_payload`
  writes a label to JSONL and `human_label_semantic_payload` projects the same
  object onto the half of it that *is* the judgement, so "what the file says"
  and "what the labelset digest covers" cannot drift apart.

**Two protocol versions, and this module owns one of them.**
`HUMAN_LABEL_PROTOCOL_VERSION` is `human-relevance-calibration-v0`: the protocol
the writer records and the reader interprets, and the one every label that
exists was made under. A real calibration round has been run under it on the
operator's machine.

The *semantic* contract frozen after that round is a different string and lives
in `rubric.py` — `FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION`,
`human-relevance-v1`. **No label carries it**, because none has been made under
it: freezing a definition and producing judgements under it are two different
acts. `SUPPORTED_PROTOCOL_VERSIONS` below therefore still holds only `v0`, so a
row claiming v1 is refused on read rather than mixed into a calibration history.
Separating a real v1 labelset from that history is a storage question, and it is
future work.

The dependency direction, which is the architectural point of the slice:

    Phase 10.1 frozen dataset
        -> human labels (here)
            -> later Phase 10 slices (metrics, error analysis)

and never the reverse. No production module imports anything from `evaluation.`,
and nothing here computes a ranking metric: this slice produces judgements, not
scores.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from evaluation.dataset import EvaluationDatasetError

__all__ = [
    "HUMAN_LABEL_PROTOCOL_VERSION",
    "HUMAN_LABEL_SCHEMA_VERSION",
    "RELEVANCE_GRADES",
    "RELEVANCE_GRADE_NAMES",
    "RELEVANCE_RUBRIC",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "DataAiJudgment",
    "GeoJudgment",
    "HumanLabelError",
    "HumanRelevanceLabel",
    "LabelDiagnostics",
    "OpportunityTypeJudgment",
    "canonical_label_order",
    "human_label_payload",
    "human_label_semantic_payload",
    "label_diagnostics_payload",
    "normalize_note",
    "normalize_reason_tags",
    "require_supported_protocol_version",
    "validate_relevance_grade",
]


#: The *shape* of a label row. It moves whenever a field is added, removed or
#: re-interpreted, so a reader can refuse a labels file it does not understand
#: instead of reading a missing key as a missing judgement.
HUMAN_LABEL_SCHEMA_VERSION = "human-label-v1"

#: The *protocol* the judgement was made under — the rubric, its wording, and
#: the instructions given to the annotator. Separate from the schema version on
#: purpose: two labels can share a row shape and mean different things because
#: the rubric they were produced under differs, and comparing them would be
#: comparing two questions rather than two answers.
#:
#: `v0` is the calibration protocol, and it is what the **writer records** and
#: the reader interprets: every label that exists was made under it.
#:
#: The *semantic* contract frozen after calibration is a different string —
#: `rubric.FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION`, `human-relevance-v1` — and
#: the two are deliberately not unified here. Switching this constant to v1
#: would either rewrite the recorded protocol of labels that were not made under
#: it, or append v1 rows into a file of v0 rows and leave one history holding
#: judgements of two different rubrics. Neither is acceptable for an audit
#: trail, and separating the two histories properly is a storage question this
#: slice does not answer.
HUMAN_LABEL_PROTOCOL_VERSION = "human-relevance-calibration-v0"


#: The protocol versions this build is able to *interpret*. Exactly one, and
#: that is the policy rather than an oversight: a stored judgement means what it
#: meant under the protocol it was made under, and this build only knows one
#: protocol. When `human-relevance-v1` arrives it will have a different rubric
#: and possibly different diagnostics, so a v0 label read under it would be a
#: judgement of one question reported as a judgement of another. Widening this
#: tuple is therefore a deliberate act that must come with a stated compatibility
#: policy — never a side effect of bumping a version string.
#:
#: `human-relevance-v1` is now frozen semantically (see `rubric.py`) and is
#: still **absent from this tuple on purpose**. No label carries it, and until a
#: v1 labelset is stored separately from the calibration history, a row claiming
#: it is a row that does not belong in the file it was found in.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (HUMAN_LABEL_PROTOCOL_VERSION,)


class HumanLabelError(EvaluationDatasetError):
    """Raised when a human label cannot be built, read or stored safely.

    A subclass of the Phase 10.1 error on purpose: a caller that already refuses
    a broken evaluation artefact refuses a broken label without learning a
    second exception, and the two failures really are the same kind of failure —
    an artefact that cannot be trusted to say what it appears to say.
    """


def require_supported_protocol_version(value: Any, *, subject: str) -> str:
    """Refuse an artefact produced under a protocol this build cannot interpret.

    The row *shape* is checked separately, by the label schema version, and the
    two checks are not interchangeable — which is the whole reason this function
    exists. A `human-relevance-v1` label would almost certainly parse under
    `human-label-v1`: same fields, same types, same JSON. What differs is the
    question the annotator was answering. Reading such a row here and folding it
    into a labelset digest would report judgements of one rubric as judgements
    of another, and the digest would not show it.

    So an unknown protocol stops at the boundary. Nothing is converted, nothing
    is migrated in place and no stored row is rewritten: the artefact is left
    exactly as it is, and this build declines to speak for it.
    """
    if value not in SUPPORTED_PROTOCOL_VERSIONS:
        raise HumanLabelError(
            f"{subject} states protocol version {value!r}; this build reads "
            f"{list(SUPPORTED_PROTOCOL_VERSIONS)} and will not reinterpret a "
            "judgement made under another protocol"
        )
    return str(value)


# --------------------------------------------------------------------------
# the rubric
# --------------------------------------------------------------------------

#: The primary judgement: one integer, four levels, and the level *is* the
#: truth a later slice measures against. The names are carried beside the
#: integers so an artefact never states a bare number a reader has to decode.
RELEVANCE_GRADES: tuple[int, ...] = (0, 1, 2, 3)

RELEVANCE_GRADE_NAMES: Mapping[int, str] = {
    3: "VERY_RELEVANT",
    2: "RELEVANT",
    1: "WEAKLY_RELEVANT",
    0: "OUT_OF_TARGET",
}

#: The rubric as it is shown to the annotator, in the order it is read. Held
#: here rather than in the CLI so the wording a judgement was made under is part
#: of the versioned contract and not part of a print statement.
RELEVANCE_RUBRIC: tuple[tuple[int, str, str], ...] = (
    (
        3,
        "VERY_RELEVANT",
        "Clearly a good opportunity for the profile and target being "
        "evaluated. You would want it surfaced first.",
    ),
    (
        2,
        "RELEVANT",
        "Relevant and reasonably actionable, with some reservations "
        "possible. You would want it surfaced.",
    ),
    (
        1,
        "WEAKLY_RELEVANT",
        "Partial or weak link, borderline, or a fit too thin to justify a "
        "strong recommendation.",
    ),
    (
        0,
        "OUT_OF_TARGET",
        "Not a relevant opportunity for this target. Note that this is a "
        "judgement someone made; an opportunity nobody judged has no label "
        "at all and is never a 0.",
    ),
)


def validate_relevance_grade(value: Any) -> int:
    """Return `value` as a rubric grade, or refuse it.

    Strict on the type as well as the range, and `bool` is refused explicitly.
    In Python `True == 1` and `isinstance(True, int)` is true, so a `True` that
    reached this function through a JSON round trip or a careless caller would
    otherwise be stored as WEAKLY_RELEVANT — a grade nobody assigned, indexed
    under a person's name. A boolean is not a judgement on a four-level scale,
    and the only safe reading of one is to stop.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise HumanLabelError(
            f"relevance_grade must be an integer in {list(RELEVANCE_GRADES)}, "
            f"not {value!r} ({type(value).__name__})"
        )
    if value not in RELEVANCE_GRADES:
        raise HumanLabelError(
            f"relevance_grade must be one of {list(RELEVANCE_GRADES)}, "
            f"not {value!r}"
        )
    return value


# --------------------------------------------------------------------------
# the diagnostics
# --------------------------------------------------------------------------
#
# Optional, versioned, and never used to recompute the grade. They exist so a
# later error analysis can ask *why* a judgement and a pipeline verdict differ —
# geography, Data/AI domain, or the kind of opportunity — instead of only that
# they do. The grade stays the primary truth.
#
# Every enum carries its own explicit unknown member, and a diagnostic left
# unset is `None`. Those are different statements and both are preserved:
#
#     None                    the annotator said nothing about this axis
#     ..._UNKNOWN             the annotator looked and could not tell
#     NON_TARGET / NON_...    the annotator decided against
#
# Collapsing the first two into the third is the single most tempting and most
# destructive simplification available here, and nothing in this package does it.


class GeoJudgment(StrEnum):
    """Is this posting located where the evaluated target actually is?"""

    TARGET_GEOGRAPHY = "TARGET_GEOGRAPHY"
    NON_TARGET_GEOGRAPHY = "NON_TARGET_GEOGRAPHY"
    UNKNOWN_GEOGRAPHY = "UNKNOWN_GEOGRAPHY"


class DataAiJudgment(StrEnum):
    """Is the *work* Data/AI, as a person reads the posting?

    Four members rather than three because the repository's own qualification
    taxonomy already distinguishes a core Data/AI role from an adjacent one, and
    a human diagnostic that could not express that distinction would be unable
    to disagree with the classifier in the most interesting way it can be wrong.
    The names are deliberately *not* the classifier's own `CORE_TARGET` /
    `ADJACENT_TARGET` / `OUT_OF_SCOPE` / `UNCERTAIN`, so no reader can mistake a
    human diagnostic for a system verdict in a later join.
    """

    CORE_DATA_AI = "CORE_DATA_AI"
    ADJACENT_DATA_AI = "ADJACENT_DATA_AI"
    NON_DATA_AI = "NON_DATA_AI"
    UNKNOWN_DATA_AI = "UNKNOWN_DATA_AI"


class OpportunityTypeJudgment(StrEnum):
    """Is this the *kind* of opportunity the target is looking for?"""

    TARGET_TYPE = "TARGET_TYPE"
    NON_TARGET_TYPE = "NON_TARGET_TYPE"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"


@dataclass(frozen=True)
class LabelDiagnostics:
    """The optional structured diagnostics beside one grade.

    All three default to `None`, which is "not stated" and is written to the
    artefact as `null` rather than omitted: a reader must be able to tell a
    diagnostic nobody filled in from a schema that never had the field.
    """

    geo_judgment: GeoJudgment | None = None
    data_ai_judgment: DataAiJudgment | None = None
    opportunity_type_judgment: OpportunityTypeJudgment | None = None


def label_diagnostics_payload(diagnostics: LabelDiagnostics) -> dict[str, Any]:
    """The one JSON shape of the diagnostics block, keys always present."""
    return {
        "geo_judgment": (
            None if diagnostics.geo_judgment is None else str(diagnostics.geo_judgment)
        ),
        "data_ai_judgment": (
            None
            if diagnostics.data_ai_judgment is None
            else str(diagnostics.data_ai_judgment)
        ),
        "opportunity_type_judgment": (
            None
            if diagnostics.opportunity_type_judgment is None
            else str(diagnostics.opportunity_type_judgment)
        ),
    }


# --------------------------------------------------------------------------
# free-form parts, normalized so the digest is about the judgement
# --------------------------------------------------------------------------

#: An open vocabulary, but a disciplined one: lowercase, no whitespace, and a
#: shape a later grouping can rely on. The vocabulary itself is deliberately not
#: closed — a calibration round exists partly to discover which tags are worth
#: having, and a closed list written before the first annotation would be a
#: guess wearing a contract's clothes.
_REASON_TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MAX_REASON_TAGS = 16
_MAX_REASON_TAG_LENGTH = 48


def normalize_reason_tags(tags: Iterable[str] | None) -> tuple[str, ...]:
    """Lowercase, strip, de-duplicate and sort the annotator's tags.

    Sorting means the order the tags were typed in is *not* part of the
    judgement, which is the honest reading: nobody ranks their own reasons by
    keystroke order, and leaving the order semantic would make two identical
    judgements fingerprint differently.
    """
    if tags is None:
        return ()
    normalized: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            raise HumanLabelError(f"reason tags must be strings, not {raw!r}")
        tag = raw.strip().lower()
        if not tag:
            continue
        if len(tag) > _MAX_REASON_TAG_LENGTH:
            raise HumanLabelError(
                f"reason tag {tag!r} is longer than "
                f"{_MAX_REASON_TAG_LENGTH} characters"
            )
        if not _REASON_TAG_PATTERN.match(tag):
            raise HumanLabelError(
                f"reason tag {tag!r} is not lowercase alphanumeric with "
                "'-' or '_' separators"
            )
        normalized.add(tag)
    if len(normalized) > _MAX_REASON_TAGS:
        raise HumanLabelError(
            f"at most {_MAX_REASON_TAGS} reason tags are allowed, "
            f"{len(normalized)} given"
        )
    return tuple(sorted(normalized))


_MAX_NOTE_LENGTH = 2000


def normalize_note(note: str | None) -> str | None:
    """Strip the note, and turn an empty one back into "no note".

    `None` and `""` must not be two different ways of saying the same thing, or
    two identical judgements would carry two different digests.
    """
    if note is None:
        return None
    if not isinstance(note, str):
        raise HumanLabelError(f"note must be a string or None, not {note!r}")
    stripped = note.strip()
    if not stripped:
        return None
    if len(stripped) > _MAX_NOTE_LENGTH:
        raise HumanLabelError(
            f"note is longer than {_MAX_NOTE_LENGTH} characters"
        )
    return stripped


# --------------------------------------------------------------------------
# the label
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HumanRelevanceLabel:
    """One person's judgement of one opportunity, and everything it is bound to.

    The binding block is not decoration. A grade means nothing on its own: it is
    a judgement of *this* posting, frozen in *this* dataset, for *this* profile
    state, under *this* rubric. Change any of the four and the judgement may
    still be defensible but it is no longer the same statement, so all four are
    carried on the row and all four are checked when the row is read back.

    What is deliberately **not** here: any personal detail of the person the
    evaluation is for. The dataset already carries the profile binding — a
    `profile_id` and a digest of what that profile declared — and a label reuses
    that binding instead of copying skills, preferences or contact details into
    a second file that would then have to be protected twice.

    `revision` and `relabel_reason` exist so that a correction is a visible
    event. Revision 1 is a first judgement and carries no reason; every later
    revision must state why it was made, and both rows stay in the file.
    """

    label_schema_version: str
    protocol_version: str
    dataset_id: str
    dataset_content_fingerprint: str
    profile_id: int
    profile_context_fingerprint: str
    opportunity_id: int
    relevance_grade: int
    diagnostics: LabelDiagnostics
    reason_tags: tuple[str, ...]
    note: str | None
    labeled_at: str
    revision: int = 1
    relabel_reason: str | None = None


def human_label_semantic_payload(label: HumanRelevanceLabel) -> dict[str, Any]:
    """The judgement itself: what a labelset digest covers, and nothing else.

    Excluded on purpose, and this is the whole point of having two payloads:

    * `labeled_at` — the clock. Re-recording the same judgement tomorrow must
      not produce a different labelset digest, or the digest would describe the
      annotation session rather than the annotations;
    * `revision` and `relabel_reason` — the *history* of how a judgement was
      arrived at. Two annotators who reach grade 2, one directly and one after
      correcting a first attempt, hold the same opinion, and a metric computed
      over their labels would compute the same number.

    Included on purpose: `note`. A note is not a comment on the process, it is
    the annotator qualifying their own judgement — "yes, but only if remote is
    negotiable" — and a labelset in which that sentence changed is a labelset
    whose judgements changed. Excluding it would let the meaning of a grade move
    silently under a stable digest.
    """
    return {
        "opportunity_id": label.opportunity_id,
        "relevance_grade": label.relevance_grade,
        "relevance_grade_name": RELEVANCE_GRADE_NAMES[label.relevance_grade],
        "diagnostics": label_diagnostics_payload(label.diagnostics),
        "reason_tags": list(label.reason_tags),
        "note": label.note,
    }


def human_label_payload(label: HumanRelevanceLabel) -> dict[str, Any]:
    """The one JSON shape of a label row: what is written to `labels.jsonl`.

    A superset of the semantic payload — the judgement, plus the bindings that
    say what it is a judgement *of*, plus the provenance of when it was made and
    whether it corrected an earlier one.
    """
    payload = {
        "label_schema_version": label.label_schema_version,
        "protocol_version": label.protocol_version,
        "dataset_id": label.dataset_id,
        "dataset_content_fingerprint": label.dataset_content_fingerprint,
        "profile_id": label.profile_id,
        "profile_context_fingerprint": label.profile_context_fingerprint,
        "labeled_at": label.labeled_at,
        "revision": label.revision,
        "relabel_reason": label.relabel_reason,
    }
    payload.update(human_label_semantic_payload(label))
    return payload


def canonical_label_order(
    labels: Sequence[HumanRelevanceLabel],
) -> tuple[HumanRelevanceLabel, ...]:
    """Labels in `opportunity_id` ASC, the same total order Phase 10.1 uses.

    A digest over a set has to fix an order, and reusing the dataset's canonical
    one means a labelset and the dataset it labels are read in the same
    sequence — there is no second ordering convention to keep in mind.
    """
    return tuple(sorted(labels, key=lambda label: label.opportunity_id))
