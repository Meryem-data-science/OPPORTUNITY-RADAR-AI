"""Strict decoding of the persisted Phase 8 fine classification, for public reads.

Migration `0025` stores the fine Data/AI classification beside the coarse
qualification it was derived from, and the classifier that produced it ran once,
during persistence. Nothing here classifies: this module turns the five persisted
columns into public values **exactly as written**, or refuses them.

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
database-error path rather than answering with a repaired or invented category.
"""

from dataclasses import dataclass
import json
from typing import Any

from pydantic import BaseModel

from services.collector.qualification.fine_taxonomy import (
    EvidenceField,
    EvidenceKind,
    FineCategory,
)


class FineClassificationDecodeError(ValueError):
    """Raised when a persisted fine classification cannot be read as written."""


class FineEvidenceResponse(BaseModel):
    """One persisted explanation of why a fine category was considered.

    The shape mirrors what persistence wrote, so evidence stays inspectable
    rather than becoming an opaque `dict[str, Any]` in the public contract.
    """

    category: FineCategory
    field: EvidenceField
    kind: EvidenceKind
    signal: str


@dataclass(frozen=True)
class FineClassificationRead:
    """The five public fine values of one opportunity, as persisted.

    `None` collections mean state A — never classified. Once
    `classifier_version` is present the three collections are lists, possibly
    empty, because the classifier ran and wrote them.
    """

    primary_category: FineCategory | None
    secondary_categories: list[FineCategory] | None
    category_evidence: list[FineEvidenceResponse] | None
    reasons: list[str] | None
    classifier_version: str | None


#: State A, and the only place the public all-`None` shape is constructed.
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


def _evidence(raw: object, context: str) -> FineEvidenceResponse:
    if not isinstance(raw, dict):
        raise FineClassificationDecodeError(f"{context} is not an object")
    if set(raw) != _EVIDENCE_KEYS:
        raise FineClassificationDecodeError(f"{context} does not carry the persisted keys")
    return FineEvidenceResponse(
        category=_member(FineCategory, raw["category"], f"{context} category"),
        field=_member(EvidenceField, raw["field"], f"{context} field"),
        kind=_member(EvidenceKind, raw["kind"], f"{context} kind"),
        signal=_text(raw["signal"], f"{context} signal"),
    )


def decode_fine_classification(
    primary_category: object,
    secondary_categories_json: object,
    category_evidence_json: object,
    reasons_json: object,
    classifier_version: object,
) -> FineClassificationRead:
    """Read the five persisted fine columns of one row, preserving their order.

    The arguments are the raw column values, in the order migration `0025` adds
    them. A row whose qualification is absent passes `None` five times and reads
    as state A, which is why the `LEFT JOIN` needs no separate branch here.
    """
    if classifier_version is None:
        # State A. The migration's CHECK already forbids a half-written row, so
        # one here means the persisted record contradicts its own schema; that
        # is refused rather than reported as "never classified".
        if any(
            value is not None
            for value in (
                primary_category,
                secondary_categories_json,
                category_evidence_json,
                reasons_json,
            )
        ):
            raise FineClassificationDecodeError(
                "fine classification is half-written: no classifier version was persisted"
            )
        return UNCLASSIFIED

    version = _text(classifier_version, "fine classifier version")
    if not version.strip():
        raise FineClassificationDecodeError("fine classifier version is empty")

    # States B and C differ only here: `OTHER` is a persisted value, NULL is the
    # classifier's deliberate refusal to name a sub-domain. Neither is invented.
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
    return FineClassificationRead(primary, secondary, evidence, reasons, version)
