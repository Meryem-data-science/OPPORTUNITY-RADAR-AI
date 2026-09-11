"""The frozen semantic contract of `human-relevance-v1`, checked as a contract.

Phase 10.2 ends by freezing what a grade *means*. This module pins that
definition down so it cannot drift quietly: the version string, the scale, the
hard-constraint rule, and above all the rule that an absence of evidence is not
a contradiction.

Nothing here reads a dataset, a label file or the operational database. No real
opportunity and no real judgement appears in it.
"""

import ast
from pathlib import Path

import pytest

from evaluation.labeling import (
    CALIBRATION_V0_PROVENANCE,
    FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION,
    FROZEN_RELEVANCE_RUBRIC,
    HUMAN_LABEL_PROTOCOL_VERSION,
    RELEVANCE_GRADE_NAMES,
    RELEVANCE_GRADES,
    SUPPORTED_PROTOCOL_VERSIONS,
    UNKNOWN_IS_NOT_FALSE,
    ConstraintEvidence,
    HardConstraint,
    HardConstraintKind,
    frozen_rubric_grade,
    hard_contradictions,
    out_of_target_is_established,
)

#: One profile's declared hard constraints, as an example rather than as a rule.
#: The geography constraint names Morocco *here*, in a test fixture, because the
#: profile being evaluated declares it — the protocol itself names only the
#: kind, which is what lets a second profile declare something else.
PROFILE_HARD_CONSTRAINTS = (
    HardConstraint(
        kind=HardConstraintKind.TARGET_GEOGRAPHY,
        statement="the work must be in Morocco unless the profile declares "
        "another country or international remote as acceptable",
    ),
    HardConstraint(
        kind=HardConstraintKind.TARGET_LEVEL,
        statement="the position must be PFE, internship or junior",
    ),
)


# --------------------------------------------------------------------------
# the frozen version, and its separation from the calibration protocol
# --------------------------------------------------------------------------


def test_the_frozen_protocol_version_is_exactly_human_relevance_v1():
    assert FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION == "human-relevance-v1"


def test_the_frozen_contract_is_not_the_protocol_the_writer_records():
    """Two strings, two jobs, and unifying them would damage the audit trail.

    Every label that exists was made under the calibration protocol. Moving the
    writer to v1 would either rewrite what those labels say they answered, or
    append v1 rows behind v0 rows in one file.
    """
    assert HUMAN_LABEL_PROTOCOL_VERSION == "human-relevance-calibration-v0"
    assert FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION != HUMAN_LABEL_PROTOCOL_VERSION


def test_the_reader_does_not_yet_interpret_v1():
    """No label carries v1, so a row claiming it does not belong in any file."""
    assert SUPPORTED_PROTOCOL_VERSIONS == (HUMAN_LABEL_PROTOCOL_VERSION,)
    assert FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION not in SUPPORTED_PROTOCOL_VERSIONS


# --------------------------------------------------------------------------
# the scale, which the freeze did not change
# --------------------------------------------------------------------------


def test_the_frozen_rubric_keeps_the_calibration_scale():
    """Same four integers, same four names. The question changed, not the alphabet."""
    assert [entry.grade for entry in FROZEN_RELEVANCE_RUBRIC] == [3, 2, 1, 0]
    assert set(RELEVANCE_GRADES) == {entry.grade for entry in FROZEN_RELEVANCE_RUBRIC}
    assert {
        entry.grade: entry.name for entry in FROZEN_RELEVANCE_RUBRIC
    } == dict(RELEVANCE_GRADE_NAMES)


def test_the_frozen_rubric_asks_about_actionability_not_lexical_proximity():
    """The sharpening the calibration produced, pinned to the wording."""
    grade_three = frozen_rubric_grade(3)
    assert any("geography" in item for item in grade_three.requires)
    assert any("level" in item for item in grade_three.requires)
    assert any("hard contradiction" in item for item in grade_three.requires)
    assert any("surfaced" in item for item in frozen_rubric_grade(2).definition.split())


def test_only_the_top_grades_forbid_a_known_hard_contradiction():
    assert frozen_rubric_grade(3).tolerates == ()
    assert "no known hard contradiction" in frozen_rubric_grade(2).requires
    assert any(
        "would impose 0" in item for item in frozen_rubric_grade(1).requires
    )


def test_grade_zero_is_a_human_judgement_and_never_a_default():
    definition = frozen_rubric_grade(0).definition
    assert "A human determined" in definition
    assert "Never a default" in definition
    assert "never the value of an opportunity nobody judged" in definition


