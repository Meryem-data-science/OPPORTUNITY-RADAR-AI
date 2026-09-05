"""Tests for the Phase 7C.2A Gmail LinkedIn intake audit.

Every message here is invented. No real alert, no real mailbox and no real
message id enters this suite, and none should: the audit's real numbers come
from running it against the project's dedicated Gmail account, and a synthetic
fixture is never evidence of what that account contains. Nothing in this file
is a validation of live coverage, and no expected count here is copied from a
live run.

No test opens a socket, a database or a token file. The Gmail client is stubbed
at the CLI boundary exactly as the existing probe tests stub it.
"""

import inspect
import json
from unittest.mock import Mock, patch

import pytest

from evaluation.morocco_pfe import linkedin_gmail_audit
from evaluation.morocco_pfe.cli import linkedin_gmail_audit as audit_cli
from evaluation.morocco_pfe.linkedin_gmail_audit import (
    DEFAULT_AUDIT_MESSAGE_LIMIT,
    DEFAULT_AUDIT_QUERY,
    GmailIntakeAuditError,
    audit_gmail_intake,
)
from services.collector.gmail.client import MAX_MESSAGE_LIMIT
from services.collector.models.gmail_message import GmailMessageCandidate
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.linkedin_job_alert import (
    LINKEDIN_JOB_ALERT_SOURCE_ID,
    parse_linkedin_job_alert,
)

QUERY = "newer_than:30d from:jobalerts-noreply@example.test"


def message(name: str = "invented") -> GmailMessageCandidate:
    """A normalized message whose every field is fictional."""
    return GmailMessageCandidate(
        message_id=f"invented-message-{name}",
        thread_id=f"invented-thread-{name}",
        sender="Alerts <jobalerts-noreply@example.test>",
        subject="INVENTED SUBJECT",
        received_at="2026-01-01T00:00:00+00:00",
        snippet="INVENTED SNIPPET",
        body_text="INVENTED BODY TEXT",
        body_html="<p>INVENTED BODY HTML</p>",
    )


def candidate(job_id: str, location: str | None = "Casablanca, Maroc") -> OpportunityCandidate:
    url = f"https://www.linkedin.com/jobs/view/{job_id}"
    return OpportunityCandidate(
        source_id=LINKEDIN_JOB_ALERT_SOURCE_ID,
        source_external_id=job_id,
        canonical_title="Invented role",
        organization="Invented organization",
        location=location,
        description=None,
        published_at=None,
        source_url=url,
        application_url=url,
        canonical_url=url,
    )


def parser_returning(*batches: list[OpportunityCandidate]):
    """A stub parser handing back one prepared batch per message, in order."""
    remaining = list(batches)

    def parse(_message: GmailMessageCandidate) -> list[OpportunityCandidate]:
        return remaining.pop(0)

    return parse


def audit(messages, batches, *, query: str = QUERY, message_limit: int = 10):
    return audit_gmail_intake(
        messages,
        query=query,
        message_limit=message_limit,
        parser=parser_returning(*batches),
    )


# ------------------------------------------------------------- aggregation ---


def test_zero_messages_report_every_count_as_zero() -> None:
    report = audit([], [])

    assert report.messages_found == 0
    assert report.messages_with_candidates == 0
    assert report.messages_without_candidates == 0
    assert report.candidates_parsed == 0
    assert report.unique_linkedin_jobs == 0
    assert report.duplicate_occurrences == 0
    assert report.candidates_with_location == 0
    assert report.candidates_without_location == 0
    assert report.truncated is False


def test_one_message_with_one_candidate() -> None:
    report = audit([message()], [[candidate("11")]])

    assert report.messages_found == 1
    assert report.messages_with_candidates == 1
    assert report.messages_without_candidates == 0
    assert report.candidates_parsed == 1
    assert report.unique_linkedin_jobs == 1
    assert report.duplicate_occurrences == 0


def test_one_message_with_several_candidates_counts_the_message_once() -> None:
    report = audit(
        [message()], [[candidate("11"), candidate("22"), candidate("33")]]
    )

    assert report.messages_found == 1
    assert report.messages_with_candidates == 1
    assert report.candidates_parsed == 3
    assert report.unique_linkedin_jobs == 3
    assert report.duplicate_occurrences == 0


def test_the_same_job_in_several_messages_is_one_unique_job() -> None:
    report = audit(
        [message("a"), message("b"), message("c")],
        [[candidate("11")], [candidate("11")], [candidate("11"), candidate("22")]],
    )

    assert report.candidates_parsed == 4
    assert report.unique_linkedin_jobs == 2
    # Three occurrences of job 11 and one of job 22: two of the four are repeats.
    assert report.duplicate_occurrences == 2


