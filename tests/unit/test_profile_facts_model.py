"""The Phase 3.3A fact vocabulary, and the boundaries this slice must keep."""

import ast
from pathlib import Path

import pytest

from services.digital_twin.cv.candidates.models import CandidateType
from services.digital_twin.facts.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    FactSourceType,
    FactStatus,
    ProfileFact,
    ProfileFactType,
    ProfileFactValueError,
    ProvenanceInput,
    decode_page_numbers,
    encode_page_numbers,
)

FACTS_PACKAGE = Path("services/digital_twin/facts")

#: Everything the fact package is allowed to import. No HTTP client, no model
#: runtime, no cloud SDK: deciding what is true is a human's job here.
ALLOWED_TOP_LEVEL_IMPORTS = frozenset(
    {"__future__", "dataclasses", "enum", "hashlib", "services", "sqlite3", "typing"}
)


def _fact(status: FactStatus, **overrides) -> ProfileFact:
    values = {
        "id": 1,
        "profile_id": 1,
        "fact_type": ProfileFactType.EMAIL.value,
        "value": "TEST ONLY value",
        "normalized_value": None,
        "status": status,
        "replaced_by_fact_id": None,
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-01 00:00:00",
        "decided_at": None if status is FactStatus.PROPOSED else "2026-01-01 00:00:01",
    }
    values.update(overrides)
    return ProfileFact(**values)


def test_the_status_taxonomy_is_exactly_the_validation_cycle() -> None:
    assert [status.value for status in FactStatus] == [
        "PROPOSED",
        "ACCEPTED",
        "CORRECTED",
        "REJECTED",
    ]


def test_the_source_taxonomy_is_the_four_kinds_of_evidence() -> None:
    assert [source.value for source in FactSourceType] == [
        "CV",
        "GITHUB",
        "USER_INPUT",
        "OTHER_ACCEPTED_EVIDENCE",
    ]


def test_the_fact_taxonomy_covers_the_digital_twin_categories() -> None:
    assert {fact_type.value for fact_type in ProfileFactType} == {
        "NAME",
        "PROFESSIONAL_TITLE",
        "EMAIL",
        "PHONE",
        "EDUCATION",
        "EXPERIENCE",
        "PROJECT",
        "SKILL",
        "CERTIFICATION",
        "LANGUAGE",
        "GITHUB_URL",
        "LINKEDIN_URL",
        "PORTFOLIO_URL",
        "PROFESSIONAL_URL",
        "PREFERENCE",
        "AVAILABILITY",
        "MOBILITY",
        "CAREER_OBJECTIVE",
    }


def test_a_fact_type_is_not_a_candidate_type() -> None:
    """A verified identity is a NAME; "candidate" would contradict "verified"."""
    assert "NAME_CANDIDATE" not in {fact_type.value for fact_type in ProfileFactType}
    assert CandidateType.NAME_CANDIDATE.value not in {
        fact_type.value for fact_type in ProfileFactType
    }
    assert ProfileFactType is not CandidateType


def test_no_level_confidence_or_score_exists_in_the_vocabulary() -> None:
    field_names = set(ProfileFact.__dataclass_fields__)
    forbidden = ("level", "proficiency", "confidence", "score", "verified")

    assert not [name for name in field_names if any(f in name for f in forbidden)]
    assert not [
        name
        for name in ProvenanceInput.__dataclass_fields__
        if any(f in name for f in forbidden)
    ]


def test_verified_is_computed_from_the_status_and_stored_nowhere() -> None:
    assert "verified" not in ProfileFact.__dataclass_fields__
    assert _fact(FactStatus.ACCEPTED).is_verified is True
    assert _fact(FactStatus.PROPOSED).is_verified is False
    assert _fact(FactStatus.REJECTED).is_verified is False
    assert _fact(
        FactStatus.CORRECTED, replaced_by_fact_id=2
    ).is_verified is False


def test_rejected_and_corrected_are_the_terminal_statuses() -> None:
    assert TERMINAL_STATUSES == {FactStatus.REJECTED, FactStatus.CORRECTED}
    assert ALLOWED_TRANSITIONS[FactStatus.CORRECTED] == frozenset()
    assert ALLOWED_TRANSITIONS[FactStatus.REJECTED] == {FactStatus.REJECTED}
    assert ALLOWED_TRANSITIONS[FactStatus.PROPOSED] == {
        FactStatus.ACCEPTED,
        FactStatus.CORRECTED,
        FactStatus.REJECTED,
    }
    assert ALLOWED_TRANSITIONS[FactStatus.ACCEPTED] == {
        FactStatus.ACCEPTED,
        FactStatus.CORRECTED,
        FactStatus.REJECTED,
    }


@pytest.mark.parametrize(
    "pages, encoded",
    [((), None), ((1,), "[1]"), ((1, 2), "[1,2]"), ((3, 12, 300), "[3,12,300]")],
)
def test_page_numbers_round_trip_through_their_canonical_form(pages, encoded) -> None:
    assert encode_page_numbers(pages) == encoded
    assert decode_page_numbers(encoded) == pages