def test_unjudged_is_still_not_zero_under_the_frozen_contract():
    """The invariant survives the freeze, stated in the contract itself."""
    assert (
        "an opportunity nobody judged",
        "an opportunity graded 0",
    ) in UNKNOWN_IS_NOT_FALSE


# --------------------------------------------------------------------------
# hard constraints: what an explicit contradiction does
# --------------------------------------------------------------------------


def test_an_explicit_geographic_contradiction_establishes_out_of_target():
    """A posting that explicitly cannot meet a declared hard geography.

    The profile in this fixture declares Morocco; the posting says it is
    somewhere else and the profile accepts no other country and no international
    remote. Under v1 that is out of target however good the rest of the fit is.
    """
    evidence = {
        HardConstraintKind.TARGET_GEOGRAPHY: ConstraintEvidence.CONTRADICTED,
        HardConstraintKind.TARGET_LEVEL: ConstraintEvidence.SATISFIED,
    }
    assert out_of_target_is_established(evidence)
    assert hard_contradictions(evidence) == (HardConstraintKind.TARGET_GEOGRAPHY,)


def test_an_explicitly_senior_posting_contradicts_a_strictly_junior_target():
    evidence = {
        HardConstraintKind.TARGET_LEVEL: ConstraintEvidence.CONTRADICTED,
        HardConstraintKind.TARGET_GEOGRAPHY: ConstraintEvidence.SATISFIED,
    }
    assert out_of_target_is_established(evidence)
    assert hard_contradictions(evidence) == (HardConstraintKind.TARGET_LEVEL,)


def test_every_declared_constraint_kind_can_carry_a_contradiction():
    for kind in HardConstraintKind:
        assert out_of_target_is_established(
            {kind: ConstraintEvidence.CONTRADICTED}
        )


def test_a_satisfied_constraint_establishes_nothing_on_its_own():
    """Grades 1, 2 and 3 are positive judgements needing positive evidence.

    The absence of a contradiction is not relevance, and this function never
    claims it is: it answers one question and says nothing about the others.
    """
    evidence = {
        kind: ConstraintEvidence.SATISFIED for kind in HardConstraintKind
    }
    assert not out_of_target_is_established(evidence)
    assert hard_contradictions(evidence) == ()


# --------------------------------------------------------------------------
# UNKNOWN != FALSE, case by case
# --------------------------------------------------------------------------


def test_an_unknown_geography_does_not_establish_out_of_target():
    """A posting that does not say where the work is has not said it is elsewhere."""
    evidence = {HardConstraintKind.TARGET_GEOGRAPHY: ConstraintEvidence.UNKNOWN}
    assert not out_of_target_is_established(evidence)
    assert hard_contradictions(evidence) == ()


def test_an_unknown_level_is_not_read_as_senior():
    evidence = {HardConstraintKind.TARGET_LEVEL: ConstraintEvidence.UNKNOWN}
    assert not out_of_target_is_established(evidence)


def test_a_constraint_the_posting_says_nothing_about_is_not_a_contradiction():
    """Absence of a key is absence of evidence, not evidence against."""
    assert not out_of_target_is_established({})
    assert hard_contradictions({}) == ()


def test_a_pile_of_unknowns_is_still_not_a_contradiction():
    """Uncertainty accumulates into a lower grade, never into a hard refusal.

    Under v1 an opportunity like this may well be 1, and may be UNJUDGED if the
    evidence is too thin to defend any grade. What it is not is 0 by accretion.
    """
    evidence = {kind: ConstraintEvidence.UNKNOWN for kind in HardConstraintKind}
    assert not out_of_target_is_established(evidence)
    assert hard_contradictions(evidence) == ()


def test_one_contradiction_among_unknowns_is_still_a_contradiction():
    evidence = {kind: ConstraintEvidence.UNKNOWN for kind in HardConstraintKind}
    evidence[HardConstraintKind.WORK_AUTHORIZATION] = ConstraintEvidence.CONTRADICTED
    assert out_of_target_is_established(evidence)
    assert hard_contradictions(evidence) == (HardConstraintKind.WORK_AUTHORIZATION,)


def test_the_unknown_is_not_false_invariant_is_stated_case_by_case():
    absences = {absence for absence, _ in UNKNOWN_IS_NOT_FALSE}
    assert "a posting with no description" in absences
    assert "a posting stating no level" in absences
    assert "a posting stating no country" in absences
    assert "a posting stating no opportunity type" in absences
    assert "evidence that is absent" in absences


