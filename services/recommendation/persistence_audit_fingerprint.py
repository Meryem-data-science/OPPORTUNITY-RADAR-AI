"""Deterministic fingerprint for persisted recommendation audit reports.

The same convention Matching's `persistence_audit_fingerprint` established, and
for the same reason: a report is only evidence if two audits of one unchanged
database produce the same digest, and if any change to what was audited — a
stored identity, a ranked position, a structured finding — produces a different
one.

What may reach this function is therefore constrained by the caller, not by this
module's signature: stable persisted identities and deterministic structured
findings only. No audit timestamp, no duration, no database path, no process or
wall-clock value, and nothing whose iteration order is incidental.
"""

from __future__ import annotations

import hashlib
from typing import Any

from services.collector.matching.fingerprint import canonical_json

__all__ = ["recommendation_persistence_audit_fingerprint"]


def recommendation_persistence_audit_fingerprint(**values: Any) -> str:
    """Hash only stable stored identities and structured audit findings."""
    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()
