"""No-network tests for the minimal Turso SQL-over-HTTP adapter."""

import json

import httpx
import pytest

from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database import turso
from services.collector.database.turso import (
    TursoHttpConnection,
    TursoHttpError,
    TursoProtocolError,
    TursoStatementError,
    connect_turso,
)


def execute_document(
    *, rows=None, baton=None, base_url=None, last_insert_rowid=None
) -> dict[str, object]:
    return {
        "baton": baton,
        "base_url": base_url,
        "results": [
            {
                "type": "ok",
                "response": {
                    "type": "execute",
                    "result": {
                        "cols": [],
                        "rows": rows or [],
                        "affected_row_count": 1,
                        "last_insert_rowid": last_insert_rowid,
                    },
                },
            }
        ],
    }


def response(request: httpx.Request, document: object, status: int = 200):
    return httpx.Response(status, json=document, request=request)


def connection_with_handler(handler):
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer TEST_ONLY_SECRET"},
        timeout=0.1,
    )
    return TursoHttpConnection("https://fixture.invalid/v2/pipeline", client)


def test_connect_builds_url_authorization_and_explicit_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: response(request, {}))
    )

    def client_factory(**kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(turso.httpx, "Client", client_factory)
    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.TURSO,
        turso_database_url="libsql://database-name.turso.io",
        turso_auth_token="TEST_ONLY_SECRET",
    )

    connection = connect_turso(settings)

    assert connection._endpoint == "https://database-name.turso.io/v2/pipeline"
    assert captured["headers"] == {"Authorization": "Bearer TEST_ONLY_SECRET"}
    assert isinstance(captured["timeout"], httpx.Timeout)
    connection.close()


def test_parameters_and_select_rows_are_typed_and_cursor_compatible() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request):
        requests.append(json.loads(request.content))
        return response(
            request,
            execute_document(
                rows=[
                    [
                        {"type": "null"},
                        {"type": "text", "value": "unchanged"},
                        {"type": "integer", "value": "42"},
                        {"type": "float", "value": 1.5},
                        {"type": "blob", "base64": "YmluYXJ5"},
                    ]
                ],
                last_insert_rowid="7",
            ),
        )

    connection = connection_with_handler(handler)
    result = connection.execute(
        "SELECT ?, ?, ?, ?, ?, ?",
        (None, "unchanged", 42, True, 1.5, b"binary"),
    )

    args = requests[0]["requests"][0]["stmt"]["args"]
    assert args == [
        {"type": "null"},
        {"type": "text", "value": "unchanged"},
        {"type": "integer", "value": "42"},
        {"type": "integer", "value": "1"},
        {"type": "float", "value": 1.5},
        {"type": "blob", "base64": "YmluYXJ5"},
    ]
    assert result.fetchone() == (None, "unchanged", 42, 1.5, b"binary")
    assert result.fetchone() is None
    assert result.lastrowid == 7
    connection.close()


def test_long_text_parameter_is_sent_and_read_without_truncation() -> None:
    long_text = "é" * 12_001
    sent_text = ""

    def handler(request: httpx.Request):
        nonlocal sent_text
        payload = json.loads(request.content)
        sent_text = payload["requests"][0]["stmt"]["args"][0]["value"]
        return response(
            request,
            execute_document(rows=[[{"type": "text", "value": sent_text}]]),
        )

    connection = connection_with_handler(handler)
    stored = connection.execute("SELECT ?", (long_text,)).fetchone()[0]
    connection.close()

    assert len(sent_text) == 12_001
    assert sent_text == long_text
    assert stored == long_text


@pytest.mark.parametrize("terminal_statement", ["COMMIT", "ROLLBACK"])
def test_transaction_reuses_baton_base_url_and_closes_stream(
    terminal_statement,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request):
        payload = json.loads(request.content)
        requests.append((str(request.url), payload))
        request_type = payload["requests"][0]["type"]
        if request_type == "close":
            return response(
                request,
                {
                    "baton": None,
                    "base_url": None,
                    "results": [{"type": "ok", "response": {"type": "close"}}],
                },
            )
        sql = payload["requests"][0]["stmt"]["sql"]
        if sql == "BEGIN":
            return response(
                request,
                execute_document(
                    baton="test-baton", base_url="https://transaction.invalid"
                ),
            )
        return response(request, execute_document(baton="test-baton"))

    connection = connection_with_handler(handler)
    connection.execute("BEGIN")
    connection.execute("SELECT 1")
    connection.execute(terminal_statement)

    assert requests[1][0] == "https://transaction.invalid/v2/pipeline"
    assert requests[1][1]["baton"] == "test-baton"
    assert requests[2][1]["baton"] == "test-baton"
    assert requests[3][1]["requests"] == [{"type": "close"}]
    assert connection._baton is None
    connection.close()


def test_close_with_active_baton_closes_remote_stream_and_client() -> None:
    def handler(request: httpx.Request):
        payload = json.loads(request.content)
        if payload["requests"][0]["type"] == "close":
            return response(
                request,
                {
                    "baton": None,
                    "base_url": None,
                    "results": [{"type": "ok", "response": {"type": "close"}}],
                },
            )
        return response(request, execute_document(baton="test-baton"))

    connection = connection_with_handler(handler)
    connection.execute("BEGIN")
    client = connection._client
    connection.close()

    assert client.is_closed
    assert connection._baton is None


def test_http_status_error_is_explicit_and_secret_free() -> None:
    connection = connection_with_handler(
        lambda request: response(request, {"token": "TEST_ONLY_SECRET"}, status=503)
    )

    with pytest.raises(TursoHttpError, match="HTTP 503") as raised:
        connection.execute("SELECT 1")

    assert "TEST_ONLY_SECRET" not in str(raised.value)
    connection.close()


def test_timeout_is_explicit(monkeypatch) -> None:
    class TimeoutClient:
        def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("TEST ONLY timeout")

        def close(self):
            pass

    connection = TursoHttpConnection(
        "https://fixture.invalid/v2/pipeline", TimeoutClient()
    )

    with pytest.raises(TursoHttpError, match="timed out"):
        connection.execute("SELECT 1")
    connection.close()


@pytest.mark.parametrize(
    ("document", "error_type"),
    [
        ({"invalid": True}, TursoProtocolError),
        (
            {
                "baton": None,
                "base_url": None,
                "results": [
                    {
                        "type": "error",
                        "error": {
                            "message": "TEST ONLY SQL error",
                            "code": "SQLITE_ERROR",
                        },
                    }
                ],
            },
            TursoStatementError,
        ),
    ],
)
def test_protocol_and_sql_errors_are_explicit(document, error_type) -> None:
    connection = connection_with_handler(lambda request: response(request, document))
    with pytest.raises(error_type):
        connection.execute("SELECT 1")
    connection.close()
