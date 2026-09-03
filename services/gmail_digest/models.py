"""Immutable contracts for the deterministic Phase 5.4A daily Gmail digest.

Nothing here computes a verdict. Every value a digest shows was decided by
Eligibility, Matching, Priority or Portfolio and persisted by them; this
module only names the shapes those persisted values travel in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.eligibility import GlobalStatus
from services.portfolio import PortfolioBucket
from services.priority import PriorityCategory

#: Explicit identity of this digest's content and rendering. Persisted with
#: every frozen row and folded into every content fingerprint, so a future
#: revision of the subject line, the ordering, or the body can never be
#: mistaken for this one — and can never silence itself against a digest this
#: version already sent.
DIGEST_VERSION = "gmail-digest-v1"

#: Bucket order, most deliberate choice first. A TARGET role is the one the
#: user is actually aiming at; SAFE is the fallback they already qualify for;
#: AMBITIOUS is the stretch. The order is the reading order and nothing else —
#: no bucket is scored against another.
BUCKET_ORDER: tuple[PortfolioBucket, ...] = (
    PortfolioBucket.TARGET,
    PortfolioBucket.SAFE,
    PortfolioBucket.AMBITIOUS,
)

#: Priority order inside one bucket, most urgent first. Portfolio v1 only ever
#: includes URGENT, HIGH and MEDIUM, but the order is stated in full so a
#: snapshot that carries another category is ordered rather than refused.
PRIORITY_ORDER: tuple[PriorityCategory, ...] = (
    PriorityCategory.URGENT,
    PriorityCategory.HIGH,
    PriorityCategory.MEDIUM,
    PriorityCategory.LOW,
    PriorityCategory.IGNORE,
)


class GmailDigestError(RuntimeError):
    """Raised when a digest cannot be assembled, decided, or stored safely."""


class GmailDigestStatus(StrEnum):
    """The lifecycle of one frozen digest row, as migration 0022 defines it.

    Phase 5.4A only ever writes ``PENDING``. The rest exist because 5.4B
    delivers against this table and must not need a second migration to do it.
    """

    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    SENT = "SENT"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class DigestMaterializationStatus(StrEnum):
    """What one materialization pass decided, and whether it wrote anything.

    Only ``CREATED`` writes. The other three are explicit no-ops: there is no
    placeholder row for a day with nothing to say, because a row in this table
    is a message someone will be sent.
    """

    #: One frozen PENDING digest was written.
    CREATED = "CREATED"
    #: The audited Portfolio has no INCLUDED opportunity: nothing to send.
    EMPTY = "EMPTY"
    #: This profile already has a digest for this local day and version.
    ALREADY_MATERIALIZED = "ALREADY_MATERIALIZED"
    #: A later day, but the content is what this recipient was last sent.
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True)
class DigestItem:
    """One INCLUDED opportunity, exactly as its authorities persisted it.

    ``location`` is ``None`` when the opportunity authority holds no location,
    and that absence is rendered as an absence. ``eligibility_status`` may be
    ``UNKNOWN``, which is a question the digest repeats rather than resolves.
    """

    opportunity_id: int
    title: str
    organization: str
    location: str | None
    url: str
    bucket: PortfolioBucket
    priority_category: PriorityCategory
    eligibility_status: GlobalStatus


@dataclass(frozen=True)
class DigestContent:
    """One rendered digest: the same items said twice, and their identity."""

    digest_version: str
    items: tuple[DigestItem, ...]
    subject: str
    body_text: str
    body_html: str
    content_fingerprint: str


@dataclass(frozen=True)
class DigestCandidate:
    """What a digest *would* be, together with where its content came from.

    A candidate is not a decision: it says what the audited Portfolio would
    produce today. ``content`` is ``None`` exactly when the snapshot has no
    INCLUDED opportunity at all.
    """

    profile_id: int
    portfolio_run_id: int
    portfolio_run_fingerprint: str
    digest_version: str
    content: DigestContent | None

    @property
    def item_count(self) -> int:
        return 0 if self.content is None else len(self.content.items)


@dataclass(frozen=True)
class DigestMaterializationResult:
    """The persisted consequence of one materialization pass, safe to print.

    Every field here can go to a terminal or a log: there is no body, no
    subject beyond its length, and no recipient address — only the recipient's
    fingerprint, which identifies a mailbox without disclosing one.
    """

    profile_id: int
    status: DigestMaterializationStatus
    digest_version: str
    digest_date: str
    timezone: str
    item_count: int
    content_fingerprint: str | None
    recipient_fingerprint: str
    outbox_id: int | None
    portfolio_run_id: int | None
    created: bool


@dataclass(frozen=True)
class DigestOutboxRecord:
    """One persisted digest row, described without its rendered bodies."""

    outbox_id: int
    profile_id: int
    digest_date: str
    timezone: str
    digest_version: str
    content_fingerprint: str
    recipient_fingerprint: str
    portfolio_run_id: int
    portfolio_run_fingerprint: str
    item_count: int
    status: GmailDigestStatus
    attempt_count: int
    created_at: str
    sent_at: str | None


@dataclass(frozen=True)
class DigestStatusReport:
    """A read-only view of one profile's digest outbox, safe to print."""

    profile_id: int
    digest_version: str
    total_count: int
    status_counts: tuple[tuple[str, int], ...]
    latest: DigestOutboxRecord | None
    latest_sent: DigestOutboxRecord | None
