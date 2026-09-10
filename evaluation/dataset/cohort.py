"""The evaluation universe: which real opportunities a snapshot contains.

**Why this is not "the recommendations".**

The obvious dataset — every row of the profile's current
`recommendation_assessments` — is exactly the dataset that cannot answer the
question Phase 10 exists to ask. Everything in it has already survived
qualification, selection, matching and the recommendation engine, so measuring
against it can only ever report on postings the pipeline already liked. Every
*false negative* — a genuinely good opportunity that the pipeline dropped
somewhere upstream — is absent by construction, and a recall computed over it
would be a tautology.

So the snapshot is taken one boundary further upstream, at the frontier the
pipeline already defines for itself:

    services/collector/matching/selection.py :: matching-selection-v1

        is_active = 1
        AND status != 'merged_duplicate'
        AND qualification IN ('CORE_TARGET', 'ADJACENT_TARGET')

That is the cohort *production* matches, and it sits above Matching,
Eligibility, Priority and Recommendation. Taking it verbatim would already make
false negatives at those four stages visible.

**`evaluation-cohort-v1` widens it by exactly one state: `UNCERTAIN`.**

`UNCERTAIN` is the coarse classifier's own "I do not know whether this is
Data/AI". Matching excludes it, which means every `UNCERTAIN` posting is
invisible to Matching, to Eligibility, to Priority and to Recommendation alike:
it is dropped at the earliest gate in the chain, and it is the single largest
reservoir of candidate false negatives the pipeline has. Excluding it here would
be the very move this contract forbids — reading the pipeline's UNKNOWN as a
FALSE — and it would quietly make the earliest and most consequential filter the
one stage Phase 10 can never evaluate.

**And it stops there.** `OUT_OF_SCOPE` is not an absence of knowledge, it is the
classifier's asserted negative: a posting it read and judged not to be Data/AI
at all. Postings with no qualification row are likewise out, because the row is
what makes an opportunity part of the Data/AI universe in the first place.
Pulling either in would drown a human-labelled benchmark in plainly off-domain
listings while the project already has a canonical frontier that does not.

Both exclusions are real limits on what can be measured, so neither is silent:
`excluded_counts` below reports how many rows each of them left behind, and an
`unclassified` count that is not near zero is a visible sign that qualification
has not caught up with collection — a fact a later slice must weigh, not one it
should have to discover.

Everything in this module is a `SELECT`. Nothing here writes.
"""

from __future__ import annotations

import sqlite3

from .schema import EvaluationDatasetError


#: The qualification states an evaluation snapshot admits, in the order the
#: contract states them. `UNCERTAIN` is present on purpose — see above.
EVALUATION_COHORT_QUALIFICATIONS: tuple[str, ...] = (
    "CORE_TARGET",
    "ADJACENT_TARGET",
    "UNCERTAIN",
)

#: `ORDER BY o.id` is the canonical order, and it is total: `opportunities.id`
#: is an INTEGER PRIMARY KEY, so no two rows tie and no record's position can
#: depend on an implicit SQL ordering.
_COHORT_SQL = """
    SELECT o.id
      FROM opportunities AS o
      JOIN opportunity_qualifications AS q
        ON q.opportunity_id = o.id
     WHERE o.is_active = 1
       AND o.status != 'merged_duplicate'
       AND q.qualification IN ('CORE_TARGET', 'ADJACENT_TARGET', 'UNCERTAIN')
     ORDER BY o.id
"""

#: What the cohort left behind, one count per reason. Each is measured against
#: the same universe the cohort starts from, so the four are readable side by
#: side rather than being four different denominators.
_EXCLUDED_COUNT_SQL: dict[str, str] = {
    "merged_duplicate": """
        SELECT COUNT(*) FROM opportunities WHERE status = 'merged_duplicate'
    """,
    "inactive": """
        SELECT COUNT(*) FROM opportunities
         WHERE is_active = 0 AND status != 'merged_duplicate'
    """,
    "out_of_scope": """
        SELECT COUNT(*)
          FROM opportunities AS o
          JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
         WHERE o.is_active = 1
           AND o.status != 'merged_duplicate'
           AND q.qualification = 'OUT_OF_SCOPE'
    """,
    "unclassified": """
        SELECT COUNT(*)
          FROM opportunities AS o
          LEFT JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
         WHERE o.is_active = 1
           AND o.status != 'merged_duplicate'
           AND q.opportunity_id IS NULL
    """,
}


def select_evaluation_cohort_ids(
    connection: sqlite3.Connection,
) -> tuple[int, ...]:
    """Return the `evaluation-cohort-v1` opportunity ids, in canonical order."""
    try:
        rows = connection.execute(_COHORT_SQL).fetchall()
    except sqlite3.Error as error:
        raise EvaluationDatasetError(
            "cannot select the evaluation cohort"
        ) from error
    return tuple(int(row[0]) for row in rows)


def count_excluded_opportunities(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    """Return how many rows each cohort exclusion left behind, by reason."""
    counts: dict[str, int] = {}
    for reason, sql in _EXCLUDED_COUNT_SQL.items():
        try:
            counts[reason] = int(connection.execute(sql).fetchone()[0])
        except sqlite3.Error as error:
            raise EvaluationDatasetError(
                f"cannot count opportunities excluded as {reason}"
            ) from error
    return counts
