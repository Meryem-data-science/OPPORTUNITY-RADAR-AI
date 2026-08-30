"""Phase 3.6 — the eligibility engine.

The first package in this project that reads both sides of the database. Phases
3.4 and 3.5 were each built to answer one half of a question and were kept
apart on purpose; this one puts them together and answers:

    given what a posting explicitly demands, and what a person's reliable facts
    state, is there a **known** reason they could not apply?

Three answers, and the distance between two of them is the whole design:

    ELIGIBLE    nothing known stands in the way
    INELIGIBLE  a hard requirement was contradicted by a reliable fact
    UNKNOWN     a hard requirement applies and the fact needed is missing

`UNKNOWN` is a question, never a soft refusal. Absence of evidence is not
evidence of ineligibility: a CV that never named a language has not said its
author cannot speak it, and a posting that never named a degree has not demanded
one. Every rule is written so that a gap becomes UNKNOWN or NOT_APPLICABLE, and
`migrations/0014` re-states that in CHECK constraints so the opposite is not
storable.

Eligibility is not matching. There is no score here, no ranking and no
similarity of any kind; a posting can be ELIGIBLE and a poor fit, or INELIGIBLE
and an excellent one. Matching is Phase 4 and does not exist.

    models.py       the vocabulary, and the two inputs a rule may read
    comparison.py   the two comparisons v1 is willing to make, and their limits
    explanations.py deterministic sentences for the reason codes
    fingerprint.py  the digest over exactly what the rules read
    engine.py       the rules, pure
    inputs.py       reading both sides out of SQLite, and what is not there
    repository.py   transactional persistence
    service.py      idempotent synchronization
    audit.py        checking the stored rows against this phase's promises
    cli.py          status, sync, audit
"""

from services.eligibility.models import (
    ELIGIBILITY_ENGINE_VERSION,
    EligibilityDecision,
    GlobalStatus,
    ReasonCode,
    RuleStatus,
)

__all__ = [
    "ELIGIBILITY_ENGINE_VERSION",
    "EligibilityDecision",
    "GlobalStatus",
    "ReasonCode",
    "RuleStatus",
]
