"""Turn one audited Portfolio snapshot into the items a digest will show.

Every value here is read, never derived. The title, the organization, the
location and the outward link come from the opportunity authority, and the
link is chosen by :mod:`services.api.link_priority` — the same deterministic
policy the Portfolio page already applies, so a digest can never point
somewhere the product's own surface would not. The bucket, the priority
category and the eligibility status come from the persisted Portfolio
assessment exactly as stored: nothing is recomputed, and ``UNKNOWN`` stays
``UNKNOWN``.

Two things are refused rather than papered over. An INCLUDED assessment with
no persisted priority category contradicts the Portfolio engine, which only
ever includes a priority candidate, so the snapshot is treated as unsafe. And
an opportunity that cannot be resolved to a real http/https link is refused
whole — a digest whose links do not work is worse than a digest that was not
sent, so the whole assembly fails closed rather than shipping one dead row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from urllib.parse import urlsplit

from services.api.link_priority import (
    SourceObservation,
    preferred_link,
    select_original_url,
)
from services.portfolio.models import PortfolioDisposition
from services.portfolio.read_model import (
    PortfolioAssessmentReadModel,
    PortfolioRunReadModel,
)

from .models import (
    BUCKET_ORDER,
    PRIORITY_ORDER,
    DigestItem,
    GmailDigestError,
)

#: The only schemes a digest will ever put behind an href. A mailto:, a
#: javascript:, or a bare path is not a job posting.
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

_BUCKET_RANK = {bucket: index for index, bucket in enumerate(BUCKET_ORDER)}
_PRIORITY_RANK = {category: index for index, category in enumerate(PRIORITY_ORDER)}


def validate_digest_url(value: object, opportunity_id: int) -> str:
    """Return this link only if it is a real, absolute http/https URL."""
    if not isinstance(value, str) or value != value.strip() or not value:
        raise GmailDigestError(
            f"opportunity {opportunity_id} has no usable authoritative link"
        )
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise GmailDigestError(
            f"opportunity {opportunity_id} has an unparseable authoritative link"
        ) from error
    if parts.scheme.lower() not in ALLOWED_URL_SCHEMES or not parts.netloc:
        raise GmailDigestError(
            f"opportunity {opportunity_id} has no absolute http/https link"
        )
    if any(character.isspace() for character in value):
        raise GmailDigestError(
            f"opportunity {opportunity_id} has whitespace in its authoritative link"
        )
    return value


def _text(value: object, opportunity_id: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GmailDigestError(f"opportunity {opportunity_id} has no usable {field}")
    return value.strip()


def _optional_text(value: object, opportunity_id: int, field: str) -> str | None:
    """Return a present, non-blank value, or None — never an invented one."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise GmailDigestError(f"opportunity {opportunity_id} has an invalid {field}")
    stripped = value.strip()
    return stripped or None


def _observations(
    connection: sqlite3.Connection, opportunity_ids: Sequence[int]
) -> dict[int, list[SourceObservation]]:
    placeholders = ", ".join("?" for _ in opportunity_ids)
    rows = connection.execute(
        f"""SELECT opportunity_sources.opportunity_id,opportunity_sources.id,sources.type,
        opportunity_sources.application_url,opportunity_sources.source_url,
        opportunity_sources.canonical_url FROM opportunity_sources JOIN sources
        ON sources.id=opportunity_sources.source_id
        WHERE opportunity_sources.opportunity_id IN ({placeholders})
        ORDER BY opportunity_sources.opportunity_id,opportunity_sources.id""",
        tuple(opportunity_ids),
    ).fetchall()
    observations: dict[int, list[SourceObservation]] = {}
    for row in rows:
        observations.setdefault(int(row[0]), []).append(SourceObservation(*row[1:]))
    return observations


def read_digest_opportunity_facts(
    connection: sqlite3.Connection, opportunity_ids: Sequence[int]
) -> dict[int, tuple[str, str, str | None, str]]:
    """Describe every requested opportunity, or refuse the whole batch."""
    wanted = sorted(set(opportunity_ids))
    if not wanted:
        return {}
    placeholders = ", ".join("?" for _ in wanted)
    rows = connection.execute(
        f"""SELECT id,canonical_title,organization,location,source_url,application_url,
        canonical_url FROM opportunities WHERE id IN ({placeholders}) ORDER BY id ASC""",
        tuple(wanted),
    ).fetchall()
    if {int(row[0]) for row in rows} != set(wanted):
        raise GmailDigestError(
            "digest opportunities are missing from the opportunity authority"
        )
    observations = _observations(connection, wanted)
    facts: dict[int, tuple[str, str, str | None, str]] = {}
    for row in rows:
        opportunity_id = int(row[0])
        fallback = preferred_link(row[5], row[4], row[6])
        url = (
            fallback
            if fallback is None
            else select_original_url(
                observations.get(opportunity_id, ()), fallback=fallback
            )
        )
        facts[opportunity_id] = (
            _text(row[1], opportunity_id, "canonical title"),
            _text(row[2], opportunity_id, "organization"),
            _optional_text(row[3], opportunity_id, "location"),
            validate_digest_url(url, opportunity_id),
        )
    return facts


def digest_sort_key(item: DigestItem) -> tuple[int, int, int]:
    """TARGET before SAFE before AMBITIOUS, then priority, then id.

    The opportunity id closes every remaining tie, so two assemblies of the
    same snapshot produce byte-identical output whatever order SQLite handed
    the rows back in.
    """
    return (
        _BUCKET_RANK[item.bucket],
        _PRIORITY_RANK[item.priority_category],
        item.opportunity_id,
    )


def _included(run: PortfolioRunReadModel) -> tuple[PortfolioAssessmentReadModel, ...]:
    included = tuple(
        assessment
        for assessment in run.assessments
        if assessment.disposition is PortfolioDisposition.INCLUDED
    )
    for assessment in included:
        if assessment.bucket is None:
            raise GmailDigestError(
                f"opportunity {assessment.opportunity_id} is included without a bucket"
            )
        if assessment.priority_category is None:
            # Portfolio v1 only ever includes a priority candidate, so this is
            # a snapshot contradicting its own engine, not a missing optional.
            raise GmailDigestError(
                f"opportunity {assessment.opportunity_id} is included without a"
                " persisted priority category"
            )
        if assessment.bucket not in _BUCKET_RANK:
            raise GmailDigestError(
                f"opportunity {assessment.opportunity_id} has an unorderable bucket"
            )
        if assessment.priority_category not in _PRIORITY_RANK:
            raise GmailDigestError(
                f"opportunity {assessment.opportunity_id} has an unorderable priority"
            )
    return included


def build_digest_items(
    connection: sqlite3.Connection, run: PortfolioRunReadModel
) -> tuple[DigestItem, ...]:
    """Return the INCLUDED opportunities of one audited run, in reading order.

    EXCLUDED assessments are not filtered out of a rendered list — they never
    enter it, and no query here can reach one.
    """
    included = _included(run)
    facts = read_digest_opportunity_facts(
        connection, [assessment.opportunity_id for assessment in included]
    )
    items = tuple(
        DigestItem(
            assessment.opportunity_id,
            *facts[assessment.opportunity_id],
            assessment.bucket,
            assessment.priority_category,
            assessment.eligibility_status,
        )
        for assessment in included
    )
    return tuple(sorted(items, key=digest_sort_key))
