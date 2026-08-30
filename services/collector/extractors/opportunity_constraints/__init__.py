"""Phase 3.5A: what an opportunity itself requires.

    opportunities
        -> extract_opportunity_constraints (pure, offline, deterministic)
             -> opportunity_constraints
                  +-> opportunity_constraint_locations
                  +-> opportunity_education_requirements
                  +-> opportunity_constraint_evidence
                  +-> opportunity_constraint_conflicts

The package reads a posting and records what it asks for: the kind of
opportunity, the education, experience, duration and start it names, where the
work is, how it is done, and what it says about visas, work authorization and
internship agreements.

It never asks what any person offers. There is no profile in this package, no
comparison against one, no eligibility verdict, no match, no score and no rank:
those are Phases 3.6 and 4, and none of them is implemented.

Absence is UNKNOWN and UNKNOWN is never FALSE. A posting silent about visas has
not refused to sponsor; a posting with an address has not required attendance;
a posting written in English has not required English. Every asserted value
carries the rule and the fragment that justified it, and two readings that
disagree assert nothing and record the contradiction.
"""

from services.collector.extractors.opportunity_constraints.extractor import (
    extract_opportunity_constraints,
    source_fingerprint,
)
from services.collector.extractors.opportunity_constraints.models import (
    EXTRACTOR_VERSION,
    ExtractedConstraints,
    OpportunitySource,
)

__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractedConstraints",
    "OpportunitySource",
    "extract_opportunity_constraints",
    "source_fingerprint",
]
