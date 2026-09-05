"""Command-line edges of the Morocco PFE evaluation artefacts.

These modules are the only place under `evaluation/` that performs live I/O.
They read; they persist nothing of what they read. Nothing here opens the
operational database, inserts an opportunity or edits `config/sources.yaml`.

The one local file that can change underneath them is the OAuth client's own
credential file at `GMAIL_TOKEN_PATH`, maintained by
`services/collector/gmail/client.py` exactly as it already was.
"""
