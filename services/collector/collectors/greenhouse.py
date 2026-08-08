"""Collector for the public Greenhouse Job Board API."""

from typing import Any
from urllib.parse import urlparse

import httpx

from services.collector.collectors.base import BaseCollector
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig


class GreenhouseCollectionError(RuntimeError):
    """Raised when Greenhouse cannot be fetched or supplies invalid data."""


class GreenhousePayloadError(ValueError):
    """Raised when a Greenhouse response does not follow the expected contract."""


class GreenhouseCollector(BaseCollector):
    """Read opportunities from one configured public Greenhouse board."""

    API_ROOT = "https://boards-api.greenhouse.io/v1/boards"
    TIMEOUT_SECONDS = 15.0

    def __init__(
        self, source: SourceConfig, client: httpx.Client | None = None
    ) -> None:
        if source.type != "greenhouse":
            raise ValueError("GreenhouseCollector requires a greenhouse source")
        self.source = source
        self.source_id = source.id
        self.endpoint = f"{self.API_ROOT}/{source.board_token}/jobs"
        self._client = client

    def fetch(self) -> Any:
        """GET public board JSON, translating transport failures explicitly."""
        try:
            if self._client is not None:
                response = self._client.get(
                    self.endpoint,
                    params={"content": "true"},
                    timeout=self.TIMEOUT_SECONDS,
                )
            else:
                response = httpx.get(
                    self.endpoint,
                    params={"content": "true"},
                    timeout=self.TIMEOUT_SECONDS,
                    follow_redirects=True,
                )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise GreenhouseCollectionError("Greenhouse request timed out") from error
        except httpx.HTTPStatusError as error:
            raise GreenhouseCollectionError(
                f"Greenhouse returned HTTP {error.response.status_code}"
            ) from error
        except httpx.RequestError as error:
            raise GreenhouseCollectionError(
                f"Greenhouse network request failed: {type(error).__name__}"
            ) from error
        try:
            return response.json()
        except ValueError as error:
            raise GreenhousePayloadError("Greenhouse returned invalid JSON") from error

    def parse(self, payload: Any) -> list[dict[str, Any]]:
        """Validate the top-level Greenhouse jobs collection."""
        if not isinstance(payload, dict) or "jobs" not in payload:
            raise GreenhousePayloadError("Greenhouse payload is missing jobs")
        jobs = payload["jobs"]
        if not isinstance(jobs, list):
            raise GreenhousePayloadError("Greenhouse payload jobs must be a list")
        if any(not isinstance(job, dict) for job in jobs):
            raise GreenhousePayloadError("each Greenhouse job must be an object")
        return jobs

    def normalize(self, item: dict[str, Any]) -> OpportunityCandidate:
        """Map only actual Greenhouse fields into an unstored candidate."""
        job_id = item.get("id")
        title = item.get("title")
        url = item.get("absolute_url")
        if (
            job_id is None
            or isinstance(job_id, bool)
            or not isinstance(job_id, (str, int))
        ):
            raise GreenhousePayloadError("Greenhouse job is missing a valid id")
        if not isinstance(title, str) or not title.strip():
            raise GreenhousePayloadError(f"Greenhouse job {job_id!r} is missing title")
        if not isinstance(url, str) or not self._is_real_http_url(url):
            raise GreenhousePayloadError(
                f"Greenhouse job {job_id!r} is missing a real absolute_url"
            )
        location_value = item.get("location")
        location = (
            location_value.get("name") if isinstance(location_value, dict) else None
        )
        description = item.get("content")
        published_at = item.get("first_published")
        return OpportunityCandidate(
            source_id=self.source.id,
            source_external_id=str(job_id),
            canonical_title=title.strip(),
            organization=self.source.organization,
            location=location if isinstance(location, str) else None,
            description=description if isinstance(description, str) else None,
            published_at=published_at if isinstance(published_at, str) else None,
            source_url=url,
            application_url=url,
            canonical_url=url,
        )

    @staticmethod
    def _is_real_http_url(value: str) -> bool:
        parsed = urlparse(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
            and parsed.hostname != "localhost"
        )
