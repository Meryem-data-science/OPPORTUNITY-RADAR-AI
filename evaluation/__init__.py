"""Evaluation artefacts: how well the radar covers a declared source universe.

Nothing under `evaluation/` is production. No module here is imported by a
collector, by the `RadarAgent`, by the API or by the web application, nothing
here writes to the operational database, and no file here is a source the radar
collects. The operational catalogue stays exactly where it is, in
`config/sources.yaml`.

This package holds the *denominator* of the coverage question — the sources we
declare we want to cover, and the real opportunities we know were published on
them — so that a later slice can measure a recall against something explicit
and versioned instead of against "the internet".

Subpackages, one layer:

    morocco_pfe/   which sources we claim to cover, and what they really list
    dataset/       Phase 10.1: a versioned, reproducible snapshot of real
                   opportunities and the production signals already attached to
                   them, frozen out of the operational SQLite database
    labeling/      Phase 10.2: the human relevance protocol, the blind evidence
                   view that applies it, and the append-only label store
    metrics/       Phase 10.3: the offline metric run contract, the availability
                   gates that decide whether a ranking metric may be reported at
                   all, and the three ranking formulas
    business_metrics/
                   Phase 10.4: the business and data-quality metrics of a frozen
                   cohort — how much of it is classified, deduplicated, datable,
                   still reachable
    experiments/   Phase 10.5: five independent projections of one frozen
                   cohort, their six overlaps, and the observed ranking
                   experiment — an offline layer over the four frozen artefacts
                   above, which owns no ranking formula of its own

and one module beside them:

    frozen_facts.py
                   the pure reading of a frozen record that Phase 10.4 and Phase
                   10.5 share: what a geography segment established, and how the
                   Data/AI classifier disposed of a posting. It exists so that
                   "explicitly out of target" and "explicitly out of scope" are
                   defined once rather than once per phase.

**This module re-exports nothing.** Each subpackage is imported by its own name,
and `experiments/` deliberately has no presence here: a root facade over a
package that reads every other one would be the easiest way for something to
end up importing it by accident, and nothing outside `evaluation/` may.

`dataset/` is the one module here that opens the operational database, and it
opens it read-only: it runs `SELECT`s through a `mode=ro` connection, adds no
table, applies no migration and writes its artefacts to `data/evaluation/`,
outside the database and outside Git.
"""