@pytest.mark.parametrize("pages", [(0,), (2, 1), (1, 1), (-1,), (True,)])
def test_page_numbers_must_be_ascending_and_start_at_one(pages) -> None:
    with pytest.raises(ProfileFactValueError):
        ProvenanceInput(source_type=FactSourceType.CV, page_numbers=pages)


@pytest.mark.parametrize(
    "overrides",
    [
        {"provenance_key": "   "},
        {"provenance_key": " padded "},
        {"source_locator": ""},
        {"rule_id": " "},
        {"parser_version": ""},
        {"section_index": -1},
    ],
)
def test_a_present_but_empty_evidence_field_is_refused(overrides) -> None:
    with pytest.raises(ProfileFactValueError):
        ProvenanceInput(source_type=FactSourceType.CV, **overrides)


def test_an_absent_evidence_field_stays_absent() -> None:
    """Nothing is defaulted to a plausible value: absence is recorded as absence."""
    provenance = ProvenanceInput(source_type=FactSourceType.USER_INPUT)

    assert provenance.cv_sha256 is None
    assert provenance.rule_id is None
    assert provenance.page_numbers == ()
    assert provenance.encoded_page_numbers is None
    assert provenance.section_index is None


def test_the_derived_provenance_key_is_a_pure_function_of_the_evidence() -> None:
    first = ProvenanceInput(
        source_type=FactSourceType.CV, rule_id="EMAIL_PATTERN", page_numbers=(1,)
    )
    same = ProvenanceInput(
        source_type=FactSourceType.CV, rule_id="EMAIL_PATTERN", page_numbers=(1,)
    )
    other = ProvenanceInput(
        source_type=FactSourceType.CV, rule_id="EMAIL_PATTERN", page_numbers=(2,)
    )

    assert first.resolved_provenance_key() == same.resolved_provenance_key()
    assert first.resolved_provenance_key() != other.resolved_provenance_key()
    assert (
        ProvenanceInput(
            source_type=FactSourceType.CV, provenance_key="explicit"
        ).resolved_provenance_key()
        == "explicit"
    )


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def test_the_fact_package_needs_no_network_and_no_model() -> None:
    for path in sorted(FACTS_PACKAGE.glob("*.py")):
        assert _imported_top_level_modules(path) <= ALLOWED_TOP_LEVEL_IMPORTS, path


def _sql_literals(path: Path) -> list[str]:
    """Every string the module builds SQL from, docstrings excluded.

    The docstrings are excluded deliberately: they name the statement this
    package refuses to write, and reading prose as SQL would flag the refusal
    itself.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    documented = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    keywords = ("insert ", "update ", "select ", "delete ", "begin ", "commit")
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documented
        and any(keyword in node.value.casefold() for keyword in keywords)
    ]


def test_the_fact_package_never_overwrites_a_value_in_place() -> None:
    """The one thing a correction must never compile to.

    Read from the SQL the module really executes, not from its prose: the
    docstrings deliberately name the statement they refuse to write.
    """
    statements = [
        statement
        for path in sorted(FACTS_PACKAGE.glob("*.py"))
        for statement in _sql_literals(path)
    ]

    assert statements
    for statement in statements:
        folded = " ".join(statement.casefold().split())
        assert "set value" not in folded
        assert "value = ?" not in folded
        assert "delete from" not in folded


def test_0007_adds_only_the_two_fact_tables() -> None:
    """The fact store is `0007` alone.

    `0008`, `0009` and `0010` project it — onto skills, then onto structured
    experiences and projects, then onto structured education, certifications
    and languages — and none of them adds a column to it.
    """
    names = [path.name for path in sorted(Path("migrations").glob("*.sql"))]

    assert names == [
        "0001_opportunity_foundation.sql",
        "0002_deduplication_decisions.sql",
        "0003_deduplication_merges.sql",
        "0004_opportunity_qualifications.sql",
        "0005_source_runs.sql",
        "0006_user_profile_foundation.sql",
        "0007_profile_facts.sql",
        "0008_normalized_profile_skills.sql",
        "0009_structured_profile_experiences_projects.sql",
        "0010_structured_profile_education_certifications_languages.sql",
    ]
    for projection in (
        "migrations/0008_normalized_profile_skills.sql",
        "migrations/0009_structured_profile_experiences_projects.sql",
    ):
        assert "ALTER TABLE" not in Path(projection).read_text(encoding="utf-8")
    statements = Path("migrations/0007_profile_facts.sql").read_text(encoding="utf-8")
    created = [
        line for line in statements.splitlines() if line.startswith("CREATE TABLE")
    ]

    assert created == [
        "CREATE TABLE profile_facts (",
        "CREATE TABLE profile_fact_provenance (",
    ]
    assert "ALTER TABLE" not in statements
