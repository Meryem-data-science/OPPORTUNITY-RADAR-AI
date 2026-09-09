"""Strict decoding of the persisted Phase 8 fine classification, as written.

Migration `0025` stores the fine Data/AI classification beside the coarse
qualification it was derived from, and the classifier that produced it ran once,
during persistence. Nothing here classifies: this module turns the five persisted
columns into values **exactly as written**, or refuses them.

It lives beside the classifier and the taxonomy it reads because that is what it
is about — the persisted shape of a Phase 8 answer. Every reader of those five
columns goes through it: the HTTP API through a thin adapter in
`services/api/fine_classification.py`, which adds the public response vocabulary
and nothing else, and the Phase 9 recommendation engine directly. A second
decoder anywhere would be a second opinion on what a persisted row means.

Three states are persisted, and folding any two of them together would be a lie:

    A. `fine_classifier_version IS NULL` — migration `0025` reached the row and
       fine classification never did. Nothing is known, so every public field is
       `None`. A visible opportunity with no `opportunity_qualifications` row at
       all reads as this same state.
    B. `fine_classifier_version` present, `fine_primary_category` NULL — the
       classifier ran and deliberately assigned no category, which is what it
       does for an `OUT_OF_SCOPE` or `UNCERTAIN` opportunity. The three
       collections are arrays (`[]`, `[]`, and the persisted not-qualified
       reason): the row states that it was classified.
    C. `fine_primary_category = 'OTHER'` — a proven Data/AI opportunity for which
       no supported sub-domain is evidenced. `OTHER` is a value, never an
       absence, and never the public shape of A or B.

The three JSON columns are TEXT with no JSON1 `CHECK`, exactly as the rest of
this schema stores JSON, so the database has not validated their contents. This
module therefore parses them with `json.loads` — never `eval` — and validates
every value against the closed vocabularies before it is exposed. Malformed or
incoherent persisted data raises, so the caller fails through its own public
error path rather than answering with a repaired or invented category.

Structure is not enough, which is why the persisted **coarse** `qualification`
is read alongside the five fine columns. Migration `0025` states the fine half
against itself — nothing written, or the version and its three JSON columns
written together — and it cannot state the fine half against the coarse half in
the same row. So the schema still admits rows that contradict Phase 8: an
`UNCERTAIN` opportunity carrying `OTHER`, an `OUT_OF_SCOPE` one carrying `NLP`,
a `CORE_TARGET` one classified into nothing. Each of those would publish exactly
the confusion this phase exists to prevent — an unknown re-labelled as a
statement — so each is refused here instead. The coarse value is an *input* to
that judgement and stays internal: it is validated, used, and never returned.

Nothing here imports a web framework. The values below are plain dataclasses so
that a domain reader does not acquire an HTTP dependency to read a column.
"""

from dataclasses import dataclass
import json
from typing import Any

from services.collector.qualification.fine_classifier import (
    FINE_ELIGIBLE_QUALIFICATIONS,
)
from services.collector.qualification.fine_taxonomy import (
    EvidenceField,
    EvidenceKind,
    FineCategory,
)
from services.collector.qualification.taxonomy import Qualification

__all__ = [
    "UNCLASSIFIED",
    "FineCategoryEvidence",
    "FineClassificationDecodeError",
    "FineClassificationRead",
    "decode_fine_classification",
]


class FineClassificationDecodeError(ValueError):
    """Raised when a persisted fine classification cannot be read as written."""


@dataclass(frozen=True)
class FineCategoryEvidence:
    """One persisted explanation of why a fine category was considered.

    The shape mirrors what persistence wrote, so evidence stays inspectable
    rather than becoming an opaque `dict[str, Any]` in any caller's contract.
    """

    category: FineCategory
    field: EvidenceField
    kind: EvidenceKind
    signal: str


