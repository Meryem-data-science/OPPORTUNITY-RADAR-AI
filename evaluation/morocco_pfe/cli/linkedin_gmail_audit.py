"""Phase 7C.2A — run the Gmail LinkedIn intake audit against the live mailbox.

This is the I/O edge, and it is deliberately thin: it reads a bounded window of
mail through the existing read-only Gmail client, hands the normalized messages
to the pure aggregation in `evaluation.morocco_pfe.linkedin_gmail_audit`, and
prints one JSON object of counts.

    python -m evaluation.morocco_pfe.cli.linkedin_gmail_audit
    python -m evaluation.morocco_pfe.cli.linkedin_gmail_audit \
        --query "newer_than:7d from:jobalerts-noreply@linkedin.com" --limit 50

Read-only against Gmail, and precise about what that does and does not cover:

* the Gmail scope stays `gmail.readonly`, enforced by the existing client — no
  credential mechanism is added, weakened or duplicated here;
* no Gmail message or label is modified, deleted or exported;
* the audit persists no Gmail data: no database write, no JSONL, no email dump,
  no evaluation artefact and no business artefact of any kind;
* what is printed is aggregate counts and the caller's own query — never a
  message id, thread id, subject, snippet, body or address.

One local file can still change, and it is the OAuth client's own, not the
audit's: authorizing this command may cause
`services/collector/gmail/client.py` to tighten the permissions of
`GMAIL_TOKEN_PATH` and to rewrite it when a token is refreshed or newly
granted. That is the credential maintenance every Gmail entry point in this
repository already delegates to, and this slice delegates to it unchanged
rather than claiming it does not happen.

The account read is the project's own dedicated Gmail account, configured by
`GMAIL_OAUTH_CLIENT_SECRET_PATH` and `GMAIL_TOKEN_PATH`. No other mailbox is in
scope for this project, and nothing here can reach one that is not configured.

What the numbers mean is documented in `evaluation/morocco_pfe/README.md`; the
short version is that they measure Gmail intake and parser yield, and say
nothing about how much of LinkedIn ever reaches an alert email.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from evaluation.morocco_pfe.linkedin_gmail_audit import (
    DEFAULT_AUDIT_MESSAGE_LIMIT,
    DEFAULT_AUDIT_QUERY,
    GmailIntakeAuditError,
    GmailIntakeAuditReport,
    audit_gmail_intake,
    validate_audit_limit,
    validate_audit_query,
)
from services.collector.cli.gmail_probe import bounded_limit
from services.collector.gmail import (
    GmailApiError,
    GmailAuthenticationError,
    GmailClient,
    GmailConfiguration,
    GmailConfigurationError,
    GmailPayloadError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Gmail LinkedIn job-alert intake read-only; prints aggregate "
            "counts only. This measures parser yield, not LinkedIn recall."
        )
    )
    parser.add_argument("--query", default=DEFAULT_AUDIT_QUERY)
    # `bounded_limit` is the Gmail probe's own bound: one definition of the
    # maximum, applied before argparse hands us anything.
    parser.add_argument("--limit", type=bounded_limit, default=DEFAULT_AUDIT_MESSAGE_LIMIT)
    return parser


def run(query: str | None, limit: int) -> GmailIntakeAuditReport:
    """Read the bounded window, aggregate it, print the report, return it."""
    # Refuse an unusable request before touching OAuth: a bad query should
    # never be a reason to open a browser or refresh a token.
    validated_query = validate_audit_query(query)
    validated_limit = validate_audit_limit(limit)
    client = GmailClient.from_configuration(GmailConfiguration.from_environment())
    messages = client.search(validated_query, validated_limit)
    report = audit_gmail_intake(
        messages, query=validated_query, message_limit=validated_limit
    )
    print(json.dumps(report.as_dict(), ensure_ascii=False))
    return report


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        run(args.query, args.limit)
    except (
        GmailIntakeAuditError,
        GmailConfigurationError,
        GmailAuthenticationError,
        GmailApiError,
        GmailPayloadError,
    ) as error:
        print(f"linkedin gmail audit error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
