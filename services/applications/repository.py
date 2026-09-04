"""SQLite access for tracked candidatures and their history.

Every function here runs inside a transaction its caller opened. That is not
an accident of style: a status and the event recording it are one fact, and
the only place that fact can be made indivisible is a single SQLite
transaction. So this module never opens, commits or rolls back anything — the
service does — and it never decides whether a change is allowed either. It
reads rows, writes rows, and refuses a database that cannot hold them.

Nothing here logs the content of `notes` or `next_action`. Those are the
user's private words about a job they may not have told anyone they applied
for; ids and statuses describe every operation well enough.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence

from services.applications.models import (
    ActorType,
    ApplicationEventRecord,
    ApplicationEventType,
    ApplicationRecord,
    ApplicationStatus,
    REQUIRED_MIGRATION_VERSION,
    ApplicationError,
)


_APPLICATION_COLUMNS = (
    "id, profile_id, opportunity_id, status, submitted_at, last_status_change,"
    " next_action, followup_date, notes, created_at, updated_at"
)

_EVENT_COLUMNS = (
    "id, application_id, event_type, from_status, to_status, actor_type, occurred_at"
)

#: The tables, the append-only guards and the constraint tying an event to the
#: state it describes. All of them come from 0023, and a database missing any
#: one of them is not one this domain may write to.
_REQUIRED_OBJECTS: tuple[tuple[str, str], ...] = (
    ("table", "applications"),
    ("table", "application_events"),
    ("trigger", "application_events_are_append_only_on_update"),
    ("trigger", "application_events_are_append_only_on_delete"),
    ("trigger", "application_events_agree_with_the_application"),
)


def preflight(connection: sqlite3.Connection) -> None:
    """Refuse a database that has not been migrated through 0023.

    The recorded migration version and the objects themselves are both
    checked, so a half-applied or hand-edited schema fails with a sentence
    here rather than with "no such table" from inside a transaction that has
    already changed something.
    """
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (REQUIRED_MIGRATION_VERSION,),
        ).fetchone()
        present = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table','trigger')"
            )
        }
    except sqlite3.Error as error:
        raise ApplicationError(
            "application tracking schema is not ready;"
            f" migrate through {REQUIRED_MIGRATION_VERSION} first"
        ) from error
    if migrated is None or not set(_REQUIRED_OBJECTS) <= present:
        raise ApplicationError(
            "application tracking schema is not ready;"
            f" migrate through {REQUIRED_MIGRATION_VERSION} first"
        )


def current_timestamp(connection: sqlite3.Connection) -> str:
    """The instant to stamp on this change, in the schema's own format.

    Read from SQLite rather than from Python so that a value written by this
    module and one written by a column default are the same kind of thing,
    down to the format the CHECK constraints validate.
    """
    row = connection.execute(
        "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')"
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise ApplicationError("database did not return a usable timestamp")
    return row[0]


def _record(row: Sequence[object]) -> ApplicationRecord:
    return ApplicationRecord(
        id=int(row[0]),  # type: ignore[arg-type]
        profile_id=int(row[1]),  # type: ignore[arg-type]
        opportunity_id=int(row[2]),  # type: ignore[arg-type]
        status=ApplicationStatus(row[3]),
        submitted_at=None if row[4] is None else str(row[4]),
        last_status_change=str(row[5]),
        next_action=None if row[6] is None else str(row[6]),
        followup_date=None if row[7] is None else str(row[7]),
        notes=None if row[8] is None else str(row[8]),
        created_at=str(row[9]),
        updated_at=str(row[10]),
    )


def _event(row: Sequence[object]) -> ApplicationEventRecord:
    return ApplicationEventRecord(
        id=int(row[0]),  # type: ignore[arg-type]
        application_id=int(row[1]),  # type: ignore[arg-type]
        event_type=ApplicationEventType(row[2]),
        from_status=None if row[3] is None else ApplicationStatus(row[3]),
        to_status=None if row[4] is None else ApplicationStatus(row[4]),
        actor_type=ActorType(row[5]),
        occurred_at=str(row[6]),
    )


def profile_exists(connection: sqlite3.Connection, profile_id: int) -> bool:
    """Whether this profile is one this database knows."""
    return (
        connection.execute(
            "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        is not None
    )


def opportunity_exists(connection: sqlite3.Connection, opportunity_id: int) -> bool:
    """Whether the opportunity a candidature would track really exists.

    Checked before anything is written, and never fixed up: a candidature for
    an opportunity this database has never seen is a bug or a stale page, not
    an invitation to invent the opportunity.
    """
    return (
        connection.execute(
            "SELECT 1 FROM opportunities WHERE id = ?", (opportunity_id,)
        ).fetchone()
        is not None
    )


def read_application(
    connection: sqlite3.Connection, *, profile_id: int, application_id: int
) -> ApplicationRecord | None:
    """One candidature of this profile, or None.

    The profile is part of the lookup rather than a check applied afterwards,
    so another profile's candidature is indistinguishable from one that does
    not exist — which is exactly what a caller should be told.
    """
    row = connection.execute(
        f"SELECT {_APPLICATION_COLUMNS} FROM applications"
        " WHERE id = ? AND profile_id = ?",
        (application_id, profile_id),
    ).fetchone()
    return None if row is None else _record(row)


def read_application_for_opportunity(
    connection: sqlite3.Connection, *, profile_id: int, opportunity_id: int
) -> ApplicationRecord | None:
    """This profile's candidature for this opportunity, or None."""
    row = connection.execute(
        f"SELECT {_APPLICATION_COLUMNS} FROM applications"
        " WHERE profile_id = ? AND opportunity_id = ?",
        (profile_id, opportunity_id),
    ).fetchone()
    return None if row is None else _record(row)


