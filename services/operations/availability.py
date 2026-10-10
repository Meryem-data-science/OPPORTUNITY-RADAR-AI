"""Verify public offer availability without deleting opportunities or history.

A successful HTTP response alone is not evidence that applications are open.
Failed, refused and partial reads remain unknown. Confirmed closures survive
old alert emails; only a new positive public-source observation reopens them.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from services.collector.config import DatabaseBackend, Settings, load_settings
from services.collector.parsers.stage_ma import (
    STATE_EXPIRED, STATE_UNPUBLISHED, publication_state,
)
from services.collector.parsers.stagiaires import (
    COLLECTOR_USER_AGENT, ROBOTS_ABSENT, ROBOTS_OBEY,
    classify_robots_response, job_posting, parse_robots_txt, robots_allows,
)
from services.collector.sources import SourceConfig, load_source_registry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = "availability-state.json"
EVENT_FILE = "availability-events.jsonl"
TTL = timedelta(hours=6)
MAX_PAGE_CHECKS = 50
TIMEOUT_SECONDS = 10.0
DELAY_SECONDS = 1.0
MAX_RESPONSE_BYTES = 20_000_000
_REDIRECTS = {301, 302, 303, 307, 308}
_GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_PAGE_HOSTS = {
    "linkedin": {"linkedin.com", "www.linkedin.com"},
    "stage": {"stage.ma", "www.stage.ma"},
    "stagiaires": {"stagiaires.ma", "www.stagiaires.ma"},
}
_CLOSED_WORDS = (
    "no longer accepting applications", "candidatures ne sont plus acceptées",
    "candidatures ne sont plus acceptees",
)


class AvailabilityError(RuntimeError):
    """The local availability state could not be safely recorded."""


@dataclass(frozen=True)
class Observation:
    opportunity_id: int
    source_id: str
    source_type: str
    source_url: str

    @property
    def key(self) -> str:
        return json.dumps([self.source_id, self.source_url], separators=(",", ":"))


@dataclass(frozen=True)
class Evidence:
    state: str
    reason: str
    http_status: int | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect(settings: Settings) -> sqlite3.Connection:
    if settings.database_backend is not DatabaseBackend.SQLITE or settings.sqlite_database_path is None:
        raise AvailabilityError("Availability verification requires the existing SQLite database")
    path = settings.sqlite_database_path.resolve()
    if not path.is_file():
        raise AvailabilityError("The SQLite database does not exist; no database was created")
    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=15)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def _observations(connection: sqlite3.Connection) -> list[Observation]:
    return [Observation(int(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in connection.execute(
                """SELECT os.opportunity_id, os.source_id, s.type, os.source_url
                   FROM opportunity_sources os JOIN opportunities o ON o.id=os.opportunity_id
                   JOIN sources s ON s.id=os.source_id
                   WHERE o.status IN ('visible','expired')
                   ORDER BY os.opportunity_id, os.id"""
            )]


def _load_state(log_dir: Path) -> dict[str, Any]:
    path = log_dir / STATE_FILE
    if not path.exists():
        return {"version": 1, "observations": {}}
    try:
        if path.stat().st_size > 20_000_000:
            raise ValueError("state exceeds its size bound")
        state = json.loads(path.read_text(encoding="utf-8"))
        records = state.get("observations")
        if state.get("version") != 1 or not isinstance(records, dict):
            raise ValueError("invalid state shape")
        for key, record in records.items():
            if (not isinstance(record, dict) or record.get("state") not in {"OPEN", "CLOSED", "UNKNOWN"}
                    or record.get("confirmed_state") not in {None, "OPEN", "CLOSED"}
                    or type(record.get("opportunity_id")) is not int
                    or not isinstance(record.get("source_id"), str)
                    or not isinstance(record.get("source_url"), str)
                    or key != json.dumps([record["source_id"], record["source_url"]], separators=(",", ":"))):
                raise ValueError("invalid observation state")
        return state
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise AvailabilityError("Availability state is unreadable; existing closures were preserved") from error


def _write_state(log_dir: Path, state: dict[str, Any]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=log_dir,
                                         prefix="availability-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, log_dir / STATE_FILE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _record(log_dir: Path, state: dict[str, Any], observation: Observation,
            evidence: Evidence, checked_at: str) -> None:
    previous = state["observations"].get(observation.key, {})
    if previous.get("opportunity_id") != observation.opportunity_id:
        previous = {}
    record = {
        "opportunity_id": observation.opportunity_id, "source_id": observation.source_id,
        "source_url": observation.source_url, "checked_at": checked_at,
        "state": evidence.state, "reason": evidence.reason, "http_status": evidence.http_status,
        "confirmed_state": previous.get("confirmed_state"),
        "confirmed_at": previous.get("confirmed_at"),
        "confirmed_reason": previous.get("confirmed_reason"),
    }
    if evidence.state in {"OPEN", "CLOSED"}:
        record.update(confirmed_state=evidence.state, confirmed_at=checked_at,
                      confirmed_reason=evidence.reason)
    log_dir.mkdir(parents=True, exist_ok=True)
    # The audit contains public identities and verdicts only, never page bodies.
    with (log_dir / EVENT_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    state["observations"][observation.key] = record


def _record_for(state: dict[str, Any], observation: Observation) -> dict[str, Any]:
    record = state["observations"].get(observation.key, {})
    return record if record.get("opportunity_id") == observation.opportunity_id else {}


def _due(record: dict[str, Any], now: datetime) -> bool:
    try:
        checked = datetime.fromisoformat(record["checked_at"])
        return checked.tzinfo is None or checked > now or now - checked >= TTL
    except (KeyError, TypeError, ValueError):
        return True


def _safe_url(url: str, allowed_hosts: set[str]) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname in allowed_hosts
                and parts.netloc == parts.hostname and not parts.fragment
                and not parts.username and not parts.password)
    except ValueError:
        return False


def _board_id(url: str, token: str) -> int | None:
    if not _safe_url(url, _GREENHOUSE_HOSTS):
        return None
    parts = urlsplit(url)
    match = re.fullmatch(r"/" + re.escape(token) + r"/jobs/([1-9][0-9]*)/?", parts.path)
    return int(match[1]) if match is not None and not parts.query else None


def _page_identity(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(url)
        if parts.query:
            return None
        patterns = {
            "linkedin": r"/jobs/view/(?:[^/]*-)?([1-9][0-9]*)/?",
            "stage": r"/offres-stage/([1-9][0-9]*)(?:-[^/]*)?/?",
            "stagiaires": r"/stage-emploi-maroc/([1-9][0-9]*)(?:-[^/]*)?/?",
        }
        for kind, hosts in _PAGE_HOSTS.items():
            match = re.fullmatch(patterns[kind], parts.path)
            if _safe_url(url, hosts) and match is not None:
                return kind, match[1]
    except (TypeError, ValueError):
        pass
    return None


class _PublicReader:
    """Bounded GETs; every redirect retains its host family and offer identity."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.last_request_at = 0.0
        self.robots: dict[str, tuple[Any, ...] | None] = {}

    def get(self, url: str) -> httpx.Response:
        wait = DELAY_SECONDS - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        self.client.cookies.clear()
        self.last_request_at = time.monotonic()
        # Stream before the size check so an unexpectedly large response cannot
        # consume arbitrary memory. Cookies and browser credentials are unused.
        with self.client.stream("GET", url, timeout=TIMEOUT_SECONDS,
                                follow_redirects=False) as response:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("Public response exceeded its size bound")
                chunks.append(chunk)
            # iter_bytes has already decoded transport compression. Retaining
            # Content-Encoding would make the new Response decode it twice.
            headers = {key: value for key, value in response.headers.items()
                       if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
            return httpx.Response(response.status_code, headers=headers,
                                  content=b"".join(chunks), request=response.request)

    def permits(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"https://{parts.hostname}"
        if origin not in self.robots:
            robots_url = origin + "/robots.txt"
            try:
                response = self.get(robots_url)
                disposition = classify_robots_response(response.status_code, robots_url, response.text)
                if disposition == ROBOTS_OBEY:
                    self.robots[origin] = parse_robots_txt(response.text)
                elif disposition == ROBOTS_ABSENT:
                    self.robots[origin] = ()
                else:
                    self.robots[origin] = None
            except (httpx.HTTPError, ValueError):
                self.robots[origin] = None
        rules = self.robots[origin]
        return rules is not None and robots_allows(rules, url)

    def detail(self, url: str) -> tuple[httpx.Response | None, str, str | None]:
        identity = _page_identity(url)
        if identity is None:
            return None, url, "UNSUPPORTED_PUBLIC_URL"
        current = url
        try:
            for hop in range(4):
                if not self.permits(current):
                    return None, current, "ROBOTS_UNRESOLVED_OR_DISALLOWED"
                response = self.get(current)
                if response.status_code not in _REDIRECTS:
                    return response, current, None
                location = response.headers.get("location")
                if not location:
                    return None, current, "REDIRECT_WITHOUT_LOCATION"
                target = urljoin(current, location)
                if _page_identity(target) != identity:
                    return None, current, "REDIRECT_LEFT_EXACT_PUBLIC_OFFER"
                if hop == 3:
                    return None, current, "REDIRECT_LIMIT"
                current = target
        except httpx.TimeoutException:
            return None, current, "NETWORK_TIMEOUT"
        except (httpx.HTTPError, ValueError):
            return None, current, "NETWORK_OR_RESPONSE_ERROR"
        return None, current, "UNKNOWN_RESPONSE"


class _LinkedInTopCard(HTMLParser):
    """Read closure text only inside LinkedIn's job heading/status containers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []
        self.parts: list[str] = []
        self.title: list[str] = []
        self.controls: list[dict[str, Any]] = []
        self.active_controls: list[tuple[str, dict[str, Any]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").lower()
        parent_top = self.stack[-1][1] if self.stack else False
        parent_hidden = self.stack[-1][2] if self.stack else False
        top = parent_top or any(value in classes for value in (
            "top-card-layout", "topcard__", "jobs-unified-top-card", "job-details-jobs-unified-top-card",
        ))
        hidden = (parent_hidden or tag in {"script", "style", "noscript"} or "hidden" in values
                  or (values.get("aria-hidden") or "").lower() == "true")
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, top, hidden))
        if (top and not hidden and tag in {"a", "button"}
                and "disabled" not in values and (values.get("aria-disabled") or "").lower() != "true"):
            href = values.get("href") or ""
            # A class name is styling, not an invitation to apply. Require a
            # functioning anchor or an enabled application-intent button.
            control = {"label": values.get("aria-label") or "", "text": [],
                       "usable": tag == "button" or href.startswith(("https://", "http://", "/"))}
            self.controls.append(control)
            self.active_controls.append((tag, control))

    def handle_endtag(self, tag: str) -> None:
        if self.active_controls and self.active_controls[-1][0] == tag:
            self.active_controls.pop()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, text: str) -> None:
        if self.stack and self.stack[-1][1] and not self.stack[-1][2]:
            self.parts.append(text)
            if any(tag == "h1" for tag, _, _ in self.stack):
                self.title.append(text)
            for _, control in self.active_controls:
                control["text"].append(text)

    @property
    def has_apply(self) -> bool:
        return any(control["usable"] and re.search(
            r"\b(apply|postuler)\b", (control["label"] + " " + " ".join(control["text"])).lower(),
        ) for control in self.controls)


class _OfferText(HTMLParser):
    """Visible offer text excluding navigation, related offers and page chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []
        self.all_parts: list[str] = []
        self.main_parts: list[str] = []
        self.has_main = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        labels = ((values.get("class") or "") + " " + (values.get("id") or "")).lower()
        parent_hidden = self.stack[-1][1] if self.stack else False
        parent_main = self.stack[-1][2] if self.stack else False
        hidden = (parent_hidden or "hidden" in values
                  or (values.get("aria-hidden") or "").lower() == "true" or tag in {
            "script", "style", "noscript", "nav", "footer", "aside",
        } or any(word in labels for word in ("related", "similar-jobs", "other-offers", "recommended", "sidebar")))
        main = parent_main or tag == "main"
        self.has_main |= tag == "main"
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, hidden, main))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, text: str) -> None:
        if self.stack and not self.stack[-1][1]:
            self.all_parts.append(text)
            if self.stack[-1][2]:
                self.main_parts.append(text)

    @property
    def text(self) -> str:
        text = " ".join(self.main_parts if self.has_main else self.all_parts)
        return " ".join(text.lower().replace("’", "'").split())


