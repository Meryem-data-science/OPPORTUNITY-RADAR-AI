"""Command-line edges of the Morocco PFE evaluation artefacts.

These modules are the only place under `evaluation/` that performs live I/O.
They read; they never write. Nothing here opens the operational database,
inserts an opportunity or edits `config/sources.yaml`.
"""