def list_applications(
    connection: sqlite3.Connection, *, profile_id: int
) -> list[ApplicationRecord]:
    """Every candidature of this profile, most recently moved first."""
    rows = connection.execute(
        f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE profile_id = ?"
        " ORDER BY last_status_change DESC, id DESC",
        (profile_id,),
    ).fetchall()
    return [_record(row) for row in rows]


def read_events(
    connection: sqlite3.Connection, application_ids: Iterable[int]
) -> dict[int, list[ApplicationEventRecord]]:
    """The history of each named candidature, oldest first.

    Ordered by id, not by ``occurred_at``: two events written in the same
    second are still ordered, and the order they were written in is the order
    they happened in.
    """
    ids = list(application_ids)
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT {_EVENT_COLUMNS} FROM application_events"
        f" WHERE application_id IN ({placeholders})"
        " ORDER BY application_id, id",
        tuple(ids),
    ).fetchall()
    history: dict[int, list[ApplicationEventRecord]] = {
        application_id: [] for application_id in ids
    }
    for row in rows:
        history.setdefault(int(row[1]), []).append(_event(row))
    return history


def insert_application(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    opportunity_id: int,
    status: ApplicationStatus,
    submitted_at: str | None,
    now: str,
) -> int:
    """Create one candidature. The caller's transaction adds its first event."""
    row = connection.execute(
        """INSERT INTO applications
           (profile_id, opportunity_id, status, submitted_at, last_status_change,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (
            profile_id,
            opportunity_id,
            status.value,
            submitted_at,
            now,
            now,
            now,
        ),
    ).fetchone()
    if row is None:
        raise ApplicationError("application insert returned no row")
    return int(row[0])


def update_status(
    connection: sqlite3.Connection,
    *,
    application_id: int,
    status: ApplicationStatus,
    submitted_at: str | None,
    now: str,
) -> None:
    """Move one candidature to a new status.

    ``submitted_at`` is passed whole rather than merged, because the caller is
    the only thing that knows whether this movement is the first submission or
    one of the many that follow it — and the difference is precisely that the
    second must not touch it.
    """
    connection.execute(
        """UPDATE applications
           SET status = ?, submitted_at = ?, last_status_change = ?, updated_at = ?
           WHERE id = ?""",
        (status.value, submitted_at, now, now, application_id),
    )


def update_tracking(
    connection: sqlite3.Connection,
    *,
    application_id: int,
    next_action: str | None,
    followup_date: str | None,
    notes: str | None,
    now: str,
) -> None:
    """Write the manual tracking fields, and only those.

    ``last_status_change`` is untouched on purpose: writing a note about a
    candidature is not the candidature moving, and a tracker that pretended
    otherwise would make "last movement" meaningless.
    """
    connection.execute(
        """UPDATE applications
           SET next_action = ?, followup_date = ?, notes = ?, updated_at = ?
           WHERE id = ?""",
        (next_action, followup_date, notes, now, application_id),
    )


def insert_event(
    connection: sqlite3.Connection,
    *,
    application_id: int,
    event_type: ApplicationEventType,
    from_status: ApplicationStatus | None,
    to_status: ApplicationStatus | None,
    actor_type: ActorType = ActorType.USER,
    occurred_at: str,
) -> int:
    """Append one event. It can never be amended or removed afterwards."""
    row = connection.execute(
        """INSERT INTO application_events
           (application_id, event_type, from_status, to_status, actor_type, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
        (
            application_id,
            event_type.value,
            None if from_status is None else from_status.value,
            None if to_status is None else to_status.value,
            actor_type.value,
            occurred_at,
        ),
    ).fetchone()
    if row is None:
        raise ApplicationError("application event insert returned no row")
    return int(row[0])
