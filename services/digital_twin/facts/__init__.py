"""Phase 3.3A: the persistent, human-validated content of a profile.

This slice is the anti-hallucination foundation of the Digital Twin. It holds
one chain and nothing else:

    profile → profile_facts → provenance → human validation

and one definition of truth: **a fact is verified when, and only when, its
status is `ACCEPTED`**. There is no `verified` column, no confidence, no score,
no skill level and no proficiency anywhere in this package, because none of
them can be derived from evidence or from a click. `is_verified` is a computed
Python property over the status, never a second stored answer that could drift
away from the first.

A correction never destroys anything: it writes a new `ACCEPTED` fact with the
person's own `USER_INPUT` evidence, marks the previous fact `CORRECTED`, and
links the two, so every value that was ever proposed stays readable and a chain
of successive corrections keeps its whole history.

What this slice does **not** do:

* it does not import the Phase 3.2B `ExtractedCandidate` values — no
  candidate-to-fact mapping exists here, and building it is Phase 3.3B;
* it offers no review CLI, no user interface, no HTTP endpoint and no
  authentication;
* it normalizes no business content: no institution, employer, date, canonical
  role, skill alias or skill level is derived, which is Phase 3.4;
* it generates no Master CV, cover letter, application or form, and it computes
  no eligibility, match, ranking or score.

It reads no network, loads no model and calls no API. The operational database
stays local SQLite, as everywhere else in the project.
"""

from services.digital_twin.facts.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    FactProvenance,
    FactSourceType,
    FactStatus,
    ProfileFact,
    ProfileFactType,
    ProfileFactValueError,
    ProvenanceInput,
    decode_page_numbers,
    encode_page_numbers,
)
from services.digital_twin.facts.repository import (
    FactCorrection,
    InvalidFactTransitionError,
    ProfileFactError,
    ProfileFactNotFoundError,
    accept_profile_fact,
    add_profile_fact_provenance,
    correct_profile_fact,
    get_profile_fact,
    list_profile_fact_provenance,
    list_profile_facts,
    list_verified_profile_facts,
    propose_profile_fact,
    reject_profile_fact,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATUSES",
    "FactCorrection",
    "FactProvenance",
    "FactSourceType",
    "FactStatus",
    "InvalidFactTransitionError",
    "ProfileFact",
    "ProfileFactError",
    "ProfileFactNotFoundError",
    "ProfileFactType",
    "ProfileFactValueError",
    "ProvenanceInput",
    "accept_profile_fact",
    "add_profile_fact_provenance",
    "correct_profile_fact",
    "decode_page_numbers",
    "encode_page_numbers",
    "get_profile_fact",
    "list_profile_fact_provenance",
    "list_profile_facts",
    "list_verified_profile_facts",
    "propose_profile_fact",
    "reject_profile_fact",
]
