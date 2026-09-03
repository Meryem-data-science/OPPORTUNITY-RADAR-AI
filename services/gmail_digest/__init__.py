"""Phase 5.4A: the deterministic daily Gmail digest, frozen into SQLite.

This package assembles one email a day from the audited Portfolio snapshot,
renders it, gives it an identity, and stores it. It does not send it. There is
no OAuth flow here, no Gmail API client, no SMTP, no mailbox reading, no
scheduler, and no credential of any kind — Gmail delivery is Phase 5.4B, and
it will read exactly the rows this package writes.
"""

from .assembly import (
    ALLOWED_URL_SCHEMES,
    build_digest_items,
    digest_sort_key,
    read_digest_opportunity_facts,
    validate_digest_url,
)
from .fingerprint import (
    canonical_digest_payload,
    digest_content_fingerprint,
    normalize_recipient,
    recipient_fingerprint,
)
from .local_day import LocalDay, resolve_local_day, resolve_timezone
from .materialize import (
    audited_current_run,
    build_digest_candidate,
    materialize_daily_digest,
    now_timestamp,
)
from .models import (
    BUCKET_ORDER,
    DIGEST_VERSION,
    PRIORITY_ORDER,
    DigestCandidate,
    DigestContent,
    DigestItem,
    DigestMaterializationResult,
    DigestMaterializationStatus,
    DigestOutboxRecord,
    DigestStatusReport,
    GmailDigestError,
    GmailDigestStatus,
)
from .persistence import (
    DIGEST_OUTBOX_TABLE,
    REQUIRED_MIGRATION_VERSION,
    insert_frozen_digest,
    read_digest_for_day,
    read_digest_status,
    read_latest_sent_digest,
)
from .rendering import (
    BUCKET_LABELS,
    UNKNOWN_LOCATION_LABEL,
    render_digest,
    render_html,
    render_subject,
    render_text,
)

__all__ = [
    "ALLOWED_URL_SCHEMES",
    "BUCKET_LABELS",
    "BUCKET_ORDER",
    "DIGEST_OUTBOX_TABLE",
    "DIGEST_VERSION",
    "PRIORITY_ORDER",
    "REQUIRED_MIGRATION_VERSION",
    "UNKNOWN_LOCATION_LABEL",
    "DigestCandidate",
    "DigestContent",
    "DigestItem",
    "DigestMaterializationResult",
    "DigestMaterializationStatus",
    "DigestOutboxRecord",
    "DigestStatusReport",
    "GmailDigestError",
    "GmailDigestStatus",
    "LocalDay",
    "audited_current_run",
    "build_digest_candidate",
    "build_digest_items",
    "canonical_digest_payload",
    "digest_content_fingerprint",
    "digest_sort_key",
    "insert_frozen_digest",
    "materialize_daily_digest",
    "normalize_recipient",
    "now_timestamp",
    "read_digest_for_day",
    "read_digest_opportunity_facts",
    "read_digest_status",
    "read_latest_sent_digest",
    "recipient_fingerprint",
    "render_digest",
    "render_html",
    "render_subject",
    "render_text",
    "resolve_local_day",
    "resolve_timezone",
    "validate_digest_url",
]