def _posting_identifies_page(posting: dict[str, Any], final_url: str) -> bool:
    """A deadline belongs to this offer only when JSON-LD names its exact identity."""
    wanted = _page_identity(final_url)
    for field in ("url", "@id"):
        value = posting.get(field)
        if isinstance(value, str):
            try:
                parts = urlsplit(urljoin(final_url, value))
                identity_url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
                if _page_identity(identity_url) == wanted:
                    return True
            except ValueError:
                continue
    return False


def _valid_through(value: Any, now: datetime) -> bool | None:
    """Date-only/naive deadlines expire on the next UTC day, never at midnight early."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            deadline = date.fromisoformat(raw)
            return now.date() > deadline if deadline.year > 1970 else None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?", raw):
            return None
        deadline_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if deadline_time.year <= 1970:
            return None
        if deadline_time.tzinfo is None:
            return now.date() > deadline_time.date()
        return now > deadline_time.astimezone(timezone.utc)
    except ValueError:
        return None


def _page_evidence(reader: _PublicReader, observation: Observation, now: datetime) -> Evidence:
    response, final_url, error = reader.detail(observation.source_url)
    if response is None:
        return Evidence("UNKNOWN", error or "PUBLIC_PAGE_UNAVAILABLE")
    status, body = response.status_code, response.text
    # Stage.ma's barrier parser permits genuine offer 404/410. Authentication,
    # rate limits, challenges and login pages are checked before gone signals.
    from services.collector.parsers.stage_ma import access_barrier as page_barrier
    barrier = page_barrier(status, final_url, body)
    if barrier or status == 999 or "authwall" in urlsplit(final_url).path.lower():
        return Evidence("UNKNOWN", barrier or "AUTHENTICATION_OR_ACCESS_BARRIER", status)
    if status in {404, 410}:
        return Evidence("CLOSED", "EXACT_OFFER_HTTP_GONE", status)
    if status != 200:
        return Evidence("UNKNOWN", "UNEXPECTED_HTTP_STATUS", status)
    identity = _page_identity(final_url)
    if identity is None:
        return Evidence("UNKNOWN", "PUBLIC_OFFER_IDENTITY_UNRESOLVED", status)
    kind = identity[0]
    if kind == "linkedin":
        # Suspicious authentication markers are intentionally conservative:
        # UNKNOWN may include a guest page with an authwall link. Never infer
        # closure from authentication markup or try to work around it.
        sample = body[:20000].lower()
        if any(marker in sample for marker in ("authwall", "sign in to view this job", "security verification")):
            return Evidence("UNKNOWN", "LINKEDIN_AUTHENTICATION_WALL", status)
        top = _LinkedInTopCard()
        top.feed(body)
        text = " ".join(" ".join(top.parts).lower().split())
        if top.title and any(marker in text for marker in _CLOSED_WORDS):
            return Evidence("CLOSED", "LINKEDIN_APPLICATIONS_EXPLICITLY_CLOSED", status)
        if top.title and top.has_apply:
            return Evidence("OPEN", "LINKEDIN_EXACT_JOB_HAS_APPLICATION_CONTROL", status)
        return Evidence("UNKNOWN", "LINKEDIN_OPEN_STATE_NOT_ESTABLISHED", status)
    posting = job_posting(body)
    if kind == "stage":
        offer_text = _OfferText()
        offer_text.feed(body)
        text = offer_text.text
        publication = publication_state(escape(text))
        # Without a main region, only a demonstrative source message clearly
        # referring to this offer is strong enough to close a persisted record.
        markers = [
            "cette offre n'est plus", "cette offre de stage n'est plus",
            "cette offre de stage n'est pas publiée", "cette offre de stage n'est pas publiee",
            "cette offre n'est pas publiée", "cette offre n'est pas publiee",
            "cette offre est expirée", "cette offre est expiree",
            "cette offre de stage est expirée", "cette offre de stage est expiree",
        ]
        if offer_text.has_main:
            markers.extend(["offre expirée", "offre expiree", "annonce expirée", "annonce expiree"])
        explicit_closure = any(marker in text for marker in markers)
        if publication in {STATE_EXPIRED, STATE_UNPUBLISHED} and explicit_closure:
            return Evidence("CLOSED", "STAGE_MA_" + publication, status)
    if posting is None:
        return Evidence("UNKNOWN", "JOBPOSTING_NOT_ESTABLISHED", status)
    if not _posting_identifies_page(posting, final_url):
        return Evidence("UNKNOWN", "JOBPOSTING_EXACT_IDENTITY_UNRESOLVED", status)
    expired = _valid_through(posting.get("validThrough"), now)
    if expired is True:
        return Evidence("CLOSED", "JOBPOSTING_VALID_THROUGH_PASSED", status)
    if expired is False:
        return Evidence("OPEN", "JOBPOSTING_VALID_THROUGH_NOT_PASSED", status)
    return Evidence("UNKNOWN", "EXPLICIT_OPEN_STATE_UNAVAILABLE", status)


def _board_evidence(reader: _PublicReader, source: SourceConfig,
                    observations: list[Observation]) -> dict[str, Evidence]:
    unknown = Evidence("UNKNOWN", "GREENHOUSE_COMPLETE_BOARD_UNAVAILABLE")
    result = {item.key: unknown for item in observations}
    token = source.board_token or ""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", token):
        return result
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    try:
        response = reader.get(url)
        if response.status_code != 200:
            return {item.key: Evidence("UNKNOWN", "GREENHOUSE_BOARD_HTTP_ERROR", response.status_code)
                    for item in observations}
        payload = response.json()
        jobs, meta = payload.get("jobs"), payload.get("meta")
        if (not isinstance(jobs, list) or not isinstance(meta, dict)
                or type(meta.get("total")) is not int or meta["total"] != len(jobs)):
            return result
        ids: set[int] = set()
        for job in jobs:
            if not isinstance(job, dict) or type(job.get("id")) is not int or job["id"] <= 0:
                return result
            identity = _board_id(job.get("absolute_url", ""), token)
            if identity != job["id"] or identity in ids:
                return result
            ids.add(identity)
        for observation in observations:
            identity = _board_id(observation.source_url, token)
            if identity is not None:
                result[observation.key] = Evidence(
                    "OPEN" if identity in ids else "CLOSED",
                    "GREENHOUSE_COMPLETE_BOARD_PRESENT" if identity in ids else "GREENHOUSE_COMPLETE_BOARD_ABSENT",
                    response.status_code,
                )
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        pass
    return result


def _apply_confirmed_states(connection: sqlite3.Connection, observations: list[Observation],
                            state: dict[str, Any], fresh_open_keys: set[str]) -> tuple[int, int]:
    grouped: dict[int, list[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.opportunity_id, []).append(observation)
    expired = reopened = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for opportunity_id, items in grouped.items():
            records = [_record_for(state, item) for item in items]
            if all(record.get("confirmed_state") == "CLOSED" for record in records):
                changed = connection.execute(
                    """UPDATE opportunities SET status='expired', is_active=0, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='visible'""", (opportunity_id,)
                ).rowcount
                expired += changed
            elif any(item.key in fresh_open_keys for item in items):
                changed = connection.execute(
                    """UPDATE opportunities SET status='visible', is_active=1, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='expired'""", (opportunity_id,)
                ).rowcount
                reopened += changed
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return expired, reopened


def verify_catalog_availability(settings: Settings, source_configs: Iterable[SourceConfig],
                                log_dir: Path, *, max_page_checks: int = MAX_PAGE_CHECKS,
                                retry_unknown: bool = False) -> dict[str, Any]:
    """Check authoritative boards and up to fifty due exact public detail pages.

    Call under the refresh lock. A refused source never causes a closure or a
    fatal error. Broken local persistence does fail so its failure is visible.
    """
    if type(max_page_checks) is not int or not 0 <= max_page_checks <= MAX_PAGE_CHECKS:
        raise ValueError("max_page_checks must be between zero and fifty")
    log_dir = Path(log_dir)
    state = _load_state(log_dir)
    now = _now()
    checked_at = now.isoformat(timespec="microseconds")
    connection = _connect(settings)
    try:
        observations = _observations(connection)
        configured = {source.id: source for source in source_configs
                      if source.enabled and source.status == "active"}
        evidence: dict[str, Evidence] = {}
        with httpx.Client(headers={"User-Agent": COLLECTOR_USER_AGENT}, follow_redirects=False) as client:
            reader = _PublicReader(client)
            for source in configured.values():
                if source.type == "greenhouse":
                    items = [item for item in observations if item.source_id == source.id]
                    if items:
                        evidence.update(_board_evidence(reader, source, items))
            # An operator can retry failed reads after fixing a transport fault;
            # successful checks keep their TTL, bounds and closure evidence.
            due = [item for item in observations if item.source_id in configured
                   and item.source_type != "greenhouse"
                   and (_due(_record_for(state, item), now)
                        or (retry_unknown and _record_for(state, item).get("state") == "UNKNOWN"))]
            due.sort(key=lambda item: (_record_for(state, item).get("checked_at", ""), item.opportunity_id, item.source_url))
            for observation in due[:max_page_checks]:
                evidence[observation.key] = _page_evidence(reader, observation, now)
        fresh_open_keys = set()
        for observation in observations:
            result = evidence.get(observation.key)
            if result is not None:
                _record(log_dir, state, observation, result, checked_at)
                if result.state == "OPEN":
                    fresh_open_keys.add(observation.key)
        unknown = sum(item.state == "UNKNOWN" for item in evidence.values())
        deferred_pages = max(0, len(due) - max_page_checks)
        unconfigured = sum(item.source_id not in configured for item in observations)
        unconfirmed = sum(_record_for(state, item).get("confirmed_state") is None for item in observations)
        summary: dict[str, Any] = {
            "state": "PARTIAL" if unknown or deferred_pages or unconfigured or unconfirmed else "READY",
            "checked": len(evidence),
            "confirmed_open": len(fresh_open_keys),
            "confirmed_closed": sum(item.state == "CLOSED" for item in evidence.values()),
            "unknown": unknown, "unconfirmed_observations": unconfirmed,
            "deferred_page_checks": deferred_pages,
            "deferred_unconfigured_observations": unconfigured,
            "max_page_checks": max_page_checks, "recheck_hours": 6,
        }
        # A durable proof is written before changing visibility. If a database
        # write fails, the next cycle can safely reapply the recorded verdict.
        state.update(updated_at=checked_at, summary=summary)
        _write_state(log_dir, state)
        expired, reopened = _apply_confirmed_states(connection, observations, state, fresh_open_keys)
        summary.update(expired=expired, reopened=reopened)
        _write_state(log_dir, state)
        return summary
    finally:
        connection.close()


def mark_user_confirmed_closed(settings: Settings, opportunity_id: int, source_url: str,
                               log_dir: Path) -> dict[str, Any]:
    """Record the human's explicit closure evidence for one exact stored source URL."""
    if type(opportunity_id) is not int or opportunity_id <= 0 or _page_identity(source_url) is None:
        raise ValueError("Expected a positive opportunity id and an exact supported public offer URL")
    connection = _connect(settings)
    try:
        observations = _observations(connection)
        wanted = [item for item in observations if item.opportunity_id == opportunity_id
                  and item.source_url == source_url]
        if len(wanted) != 1:
            raise AvailabilityError("The exact offer observation was not uniquely found")
        state = _load_state(Path(log_dir))
        checked_at = _now().isoformat(timespec="microseconds")
        _record(Path(log_dir), state, wanted[0], Evidence("CLOSED", "USER_CONFIRMED_CLOSED"), checked_at)
        state["updated_at"] = checked_at
        _write_state(Path(log_dir), state)
        # Other never-verified observations still protect a merged opportunity.
        target_observations = [item for item in observations if item.opportunity_id == opportunity_id]
        expired, _ = _apply_confirmed_states(connection, target_observations, state, set())
        summary = {"state": "READY", "opportunity_id": opportunity_id,
                   "source_url": source_url, "reason": "USER_CONFIRMED_CLOSED", "expired": expired}
        return summary
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--log-dir", type=Path, default=PROJECT_ROOT / "data" / "runtime")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-closed", type=int, metavar="OPPORTUNITY_ID")
    parser.add_argument("--source-url")
    parser.add_argument("--max-page-checks", type=int, default=MAX_PAGE_CHECKS)
    parser.add_argument("--retry-unknown", action="store_true",
                        help="Retry inconclusive reads within the existing per-cycle limit")
    args = parser.parse_args()
    if not args.apply:
        parser.error("Availability writes require --apply")
    if bool(args.confirm_closed) != bool(args.source_url):
        parser.error("--confirm-closed and --source-url must be provided together")
    os.chdir(PROJECT_ROOT)
    settings = load_settings()
    if args.database is not None:
        settings = replace(settings, sqlite_database_path=args.database.resolve())
    if settings.database_backend is not DatabaseBackend.SQLITE or settings.sqlite_database_path is None:
        parser.error("The local SQLite deployment is required")
    from services.operations.refresh import refresh_lock
    with refresh_lock(settings.sqlite_database_path):
        result = (mark_user_confirmed_closed(settings, args.confirm_closed, args.source_url, args.log_dir)
                  if args.confirm_closed else verify_catalog_availability(
                      settings, load_source_registry(), args.log_dir, max_page_checks=args.max_page_checks,
                      retry_unknown=args.retry_unknown))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["state"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