def test_duplicate_occurrences_is_exactly_parsed_minus_unique() -> None:
    report = audit(
        [message("a"), message("b")],
        [[candidate("11"), candidate("22")], [candidate("22"), candidate("33")]],
    )

    assert report.duplicate_occurrences == (
        report.candidates_parsed - report.unique_linkedin_jobs
    )
    assert report.duplicate_occurrences == 1


def test_messages_with_and_without_candidates_partition_the_window() -> None:
    report = audit(
        [message("a"), message("b"), message("c"), message("d")],
        [[candidate("11")], [], [candidate("22")], []],
    )

    assert report.messages_found == 4
    assert report.messages_with_candidates == 2
    assert report.messages_without_candidates == 2
    assert (
        report.messages_with_candidates + report.messages_without_candidates
        == report.messages_found
    )


def test_messages_without_candidates_can_be_the_whole_window() -> None:
    report = audit([message("a"), message("b")], [[], []])

    assert report.messages_with_candidates == 0
    assert report.messages_without_candidates == 2
    assert report.candidates_parsed == 0


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_location_counts_as_no_location(blank: str | None) -> None:
    report = audit(
        [message("a")],
        [[candidate("11", "Rabat, Maroc"), candidate("22", blank)]],
    )

    assert report.candidates_with_location == 1
    assert report.candidates_without_location == 1
    assert (
        report.candidates_with_location + report.candidates_without_location
        == report.candidates_parsed
    )


def test_every_candidate_can_carry_a_location() -> None:
    report = audit(
        [message("a"), message("b")],
        [[candidate("11")], [candidate("22"), candidate("33")]],
    )

    assert report.candidates_with_location == 3
    assert report.candidates_without_location == 0


# -------------------------------------------------------------- truncation ---


def test_truncated_is_false_below_the_requested_limit() -> None:
    report = audit([message("a"), message("b")], [[], []], message_limit=5)

    assert report.messages_found == 2
    assert report.truncated is False


def test_truncated_is_true_when_the_window_reaches_the_requested_limit() -> None:
    report = audit(
        [message("a"), message("b"), message("c")], [[], [], []], message_limit=3
    )

    assert report.messages_found == 3
    assert report.truncated is True


# ------------------------------------------------------------ report shape ---


def test_the_report_repeats_the_query_and_the_limit_it_was_given() -> None:
    report = audit([message()], [[candidate("11")]], query=QUERY, message_limit=7)

    assert report.query == QUERY
    assert report.message_limit == 7
    assert report.as_dict()["query"] == QUERY
    assert report.as_dict()["message_limit"] == 7


def test_the_report_is_deterministic_for_the_same_inputs() -> None:
    first = audit([message("a"), message("b")], [[candidate("11")], [candidate("11")]])
    second = audit([message("a"), message("b")], [[candidate("11")], [candidate("11")]])

    assert first == second
    assert first.as_dict() == second.as_dict()


def test_the_default_query_and_limit_are_the_declared_evaluation_defaults() -> None:
    assert DEFAULT_AUDIT_QUERY == "newer_than:30d from:jobalerts-noreply@linkedin.com"
    assert DEFAULT_AUDIT_MESSAGE_LIMIT == MAX_MESSAGE_LIMIT == 100


@pytest.mark.parametrize("limit", [0, -1, MAX_MESSAGE_LIMIT + 1, True, 1.0, "10"])
def test_the_aggregation_refuses_limits_outside_the_gmail_bound(limit: object) -> None:
    with pytest.raises(GmailIntakeAuditError):
        audit_gmail_intake([], query=QUERY, message_limit=limit)


@pytest.mark.parametrize("query", [None, "", "   ", 5])
def test_the_aggregation_refuses_an_unusable_query(query: object) -> None:
    with pytest.raises(GmailIntakeAuditError):
        audit_gmail_intake([], query=query, message_limit=10)


# -------------------------------------------------------- reuse and purity ---


def test_the_production_linkedin_parser_is_reused_not_reimplemented() -> None:
    signature = inspect.signature(audit_gmail_intake)

    assert signature.parameters["parser"].default is parse_linkedin_job_alert
    assert linkedin_gmail_audit.parse_linkedin_job_alert is parse_linkedin_job_alert
    source = inspect.getsource(linkedin_gmail_audit)
    # Parsing alert mail lives in one module, and it is not this one.
    for parsing_machinery in ("HTMLParser", "re.compile", "linkedin.com/jobs/view"):
        assert parsing_machinery not in source


def test_the_audit_reaches_no_database_and_no_persistence(tmp_path) -> None:
    for module in (linkedin_gmail_audit, audit_cli):
        source = inspect.getsource(module)
        for forbidden in ("sqlite", "libsql", "opportunity-radar.db", "INSERT", "open("):
            assert forbidden not in source

    report = audit([message()], [[candidate("11")]])

    assert report.candidates_parsed == 1
    assert not list(tmp_path.iterdir())


