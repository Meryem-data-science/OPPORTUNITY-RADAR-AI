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

Two subpackages, one layer:

    morocco_pfe/   which sources we claim to cover, and what they really list
    dataset/       Phase 10.1: a versioned, reproducible snapshot of real
                   opportunities and the production signals already attached to
                   them, frozen out of the operational SQLite database

`dataset/` is the one module here that opens the operational database, and it
opens it read-only: it runs `SELECT`s through a `mode=ro` connection, adds no
table, applies no migration and writes its artefacts to `data/evaluation/`,
outside the database and outside Git.
"""
