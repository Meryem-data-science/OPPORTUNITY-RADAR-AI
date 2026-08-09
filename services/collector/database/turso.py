"""Minimal Turso SQL-over-HTTP connection adapter."""

from base64 import b64decode, b64encode
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from services.collector.config import Settings

TURSO_HTTP_TIMEOUT_SECONDS = 20.0


class DatabaseConnectionError(RuntimeError):
    """Raised when a configured database operation cannot be completed."""


class DatabaseDependencyError(DatabaseConnectionError):
    """Retained for compatibility with callers of the previous driver adapter."""


class TursoHttpError(DatabaseConnectionError):
    """Raised when the Turso HTTP request fails or times out."""


class TursoProtocolError(DatabaseConnectionError):
    """Raised when Turso returns an invalid SQL-over-HTTP response."""


class TursoStatementError(DatabaseConnectionError):
    """Raised when Turso reports an SQL statement error."""


@dataclass
class TursoResult:
    """Small cursor-compatible view of one Turso execute result."""

    rows: list[tuple[Any, ...]]
    lastrowid: int | None = None
    rowcount: int = 0
    _offset: int = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._offset >= len(self.rows):
            return None
        row = self.rows[self._offset]
        self._offset += 1
        return row

    def fetchall(self) -> list[tuple[Any, ...]]:
        remaining = self.rows[self._offset :]
        self._offset = len(self.rows)
        return remaining


def _pipeline_url(database_url: str) -> str:
    parsed = urlparse(database_url)
    if parsed.scheme != "libsql" or not parsed.netloc:
        raise DatabaseConnectionError(
            "TURSO_DATABASE_URL must be a valid libsql:// URL"
        )
    return urlunparse(("https", parsed.netloc, "/v2/pipeline", "", "", ""))


def _encode_parameter(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, bytes):
        return {"type": "blob", "base64": b64encode(value).decode("ascii")}
    raise TypeError(f"unsupported Turso SQL parameter type: {type(value).__name__}")


def _decode_value(value: Any) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise TursoProtocolError("Turso returned an invalid typed SQL value")
    value_type = value["type"]
    if value_type == "null":
        return None
    if value_type == "integer":
        try:
            return int(value["value"])
        except (KeyError, TypeError, ValueError) as error:
            raise TursoProtocolError(
                "Turso returned an invalid integer value"
            ) from error
    if value_type == "float":
        try:
            return float(value["value"])
        except (KeyError, TypeError, ValueError) as error:
            raise TursoProtocolError("Turso returned an invalid float value") from error
    if value_type == "text" and isinstance(value.get("value"), str):
        return value["value"]
    if value_type == "blob" and isinstance(value.get("base64"), str):
        try:
            return b64decode(value["base64"], validate=True)
        except ValueError as error:
            raise TursoProtocolError("Turso returned an invalid blob value") from error
    raise TursoProtocolError(
        f"Turso returned unsupported SQL value type {value_type!r}"
    )


