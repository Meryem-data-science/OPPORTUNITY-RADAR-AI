"""Phase 7C.2A — aggregate intake quality of the Gmail LinkedIn job alerts.

This module answers exactly one question, and it is a narrow one:

    Of the Gmail messages a bounded, read-only Gmail search returns, how many
    does the existing LinkedIn alert parser turn into job candidates, and how
    many distinct jobs do those candidates describe?

That is **Gmail intake coverage and parser yield**. It is emphatically *not*
LinkedIn recall: a job LinkedIn never put in an alert email is invisible here,
and this audit can say nothing about it. The distinction is the whole reason
the module is named for the intake and never for "recall".

Two halves, kept apart on purpose:

    audit_gmail_intake()   pure aggregation over already-normalized messages
    evaluation/morocco_pfe/cli/linkedin_gmail_audit.py   the live Gmail/CLI edge

The pure half performs no I/O of any kind. It opens no socket, no file and no
database connection, and it holds no clock: for the same messages and the same
parser behaviour it returns the same report, field for field. The report itself
carries **counts only** — never a message id, a thread id, a subject, a
snippet, a body, a URL or an address — so it is safe to print, paste into a
review and keep.

Nothing here writes anything. There is no persistence, no JSONL dump and no
operational table: `linkedin_job_alert_email` rows in `opportunity_sources` are
the business of a later operational slice, not of this measurement.

The parser is **reused, never reimplemented**:
`services/collector/parsers/linkedin_job_alert.py` is the one definition of
what a LinkedIn alert email contains, and an audit that parsed alerts its own
way would measure a parser the radar does not run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from services.collector.gmail.client import MAX_MESSAGE_LIMIT
from services.collector.models.gmail_message import GmailMessageCandidate
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.linkedin_job_alert import parse_linkedin_job_alert

#: The query this *evaluation* uses when the caller names none. It is a
#: 30-day audit window against the address LinkedIn really sends job alerts
#: from, and it is deliberately narrower than the production query, which still
#: reads the whole of `linkedin.com` and therefore also sees newsletters.
#: Changing production is an operational decision and is not made here:
#: `config/sources.yaml` is untouched by this slice.
DEFAULT_AUDIT_QUERY = "newer_than:30d from:jobalerts-noreply@linkedin.com"

#: Default bound on the audit search. The Gmail client refuses anything above
#: its own hard maximum, and this default deliberately equals it: a smaller
#: default would silently report a truncated window as if it were the whole one.
DEFAULT_AUDIT_MESSAGE_LIMIT = MAX_MESSAGE_LIMIT

#: The parser signature the audit accepts. Only the production LinkedIn parser
#: is ever passed in real use; the parameter exists so a unit test can prove
#: the aggregation rules without inventing alert HTML for every case.
AlertParser = Callable[[GmailMessageCandidate], Sequence[OpportunityCandidate]]


class GmailIntakeAuditError(ValueError):
    """Raised when an audit is asked for on inputs that cannot be measured."""


@dataclass(frozen=True)
class GmailIntakeAuditReport:
    """Aggregate counts for one bounded Gmail window. Never a message detail.

    Every field is a count, a bound, a flag or the query the caller asked for.
    There is no timestamp, no identifier and no text drawn from any email, so
    two audits of the same messages compare equal and the report can be shown
    to anyone the repository can be shown to.
    """

    #: The Gmail query the window was read with, echoed back so a report is
    #: never separated from the question it answers.
    query: str
    #: The caller's bound on the search, not a statement about the mailbox.
    message_limit: int
    #: Messages the bounded Gmail search returned.
    messages_found: int
    #: Messages the parser turned into at least one candidate.
    messages_with_candidates: int
    #: Messages the parser turned into no candidate at all.
    messages_without_candidates: int
    #: Candidate *occurrences* across every message — the same job appearing in
    #: three alerts counts three times.
    candidates_parsed: int
    #: Distinct `source_external_id` values, i.e. distinct LinkedIn jobs.
    unique_linkedin_jobs: int
    #: `candidates_parsed - unique_linkedin_jobs`: how much of the intake is
    #: the same job arriving again, which is normal for a daily alert feed.
    duplicate_occurrences: int
    #: Candidate occurrences carrying a non-empty location.
    candidates_with_location: int
    #: Candidate occurrences whose location is null or blank.
    candidates_without_location: int
    #: True when the search returned exactly as many messages as the caller
    #: allowed. The Gmail client cannot prove that nothing lies beyond a bound
    #: it was told to stop at, so this says "the window may be incomplete" and
    #: never "there is more". False means the window was not cut short by the
    #: bound — it never means the query itself was complete.
    truncated: bool

    def as_dict(self) -> dict[str, str | int | bool]:
        """Return the report as a plain, machine-readable, privacy-safe mapping."""
        return {
            "query": self.query,
            "message_limit": self.message_limit,
            "messages_found": self.messages_found,
            "messages_with_candidates": self.messages_with_candidates,
            "messages_without_candidates": self.messages_without_candidates,
            "candidates_parsed": self.candidates_parsed,
            "unique_linkedin_jobs": self.unique_linkedin_jobs,
            "duplicate_occurrences": self.duplicate_occurrences,
            "candidates_with_location": self.candidates_with_location,
            "candidates_without_location": self.candidates_without_location,
            "truncated": self.truncated,
        }


def validate_audit_query(query: str | None) -> str:
    """Return a usable Gmail query, or refuse an empty one before any I/O."""
    if not isinstance(query, str) or not query.strip():
        raise GmailIntakeAuditError("audit query must be a non-empty Gmail query")
    return query


def validate_audit_limit(limit: int) -> int:
    """Apply the Gmail client's own bound, rather than inventing a second one."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise GmailIntakeAuditError("message limit must be an integer")
    if not 1 <= limit <= MAX_MESSAGE_LIMIT:
        raise GmailIntakeAuditError(
            f"message limit must be between 1 and {MAX_MESSAGE_LIMIT}"
        )
    return limit


