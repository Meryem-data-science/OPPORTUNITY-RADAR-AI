-- Phase 5.4A: the frozen daily Gmail digest, persisted before anything sends it.
--
-- 0019 gave a profile an append-only stream of notification *events* and 0020
-- froze the Web Push recipients of each one. Both are per-movement: they exist
-- to tell a device, immediately, that one opportunity moved. The digest is the
-- other half of that promise and deliberately not the same shape — it is one
-- message a day that summarises the *whole* audited Portfolio, and it is the
-- only place a user who never granted push permission ever sees it.
--
-- What this table stores is a digest that has already been decided. Subject,
-- text body and HTML body are rendered once, from the audited Portfolio
-- snapshot named by portfolio_run_id, and never re-rendered: a row is the
-- message as it will be sent, not a recipe for building one later. Phase 5.4B
-- adds Gmail delivery on top of exactly these columns and does not rewrite
-- this migration — the lifecycle, the claim, the attempt counter and the
-- error vocabulary are all already here, unused.
--
-- Nothing in Phase 5.4A opens a socket, reads a mailbox, or holds a Gmail
-- credential. The row is durable state; delivery is a later phase.
CREATE TABLE gmail_digest_outbox (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL CHECK (profile_id > 0),
    -- The local calendar day this digest belongs to, resolved through an
    -- explicit IANA timezone rather than through whatever the machine that ran
    -- the materialization happened to be set to. `date(x) IS x` rejects both a
    -- malformed date and an impossible one: SQLite returns NULL for the
    -- second, and `IS` — unlike `=` — makes that NULL a failure rather than an
    -- unknown that a CHECK would let through.
    digest_date TEXT NOT NULL CHECK (
        digest_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(digest_date) IS digest_date
    ),
    timezone TEXT NOT NULL CHECK (
        length(trim(timezone)) > 0 AND timezone = trim(timezone)
        AND length(timezone) <= 64
    ),
    -- Provenance: which audited Portfolio snapshot this digest was rendered
    -- from. It is recorded, never folded into the content fingerprint, because
    -- a Portfolio run that changes nothing a reader would see must not make
    -- the same digest look new.
    portfolio_run_id INTEGER NOT NULL CHECK (portfolio_run_id > 0),
    portfolio_run_fingerprint TEXT NOT NULL CHECK (
        length(portfolio_run_fingerprint) = 64
        AND portfolio_run_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    digest_version TEXT NOT NULL CHECK (
        length(trim(digest_version)) > 0 AND digest_version = trim(digest_version)
        AND length(digest_version) <= 64
    ),
    -- The identity of what the reader actually sees. Two digests with the same
    -- content fingerprint say the same thing, whatever day it is and whatever
    -- Portfolio run produced them.
    content_fingerprint TEXT NOT NULL CHECK (
        length(content_fingerprint) = 64
        AND content_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    -- Who this frozen message is for, as a fingerprint rather than an address.
    -- The plaintext recipient is configuration, supplied at send time in 5.4B
    -- and checked against this value; it is never persisted here, so a copy of
    -- this database carries no mailbox.
    recipient_fingerprint TEXT NOT NULL CHECK (
        length(recipient_fingerprint) = 64
        AND recipient_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    -- A digest with nothing in it is never materialized; it is a no-op result,
    -- not a row.
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    subject TEXT NOT NULL CHECK (
        length(trim(subject)) > 0 AND subject = trim(subject)
        AND length(subject) <= 512
    ),
    body_text TEXT NOT NULL CHECK (length(trim(body_text)) > 0),
    body_html TEXT NOT NULL CHECK (length(trim(body_html)) > 0),
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'IN_FLIGHT', 'SENT', 'PERMANENT_FAILURE')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    claim_token TEXT CHECK (
        claim_token IS NULL
        OR (length(claim_token) = 32 AND claim_token NOT GLOB '*[^0-9a-f]*')
    ),
    claimed_at TEXT,
    last_attempt_at TEXT,
    sent_at TEXT,
    gmail_message_id TEXT CHECK (
        gmail_message_id IS NULL
        OR (length(trim(gmail_message_id)) > 0
            AND gmail_message_id = trim(gmail_message_id)
            AND length(gmail_message_id) <= 128)
    ),
    last_error_code TEXT CHECK (
        last_error_code IS NULL
        OR (length(trim(last_error_code)) > 0
            AND last_error_code = trim(last_error_code)
            AND length(last_error_code) <= 64)
    ),
    last_error_category TEXT CHECK (
        last_error_category IS NULL
        OR last_error_category IN ('RETRYABLE', 'PERMANENT')
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- The three operational shapes, and the only three: waiting with a
    -- schedule and no claim, held by exactly one sender, or terminal with
    -- neither. A claim is the token and its instant together, never one alone.
    CHECK ((status = 'PENDING'
            AND next_attempt_at IS NOT NULL AND claim_token IS NULL AND claimed_at IS NULL)
        OR (status = 'IN_FLIGHT'
            AND next_attempt_at IS NULL AND claim_token IS NOT NULL AND claimed_at IS NOT NULL)
        OR (status IN ('SENT', 'PERMANENT_FAILURE')
            AND next_attempt_at IS NULL AND claim_token IS NULL AND claimed_at IS NULL)),
    -- A sent digest names the instant it was sent and the message Gmail
    -- created for it, and was attempted at least once. Nothing else may claim
    -- either fact.
    CHECK ((status = 'SENT'
            AND sent_at IS NOT NULL AND gmail_message_id IS NOT NULL AND attempt_count > 0)
        OR (status <> 'SENT' AND sent_at IS NULL AND gmail_message_id IS NULL)),
    -- An attempt and the instant it happened are one fact, so a freshly
    -- materialized PENDING row carries neither.
    CHECK ((attempt_count = 0 AND last_attempt_at IS NULL)
        OR (attempt_count > 0 AND last_attempt_at IS NOT NULL)),
    -- A success carries no error, and a terminal failure names one.
    CHECK ((status = 'SENT' AND last_error_code IS NULL AND last_error_category IS NULL)
        OR (status IN ('PENDING', 'IN_FLIGHT')
            AND ((last_error_code IS NULL) = (last_error_category IS NULL)))
        OR (status = 'PERMANENT_FAILURE'
            AND last_error_code IS NOT NULL AND last_error_category IS NOT NULL)),
    -- At most one digest per profile, per local day, per digest version. This
    -- is the anti-spam invariant the database itself holds: two materializers
    -- racing on the same day cannot both win, whatever they each decided.
    UNIQUE (profile_id, digest_date, digest_version),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

-- The daily decision: does this profile already have a digest for this day?
CREATE INDEX idx_gmail_digest_outbox_profile_day
    ON gmail_digest_outbox(profile_id, digest_version, digest_date);

-- The unchanged decision: what did this recipient last actually receive?
CREATE INDEX idx_gmail_digest_outbox_last_sent
    ON gmail_digest_outbox(profile_id, digest_version, recipient_fingerprint, status, id);

-- Phase 5.4B's drain: the digests due to be sent, oldest first.
CREATE INDEX idx_gmail_digest_outbox_due
    ON gmail_digest_outbox(status, next_attempt_at, id);
