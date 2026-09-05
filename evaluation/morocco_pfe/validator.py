"""Offline, deterministic validation of the Morocco PFE evaluation artefacts.

Nothing in this module opens a socket. Every URL it sees is a **fact already
recorded** in the benchmark or in the source map, and checking that a URL is
still reachable is a live concern that belongs to a later slice, not to a unit
test. There is no `requests`, no `urllib` retrieval, no browser and no retry
here, and there never should be: a benchmark whose validity depends on the
network is a benchmark that fails for reasons that have nothing to do with it.

Two artefacts are validated, and they answer different questions:

    the gold benchmark   opportunities we know were really published
    the source map       the sources we declare we want to be measured against

Both are read-only. This module writes no file, opens no database connection
and imports nothing that does. It reads `config/sources.yaml` through the
production loader for exactly one purpose — to refuse a source map that claims
to be an active collector when no such collector is configured — and it never
writes that file back.

The opportunity-type vocabulary is **imported**, not redeclared:
`OpportunityType` is the one closed registry the profile side and the offer
side already share (`services/digital_twin/preferences/models.py`). A benchmark
with a type vocabulary of its own would be a third taxonomy able to disagree
with both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

import yaml

from services.collector.sources import DEFAULT_SOURCE_REGISTRY, load_source_registry
from services.digital_twin.preferences.models import OpportunityType

#: The only country this benchmark and this map describe. Phase 7A.1 narrowed
#: the product to Morocco; a row for anywhere else belongs to another artefact.
BENCHMARK_COUNTRY_CODE = "MA"

DEFAULT_BENCHMARK_PATH = Path("evaluation/benchmarks/morocco_pfe_gold_v1.jsonl")
DEFAULT_MANIFEST_PATH = Path(
    "evaluation/benchmarks/morocco_pfe_gold_v1.manifest.json"
)
DEFAULT_SOURCE_MAP_PATH = Path(
    "evaluation/source_coverage/morocco_pfe_sources_v1.yaml"
)

HISTORICAL = "HISTORICAL"
LIVE = "LIVE"
#: Whether a row is evidence of something already gone, or of something the
#: radar could still be caught failing to find today. Closed on purpose.
OBSERVATION_HORIZONS = frozenset({HISTORICAL, LIVE})

#: How authoritative the place we saw it is. `OFFICIAL` is the employer itself;
#: everything else is somebody republishing the employer.
SOURCE_AUTHORITIES = frozenset(
    {"OFFICIAL", "JOB_BOARD", "AGGREGATOR", "LINKEDIN_ALERT"}
)

#: Exactly the keys a benchmark record has. Declared closed in both directions:
#: a missing key is a hole, and an unexpected key is a schema drifting in a
#: file nothing else validates.
BENCHMARK_FIELDS = (
    "benchmark_id",
    "title",
    "organization",
    "location",
    "country_code",
    "source_name",
    "source_url",
    "official_application_url",
    "published_at",
    "observed_at",
    "pfe_cohort_year",
    "expected_opportunity_type",
    "expected_data_ai",
    "historical_or_live",
    "source_authority",
    "notes",
)

MANIFEST_FIELDS = (
    "benchmark_name",
    "version",
    "scope_country",
    "scope_goal",
    "status",
    "target_minimum_rows",
    "current_rows",
    "evaluation_ready",
    "created_for_phase",
)

SOURCE_CLASSES = frozenset(
    {
        "OFFICIAL_CAREER",
        "ATS",
        "JOB_BOARD",
        "INTERNSHIP_BOARD",
        "DISCOVERY_AGGREGATOR",
        "LINKEDIN_ALERT",
    }
)
PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})
COVERAGE_ROLES = frozenset({"PRIMARY", "DISCOVERY", "AUDIT", "BENCHMARK_ONLY"})
COLLECTION_STRATEGIES = frozenset(
    {
        "EXISTING_COLLECTOR",
        "GMAIL_ALERT",
        "FUTURE_COLLECTOR",
        "FUTURE_GENERIC_ATS",
        "MANUAL_BENCHMARK",
    }
)
#: Where an entry stands with respect to being collected.
#:
#: `NOT_SELECTED` records a *product* decision and nothing more: the source was
#: evaluated and deliberately not chosen for production in the current PFA
#: scope. It is emphatically **not** a claim that a source is legally
#: forbidden, permanently impossible, or fake — a source can be perfectly real,
#: perfectly reachable by a human, and still not be worth integrating. Keeping
#: that distinction in the vocabulary is the point: without it, "we decided
#: against it" and "we are not allowed" collapse into the same silence.
INTEGRATION_STATUSES = frozenset(
    {"ACTIVE", "CANDIDATE", "BENCHMARK_ONLY", "NEEDS_VERIFICATION", "NOT_SELECTED"}
)
#: The strategies that describe something that already runs. An entry claiming
#: either of them must name a real row of `config/sources.yaml`.
IMPLEMENTED_STRATEGIES = frozenset({"EXISTING_COLLECTOR", "GMAIL_ALERT"})

#: The `status` a configured source must carry for the `RadarAgent` to run it.
#: Mirrors `RadarAgent.run_once`, which keeps a source only when it is both
#: enabled and of this status.
PRODUCTION_ACTIVE_STATUS = "active"

#: The production collector type a strategy commits to, where it commits to a
#: specific one. `EXISTING_COLLECTOR` is deliberately absent: it says "some
#: collector already exists", not which. `GMAIL_ALERT` is not that loose — it
#: names the Gmail LinkedIn alert intake and nothing else.
STRATEGY_PRODUCTION_TYPES = {"GMAIL_ALERT": "gmail_linkedin_alert"}

#: How much we actually know about a homepage URL. No URL in this slice was
#: fetched, so a URL is either evidence somebody handed us, a domain we wrote
#: down from public knowledge and have not confirmed, or nothing at all.
HOMEPAGE_URL_STATUSES = frozenset(
    {"EVIDENCED", "WELL_KNOWN_UNVERIFIED", "UNKNOWN"}
)

SOURCE_MAP_FIELDS = (
    "id",
    "name",
    "homepage_url",
    "homepage_url_status",
    "source_class",
    "priority",
    "coverage_role",
    "collection_strategy",
    "integration_status",
    "country",
    "production_source_id",
    "live_canary",
    "notes",
)

SOURCE_MAP_HEADER_FIELDS = (
    "map_name",
    "version",
    "scope_country",
    "created_for_phase",
    "is_production_registry",
    "production_registry_path",
    "sources",
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Words that mark a record as written to fill a file rather than observed.
#: Matched on word boundaries so a real name that merely contains one of them
#: — "Barcelona", "Testour" — is not thrown away.
_SYNTHETIC_WORDS = re.compile(
    r"(?i)\b(lorem|ipsum|foo|bar|baz|qux|acme|dummy|placeholder|sample|"
    r"synthetic|fixture|mock|fake|todo|tbd|xxx|example|widget)\b"
)
#: Hosts that cannot be a real posting, whatever the path says.
_SYNTHETIC_HOSTS = frozenset(
    {
        "example.com",
        "www.example.com",
        "example.org",
        "www.example.org",
        "example.net",
        "www.example.net",
        "test.com",
        "www.test.com",
        "localhost",
        "127.0.0.1",
    }
)
#: The fields whose text identifies the opportunity. `notes` is deliberately
#: excluded: a reviewer must stay free to write "no example of X yet" there.
_IDENTITY_FIELDS = ("benchmark_id", "title", "organization", "source_name", "location")

_EARLIEST_COHORT_YEAR = 2000
_LATEST_COHORT_YEAR = 2100


class BenchmarkValidationError(ValueError):
    """Raised when the gold benchmark or its manifest is not usable as one."""


class SourceMapValidationError(ValueError):
    """Raised when the declared source universe is malformed or overclaims."""


@dataclass(frozen=True)
class BenchmarkReport:
    """What one validated benchmark is, stated in numbers rather than prose."""

    record_count: int
    status: str
    target_minimum_rows: int
    evaluation_ready: bool

    @property
    def rows_missing_for_target(self) -> int:
        """How many more reviewed rows the declared target still needs."""
        return max(0, self.target_minimum_rows - self.record_count)


@dataclass(frozen=True)
class SourceMapEntry:
    """One source we declare we want our coverage measured against.

    This is **not** a collector and never becomes one by being written here.
    `production_source_id` is the only link to the operational catalogue, and
    it is null for everything that is not already running.
    """

    id: str
    name: str
    homepage_url: str | None
    homepage_url_status: str
    source_class: str
    priority: str
    coverage_role: str
    collection_strategy: str
    integration_status: str
    country: str
    production_source_id: str | None
    live_canary: bool
    notes: str | None

    @property
    def claims_to_be_implemented(self) -> bool:
        return (
            self.integration_status == "ACTIVE"
            or self.collection_strategy in IMPLEMENTED_STRATEGIES
        )


@dataclass(frozen=True)
class SourceMap:
    """The declared, versioned universe of sources — the coverage denominator."""

    map_name: str
    version: str
    scope_country: str
    created_for_phase: str
    is_production_registry: bool
    production_registry_path: str
    sources: tuple[SourceMapEntry, ...]

    def by_priority(self, priority: str) -> tuple[SourceMapEntry, ...]:
        return tuple(item for item in self.sources if item.priority == priority)

    @property
    def active_sources(self) -> tuple[SourceMapEntry, ...]:
        return tuple(
            item for item in self.sources if item.integration_status == "ACTIVE"
        )

    @property
    def live_canaries(self) -> tuple[SourceMapEntry, ...]:
        return tuple(item for item in self.sources if item.live_canary)


def _require_text(value: Any, field: str, where: str, error: type[ValueError]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{where}: {field} must be a non-empty string")
    if value != value.strip():
        raise error(f"{where}: {field} must not be padded with whitespace")
    return value


def _require_optional_text(
    value: Any, field: str, where: str, error: type[ValueError]
) -> str | None:
    if value is None:
        return None
    return _require_text(value, field, where, error)


def _require_http_url(
    value: Any, field: str, where: str, error: type[ValueError]
) -> str:
    url = _require_text(value, field, where, error)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise error(f"{where}: {field} must be an absolute http or https URL")
    return url


def _require_optional_http_url(
    value: Any, field: str, where: str, error: type[ValueError]
) -> str | None:
    if value is None:
        return None
    return _require_http_url(value, field, where, error)


def _require_bool(value: Any, field: str, where: str, error: type[ValueError]) -> bool:
    if not isinstance(value, bool):
        raise error(f"{where}: {field} must be a boolean")
    return value


def _require_member(
    value: Any,
    registry: frozenset[str],
    field: str,
    where: str,
    error: type[ValueError],
) -> str:
    text = _require_text(value, field, where, error)
    if text not in registry:
        raise error(
            f"{where}: {field} must be one of {sorted(registry)}, got {text!r}"
        )
    return text


def _require_exact_keys(
    record: Any, fields: tuple[str, ...], where: str, error: type[ValueError]
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise error(f"{where}: must be a JSON/YAML object")
    keys = set(record)
    expected = set(fields)
    missing = sorted(expected - keys)
    if missing:
        raise error(f"{where}: missing field(s) {missing}")
    unexpected = sorted(keys - expected)
    if unexpected:
        raise error(f"{where}: unexpected field(s) {unexpected}")
    return record


def _require_iso_date(
    value: Any, field: str, where: str, error: type[ValueError]
) -> date:
    text = _require_text(value, field, where, error)
    if not _ISO_DATE.match(text):
        raise error(f"{where}: {field} must be an ISO YYYY-MM-DD date")
    try:
        return date.fromisoformat(text)
    except ValueError as failure:
        raise error(f"{where}: {field} is not a real date: {text!r}") from failure


def _reject_synthetic(record: dict[str, Any], where: str) -> None:
    """Refuse a row that reads as invented rather than observed.

    The benchmark's whole value is that every line happened. A padded corpus
    would inflate a recall number by measuring the radar against postings no
    employer ever wrote, which is worse than a small honest one.
    """
    for field in _IDENTITY_FIELDS:
        value = record.get(field)
        if not isinstance(value, str):
            continue
        marker = _SYNTHETIC_WORDS.search(value)
        if marker is not None:
            raise BenchmarkValidationError(
                f"{where}: {field} looks synthetic ({marker.group(0)!r}); "
                "the benchmark holds observed opportunities only"
            )
    for field in ("source_url", "official_application_url"):
        value = record.get(field)
        if not isinstance(value, str):
            continue
        host = urlsplit(value).netloc.lower()
        if host in _SYNTHETIC_HOSTS:
            raise BenchmarkValidationError(
                f"{where}: {field} points at placeholder host {host!r}"
            )


def _validate_record(record: Any, where: str) -> dict[str, Any]:
    """Validate one benchmark line and return it unchanged."""
    value = _require_exact_keys(
        record, BENCHMARK_FIELDS, where, BenchmarkValidationError
    )
    _require_text(value["benchmark_id"], "benchmark_id", where, BenchmarkValidationError)
    for field in ("title", "organization", "source_name"):
        _require_text(value[field], field, where, BenchmarkValidationError)
    _require_optional_text(value["location"], "location", where, BenchmarkValidationError)
    _require_optional_text(value["notes"], "notes", where, BenchmarkValidationError)

    country_code = _require_text(
        value["country_code"], "country_code", where, BenchmarkValidationError
    )
    if country_code != BENCHMARK_COUNTRY_CODE:
        raise BenchmarkValidationError(
            f"{where}: country_code must be {BENCHMARK_COUNTRY_CODE!r}, "
            f"got {country_code!r}"
        )

    _require_http_url(value["source_url"], "source_url", where, BenchmarkValidationError)
    _require_optional_http_url(
        value["official_application_url"],
        "official_application_url",
        where,
        BenchmarkValidationError,
    )

    _require_member(
        value["historical_or_live"],
        OBSERVATION_HORIZONS,
        "historical_or_live",
        where,
        BenchmarkValidationError,
    )
    _require_member(
        value["source_authority"],
        SOURCE_AUTHORITIES,
        "source_authority",
        where,
        BenchmarkValidationError,
    )
    _require_member(
        value["expected_opportunity_type"],
        frozenset(member.value for member in OpportunityType),
        "expected_opportunity_type",
        where,
        BenchmarkValidationError,
    )

    # A gold label, and only that: three answers, where None means "no human has
    # decided yet". It is never `False` by default, and it changes no classifier.
    data_ai = value["expected_data_ai"]
    # `isinstance`, not `in (True, False, None)`: `1 == True` in Python, and an
    # integer smuggled into a gold label is exactly the silent corruption a
    # closed three-answer field exists to prevent.
    if data_ai is not None and not isinstance(data_ai, bool):
        raise BenchmarkValidationError(
            f"{where}: expected_data_ai must be true, false or null"
        )

    published_at = value["published_at"]
    published = (
        None
        if published_at is None
        else _require_iso_date(
            published_at, "published_at", where, BenchmarkValidationError
        )
    )
    observed = _require_iso_date(
        value["observed_at"], "observed_at", where, BenchmarkValidationError
    )
    if published is not None and observed < published:
        raise BenchmarkValidationError(
            f"{where}: observed_at precedes published_at"
        )

    # An observed, reviewed fact — never derived. A publication date is not a
    # cohort: a campaign published in October 2025 can be PFE 2026, and one
    # published in August 2026 can be PFE 2027. Where no evidence states the
    # cohort, the field stays null rather than being guessed from published_at,
    # and nothing in this module infers it.
    cohort_year = value["pfe_cohort_year"]
    if cohort_year is not None:
        if isinstance(cohort_year, bool) or not isinstance(cohort_year, int):
            raise BenchmarkValidationError(
                f"{where}: pfe_cohort_year must be null or an integer"
            )
        if not _EARLIEST_COHORT_YEAR <= cohort_year <= _LATEST_COHORT_YEAR:
            raise BenchmarkValidationError(
                f"{where}: pfe_cohort_year {cohort_year} is out of range"
            )

    _reject_synthetic(value, where)
    return value


def parse_benchmark_lines(text: str, *, origin: str = "benchmark") -> list[dict[str, Any]]:
    """Parse and validate JSONL content already read into memory.

    Blank lines are skipped so the file can end with a newline; anything else
    on a line has to be one complete JSON object.
    """
    records: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{origin} line {number}"
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as failure:
            raise BenchmarkValidationError(
                f"{where}: invalid JSON ({failure.msg})"
            ) from failure
        record = _validate_record(parsed, where)
        benchmark_id = record["benchmark_id"]
        if benchmark_id in seen:
            raise BenchmarkValidationError(
                f"{where}: duplicate benchmark_id {benchmark_id!r}, "
                f"first seen on line {seen[benchmark_id]}"
            )
        seen[benchmark_id] = number
        records.append(record)
    return records


def load_benchmark_records(
    path: Path = DEFAULT_BENCHMARK_PATH,
) -> list[dict[str, Any]]:
    """Read one JSONL benchmark file and validate every line in it."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as failure:
        raise BenchmarkValidationError(
            f"cannot read benchmark: {failure}"
        ) from failure
    return parse_benchmark_lines(text, origin=str(path))


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    """Read and validate a benchmark manifest, without comparing it to rows."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as failure:
        raise BenchmarkValidationError(f"cannot read manifest: {failure}") from failure
    try:
        document = json.loads(text)
    except json.JSONDecodeError as failure:
        raise BenchmarkValidationError(
            f"invalid manifest JSON ({failure.msg})"
        ) from failure
    return validate_manifest_document(document, origin=str(path))


def validate_manifest_document(
    document: Any, *, origin: str = "manifest"
) -> dict[str, Any]:
    """Validate a manifest mapping on its own terms."""
    manifest = _require_exact_keys(
        document, MANIFEST_FIELDS, origin, BenchmarkValidationError
    )
    for field in ("benchmark_name", "version", "scope_goal", "status", "created_for_phase"):
        _require_text(manifest[field], field, origin, BenchmarkValidationError)

    scope_country = _require_text(
        manifest["scope_country"], "scope_country", origin, BenchmarkValidationError
    )
    if scope_country != BENCHMARK_COUNTRY_CODE:
        raise BenchmarkValidationError(
            f"{origin}: scope_country must be {BENCHMARK_COUNTRY_CODE!r}"
        )

    for field in ("target_minimum_rows", "current_rows"):
        count = manifest[field]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise BenchmarkValidationError(
                f"{origin}: {field} must be a non-negative integer"
            )
    _require_bool(
        manifest["evaluation_ready"], "evaluation_ready", origin, BenchmarkValidationError
    )
    return manifest


def validate_benchmark(
    benchmark_path: Path = DEFAULT_BENCHMARK_PATH,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> BenchmarkReport:
    """Validate the rows, the manifest, and that the two agree with each other.

    The readiness rule is a **coherence** check, not a ceiling: a benchmark
    smaller than its own declared target may not call itself evaluation-ready,
    and one that has reached the target may be flipped to ready by a human who
    has reviewed it. Nothing here flips it automatically — row count is not
    review.
    """
    records = load_benchmark_records(benchmark_path)
    manifest = load_manifest(manifest_path)
    origin = str(manifest_path)

    if manifest["current_rows"] != len(records):
        raise BenchmarkValidationError(
            f"{origin}: current_rows is {manifest['current_rows']} but the "
            f"benchmark holds {len(records)} row(s)"
        )
    if manifest["evaluation_ready"] and len(records) < manifest["target_minimum_rows"]:
        raise BenchmarkValidationError(
            f"{origin}: evaluation_ready must be false while the benchmark holds "
            f"{len(records)} row(s), below its target of "
            f"{manifest['target_minimum_rows']}"
        )
    return BenchmarkReport(
        record_count=len(records),
        status=manifest["status"],
        target_minimum_rows=manifest["target_minimum_rows"],
        evaluation_ready=manifest["evaluation_ready"],
    )


def _validate_source_entry(entry: Any, where: str) -> SourceMapEntry:
    value = _require_exact_keys(
        entry, SOURCE_MAP_FIELDS, where, SourceMapValidationError
    )
    identifier = _require_text(value["id"], "id", where, SourceMapValidationError)
    name = _require_text(value["name"], "name", where, SourceMapValidationError)
    homepage_url = _require_optional_http_url(
        value["homepage_url"], "homepage_url", where, SourceMapValidationError
    )
    homepage_url_status = _require_member(
        value["homepage_url_status"],
        HOMEPAGE_URL_STATUSES,
        "homepage_url_status",
        where,
        SourceMapValidationError,
    )
    # An unfetched URL and a URL we do not have are different states, and the
    # map has to say which it is rather than letting a blank field mean either.
    if homepage_url is None and homepage_url_status != "UNKNOWN":
        raise SourceMapValidationError(
            f"{where}: homepage_url_status must be 'UNKNOWN' when no URL is recorded"
        )
    if homepage_url is not None and homepage_url_status == "UNKNOWN":
        raise SourceMapValidationError(
            f"{where}: a recorded homepage_url cannot have status 'UNKNOWN'"
        )

    source_class = _require_member(
        value["source_class"], SOURCE_CLASSES, "source_class", where, SourceMapValidationError
    )
    priority = _require_member(
        value["priority"], PRIORITIES, "priority", where, SourceMapValidationError
    )
    coverage_role = _require_member(
        value["coverage_role"],
        COVERAGE_ROLES,
        "coverage_role",
        where,
        SourceMapValidationError,
    )
    collection_strategy = _require_member(
        value["collection_strategy"],
        COLLECTION_STRATEGIES,
        "collection_strategy",
        where,
        SourceMapValidationError,
    )
    integration_status = _require_member(
        value["integration_status"],
        INTEGRATION_STATUSES,
        "integration_status",
        where,
        SourceMapValidationError,
    )
    country = _require_text(value["country"], "country", where, SourceMapValidationError)
    if country != BENCHMARK_COUNTRY_CODE:
        raise SourceMapValidationError(
            f"{where}: country must be {BENCHMARK_COUNTRY_CODE!r}, got {country!r}"
        )
    production_source_id = _require_optional_text(
        value["production_source_id"],
        "production_source_id",
        where,
        SourceMapValidationError,
    )
    live_canary = _require_bool(
        value["live_canary"], "live_canary", where, SourceMapValidationError
    )
    notes = _require_optional_text(
        value["notes"], "notes", where, SourceMapValidationError
    )

    # LinkedIn is read out of Gmail alert mail and nowhere else. The closed
    # strategy registry has no scraping member at all; this pins the one class
    # that could plausibly acquire one to the strategy actually implemented.
    if source_class == "LINKEDIN_ALERT" and collection_strategy != "GMAIL_ALERT":
        raise SourceMapValidationError(
            f"{where}: a LINKEDIN_ALERT source is collected through GMAIL_ALERT only"
        )
    if collection_strategy == "GMAIL_ALERT" and source_class != "LINKEDIN_ALERT":
        raise SourceMapValidationError(
            f"{where}: GMAIL_ALERT describes the LinkedIn alert source only"
        )

    if integration_status == "ACTIVE":
        if collection_strategy not in IMPLEMENTED_STRATEGIES:
            raise SourceMapValidationError(
                f"{where}: an ACTIVE source names a strategy that already runs"
            )
        if production_source_id is None:
            raise SourceMapValidationError(
                f"{where}: an ACTIVE source must name its config/sources.yaml id"
            )
    else:
        if production_source_id is not None:
            raise SourceMapValidationError(
                f"{where}: only an ACTIVE source may name a production_source_id"
            )
        if collection_strategy in IMPLEMENTED_STRATEGIES:
            raise SourceMapValidationError(
                f"{where}: {collection_strategy} claims an implemented collector, "
                f"so integration_status cannot be {integration_status}"
            )

    return SourceMapEntry(
        id=identifier,
        name=name,
        homepage_url=homepage_url,
        homepage_url_status=homepage_url_status,
        source_class=source_class,
        priority=priority,
        coverage_role=coverage_role,
        collection_strategy=collection_strategy,
        integration_status=integration_status,
        country=country,
        production_source_id=production_source_id,
        live_canary=live_canary,
        notes=notes,
    )


def parse_source_map(document: Any, *, origin: str = "source map") -> SourceMap:
    """Validate an already-parsed source map document."""
    header = _require_exact_keys(
        document, SOURCE_MAP_HEADER_FIELDS, origin, SourceMapValidationError
    )
    for field in ("map_name", "version", "created_for_phase", "production_registry_path"):
        _require_text(header[field], field, origin, SourceMapValidationError)
    scope_country = _require_text(
        header["scope_country"], "scope_country", origin, SourceMapValidationError
    )
    if scope_country != BENCHMARK_COUNTRY_CODE:
        raise SourceMapValidationError(
            f"{origin}: scope_country must be {BENCHMARK_COUNTRY_CODE!r}"
        )
    # The map declares intent. If this ever reads true, somebody has confused it
    # with `config/sources.yaml`, which is the file that actually turns sources on.
    if _require_bool(
        header["is_production_registry"],
        "is_production_registry",
        origin,
        SourceMapValidationError,
    ):
        raise SourceMapValidationError(
            f"{origin}: the coverage map is not a production source registry"
        )
    if not isinstance(header["sources"], list) or not header["sources"]:
        raise SourceMapValidationError(f"{origin}: sources must be a non-empty list")

    entries: list[SourceMapEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(header["sources"], start=1):
        entry = _validate_source_entry(item, f"{origin} source {index}")
        if entry.id in seen:
            raise SourceMapValidationError(
                f"{origin}: duplicate source id {entry.id!r}"
            )
        seen.add(entry.id)
        entries.append(entry)

    return SourceMap(
        map_name=header["map_name"],
        version=header["version"],
        scope_country=scope_country,
        created_for_phase=header["created_for_phase"],
        is_production_registry=False,
        production_registry_path=header["production_registry_path"],
        sources=tuple(entries),
    )


def load_source_map(path: Path = DEFAULT_SOURCE_MAP_PATH) -> SourceMap:
    """Read and validate the declared Morocco source universe."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as failure:
        raise SourceMapValidationError(
            f"cannot read source map: {failure}"
        ) from failure
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as failure:
        raise SourceMapValidationError(
            f"invalid source map YAML: {failure}"
        ) from failure
    return parse_source_map(document, origin=str(path))


