"""Unit tests for source-run values, redaction, and optional collector metrics."""

import pytest

from services.collector.models.source_run import (
    FAILED,
    MAX_ERROR_MESSAGE_LENGTH,
    NO_METRICS,
    REDACTED,
    SOURCE_RUN_STATUSES,
    SUCCESS,
    SourceRun,
    SourceRunMetrics,
    metrics_reported_by,
    redact_error_message,
)


def test_status_taxonomy_is_closed_and_minimal():
    assert SOURCE_RUN_STATUSES == {"SUCCESS", "FAILED"}
    assert (SUCCESS, FAILED) == ("SUCCESS", "FAILED")


def test_unknown_metrics_stay_none_rather_than_zero():
    assert NO_METRICS == SourceRunMetrics()
    assert NO_METRICS.pages_checked is None
    assert NO_METRICS.items_found is None
    assert NO_METRICS.new_items is None
    assert NO_METRICS.relevant_items is None
    assert NO_METRICS.http_status is None
    assert NO_METRICS.parser_version is None


def test_merge_applies_only_known_values_and_keeps_real_zero():
    merged = NO_METRICS.merge(items_found=0, new_items=None)
    assert merged.items_found == 0
    assert merged.new_items is None

    kept = SourceRunMetrics(items_found=5).merge(items_found=None)
    assert kept.items_found == 5


@pytest.mark.parametrize(
    ("message", "present", "absent"),
    [
        (
            "Gmail refused: TURSO_AUTH_TOKEN=supersecret",
            ["Gmail refused", REDACTED],
            ["supersecret"],
        ),
        (
            "request failed Authorization=Bearer-private-value",
            ["request failed", REDACTED],
            ["Bearer-private-value"],
        ),
        (
            "denied with Authorization: Bearer abcdefghijklmnop",
            ["denied with", REDACTED],
            ["abcdefghijklmnop"],
        ),
        (
            "cannot reach https://user:hunter2@db.example/path",
            ["cannot reach", REDACTED],
            ["hunter2"],
        ),
        (
            "rejected token eyJhbGciOi.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP",
            ["rejected token", REDACTED],
            ["eyJzdWIiOiIxMjM0NTY3ODkwIn0"],
        ),
        (
            "bad api_key=abc123 and secret: xyz789",
            [REDACTED],
            ["abc123", "xyz789"],
        ),
        (
            "raw blob " + "A1b2C3d4" * 6,
            ["raw blob", REDACTED],
            ["A1b2C3d4" * 6],
        ),
    ],
)
def test_redaction_removes_credential_shaped_text_but_keeps_diagnosis(message, present, absent):
    redacted = redact_error_message(message)
    for fragment in present:
        assert fragment in redacted
    for fragment in absent:
        assert fragment not in redacted


def test_redaction_bounds_length_and_normalizes_empty_messages():
    assert redact_error_message(None) is None
    assert redact_error_message("") is None
    assert redact_error_message("   \n  ") is None
    assert redact_error_message("HTTP 503 from the public board") == "HTTP 503 from the public board"
    assert len(redact_error_message("x " * 4000)) <= MAX_ERROR_MESSAGE_LENGTH


def test_collectors_without_the_optional_hook_report_nothing():
    class PlainCollector:
        def collect(self):
            return []

    assert metrics_reported_by(PlainCollector()) == NO_METRICS
    assert metrics_reported_by(None) == NO_METRICS


def test_reported_metrics_are_filtered_and_never_break_the_run():
    class ReportingCollector:
        def run_metrics(self):
            return {
                "pages_checked": 3,
                "http_status": 200,
                "parser_version": "  greenhouse-v2  ",
                "items_found": -1,
                "new_items": True,
                "relevant_items": "many",
                "unexpected": 42,
            }

    metrics = metrics_reported_by(ReportingCollector())
    assert metrics.pages_checked == 3
    assert metrics.http_status == 200
    assert metrics.parser_version == "greenhouse-v2"
    assert metrics.items_found is None
    assert metrics.new_items is None
    assert metrics.relevant_items is None

    class BrokenCollector:
        def run_metrics(self):
            raise RuntimeError("metrics unavailable")

    class WrongShapeCollector:
        def run_metrics(self):
            return ["not", "a", "mapping"]

    assert metrics_reported_by(BrokenCollector()) == NO_METRICS
    assert metrics_reported_by(WrongShapeCollector()) == NO_METRICS


def test_source_run_reports_its_own_outcome():
    row = SourceRun(
        1, "source_a", "2026-01-01T00:00:00.000000+00:00",
        "2026-01-01T00:00:01.000000+00:00", SUCCESS,
        None, 2, 1, None, None, None, None, None,
    )
    assert row.succeeded
    assert not SourceRun(
        2, "source_b", "2026-01-01T00:00:00.000000+00:00",
        "2026-01-01T00:00:01.000000+00:00", FAILED,
        None, None, None, None, None, "RuntimeError", "boom", None,
    ).succeeded