def test_the_audit_runs_the_real_parser_over_a_synthetic_alert() -> None:
    html = (
        '<div><a href="https://www.linkedin.com/comm/jobs/view/987654321'
        '?trk=invented-tracking">Data Engineer</a><div>Invented Co · Casablanca</div>'
        "</div>"
    )
    alert = GmailMessageCandidate(
        message_id="invented-message",
        thread_id=None,
        sender="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        subject="INVENTED SUBJECT",
        received_at=None,
        snippet=None,
        body_text=None,
        body_html=html,
    )

    report = audit_gmail_intake([alert], query=QUERY, message_limit=10)

    assert report.candidates_parsed == 1
    assert report.unique_linkedin_jobs == 1
    assert report.candidates_with_location == 1


# --------------------------------------------------------------------- CLI ---


def cli_client(*messages: GmailMessageCandidate) -> Mock:
    client = Mock()
    client.search.return_value = list(messages)
    return client


def run_cli(client: Mock, query: str, limit: int):
    with patch(
        "evaluation.morocco_pfe.cli.linkedin_gmail_audit.GmailConfiguration.from_environment"
    ), patch(
        "evaluation.morocco_pfe.cli.linkedin_gmail_audit.GmailClient.from_configuration",
        return_value=client,
    ):
        return audit_cli.run(query, limit)


def test_cli_prints_one_json_object_of_aggregate_metrics_only(capsys) -> None:
    client = cli_client(message("a"), message("b"))
    with patch(
        "evaluation.morocco_pfe.cli.linkedin_gmail_audit.audit_gmail_intake",
        wraps=lambda messages, **kwargs: audit_gmail_intake(
            messages, parser=parser_returning([candidate("11")], []), **kwargs
        ),
    ):
        report = run_cli(client, QUERY, 2)

    client.search.assert_called_once_with(QUERY, 2)
    printed = json.loads(capsys.readouterr().out)
    assert printed == report.as_dict()
    assert set(printed) == {
        "query",
        "message_limit",
        "messages_found",
        "messages_with_candidates",
        "messages_without_candidates",
        "candidates_parsed",
        "unique_linkedin_jobs",
        "duplicate_occurrences",
        "candidates_with_location",
        "candidates_without_location",
        "truncated",
    }
    assert printed["messages_found"] == 2
    assert printed["truncated"] is True
    assert all(
        isinstance(value, (int, bool)) for key, value in printed.items() if key != "query"
    )


def test_cli_output_carries_no_message_identifier_and_no_email_content(capsys) -> None:
    report = run_cli(cli_client(message("a")), QUERY, 5)
    output = capsys.readouterr().out

    for forbidden in (
        "invented-message-a",
        "invented-thread-a",
        "INVENTED SUBJECT",
        "INVENTED SNIPPET",
        "INVENTED BODY TEXT",
        "INVENTED BODY HTML",
        "message_id",
        "thread_id",
        "subject",
        "snippet",
        "body_text",
        "body_html",
        "sender",
        "access_token",
        "client_secret",
    ):
        assert forbidden not in output
    assert report.messages_found == 1


def test_cli_defaults_to_the_thirty_day_evaluation_window() -> None:
    arguments = audit_cli.build_parser().parse_args([])

    assert arguments.query == DEFAULT_AUDIT_QUERY
    assert arguments.limit == DEFAULT_AUDIT_MESSAGE_LIMIT


def test_cli_accepts_an_explicit_query_and_limit_override() -> None:
    arguments = audit_cli.build_parser().parse_args(
        ["--query", "newer_than:7d from:jobalerts-noreply@linkedin.com", "--limit", "50"]
    )

    assert arguments.query == "newer_than:7d from:jobalerts-noreply@linkedin.com"
    assert arguments.limit == 50


@pytest.mark.parametrize("value", ["0", "-1", "101", "not-an-int", "1.5"])
def test_cli_rejects_invalid_limits_using_the_gmail_bound(value: str) -> None:
    with pytest.raises(SystemExit):
        audit_cli.build_parser().parse_args(["--limit", value])


def test_cli_refuses_a_blank_query_before_touching_gmail() -> None:
    configuration = patch(
        "evaluation.morocco_pfe.cli.linkedin_gmail_audit.GmailConfiguration.from_environment"
    )
    client = patch(
        "evaluation.morocco_pfe.cli.linkedin_gmail_audit.GmailClient.from_configuration"
    )
    with configuration as loader, client as factory:
        with pytest.raises(GmailIntakeAuditError):
            audit_cli.run("  ", 10)
    loader.assert_not_called()
    factory.assert_not_called()


def test_cli_exits_nonzero_on_an_audit_error(capsys) -> None:
    with pytest.raises(SystemExit):
        audit_cli.main(["--query", "   "])
    assert "linkedin gmail audit error" in capsys.readouterr().err
