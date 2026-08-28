"""Deterministic, read-only Source Health derived from persisted run history.

Nothing here writes, migrates, or invents data. Every value is read from
``sources`` and ``source_runs`` as they already stand, and an unknown value
stays unknown: a source that never ran has no run, and a metric no run measured
stays ``None`` rather than becoming a zero that would read as an observation.

The truth about a source's state remains the real status of its most recent
``source_runs`` row — ``RUNNING``, ``SUCCESS`` or ``FAILED``, or nothing at all.
No second health taxonomy is derived on top of it. The one derived signal is the
repeated-zero anomaly, exposed separately so it can be shown without pretending
the database has a new state machine.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from services.collector.database.connection import DatabaseConnection
from services.collector.models.source_run import SUCCESS
from services.collector.sources import SourceConfig

#: Number of consecutive zero-item successful runs that constitutes an anomaly.
ZERO_RESULT_STREAK_THRESHOLD = 3

#: Stable identifier of the only anomaly this slice detects.
ZERO_RESULT_ANOMALY_CODE = "ZERO_RESULTS_STREAK"


def zero_result_anomaly_message(streak: int) -> str:
    """Build the deterministic anomaly text for an observed zero-result streak.

    The wording is a fixed template filled with the observed count. No language
    model, no heuristic phrasing, no severity invented beyond the count itself.
    """
    return (
        f"0 résultat trouvé lors de {streak} exécutions réussies consécutives. "
        "Défaillance possible du collecteur ou du parseur."
    )


@dataclass(frozen=True)
class SourceHealth:
    """One source as currently observable, without any persisted derivation.

    ``last_run_at`` is the ``started_at`` of the most recent run, so the
    timestamp always describes the very run whose status and metrics are shown
    here. It is ``None`` when the source has never run: ``sources.last_run_at``
    is deliberately not substituted, because a stamp with no run behind it would
    claim an execution this model cannot show.
    """

    source_id: str
    enabled: bool
    last_run_at: str | None
    status: str | None
    items_found: int | None
    new_items: int | None
    relevant_items: int | None
    error_type: str | None
    error_message: str | None
    zero_result_streak: int
    anomaly_code: str | None
    anomaly_message: str | None

    @property
    def has_run(self) -> bool:
        return self.status is not None

    @property
    def has_anomaly(self) -> bool:
        return self.anomaly_code is not None


@dataclass(frozen=True)
class _Run:
    """The subset of one persisted run this read model needs."""

    started_at: str
    status: str
    items_found: int | None
    new_items: int | None
    relevant_items: int | None
    error_type: str | None
    error_message: str | None


def _is_zero_result_run(run: _Run) -> bool:
    """Report whether one run is a completed success that found exactly zero.

    Only a ``SUCCESS`` carrying an explicit ``items_found`` of 0 qualifies. A
    ``RUNNING`` run has not finished, a ``FAILED`` run did not succeed, and an
    ``items_found`` of ``None`` means the run did not know — none of the three
    is evidence that a source returned nothing.
    """
    return (
        run.status == SUCCESS
        and run.items_found is not None
        and run.items_found == 0
    )


def _zero_result_streak(runs: Sequence[_Run]) -> int:
    """Count zero-result runs from the newest backwards.

    "Consecutive" is read literally against the run history: the count stops at
    the first run that is not a zero-result success, whether that run succeeded
    with items, failed, is still running, or did not measure ``items_found``.
    """
    streak = 0
    for run in runs:
        if not _is_zero_result_run(run):
            break
        streak += 1
    return streak


def _health_for(source_id: str, enabled: bool, runs: Sequence[_Run]) -> SourceHealth:
    latest = runs[0] if runs else None
    streak = _zero_result_streak(runs)
    anomalous = streak >= ZERO_RESULT_STREAK_THRESHOLD
    return SourceHealth(
        source_id=source_id,
        enabled=enabled,
        last_run_at=latest.started_at if latest else None,
        status=latest.status if latest else None,
        items_found=latest.items_found if latest else None,
        new_items=latest.new_items if latest else None,
        relevant_items=latest.relevant_items if latest else None,
        error_type=latest.error_type if latest else None,
        error_message=latest.error_message if latest else None,
        zero_result_streak=streak,
        anomaly_code=ZERO_RESULT_ANOMALY_CODE if anomalous else None,
        anomaly_message=zero_result_anomaly_message(streak) if anomalous else None,
    )


def read_source_health(
    connection: DatabaseConnection,
    *,
    configured_sources: Iterable[SourceConfig] = (),
) -> list[SourceHealth]:
    """Derive one health entry per known source from its latest run, read-only.

    A source is known when the validated registry configures it or when the
    database already holds it. The registry wins on ``enabled`` for a source it
    configures, because a persisted row is registered once at a source's first
    run and is never rewritten afterwards, so the configured flag is the fresher
    truth. Nothing is inserted for a source the registry knows and the database
    does not: it is simply reported as never run.

    Entries are ordered by ``source_id`` so two reads of the same database
    always produce the same list.
    """
    persisted = connection.execute("SELECT id, enabled FROM sources").fetchall()
    run_rows = connection.execute(
        """
        SELECT source_id, started_at, status, items_found, new_items,
               relevant_items, error_type, error_message
        FROM source_runs
        ORDER BY source_id ASC, started_at DESC, id DESC
        """
    ).fetchall()

    enabled_by_source: dict[str, bool] = {
        str(row[0]): bool(row[1]) for row in persisted
    }
    for source in configured_sources:
        enabled_by_source[source.id] = source.enabled

    runs_by_source: dict[str, list[_Run]] = {}
    for row in run_rows:
        runs_by_source.setdefault(str(row[0]), []).append(
            _Run(
                started_at=str(row[1]),
                status=str(row[2]),
                items_found=_optional_int(row[3]),
                new_items=_optional_int(row[4]),
                relevant_items=_optional_int(row[5]),
                error_type=_optional_text(row[6]),
                error_message=_optional_text(row[7]),
            )
        )

    # A run can only exist for a persisted source, but a source seen only in the
    # run history is still reported rather than silently dropped.
    for source_id in runs_by_source:
        enabled_by_source.setdefault(source_id, False)

    return [
        _health_for(
            source_id, enabled_by_source[source_id], runs_by_source.get(source_id, ())
        )
        for source_id in sorted(enabled_by_source)
    ]


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[arg-type]


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
