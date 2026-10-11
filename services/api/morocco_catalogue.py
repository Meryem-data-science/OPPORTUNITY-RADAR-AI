"""Read-only scope of the public Morocco Data & AI catalogue.

Location evidence must already be resolved by the collection pipeline. Neither
a source's country nor a company name proves the location of a posting.
Historic analysis runs and candidatures remain outside this catalogue scope.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


MOROCCO_COUNTRY_CODE = "MA"
CATALOGUE_QUALIFICATIONS = ("CORE_TARGET", "ADJACENT_TARGET")
MOROCCO_LOCATION_PARAMETERS = ("RESOLVED", MOROCCO_COUNTRY_CODE)
MOROCCO_CATALOGUE_PARAMETERS = (
    "visible", 1, *CATALOGUE_QUALIFICATIONS, *MOROCCO_LOCATION_PARAMETERS,
)


def morocco_location_predicate(opportunity_id_column: str) -> str:
    """SQL fragment for a module-defined identifier, never request input."""
    if opportunity_id_column not in {"o.id", "opportunities.id"}:
        raise ValueError("Unsupported catalogue opportunity identifier")
    return (
        "EXISTS (SELECT 1 FROM opportunity_location_resolutions AS catalogue_location "
        f"WHERE catalogue_location.opportunity_id = {opportunity_id_column} "
        "AND catalogue_location.resolution_status = ? "
        "AND catalogue_location.country_code = ?)"
    )


def morocco_catalogue_predicate(opportunity_alias: str) -> str:
    """The complete parameterized scope shared by listing and detail readers."""
    if opportunity_alias not in {"o", "opportunities"}:
        raise ValueError("Unsupported catalogue opportunity alias")
    return (
        f"{opportunity_alias}.status = ? AND {opportunity_alias}.is_active = ? "
        "AND EXISTS (SELECT 1 FROM opportunity_qualifications AS catalogue_qualification "
        f"WHERE catalogue_qualification.opportunity_id = {opportunity_alias}.id "
        "AND catalogue_qualification.qualification IN (?, ?)) "
        f"AND {morocco_location_predicate(opportunity_alias + '.id')}"
    )


def read_morocco_catalogue_ids(
    connection: Any, opportunity_ids: Sequence[int]
) -> frozenset[int]:
    """Return active Data/AI postings with confirmed Morocco location.

    Snapshot readers audit their complete immutable cohort first, then use
    this subset solely for the current user-facing projection.
    """
    wanted = tuple(sorted(set(opportunity_ids)))
    visible: set[int] = set()
    for offset in range(0, len(wanted), 500):
        batch = wanted[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT o.id FROM opportunities AS o
            WHERE o.id IN ({placeholders}) AND {morocco_catalogue_predicate('o')}
            ORDER BY o.id""",
            (*batch, *MOROCCO_CATALOGUE_PARAMETERS),
        ).fetchall()
        visible.update(int(row[0]) for row in rows)
    return frozenset(visible)