@dataclass(frozen=True)
class FineClassificationRead:
    """The five fine values of one opportunity, as persisted.

    `None` collections mean state A — never classified. Once
    `classifier_version` is present the three collections are lists, possibly
    empty, because the classifier ran and wrote them.
    """

    primary_category: FineCategory | None
    secondary_categories: list[FineCategory] | None
    category_evidence: list[FineCategoryEvidence] | None
    reasons: list[str] | None
    classifier_version: str | None


#: State A, and the only place the all-`None` shape is constructed.
UNCLASSIFIED = FineClassificationRead(None, None, None, None, None)

#: An evidence object carries exactly these keys. An unexpected or missing key
#: is malformed persisted data, not a value to drop quietly.
_EVIDENCE_KEYS = frozenset({"category", "field", "kind", "signal"})


def _array(raw: object, context: str) -> list[Any]:
    """Parse one persisted JSON TEXT column that must hold an array."""
    if not isinstance(raw, str):
        raise FineClassificationDecodeError(f"{context} is not persisted JSON text")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise FineClassificationDecodeError(f"{context} is not valid JSON") from error
    if not isinstance(value, list):
        raise FineClassificationDecodeError(f"{context} is not a JSON array")
    return value


def _member(vocabulary: type[Any], value: object, context: str) -> Any:
    """Resolve one closed-vocabulary member, refusing anything outside it."""
    if not isinstance(value, str):
        raise FineClassificationDecodeError(f"{context} is not a string")
    try:
        return vocabulary(value)
    except ValueError as error:
        raise FineClassificationDecodeError(f"{context} is not a known value") from error


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise FineClassificationDecodeError(f"{context} is not a string")
    return value


def _evidence(raw: object, context: str) -> FineCategoryEvidence:
    if not isinstance(raw, dict):
        raise FineClassificationDecodeError(f"{context} is not an object")
    if set(raw) != _EVIDENCE_KEYS:
        raise FineClassificationDecodeError(f"{context} does not carry the persisted keys")
    return FineCategoryEvidence(
        category=_member(FineCategory, raw["category"], f"{context} category"),
        field=_member(EvidenceField, raw["field"], f"{context} field"),
        kind=_member(EvidenceKind, raw["kind"], f"{context} kind"),
        signal=_text(raw["signal"], f"{context} signal"),
    )


def _refuse_other_in(collection: str, categories: list[FineCategory]) -> None:
    """`OTHER` is a primary-only value; it is never evidence for anything.

    The fine classifier assigns `OTHER` precisely when a qualified opportunity
    produced *no* evidence at all, so it can never appear as a matched secondary
    category or as the category of a piece of evidence. A persisted row that
    says otherwise is describing a classification that could not have happened.
    """
    for index, category in enumerate(categories):
        if category is FineCategory.OTHER:
            raise FineClassificationDecodeError(
                f"{collection} {index} is OTHER, which is never evidenced"
            )


def _check_coherence(
    coarse: Qualification,
    primary: FineCategory | None,
    secondary: list[FineCategory],
    evidence: list[FineCategoryEvidence],
) -> None:
    """Refuse a classified row whose fine half contradicts its coarse half.

    `FINE_ELIGIBLE_QUALIFICATIONS` is imported from the fine classifier rather
    than restated here: the set of coarse outcomes that receive a fine category
    is the classifier's own contract, and a second copy of it in the read model
    would silently start refusing valid rows the day that contract moves.
    """
    _refuse_other_in("fine secondary category", secondary)
    _refuse_other_in("fine evidence", [item.category for item in evidence])

    if coarse not in FINE_ELIGIBLE_QUALIFICATIONS:
        # An `OUT_OF_SCOPE` or `UNCERTAIN` row was classified and deliberately
        # assigned nothing: absence of evidence that an opportunity is Data/AI
        # is not evidence about its sub-domain. A category here — `OTHER` most
        # of all — would convert that unknown into an assertion.
        if primary is not None:
            raise FineClassificationDecodeError(
                f"{coarse} carries a fine primary category"
            )
        if secondary:
            raise FineClassificationDecodeError(
                f"{coarse} carries fine secondary categories"
            )
        if evidence:
            raise FineClassificationDecodeError(f"{coarse} carries fine evidence")
        return

    # A qualified opportunity was classified, so the classifier reached a
    # verdict: a sub-domain, or `OTHER`. Nothing is not one of its answers.
    if primary is None:
        raise FineClassificationDecodeError(
            f"{coarse} was classified into no fine primary category"
        )
    if primary is FineCategory.OTHER:
        # `OTHER` *means* no supported sub-domain was evidenced, so evidence or
        # a secondary category beside it contradicts the value itself.
        if secondary:
            raise FineClassificationDecodeError(
                "fine primary category OTHER carries secondary categories"
            )
        if evidence:
            raise FineClassificationDecodeError(
                "fine primary category OTHER carries evidence"
            )


