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

**Why it is not the Data/AI cohort either.**

`evaluation-cohort-v1` selected the frontier production matches
(`matching-selection-v1`, widened to admit `UNCERTAIN`). That was still too
narrow, and for a reason that goes to the heart of what Phase 10 measures:
`qualification` is a **prediction of the Data/AI classifier, not a human
truth**. A real Data/AI opportunity that the classifier read and judged
`OUT_OF_SCOPE` is a false negative — the earliest and most consequential one the
pipeline can make — and filtering on the classifier's own verdict deletes
exactly those rows before anyone can look at them. Filtering on the presence of
a qualification row has the same effect for a posting the classifier has not
read yet: its absence is an upstream fact worth measuring, not a negative
judgement about the posting.

**`evaluation-cohort-v2` is therefore defined by collection, not by prediction:**

    opportunities.is_active = 1
    AND opportunities.status != 'merged_duplicate'
    ORDER BY opportunities.id

Everything the collector found and deduplicated, whatever any downstream model
later said about it: `CORE_TARGET`, `ADJACENT_TARGET`, `UNCERTAIN`,
`OUT_OF_SCOPE` and postings carrying no qualification row at all. Those last
ones arrive with `qualification = None`, which is never rewritten into
`OUT_OF_SCOPE`, `UNCERTAIN`, `UNKNOWN`, `False` or an empty object.

**Two exclusions remain, and neither hides a judgement.**

`is_active = 0` is a posting the collector stopped seeing, and
`status = 'merged_duplicate'` is a tombstone whose content was folded into a
canonical row that *is* in the cohort — with `absorbed_duplicate_ids` naming the
merge, so the deduplication context stays traceable from inside the dataset.
Both are decisions about *collection*, made before any model ran, and both are
counted in `excluded_counts` so their size is visible.

**And no sampling.** Stratifying a human benchmark is Phase 10.2's problem; a
snapshot that has already thrown rows away cannot be stratified honestly later.

Everything in this module is a `SELECT`. Nothing here writes.
"""

from __future__ import annotations

import sqlite3

from .schema import EvaluationDatasetError


#: Every qualification state a snapshot admits — which, deliberately, is all of
#: them, plus the absence of a qualification row entirely. Kept as a named
#: constant because "the cohort does not filter on the classifier's verdict" is
#: the load-bearing property of `v2` and deserves somewhere to be asserted.
EVALUATION_COHORT_QUALIFICATIONS: tuple[str, ...] = (
    "CORE_TARGET",
    "ADJACENT_TARGET",
    "OUT_OF_SCOPE",
    "UNCERTAIN",
)

#: `ORDER BY o.id` is the canonical order, and it is total: `opportunities.id`
#: is an INTEGER PRIMARY KEY, so no two rows tie and no record's position can
#: depend on an implicit SQL ordering.
_COHORT_SQL = """
    SELECT o.id
      FROM opportunities AS o
     WHERE o.is_active = 1
       AND o.status != 'merged_duplicate'
     ORDER BY o.id
"""

#: One count per exclusion rule this cohort applies, and no other count: a key
#: here is a row the snapshot refused. The two are disjoint and sum with the
#: cohort to the whole `opportunities` table, so a reader can check the
#: partition rather than trust it.
#:
#: These counts are **inside the content fingerprint**. They state what the
#: selection did, so two snapshots that excluded different numbers of postings
#: did not select the same universe and must not share a `dataset_id` — even in
#: the unlikely case that the rows they did select happen to match.
_EXCLUDED_COUNT_SQL: dict[str, str] = {
    "merged_duplicate": """
        SELECT COUNT(*) FROM opportunities WHERE status = 'merged_duplicate'
    """,
    "inactive": """
        SELECT COUNT(*) FROM opportunities
         WHERE is_active = 0 AND status != 'merged_duplicate'
    """,
}


def select_evaluation_cohort_ids(
    connection: sqlite3.Connection,
) -> tuple[int, ...]:
    """Return the `evaluation-cohort-v2` opportunity ids, in canonical order."""
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
