"""The public response vocabulary of the persisted Phase 8 fine classification.

The decoding itself — the three persisted states, the closed vocabularies, and
the coherence of the fine half against the coarse one — lives in
`services/collector/qualification/fine_read_model.py`, beside the classifier and
the taxonomy it reads. Those are rules about what a persisted Phase 8 row means,
not about HTTP, and a domain reader must not acquire a web dependency to ask.

What remains here is exactly the adapter: the same values, expressed in the
types the response models are built from. `FineEvidenceResponse` is the pydantic
shape the API publishes, and `decode_fine_classification` below is the domain
decoder plus that one conversion. The public contract is unchanged — same
function name, same six raw column arguments, same five fields, same order, same
`FineClassificationDecodeError` on malformed or incoherent persisted data.

Nothing is classified during a request, here or downstream.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from services.collector.qualification.fine_read_model import (
    FineClassificationDecodeError,
    FineClassificationRead as PersistedFineClassification,
)
from services.collector.qualification.fine_read_model import (
    decode_fine_classification as decode_persisted_fine_classification,
)
from services.collector.qualification.fine_taxonomy import (
    EvidenceField,
    EvidenceKind,
    FineCategory,
)

__all__ = [
    "UNCLASSIFIED",
    "FineClassificationDecodeError",
    "FineClassificationRead",
    "FineEvidenceResponse",
    "decode_fine_classification",
]


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

    `None` collections mean the row was never classified. Once
    `classifier_version` is present the three collections are lists, possibly
    empty, because the classifier ran and wrote them.
    """

    primary_category: FineCategory | None
    secondary_categories: list[FineCategory] | None
    category_evidence: list[FineEvidenceResponse] | None
    reasons: list[str] | None
    classifier_version: str | None


#: The never-classified state, and the only place the all-`None` public shape is
#: constructed.
UNCLASSIFIED = FineClassificationRead(None, None, None, None, None)


def _public(decoded: PersistedFineClassification) -> FineClassificationRead:
    """Re-express one decoded row in the response vocabulary, changing nothing.

    Order is preserved because the persisted order is part of what the API
    reports: the classifier wrote its evidence in the order it produced it.
    """
    if decoded.category_evidence is None:
        return UNCLASSIFIED
    return FineClassificationRead(
        primary_category=decoded.primary_category,
        secondary_categories=decoded.secondary_categories,
        category_evidence=[
            FineEvidenceResponse(
                category=item.category,
                field=item.field,
                kind=item.kind,
                signal=item.signal,
            )
            for item in decoded.category_evidence
        ],
        reasons=decoded.reasons,
        classifier_version=decoded.classifier_version,
    )


def decode_fine_classification(
    qualification: object,
    primary_category: object,
    secondary_categories_json: object,
    category_evidence_json: object,
    reasons_json: object,
    classifier_version: object,
) -> FineClassificationRead:
    """Read one row's persisted fine classification for the public contract.

    The arguments are the raw column values the listing query selects: the
    coarse `qualification` of the joined `opportunity_qualifications` row, then
    the five fine columns in the order migration `0025` adds them.
    """
    return _public(
        decode_persisted_fine_classification(
            qualification,
            primary_category,
            secondary_categories_json,
            category_evidence_json,
            reasons_json,
            classifier_version,
        )
    )
