"""Values describing one attempted execution of a source by the radar."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
import re
from typing import Any

SUCCESS = "SUCCESS"
FAILED = "FAILED"
SOURCE_RUN_STATUSES = frozenset({SUCCESS, FAILED})

MAX_ERROR_MESSAGE_LENGTH = 500

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([\w.-]*(?:token|secret|password|passwd|credential|api[_-]?key"
    r"|authorization|auth|cookie|session)[\w.-]*)\s*[=:]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s&;,)\]}]+)"
)
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;)\]}]+")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_OPAQUE_BLOB = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")

REDACTED = "[REDACTED]"


def redact_error_message(message: str | None) -> str | None:
    """Return a persistable failure message with credential-shaped text removed.

    Only the structure of a secret can be recognised here, never its meaning, so
    this removes credential assignments, authorization schemes, URL user-info,
    JWTs, and long opaque blobs, then bounds the length. Free prose is kept
    because it is what makes the stored failure diagnosable.
    """
    if message is None:
        return None
    redacted = _URL_USERINFO.sub(rf"\1{REDACTED}@", message)
    # The scheme runs first: "Authorization: Bearer <token>" would otherwise be
    # consumed as an assignment whose value is the word "Bearer", exposing the
    # token that follows it.
    redacted = _AUTH_SCHEME.sub(rf"\1 {REDACTED}", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(rf"\1={REDACTED}", redacted)
    redacted = _JWT.sub(REDACTED, redacted)
    redacted = _OPAQUE_BLOB.sub(REDACTED, redacted)
    redacted = " ".join(redacted.split())
    if not redacted:
        return None
    if len(redacted) > MAX_ERROR_MESSAGE_LENGTH:
        return redacted[:MAX_ERROR_MESSAGE_LENGTH].rstrip()
    return redacted


@dataclass(frozen=True)
class SourceRunMetrics:
    """Per-run metrics, where ``None`` means "this run did not know it".

    No value is ever invented: a metric no collector measured stays ``None``
    rather than becoming a zero that later reads as a real observation.
    """

    pages_checked: int | None = None
    items_found: int | None = None
    new_items: int | None = None
    relevant_items: int | None = None
    http_status: int | None = None
    parser_version: str | None = None

    @classmethod
    def from_reported(cls, reported: Mapping[str, Any]) -> "SourceRunMetrics":
        """Accept only known, well-typed metrics and ignore everything else."""
        counters = {
            name: reported[name]
            for name in ("pages_checked", "items_found", "new_items", "relevant_items", "http_status")
            if _is_count(reported.get(name))
        }
        version = reported.get("parser_version")
        if isinstance(version, str) and version.strip():
            counters["parser_version"] = version.strip()
        return cls(**counters)

    def merge(self, **overrides: Any) -> "SourceRunMetrics":
        """Return a copy where only explicitly known overrides are applied."""
        return replace(
            self, **{name: value for name, value in overrides.items() if value is not None}
        )


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


NO_METRICS = SourceRunMetrics()


def metrics_reported_by(collector: object) -> SourceRunMetrics:
    """Read the optional ``run_metrics()`` a collector may expose.

    Collectors opt in; none is required to. A collector that reports nothing,
    reports the wrong shape, or raises leaves every metric unknown instead of
    failing the run it is only describing.
    """
    reader = getattr(collector, "run_metrics", None)
    if not callable(reader):
        return NO_METRICS
    try:
        reported = reader()
    except Exception:
        return NO_METRICS
    if not isinstance(reported, Mapping):
        return NO_METRICS
    return SourceRunMetrics.from_reported(reported)


@dataclass(frozen=True)
class SourceRunAttempt:
    """A started attempt, holding the instant collection actually began."""

    source_id: str
    started_at: str


@dataclass(frozen=True)
class SourceRun:
    """One persisted, terminal source run as stored in ``source_runs``."""

    id: int
    source_id: str
    started_at: str
    finished_at: str
    status: str
    pages_checked: int | None
    items_found: int | None
    new_items: int | None
    relevant_items: int | None
    http_status: int | None
    error_type: str | None
    error_message: str | None
    parser_version: str | None

    @property
    def succeeded(self) -> bool:
        return self.status == SUCCESS