def decode_fine_classification(
    qualification: object,
    primary_category: object,
    secondary_categories_json: object,
    category_evidence_json: object,
    reasons_json: object,
    classifier_version: object,
) -> FineClassificationRead:
    """Read one row's persisted fine classification, preserving its order.

    The arguments are raw column values: the coarse `qualification` of the
    joined `opportunity_qualifications` row, then the five fine columns in the
    order migration `0025` adds them. A row whose qualification is absent passes
    `None` six times and reads as state A, which is why a `LEFT JOIN` needs no
    separate branch here. `qualification` is read to validate the rest and is
    never part of the result.
    """
    fine_columns = (
        primary_category,
        secondary_categories_json,
        category_evidence_json,
        reasons_json,
        classifier_version,
    )
    if qualification is None:
        # State A, reached by a LEFT JOIN matching no qualification row: every
        # joined column is NULL, coarse and fine alike. Any fine value here came
        # from a row that does not exist, which is not a classification.
        if any(value is not None for value in fine_columns):
            raise FineClassificationDecodeError(
                "fine classification without a qualification row"
            )
        return UNCLASSIFIED

    coarse = _member(Qualification, qualification, "qualification")

    if classifier_version is None:
        # State A again, this time with a qualification row present: migration
        # 0025 reached it and fine classification never did. Legal under every
        # coarse qualification, because that is the legacy pre-reconciliation
        # state and the coarse outcome says nothing about it. The migration's
        # CHECK already forbids a half-written row, so one here means the
        # persisted record contradicts its own schema; that is refused rather
        # than reported as "never classified".
        if any(value is not None for value in fine_columns[:-1]):
            raise FineClassificationDecodeError(
                "fine classification is half-written: no classifier version was persisted"
            )
        return UNCLASSIFIED

    # The stored version is read, never compared to the version running now: a
    # reader reports versioned persisted data and must not recompute it.
    version = _text(classifier_version, "fine classifier version")
    if not version.strip():
        raise FineClassificationDecodeError("fine classifier version is empty")

    # States B and C differ only here: `OTHER` is a persisted value, NULL is the
    # classifier's deliberate refusal to name a sub-domain. Neither is invented,
    # and `_check_coherence` below decides which of the two this row may be.
    primary = (
        None
        if primary_category is None
        else _member(FineCategory, primary_category, "fine primary category")
    )
    secondary = [
        _member(FineCategory, value, f"fine secondary category {index}")
        for index, value in enumerate(
            _array(secondary_categories_json, "fine secondary categories")
        )
    ]
    evidence = [
        _evidence(value, f"fine evidence {index}")
        for index, value in enumerate(_array(category_evidence_json, "fine evidence"))
    ]
    reasons = [
        _text(value, f"fine reason {index}")
        for index, value in enumerate(_array(reasons_json, "fine reasons"))
    ]
    # Structure is sound; now the row has to mean something. Reasons are checked
    # for shape only — the classifier's prose is not a contract this read model
    # is entitled to pin down.
    _check_coherence(coarse, primary, secondary, evidence)
    return FineClassificationRead(primary, secondary, evidence, reasons, version)
