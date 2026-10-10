"""Collect and refresh the existing local radar without editing a profile.

The domain services retain their own transaction and scoring contracts. This
module orders them, serializes refreshes and reports partial source failures.
It never migrates a database, starts an OAuth consent flow, or sends a message.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator
from urllib.parse import quote
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_MIGRATIONS = {f"{number:04d}" for number in range(1, 27)}


class RefreshConfigurationError(RuntimeError):
    """The existing deployment is not prepared for a refresh."""


class RefreshAlreadyRunning(RuntimeError):
    """Another refresh already holds the lock for this SQLite file."""


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--profile-id", type=_positive_integer)
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--apply", action="store_true", help="Collect and persist derived results")
    parser.add_argument("--check", action="store_true", help="Read configuration and schema only")
    parser.add_argument("--skip-collection", action="store_true", help="Refresh the existing corpus only")
    parser.add_argument("--watch", action="store_true", help="Run immediately, then at the given interval")
    parser.add_argument("--interval-minutes", type=_positive_integer, default=60)
    parser.add_argument("--timezone", default=os.environ.get("OPPORTUNITY_RADAR_TIMEZONE", "Europe/Paris"))
    parser.add_argument("--log-dir", type=Path, default=PROJECT_ROOT / "data" / "runtime")
    args = parser.parse_args()
    if args.check and (args.apply or args.watch):
        parser.error("--check cannot be combined with --apply or --watch")
    if not args.check and not args.apply:
        parser.error("a refresh requires --apply; use --check for a read-only preflight")
    return args


def _configuration(args: argparse.Namespace):
    from dataclasses import replace
    from services.collector.config import DatabaseBackend, load_settings

    settings = load_settings()
    if settings.database_backend is not DatabaseBackend.SQLITE:
        raise RefreshConfigurationError("automatic refresh requires the existing local SQLite deployment")
    database = (args.database or settings.sqlite_database_path).resolve()
    if not database.is_file():
        raise RefreshConfigurationError("the configured SQLite file does not exist; no database was created")
    raw = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID", "")
    try:
        profile_id = args.profile_id or int(raw)
    except ValueError as error:
        raise RefreshConfigurationError("configure OPPORTUNITY_RADAR_PROFILE_ID for the existing profile") from error
    if profile_id <= 0:
        raise RefreshConfigurationError("profile id must be positive")
    if args.profile_id is None and raw.strip() != str(profile_id):
        raise RefreshConfigurationError("profile id must use the same canonical integer as the API")
    zone = ZoneInfo(args.timezone)
    return replace(settings, sqlite_database_path=database), database, profile_id, zone


def _connect(database: Path, *, readonly: bool = False) -> sqlite3.Connection:
    mode = "ro" if readonly else "rw"
    connection = sqlite3.connect(f"file:{quote(str(database))}?mode={mode}", uri=True, timeout=15)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def preflight(database: Path, profile_id: int) -> int:
    """Validate without creating a DB or applying any migration; return its owner."""
    connection = _connect(database, readonly=True)
    try:
        applied = {str(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}
        if not REQUIRED_MIGRATIONS.issubset(applied):
            raise RefreshConfigurationError("database migrations 0001 through 0026 must already be applied")
        owner = connection.execute(
            "SELECT p.user_id FROM profiles p JOIN users u ON u.id=p.user_id WHERE p.id=?",
            (profile_id,),
        ).fetchone()
        if owner is None:
            raise RefreshConfigurationError("the configured profile does not exist")
        return int(owner[0])
    finally:
        connection.close()


@contextmanager
def refresh_lock(database: Path) -> Iterator[None]:
    """One OS lock per database, released even after a crash; no stale PID lock."""
    lock_path = database.with_name(database.name + ".refresh.lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RefreshAlreadyRunning("a refresh is already running") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _gmail_client(configuration):
    """Read-only credentials only; refresh in memory and never request consent."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp
    from httplib2 import Http
    from services.collector.gmail.client import GMAIL_SCOPES, GmailAuthenticationError, GmailClient

    if not configuration.token_path.is_file():
        raise GmailAuthenticationError("existing Gmail authorization is unavailable for unattended collection")
    credentials = Credentials.from_authorized_user_file(str(configuration.token_path))
    required = frozenset(GMAIL_SCOPES)
    views = [frozenset(value) for name in ("scopes", "granted_scopes")
             if (value := getattr(credentials, name, None)) is not None]
    if not views or any(view != required for view in views):
        raise GmailAuthenticationError("unattended collection requires exactly gmail.readonly")
    if not credentials.valid:
        if not credentials.expired or not credentials.refresh_token:
            raise GmailAuthenticationError("Gmail authorization must be renewed manually")
        request = Request()
        def bounded_request(*values, **options):
            options["timeout"] = 30
            return request(*values, **options)
        credentials.refresh(bounded_request)
    if not credentials.valid:
        raise GmailAuthenticationError("Gmail authorization is unavailable")
    views = [frozenset(value) for name in ("scopes", "granted_scopes")
             if (value := getattr(credentials, name, None)) is not None]
    if not views or any(view != required for view in views):
        raise GmailAuthenticationError("the refreshed authorization is not exactly gmail.readonly")
    http = AuthorizedHttp(credentials, http=Http(timeout=30))
    return GmailClient(build("gmail", "v1", http=http, cache_discovery=False))


