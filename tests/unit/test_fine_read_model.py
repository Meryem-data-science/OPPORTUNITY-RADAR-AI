"""The persisted Phase 8 fine classification, decoded where the rules live.

These are the layering guarantees of the move, not a second copy of the decoding
tests: `tests/integration/test_opportunity_api_fine_classification_sqlite.py`
keeps exercising the rules through the public API, whose contract did not move.
"""

import pathlib

import pytest

from services.api import fine_classification as api
from services.collector.qualification import fine_read_model as domain
from services.collector.qualification.fine_taxonomy import (
    EvidenceField,
    EvidenceKind,
    FineCategory,
)

FINE_VERSION = "fine-data-ai-rules-v2"
EVIDENCE = (
    '[{"category":"DATA_SCIENCE","field":"TITLE","kind":"ROLE_PHRASE",'
    '"signal":"data scientist"}]'
)


def test_the_domain_reader_needs_no_web_framework():
    """A domain reader must not acquire an HTTP dependency to read a column."""
    source = pathlib.Path(
        "services/collector/qualification/fine_read_model.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("pydantic", "fastapi", "BaseModel", "services.api"):
        assert forbidden not in source


def test_the_recommendation_domain_does_not_depend_on_the_api_layer():
    for name in ("input_assembly", "models", "engine", "fine_domain", "fingerprint"):
        source = pathlib.Path(f"services/recommendation/{name}.py").read_text(
            encoding="utf-8"
        )
        assert "services.api" not in source, name


def test_the_api_raises_the_domain_error_rather_than_one_of_its_own():
    assert api.FineClassificationDecodeError is domain.FineClassificationDecodeError


def test_both_readers_agree_on_a_classified_row():
    arguments = ("CORE_TARGET", "DATA_SCIENCE", "[]", EVIDENCE, '["reason"]', FINE_VERSION)
    read = domain.decode_fine_classification(*arguments)
    published = api.decode_fine_classification(*arguments)

    assert read.primary_category is published.primary_category is FineCategory.DATA_SCIENCE
    assert read.classifier_version == published.classifier_version == FINE_VERSION
    assert read.secondary_categories == published.secondary_categories == []
    assert read.reasons == published.reasons == ["reason"]

    evidence, response = read.category_evidence[0], published.category_evidence[0]
    assert isinstance(evidence, domain.FineCategoryEvidence)
    assert isinstance(response, api.FineEvidenceResponse)
    assert (evidence.category, evidence.field, evidence.kind, evidence.signal) == (
        FineCategory.DATA_SCIENCE,
        EvidenceField.TITLE,
        EvidenceKind.ROLE_PHRASE,
        "data scientist",
    )
    assert (response.category, response.field, response.kind, response.signal) == (
        evidence.category,
        evidence.field,
        evidence.kind,
        evidence.signal,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        (None, None, None, None, None, None),
        ("CORE_TARGET", None, None, None, None, None),
        ("OUT_OF_SCOPE", None, None, None, None, None),
    ],
)
def test_the_never_classified_state_reads_as_nothing_on_both_sides(arguments):
    assert domain.decode_fine_classification(*arguments) is domain.UNCLASSIFIED
    assert api.decode_fine_classification(*arguments) == api.UNCLASSIFIED


def test_other_stays_a_value_and_not_an_absence_on_both_sides():
    arguments = ("CORE_TARGET", "OTHER", "[]", "[]", '["no evidence"]', FINE_VERSION)
    assert domain.decode_fine_classification(*arguments).primary_category is (
        FineCategory.OTHER
    )
    published = api.decode_fine_classification(*arguments)
    assert published.primary_category is FineCategory.OTHER
    assert published != api.UNCLASSIFIED
    assert published.category_evidence == []


@pytest.mark.parametrize(
    "arguments",
    [
        # A classified row whose fine half contradicts its coarse half.
        ("UNCERTAIN", "NLP", "[]", "[]", "[]", FINE_VERSION),
        ("CORE_TARGET", None, "[]", "[]", "[]", FINE_VERSION),
        # OTHER is never evidenced.
        ("CORE_TARGET", "DATA_SCIENCE", '["OTHER"]', "[]", "[]", FINE_VERSION),
        # Half-written, which `0025`'s own CHECK forbids.
        ("CORE_TARGET", "NLP", None, None, None, None),
        # A fine value with no qualification row behind it.
        (None, None, None, None, None, FINE_VERSION),
    ],
)
def test_an_incoherent_row_is_refused_by_both_readers(arguments):
    with pytest.raises(domain.FineClassificationDecodeError):
        domain.decode_fine_classification(*arguments)
    with pytest.raises(api.FineClassificationDecodeError):
        api.decode_fine_classification(*arguments)