def _has_location(candidate: OpportunityCandidate) -> bool:
    """A location is present only when it carries at least one visible character."""
    location = candidate.location
    return bool(location and location.strip())


def audit_gmail_intake(
    messages: Sequence[GmailMessageCandidate],
    *,
    query: str,
    message_limit: int,
    parser: AlertParser = parse_linkedin_job_alert,
) -> GmailIntakeAuditReport:
    """Aggregate parser yield over already-fetched messages. No I/O, no writes.

    `messages` are messages a caller has *already* obtained; this function
    never fetches, and never learns which mailbox they came from. It reads each
    message once, through the given parser, and keeps nothing but counts.
    """
    validated_query = validate_audit_query(query)
    validated_limit = validate_audit_limit(message_limit)

    messages_with_candidates = 0
    candidates_parsed = 0
    candidates_with_location = 0
    unique_job_ids: set[str] = set()
    for message in messages:
        candidates = parser(message)
        if candidates:
            messages_with_candidates += 1
        for candidate in candidates:
            candidates_parsed += 1
            unique_job_ids.add(candidate.source_external_id)
            if _has_location(candidate):
                candidates_with_location += 1

    messages_found = len(messages)
    unique_linkedin_jobs = len(unique_job_ids)
    return GmailIntakeAuditReport(
        query=validated_query,
        message_limit=validated_limit,
        messages_found=messages_found,
        messages_with_candidates=messages_with_candidates,
        messages_without_candidates=messages_found - messages_with_candidates,
        candidates_parsed=candidates_parsed,
        unique_linkedin_jobs=unique_linkedin_jobs,
        duplicate_occurrences=candidates_parsed - unique_linkedin_jobs,
        candidates_with_location=candidates_with_location,
        candidates_without_location=candidates_parsed - candidates_with_location,
        # A bound that was reached is a window that may have been cut short.
        truncated=messages_found == validated_limit,
    )
