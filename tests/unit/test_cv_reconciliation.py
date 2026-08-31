"""Unit coverage for the pure parts of the Phase 3.3C reconciliation.

Nothing here touches a database. These are the promises the module makes about
itself — which packages it is allowed to know about, that it never deletes, and
that it prints nothing — plus the one guard that keeps it from reconciling
towards a stale reading. What it *does* with two campaigns is exercised against
real rows in `tests/integration/test_cv_reconciliation_sqlite.py`.
"""

import io
import tokenize
from pathlib import Path

import pytest

from services.digital_twin.cv import reconciliation
from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    StructuredCvExtraction,
)
from services.digital_twin.cv.models import PARSER_VERSION
from services.digital_twin.cv.reconciliation import (
    CvReconciliationError,
    CvReconciliationVersionError,
    extraction_is_current,
)

RECONCILIATION_SOURCE = Path("services/digital_twin/cv/reconciliation.py")

# A synthetic 64-hex digest; no real file was hashed to produce it.
TEST_ONLY_SHA256 = "ab" * 32


def code_only(path: Path) -> str:
    """Return a module's executable source, without comments or docstrings.

    The module documents at length what it refuses to do, so a plain substring
    search over the file would match its own prose. Dropping every comment and
    every string literal leaves the code, which is what these assertions are
    actually about.
    """
    tokens = tokenize.generate_tokens(
        io.StringIO(path.read_text(encoding="utf-8")).readline
    )
    return "".join(
        token.string
        for token in tokens
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def make_extraction(
    *, parser_version: str, extractor_version: str
) -> StructuredCvExtraction:
    return StructuredCvExtraction(
        extractor_version=extractor_version,
        parser_version=parser_version,
        cv_sha256=TEST_ONLY_SHA256,
        candidates=(),
        warnings=(),
    )


def test_the_current_extraction_is_the_one_this_checkout_produces():
    assert extraction_is_current(
        make_extraction(
            parser_version=PARSER_VERSION,
            extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        )
    )


@pytest.mark.parametrize(
    ("parser_version", "extractor_version"),
    [
        ("cv-parser-v1", CANDIDATE_EXTRACTOR_VERSION),
        (PARSER_VERSION, "cv-candidates-v1"),
        ("cv-parser-v1", "cv-candidates-v1"),
    ],
)
def test_an_older_extraction_is_not_current(parser_version, extractor_version):
    assert not extraction_is_current(
        make_extraction(
            parser_version=parser_version, extractor_version=extractor_version
        )
    )


def test_the_documented_previous_campaign_is_the_one_this_project_moved_off():
    assert reconciliation.PREVIOUS_PARSER_VERSION == "cv-parser-v2"
    assert reconciliation.PREVIOUS_EXTRACTOR_VERSION == "cv-candidates-v2"
    # And it is genuinely an older one, so the CLI defaults cannot name the
    # campaign the checkout itself produces.
    assert reconciliation.PREVIOUS_PARSER_VERSION != PARSER_VERSION
    assert reconciliation.PREVIOUS_EXTRACTOR_VERSION != CANDIDATE_EXTRACTOR_VERSION


@pytest.mark.parametrize("blank", ["", "   ", " cv-parser-v1"])
def test_a_blank_or_untrimmed_old_version_is_refused(blank):
    with pytest.raises(CvReconciliationVersionError):
        reconciliation.compute_cv_reconciliation_plan(
            None,
            profile_id=1,
            extraction=make_extraction(
                parser_version=PARSER_VERSION,
                extractor_version=CANDIDATE_EXTRACTOR_VERSION,
            ),
            old_parser_version=blank,
            old_extractor_version="cv-candidates-v1",
        )


def test_something_other_than_an_extraction_is_refused():
    with pytest.raises(CvReconciliationError):
        reconciliation.compute_cv_reconciliation_plan(
            None,
            profile_id=1,
            extraction={"candidates": []},
            old_parser_version="cv-parser-v1",
            old_extractor_version="cv-candidates-v1",
        )


def test_the_refusals_happen_before_a_row_is_read():
    """Passing `None` as the connection is the proof: nothing was queried."""
    with pytest.raises(CvReconciliationVersionError):
        reconciliation.compute_cv_reconciliation_plan(
            None,
            profile_id=1,
            extraction=make_extraction(
                parser_version=PARSER_VERSION,
                extractor_version=CANDIDATE_EXTRACTOR_VERSION,
            ),
            old_parser_version=PARSER_VERSION,
            old_extractor_version=CANDIDATE_EXTRACTOR_VERSION,
        )


def test_the_module_deletes_nothing():
    """No fact and no provenance row is ever removed, so history stays readable."""
    code = code_only(RECONCILIATION_SOURCE).casefold()

    assert "delete" not in code
    assert "drop" not in code
    assert "truncate" not in code


def test_the_module_writes_no_sql_of_its_own():
    """Every write goes through the audited primitives of the facts package."""
    code = code_only(RECONCILIATION_SOURCE).casefold()

    for statement in ("insert", "update", "execute", "cursor", "commit", "rollback"):
        assert statement not in code


def test_the_module_knows_nothing_about_skills_opportunities_or_migrations():
    code = code_only(RECONCILIATION_SOURCE)

    for forbidden in (
        "skills",
        "profile_skills",
        "synchronize_profile_skills",
        "opportunit",
        "migration",
        "apply_migrations",
    ):
        assert forbidden not in code


def test_the_module_does_no_fuzzy_matching():
    """Identity is byte equality; nothing else is imported or called."""
    code = code_only(RECONCILIATION_SOURCE).casefold()

    for forbidden in (
        "difflib",
        "sequencematcher",
        "levenshtein",
        "fuzz",
        "similar",
        "casefold",
        "startswith",
        "endswith",
        # `normalized_value` is carried over verbatim, so the bare word is
        # legitimate; calling a normalizer is what must not happen.
        "normalize(",
        "normalization",
        "normalize_skill",
        "unicodedata",
        "re.",
        "embedding",
        "anthropic",
        "openai",
    ):
        assert forbidden not in code


def test_the_module_prints_nothing_and_logs_nothing():
    """Reporting is the caller's job, so no value can escape through this module."""
    code = code_only(RECONCILIATION_SOURCE)

    assert "print(" not in code
    assert "logger" not in code
    assert "get_logger" not in code


def test_the_module_accepts_nothing():
    """There is no acceptance anywhere in it, and no argument that adds one."""
    code = code_only(RECONCILIATION_SOURCE)

    assert "accept_profile_fact" not in code
    assert "correct_profile_fact" not in code
    # Rejecting a superseded reading is the one decision it takes, and it takes
    # it in exactly one place.
    assert code.count("reject_profile_fact") == 2  # the import, and the one call
