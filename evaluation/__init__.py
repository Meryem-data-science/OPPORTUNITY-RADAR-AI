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
"""
