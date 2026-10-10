"""Validated links between public postings of the same official ATS job.

Only presentation links change. Collected candidates, identifiers and database
URLs remain untouched. A request reads the cache without any network or write.
"""

from __future__ import annotations

import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from services.collector.extractors.opportunity_constraints.text import normalize_description


CACHE_PATH = Path(__file__).resolve().parents[2] / "data/runtime/official-offer-links.json"
_HOSTS = {"job-boards.greenhouse.io", "boards.greenhouse.io"}


def _posting_url(value: object, board: str, posting_id: int | None = None) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        prefix = f"/{board}/jobs/"
        suffix = parsed.path.removeprefix(prefix)
        return (
            parsed.scheme == "https" and parsed.hostname in _HOSTS
            and parsed.netloc == parsed.hostname
            and not parsed.query and not parsed.fragment
            and parsed.path.startswith(prefix) and suffix.isascii() and suffix.isdigit()
            and str(int(suffix)) == suffix and int(suffix) > 0
            and (posting_id is None or int(suffix) == posting_id)
        )
    except (ValueError, OverflowError):
        return False


def _jobs(payload: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list)
        or any(not isinstance(job, dict) for job in payload["jobs"])
    ):
        raise ValueError("Invalid public Greenhouse jobs payload")
    return payload["jobs"]


def _identity(job: dict[str, Any], board: str) -> tuple[int, str, str] | None:
    internal_id, posting_id = job.get("internal_job_id"), job.get("id")
    title, location = job.get("title"), job.get("location")
    location = location.get("name") if isinstance(location, dict) else None
    if (
        type(internal_id) is not int or internal_id <= 0
        or type(posting_id) is not int or posting_id <= 0
        or not isinstance(title, str) or not title.strip()
        or not isinstance(location, str) or not location.strip()
        or not _posting_url(job.get("absolute_url"), board, posting_id)
    ):
        return None
    return internal_id, " ".join(title.split()), " ".join(location.split())


def build_official_offer_links(primary_payload: Any, alternate_payload: Any) -> dict[str, str]:
    """Match only exact persisted ATS identities with real descriptive content."""
    primary_jobs, alternate_jobs = _jobs(primary_payload), _jobs(alternate_payload)
    candidates: dict[tuple[int, str, str], set[str]] = {}
    for job in alternate_jobs:
        identity = _identity(job, "artefactlinkedin")
        content = job.get("content")
        if identity is not None and isinstance(content, str) and normalize_description(content):
            candidates.setdefault(identity, set()).add(job["absolute_url"])
    links: dict[str, str] = {}
    for job in primary_jobs:
        identity = _identity(job, "artefact")
        matches = candidates.get(identity, set()) if identity is not None else set()
        if len(matches) == 1:
            links[job["absolute_url"]] = next(iter(matches))
    return links


def refresh_official_offer_links(primary_payload: Any, alternate_payload: Any) -> int:
    """Validate both positive API responses before atomically replacing the cache."""
    links = build_official_offer_links(primary_payload, alternate_payload)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=CACHE_PATH.parent,
            prefix="official-offer-links-", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump({"version": 1, "links": links}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, CACHE_PATH)
        _read_cache.cache_clear()
        return len(links)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@lru_cache(maxsize=4)
def _read_cache(path: str, modified_ns: int, size: int) -> dict[str, str]:
    try:
        if size > 2_000_000:
            return {}
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or type(payload.get("version")) is not int or payload["version"] != 1
        ):
            return {}
        links = payload.get("links")
        if not isinstance(links, dict) or any(
            not _posting_url(source, "artefact")
            or not _posting_url(target, "artefactlinkedin")
            for source, target in links.items()
        ):
            return {}
        return links
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def resolve_official_offer_url(url: str) -> str:
    """Read a validated official destination, falling back to the original URL."""
    if not _posting_url(url, "artefact"):
        return url
    try:
        metadata = CACHE_PATH.stat()
        return _read_cache(str(CACHE_PATH), metadata.st_mtime_ns, metadata.st_size).get(url, url)
    except OSError:
        return url
