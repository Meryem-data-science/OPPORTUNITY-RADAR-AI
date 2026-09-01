"""Deterministic fingerprint for persisted matching audit reports."""

from __future__ import annotations

import hashlib
from typing import Any

from .fingerprint import canonical_json


def matching_persistence_audit_fingerprint(**values: Any) -> str:
    """Hash only stable stored identities and structured audit findings."""
    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()