def _collector(source):
    from services.collector.collectors.factory import collector_for
    from services.collector.collectors.linkedin_job_alert import LinkedInJobAlertCollector
    if source.type == "gmail_linkedin_alert":
        return LinkedInJobAlertCollector(source, gmail_client_factory=_gmail_client)
    return collector_for(source)


def _logger(log_dir: Path) -> logging.Logger:
    from services.collector.logging_config import JsonFormatter, get_logger
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = get_logger("services.collector.operations")
    service_logger = logging.getLogger("services.collector")
    if not any(handler.name == "radar_refresh_file" for handler in service_logger.handlers):
        handler = RotatingFileHandler(log_dir / "refresh.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        handler.name = "radar_refresh_file"
        handler.setFormatter(JsonFormatter())
        service_logger.addHandler(handler)
    return logger


def _summary(result) -> dict[str, object]:
    fields = ("total", "processed", "created", "replaced", "unchanged", "selected_count",
              "assessment_count", "included_count", "excluded_count", "run_id", "state", "status")
    values = {}
    for name in fields:
        if hasattr(result, name):
            value = getattr(result, name)
            values[name] = value.value if isinstance(value, Enum) else value
    issues = getattr(result, "issues", ())
    if issues:
        values["issue_codes"] = sorted({str(getattr(issue.code, "value", issue.code)) for issue in issues})
    return values


def _publish_status(log_dir: Path, status: dict[str, object]) -> None:
    temporary = log_dir / "refresh-status.tmp"
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(log_dir / "refresh-status.json")


@contextmanager
def _cycle_status(log_dir: Path, status: dict[str, object]) -> Iterator[None]:
    _publish_status(log_dir, status)
    try:
        yield
    except Exception as error:
        status.update(state="FAILED", error_type=type(error).__name__,
                      finished_at=datetime.now(timezone.utc).isoformat())
        _publish_status(log_dir, status)
        raise


def run_refresh(settings, database: Path, profile_id: int, zone: ZoneInfo, args, logger) -> dict:
    from services.collector.agent import RadarAgent
    from services.collector.sources import load_source_registry
    from services.operations.availability import verify_catalog_availability
    from services.collector.database.source_runs import finalize_configured_source_run
    from services.collector.extractors.opportunity_constraints.service import synchronize_opportunity_constraints
    from services.collector.extractors.opportunity_constraints.requirements.service import synchronize_opportunity_requirements
    from services.collector.matching.sync import sync_matching
    from services.eligibility.service import synchronize_eligibility
    from services.geography.service import synchronize_location_resolutions
    from services.portfolio.sync import sync_portfolio
    from services.priority.sync import sync_priority
    from services.recommendation.sync import sync_recommendations

    status = {"started_at": datetime.now(timezone.utc).isoformat(), "state": "RUNNING", "stages": {}, "sources": []}
    stages = status["stages"]
    with refresh_lock(database), _cycle_status(args.log_dir, status):
        user_id = preflight(database, profile_id)
        _publish_status(args.log_dir, status)
        partial = False
        if not args.skip_collection:
            finalization_errors = []

            def observed_finalizer(settings, attempt, *, status, metrics, error=None):
                try:
                    return finalize_configured_source_run(
                        settings, attempt, status=status, metrics=metrics, error=error
                    )
                except Exception as failure:
                    # RadarAgent intentionally logs and suppresses a finalizer
                    # failure after collection has committed. Preserve that
                    # audit uncertainty in the overall refresh outcome.
                    finalization_errors.append({
                        "source_id": getattr(attempt, "source_id", None),
                        "error_type": type(failure).__name__,
                    })
                    raise

            collection = RadarAgent(settings_loader=lambda: settings, collector_factory=_collector,
                                    source_ids=args.sources, run_finalizer=observed_finalizer).run_once()
            status["sources"] = [
                {"source_id": item.source_id, "success": item.success, "collected": item.collected,
                 "created": item.created, "updated": item.updated, "error_type": item.error_type}
                for item in collection.source_results
            ]
            status["source_finalization_errors"] = finalization_errors
            partial = collection.sources_failed > 0 or bool(finalization_errors)
            stages["collection"] = {"state": "PARTIAL" if partial else "READY",
                                    "sources_succeeded": collection.sources_succeeded,
                                    "sources_failed": collection.sources_failed,
                                    "unfinalized_runs": len(finalization_errors)}
            stages["qualification"] = {"state": "READY" if collection.qualification.success else "FAILED"}
            if not collection.qualification.success:
                status.update(state="FAILED", finished_at=datetime.now(timezone.utc).isoformat())
                _publish_status(args.log_dir, status)
                return status
        else:
            # Qualification must still be refreshed before its derived projections.
            from services.collector.qualification.persistence import persist_configured_qualifications
            qualified = persist_configured_qualifications(settings)
            stages["qualification"] = {"state": "READY", **_summary(qualified)}

        # Availability precedes every derived result so expired opportunities
        # cannot enter a fresh matching/recommendation/portfolio projection.
        try:
            sources = load_source_registry()
            if args.sources is not None:
                sources = [source for source in sources if source.id in args.sources]
            stages["availability"] = verify_catalog_availability(settings, sources, args.log_dir)
            partial |= stages["availability"]["state"] != "READY"
            logger.info("Availability checks completed.", extra={
                "event": "availability_completed", "stage_summary": stages["availability"],
            })
        except Exception as error:
            stages["availability"] = {"state": "FAILED", "error_type": type(error).__name__}
            partial = True
            logger.error("Availability checks failed; existing records were preserved.", extra={
                "event": "availability_failed", "error_type": type(error).__name__,
            })
        _publish_status(args.log_dir, status)

        evaluation_date = datetime.now(zone).date()
        steps = (
            ("constraints", (), lambda c: synchronize_opportunity_constraints(c)),
            ("requirements", ("constraints",), lambda c: synchronize_opportunity_requirements(c)),
            ("geography", ("constraints",), lambda c: synchronize_location_resolutions(c)),
            ("eligibility", ("requirements",), lambda c: synchronize_eligibility(c, user_id, profile_id)),
            ("matching", ("requirements",), lambda c: sync_matching(c, profile_id)),
            ("recommendation", ("matching", "geography", "eligibility"), lambda c: sync_recommendations(c, profile_id)),
            ("priority", ("matching", "eligibility"), lambda c: sync_priority(c, profile_id, evaluation_date)),
            ("portfolio", ("priority",), lambda c: sync_portfolio(c, profile_id)),
        )
        for name, dependencies, operation in steps:
            # EMPTY Matching is a completed calculation: Recommendation must be
            # called so it can publish its own honest INCOMPLETE state.
            if any(stages[dependency]["state"] not in ("READY", "EMPTY") for dependency in dependencies):
                stages[name] = {"state": "SKIPPED", "reason": "UPSTREAM_NOT_READY"}
                partial = True
                continue
            connection = _connect(database)
            try:
                logger.info("Refresh stage started.", extra={"event": "refresh_stage_started", "stage": name})
                result = operation(connection)
                values = _summary(result)
                state = str(values.pop("state", values.pop("status", "READY")))
                stages[name] = {"state": state, **values}
                partial |= state != "READY"
                logger.info("Refresh stage completed.", extra={"event": "refresh_stage_completed", "stage": name, "stage_summary": stages[name]})
            except Exception as error:
                if connection.in_transaction:
                    connection.rollback()
                stages[name] = {"state": "FAILED", "error_type": type(error).__name__}
                partial = True
                logger.error("Refresh stage failed.", extra={"event": "refresh_stage_failed", "stage": name, "error_type": type(error).__name__})
            finally:
                connection.close()
            _publish_status(args.log_dir, status)
        status.update(state="PARTIAL" if partial else "READY", finished_at=datetime.now(timezone.utc).isoformat())
        _publish_status(args.log_dir, status)
        logger.info("Opportunity refresh completed.", extra={"event": "refresh_completed", "state": status["state"]})
        return status


def main() -> int:
    args = _arguments()
    os.chdir(PROJECT_ROOT)
    logger = _logger(args.log_dir)
    try:
        settings, database, profile_id, zone = _configuration(args)
        if args.check:
            preflight(database, profile_id)
            print(json.dumps({"state": "CONFIGURATION_READY", "profile_id": profile_id, "database_exists": True}))
            return 0
        while True:
            try:
                status = run_refresh(settings, database, profile_id, zone, args, logger)
                code = {"READY": 0, "PARTIAL": 2}.get(status["state"], 1)
            except RefreshAlreadyRunning:
                logger.info("Refresh skipped because another refresh is running.", extra={"event": "refresh_already_running"})
                code = 0
            except Exception as error:
                logger.error("Refresh could not complete.", extra={"event": "refresh_failed", "error_type": type(error).__name__})
                code = 1
            if not args.watch:
                return code
            time.sleep(args.interval_minutes * 60)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        logger.error("Refresh configuration is unavailable.", extra={"event": "refresh_configuration_failed", "error_type": type(error).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
