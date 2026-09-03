"""The local calendar day a digest belongs to, decided explicitly.

A daily digest is only "daily" relative to somewhere. Left to the process
environment, the boundary would be whatever ``TZ`` the machine running the
materialization happened to carry — so the same Portfolio could produce two
digests in one of the user's days, or none, purely because a job moved hosts.

So the timezone is an argument, never an ambient value: an IANA name resolved
through :mod:`zoneinfo`, required, and validated before any database work
starts. The instant is an argument too, defaulting to now in UTC, which is
what lets a test place a materialization one second either side of midnight
without touching a clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import GmailDigestError

#: The maximum length migration 0022 accepts for a stored timezone name.
MAX_TIMEZONE_NAME_LENGTH = 64


@dataclass(frozen=True)
class LocalDay:
    """One resolved local calendar day and the timezone that decided it."""

    date: str
    timezone: str


def resolve_timezone(timezone_name: object) -> ZoneInfo:
    """Return the IANA zone this name denotes, or refuse to guess one.

    A missing, blank, or unknown name is an error rather than a fallback to
    UTC or to the machine's zone: silently picking a boundary is exactly the
    failure this module exists to prevent.
    """
    if not isinstance(timezone_name, str):
        raise GmailDigestError("timezone must be an explicit IANA timezone name")
    if (
        not timezone_name.strip()
        or timezone_name != timezone_name.strip()
        or len(timezone_name) > MAX_TIMEZONE_NAME_LENGTH
    ):
        raise GmailDigestError("timezone must be an explicit IANA timezone name")
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise GmailDigestError(f"unknown IANA timezone {timezone_name!r}") from error


def resolve_local_day(timezone_name: str, now: datetime | None = None) -> LocalDay:
    """Return the local day ``now`` falls in, inside the named timezone."""
    zone = resolve_timezone(timezone_name)
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime):
        raise GmailDigestError("now must be a datetime")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        # A naive instant carries no boundary of its own, so reading one as
        # UTC — or as local time — would reintroduce the ambient guess.
        raise GmailDigestError("now must be timezone-aware")
    return LocalDay(moment.astimezone(zone).date().isoformat(), timezone_name)
