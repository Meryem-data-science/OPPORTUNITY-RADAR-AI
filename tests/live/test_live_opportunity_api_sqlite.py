"""Opt-in API smoke test against the operational local SQLite database."""

import os

from fastapi.testclient import TestClient
import pytest

from services.api.main import app


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_OPPORTUNITY_API_TEST") != "1",
    reason="requires explicit opt-in and an existing operational SQLite database",
)


def test_live_opportunity_api_reads_real_sqlite_data() -> None:
    response = TestClient(app).get("/api/opportunities?limit=5")

    assert response.status_code == 200
    payload = response.json()
    assert payload["returned"] > 0
    assert payload["total"] > 0
    assert any(item["organization"].strip() for item in payload["items"])
    assert all(item["original_url"].startswith(("http://", "https://")) for item in payload["items"])
    assert all("description" not in item for item in payload["items"])