def check_source_map_against_production_registry(
    source_map: SourceMap,
    registry_path: Path = DEFAULT_SOURCE_REGISTRY,
) -> tuple[SourceMapEntry, ...]:
    """Refuse a map that claims a collector the operational catalogue lacks.

    This is the one place the evaluation side reads the production side, and it
    reads it to be contradicted: an entry may only call itself ACTIVE if
    `config/sources.yaml` really configures the id it names. Writing a row here
    activates nothing, and this check is what keeps that true as the map grows.

    "ACTIVE" means exactly what the `RadarAgent` means by it: `run_once` keeps
    a configured source only when `source.enabled and source.status ==
    "active"`. A source that is enabled but inactive is never collected, so
    calling it ACTIVE here would put a source in the coverage numerator that
    contributes nothing to it — the denominator would silently absorb a source
    nobody is reading.
    """
    configured = {source.id: source for source in load_source_registry(registry_path)}
    active = source_map.active_sources
    for entry in active:
        configured_source = configured.get(str(entry.production_source_id))
        if configured_source is None:
            raise SourceMapValidationError(
                f"source {entry.id!r} claims production source "
                f"{entry.production_source_id!r}, which {registry_path} does not configure"
            )
        if not configured_source.enabled:
            raise SourceMapValidationError(
                f"source {entry.id!r} is ACTIVE but production source "
                f"{configured_source.id!r} is disabled in {registry_path}"
            )
        if configured_source.status != PRODUCTION_ACTIVE_STATUS:
            raise SourceMapValidationError(
                f"source {entry.id!r} is ACTIVE but production source "
                f"{configured_source.id!r} has status "
                f"{configured_source.status!r}, so the RadarAgent never runs it"
            )
        expected_type = STRATEGY_PRODUCTION_TYPES.get(entry.collection_strategy)
        if expected_type is not None and configured_source.type != expected_type:
            raise SourceMapValidationError(
                f"source {entry.id!r} declares {entry.collection_strategy}, but "
                f"production source {configured_source.id!r} is of type "
                f"{configured_source.type!r}, not {expected_type!r}"
            )
    return active
