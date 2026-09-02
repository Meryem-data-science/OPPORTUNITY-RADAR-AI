from datetime import date

from services.priority.dry_run import run_priority_dry_run
from services.priority.input_assembly import (
    PRIORITY_INPUT_ASSEMBLY_VERSION,
    PriorityInputAssemblyResult,
    PriorityReadinessIssue,
    PriorityReadinessIssueCode,
    PriorityReadinessStatus,
)


def test_incomplete_preflight_never_scores(monkeypatch):
    assembly = PriorityInputAssemblyResult(
        PRIORITY_INPUT_ASSEMBLY_VERSION,
        PriorityReadinessStatus.INCOMPLETE,
        7,
        19,
        2,
        date(2026, 9, 2),
        (
            PriorityReadinessIssue(
                PriorityReadinessIssueCode.INVALID_DEADLINE, "bad", 3
            ),
        ),
        (),
    )
    monkeypatch.setattr(
        "services.priority.dry_run.assemble_priority_inputs", lambda *_: assembly
    )

    def forbidden(_):
        raise AssertionError("scoring must not run")

    monkeypatch.setattr(
        "services.priority.dry_run.build_priority_assessment", forbidden
    )
    report = run_priority_dry_run(object(), 7, date(2026, 9, 2))
    assert report.status is PriorityReadinessStatus.INCOMPLETE
    assert report.assessment_count == 0 and report.lines == ()