class TursoHttpConnection:
    """Execute SQL through Turso's official v2 pipeline endpoint."""

    def __init__(self, endpoint: str, client: httpx.Client) -> None:
        self._endpoint = endpoint
        self._client = client
        self._baton: str | None = None
        self._closed = False

    def execute(self, statement: str, *parameters: Any) -> TursoResult:
        if self._closed:
            raise DatabaseConnectionError("Turso connection is closed")
        args = self._normalize_parameters(parameters)
        normalized_statement = statement.strip().rstrip(";").upper()
        result = self._pipeline(
            {
                "type": "execute",
                "stmt": {
                    "sql": statement,
                    "args": [_encode_parameter(value) for value in args],
                    "named_args": [],
                    "want_rows": True,
                },
            }
        )
        if normalized_statement in {"COMMIT", "ROLLBACK"}:
            self._close_stream()
        return result

    @staticmethod
    def _normalize_parameters(parameters: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(parameters) == 1 and isinstance(parameters[0], (tuple, list)):
            return tuple(parameters[0])
        return parameters

    def _pipeline(self, request: dict[str, Any]) -> TursoResult:
        payload = {"baton": self._baton, "requests": [request]}
        try:
            response = self._client.post(self._endpoint, json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise TursoHttpError("Turso SQL-over-HTTP request timed out") from error
        except httpx.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            detail = f" with HTTP {status}" if status is not None else ""
            raise TursoHttpError(
                f"Turso SQL-over-HTTP request failed{detail}"
            ) from error
        try:
            document = response.json()
        except ValueError as error:
            raise TursoProtocolError("Turso returned invalid JSON") from error
        return self._parse_pipeline_response(document, request["type"])

    def _parse_pipeline_response(self, document: Any, request_type: str) -> TursoResult:
        if not isinstance(document, dict):
            raise TursoProtocolError("Turso returned an invalid pipeline response")
        baton = document.get("baton")
        if baton is not None and not isinstance(baton, str):
            raise TursoProtocolError("Turso returned an invalid transaction baton")
        base_url = document.get("base_url")
        if base_url is not None:
            if not isinstance(base_url, str):
                raise TursoProtocolError("Turso returned an invalid base_url")
            self._endpoint = self._pipeline_endpoint_from_base_url(base_url)
        self._baton = baton

        results = document.get("results")
        if not isinstance(results, list) or len(results) != 1:
            raise TursoProtocolError("Turso returned an invalid result count")
        item = results[0]
        if not isinstance(item, dict):
            raise TursoProtocolError("Turso returned an invalid pipeline result")
        if item.get("type") == "error":
            error = item.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            suffix = f" ({code})" if isinstance(code, str) else ""
            raise TursoStatementError(f"Turso SQL statement failed{suffix}")
        if item.get("type") != "ok":
            raise TursoProtocolError("Turso returned an unknown pipeline result")
        response = item.get("response")
        if not isinstance(response, dict) or response.get("type") != request_type:
            raise TursoProtocolError("Turso returned a mismatched pipeline response")
        if request_type == "close":
            return TursoResult([])
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
            raise TursoProtocolError("Turso returned an invalid execute result")
        rows = []
        for row in result["rows"]:
            if not isinstance(row, list):
                raise TursoProtocolError("Turso returned an invalid SQL row")
            rows.append(tuple(_decode_value(value) for value in row))
        lastrowid_value = result.get("last_insert_rowid")
        try:
            lastrowid = int(lastrowid_value) if lastrowid_value is not None else None
            rowcount = int(result.get("affected_row_count", 0))
        except (TypeError, ValueError) as error:
            raise TursoProtocolError(
                "Turso returned invalid result metadata"
            ) from error
        return TursoResult(rows, lastrowid=lastrowid, rowcount=rowcount)

    @staticmethod
    def _pipeline_endpoint_from_base_url(base_url: str) -> str:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise TursoProtocolError("Turso returned an invalid base_url")
        path = parsed.path.rstrip("/")
        if not path.endswith("/v2/pipeline"):
            path += "/v2/pipeline"
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

    def _close_stream(self) -> None:
        if self._baton is None:
            return
        try:
            self._pipeline({"type": "close"})
        finally:
            self._baton = None

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._close_stream()
        finally:
            self._closed = True
            self._client.close()


def connect_turso(settings: Settings) -> TursoHttpConnection:
    """Create a bounded SQL-over-HTTP connection without exposing credentials."""
    if settings.turso_database_url is None or settings.turso_auth_token is None:
        raise DatabaseConnectionError("Validated Turso settings are incomplete")
    endpoint = _pipeline_url(settings.turso_database_url)
    try:
        client = httpx.Client(
            headers={"Authorization": f"Bearer {settings.turso_auth_token}"},
            timeout=httpx.Timeout(TURSO_HTTP_TIMEOUT_SECONDS),
        )
    except Exception as error:
        raise DatabaseConnectionError(
            "Unable to initialize the Turso SQL-over-HTTP client"
        ) from error
    return TursoHttpConnection(endpoint, client)