def test_the_contract_names_kinds_of_constraint_and_never_a_place():
    """A protocol that hard-coded one profile's geography would be that profile's.

    The places, levels and languages live in the profile and preferences behind
    `profile_context_fingerprint`; the protocol knows only that a constraint of
    a given kind is in force.
    """
    vocabulary = " ".join(str(kind) for kind in HardConstraintKind).upper()
    for place in ("MOROCCO", "MAROC", "_MA_", "CASABLANCA", "FRANCE"):
        assert place not in vocabulary
    # The example profile states its own geography, in prose, where it belongs.
    geography = next(
        constraint
        for constraint in PROFILE_HARD_CONSTRAINTS
        if constraint.kind is HardConstraintKind.TARGET_GEOGRAPHY
    )
    assert "Morocco" in geography.statement


# --------------------------------------------------------------------------
# where the frozen contract came from
# --------------------------------------------------------------------------


def test_the_freeze_records_the_calibration_it_came_from():
    provenance = CALIBRATION_V0_PROVENANCE
    assert provenance.protocol_version == HUMAN_LABEL_PROTOCOL_VERSION
    assert provenance.judged_count == 12
    assert provenance.unjudged_count == 0
    # Thirteen rows for twelve judgements: one explicit relabel at revision 2.
    assert provenance.recorded_label_rows == 13
    assert sum(provenance.grade_distribution.values()) == provenance.judged_count
    assert provenance.grade_distribution == {
        "OUT_OF_TARGET": 7,
        "WEAKLY_RELEVANT": 2,
        "RELEVANT": 3,
        "VERY_RELEVANT": 0,
    }
    assert set(provenance.grade_distribution) == set(RELEVANCE_GRADE_NAMES.values())


def test_the_calibration_is_recorded_as_ai_assisted_and_not_as_a_benchmark():
    """What a round can be used to claim depends on how it was run.

    A model proposed a reading and a person decided each grade. That is how a
    rubric gets usable; it is not an independent measurement of anything, and
    saying so here means nobody downstream has to infer it.
    """
    provenance = CALIBRATION_V0_PROVENANCE
    assert "AI-assisted" in provenance.method
    assert "human validation" in provenance.method
    assert "an independent human benchmark" in provenance.not_a
    assert "an inter-annotator agreement study" in provenance.not_a
    assert "an independent gold-standard holdout" in provenance.not_a


def test_the_provenance_records_digests_and_counts_and_nothing_else():
    """No opportunity, no judgement, no personal detail is frozen into the repo."""
    provenance = CALIBRATION_V0_PROVENANCE
    assert provenance.dataset_id == "evaluation-dataset-v3-8ed3d8fa9359f24c"
    for digest in (
        provenance.dataset_content_fingerprint,
        provenance.selection_fingerprint,
        provenance.labelset_fingerprint,
    ):
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
    # The dataset id is the schema version plus the first half-digest, exactly as
    # Phase 10.1 derives it.
    assert provenance.dataset_id.endswith(
        provenance.dataset_content_fingerprint[:16]
    )


def test_no_module_but_the_contract_itself_spells_the_frozen_version():
    """The freeze is a definition; judgements under it have not been made.

    Checked against string *literals* rather than prose: the other modules
    discuss `human-relevance-v1` in their docstrings and comments — they should,
    since the separation is the interesting part — but only `rubric.py` may
    contain it as a value. So there is no path by which the writer could stamp
    it on a label.
    """
    package = Path(__file__).resolve().parents[2] / "evaluation" / "labeling"
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node,
                (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            )
            and getattr(node, "body", None)
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        }
        if path.name == "rubric.py":
            assert FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION in literals
            continue
        assert FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION not in literals, path.name


def test_the_writer_stamps_the_calibration_protocol_on_every_label():
    """The one place a protocol version reaches a stored row."""
    storage = (
        Path(__file__).resolve().parents[2]
        / "evaluation"
        / "labeling"
        / "storage.py"
    ).read_text(encoding="utf-8")
    assert "protocol_version=HUMAN_LABEL_PROTOCOL_VERSION," in storage


@pytest.mark.parametrize("kind", list(HardConstraintKind))
def test_a_hard_constraint_states_its_kind_and_its_content_separately(kind):
    constraint = HardConstraint(kind=kind, statement="something the profile says")
    assert constraint.kind is kind
    assert constraint.statement
