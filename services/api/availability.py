"""Read-only projection of current public availability evidence.

This surface neither checks a URL nor changes the catalogue. A current source
observation may supply a fresh, explicit OPEN verdict; an UNKNOWN verdict never
borrows an older positive confirmation. Missing or incoherent state falls back
to TO_VERIFY. The dates describe checks, never collection dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel

from services.api.link_priority import (
    SourceObservation, official_rank, preferred_link, select_original_url,
)
from services.collector.sources import SourceConfig, load_source_registry
from services.operations.availability import (
    TTL, _board_id, _page_identity, lever_posting_identity,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = PROJECT_ROOT / "data" / "runtime" / "availability-state.json"
REGISTRY_PATH = PROJECT_ROOT / "config" / "sources.yaml"
MAX_STATE_BYTES = 20_000_000
MAX_OBSERVATIONS = 50_000
ID_BATCH_SIZE = 500
OPEN_REASONS_BY_SOURCE = {
    "greenhouse": "GREENHOUSE_COMPLETE_BOARD_PRESENT",
    "lever": "LEVER_EXACT_PUBLISHED_POSTING_PRESENT",
    "gmail_linkedin_alert": "LINKEDIN_EXACT_JOB_HAS_APPLICATION_CONTROL",
    "stage_ma_html": "JOBPOSTING_VALID_THROUGH_NOT_PASSED",
    "stagiaires_sitemap": "JOBPOSTING_VALID_THROUGH_NOT_PASSED",
    "sqli_careers": "JOBPOSTING_VALID_THROUGH_NOT_PASSED",
}


class AvailabilityResponse(BaseModel):
    """A public verdict with no source URL or diagnostic reason."""

    status: Literal["OPEN_CONFIRMED", "TO_VERIFY"] = "TO_VERIFY"
    last_checked_at: str | None = None
    verified_at: str | None = None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate state key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON numeric constant")


def _load_records(path: Path) -> dict[str, dict[str, Any]]:
    """Bound the actual read, including when a file changes after stat()."""
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_STATE_BYTES + 1)
        if len(content) > MAX_STATE_BYTES:
            return {}
        state = json.loads(
            content.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        if (not isinstance(state, dict) or type(state.get("version")) is not int
                or state["version"] != 1):
            return {}
        records = state.get("observations")
        if not isinstance(records, dict) or len(records) > MAX_OBSERVATIONS:
            return {}
        for key, record in records.items():
            if (not isinstance(record, dict)
                    or type(record.get("opportunity_id")) is not int
                    or record["opportunity_id"] <= 0
                    or not isinstance(record.get("source_id"), str)
                    or not record["source_id"]
                    or not isinstance(record.get("source_url"), str)
                    or not record["source_url"]
                    or record.get("state") not in {"OPEN", "CLOSED", "UNKNOWN"}
                    or record.get("confirmed_state") not in {None, "OPEN", "CLOSED"}
                    or key != json.dumps(
                        [record["source_id"], record["source_url"]], separators=(",", ":")
                    )):
                return {}
        return records
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return {}


def _timestamp(value: Any, now: datetime) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        parsed = parsed.astimezone(timezone.utc)
        return parsed if parsed <= now else None
    except (TypeError, ValueError, OverflowError):
        return None


def _in_current_scope(source: SourceConfig, source_url: str) -> bool:
    """A changed board or source kind cannot borrow its old check result."""
    if source.type == "greenhouse":
        return bool(source.board_token) and _board_id(source_url, source.board_token) is not None
    if source.type == "lever":
        return bool(source.board_token) and lever_posting_identity(source_url, source.board_token) is not None
    page_kind = {
        "gmail_linkedin_alert": "linkedin", "stage_ma_html": "stage",
        "stagiaires_sitemap": "stagiaires", "sqli_careers": "sqli",
    }.get(source.type)
    identity = _page_identity(source_url)
    return identity is not None and identity[0] == page_kind


@dataclass(frozen=True)
class AvailabilityProjection:
    """One file/config picture and one clock reused throughout a request."""

    records: dict[str, dict[str, Any]]
    active_sources: dict[str, SourceConfig]
    now: datetime

    def read(
        self, connection: Any, opportunity_ids: Sequence[int]
    ) -> dict[int, AvailabilityResponse]:
        ids = list(dict.fromkeys(int(value) for value in opportunity_ids))
        checked: dict[int, datetime] = {}
        positive: dict[int, datetime] = {}
        opened: dict[int, tuple[int, int, str, datetime]] = {}
        if not self.records:
            return {value: AvailabilityResponse() for value in ids}
        for start in range(0, len(ids), ID_BATCH_SIZE):
            batch = ids[start:start + ID_BATCH_SIZE]
            placeholders = ", ".join("?" for _ in batch)
            rows = connection.execute(
                f"""SELECT os.opportunity_id, os.source_id, os.source_url, s.type, os.id,
                (SELECT COUNT(*) FROM opportunity_sources AS identity
                 WHERE identity.source_id = os.source_id AND identity.source_url = os.source_url),
                os.application_url, os.canonical_url, o.source_url, o.application_url, o.canonical_url
                FROM opportunity_sources AS os JOIN sources AS s ON s.id = os.source_id
                JOIN opportunities AS o ON o.id = os.opportunity_id
                WHERE os.opportunity_id IN ({placeholders})
                ORDER BY os.opportunity_id, os.id""",
                tuple(batch),
            ).fetchall()
            # Match the destination the existing presentation policy selects,
            # including the validated descriptive-page cache. Never replace a
            # descriptive page with a positively checked application form.
            fallbacks: dict[int, str] = {}
            observations: dict[int, list[SourceObservation]] = {}
            for row in rows:
                value = int(row[0])
                fallbacks[value] = preferred_link(row[9], row[8], row[10]) or row[8]
                observations.setdefault(value, []).append(SourceObservation(
                    observation_id=int(row[4]), source_type=row[3],
                    application_url=row[6], source_url=row[2], canonical_url=row[7],
                ))
            original_urls = {
                value: select_original_url(observations[value], fallback=fallback)
                for value, fallback in fallbacks.items()
            }
            for row in rows:
                opportunity_id, source_id, source_url, source_type, observation_id, identity_count = row[:6]
                opportunity_id = int(opportunity_id)
                if identity_count != 1:
                    continue
                key = json.dumps([source_id, source_url], separators=(",", ":"))
                record = self.records.get(key)
                # Identity is the current DB relationship, not a remembered
                # opportunity ID, a canonical URL, or the last collection.
                if (record is None or record["opportunity_id"] != opportunity_id
                        or record["source_id"] != source_id
                        or record["source_url"] != source_url):
                    continue
                check_date = _timestamp(record.get("checked_at"), self.now)
                if check_date is None:
                    continue
                checked[opportunity_id] = max(
                    check_date, checked.get(opportunity_id, check_date)
                )
                source = self.active_sources.get(source_id)
                if (source is None or source.type != source_type
                        or not _in_current_scope(source, source_url)
                        or source_url != original_urls[opportunity_id]):
                    continue
                confirm_date = _timestamp(record.get("confirmed_at"), self.now)
                if (record.get("confirmed_state") != "OPEN" or confirm_date is None
                        or confirm_date > check_date
                        or record.get("confirmed_reason") != OPEN_REASONS_BY_SOURCE.get(source_type)):
                    continue
                # Keep the last usable positive date even if a more recent
                # UNKNOWN check makes this observation unverified today.
                positive[opportunity_id] = max(
                    confirm_date, positive.get(opportunity_id, confirm_date)
                )
                if (record["state"] == "OPEN"
                        and check_date == confirm_date
                        and type(record.get("http_status")) is int
                        and record["http_status"] == 200
                        and record.get("reason") == OPEN_REASONS_BY_SOURCE.get(source_type)
                        and self.now - check_date <= TTL
                        and self.now - confirm_date <= TTL):
                    rank = official_rank(source_type)
                    candidate = (
                        rank if rank is not None else 100, int(observation_id),
                        source_url, confirm_date,
                    )
                    if opportunity_id not in opened or candidate[:2] < opened[opportunity_id][:2]:
                        opened[opportunity_id] = candidate
        result = {}
        for opportunity_id in ids:
            selected = opened.get(opportunity_id)
            response = AvailabilityResponse(
                status="OPEN_CONFIRMED" if opportunity_id in opened else "TO_VERIFY",
                last_checked_at=(checked[opportunity_id].isoformat()
                                 if opportunity_id in checked else None),
                verified_at=(selected[3].isoformat() if selected is not None
                             else positive[opportunity_id].isoformat()
                             if opportunity_id in positive else None),
            )
            result[opportunity_id] = response
        return result


def load_availability_projection(
    *, now: datetime | None = None, state_path: Path = STATE_PATH,
    registry_path: Path = REGISTRY_PATH, enabled: bool = True,
) -> AvailabilityProjection:
    """Fail closed for the green label, while keeping every GET available."""
    clock = now if now is not None else datetime.now(timezone.utc)
    try:
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("naive request clock")
        clock = clock.astimezone(timezone.utc)
        if not enabled:
            return AvailabilityProjection({}, {}, clock)
        records = _load_records(state_path)
        if not records:
            return AvailabilityProjection({}, {}, clock)
        active = {
            source.id: source for source in load_source_registry(registry_path)
            if source.enabled and source.status == "active"
        }
        return AvailabilityProjection(records, active, clock)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return AvailabilityProjection({}, {}, datetime.now(timezone.utc))
