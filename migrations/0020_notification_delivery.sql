CREATE TABLE notification_delivery_batches (
    id INTEGER PRIMARY KEY,
    outbox_id INTEGER NOT NULL UNIQUE CHECK (outbox_id > 0),
    status TEXT NOT NULL CHECK (status IN ('PENDING','NO_ACTIVE_SUBSCRIPTIONS','COMPLETED')),
    target_count INTEGER NOT NULL CHECK (target_count >= 0),
    materialized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    CHECK ((status = 'PENDING' AND target_count > 0 AND completed_at IS NULL)
        OR (status = 'NO_ACTIVE_SUBSCRIPTIONS' AND target_count = 0 AND completed_at IS NOT NULL)
        OR (status = 'COMPLETED' AND target_count > 0 AND completed_at IS NOT NULL)),
    FOREIGN KEY (outbox_id) REFERENCES notification_outbox(id) ON DELETE CASCADE
);

CREATE TABLE notification_delivery_targets (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL CHECK (batch_id > 0),
    subscription_id INTEGER NOT NULL CHECK (subscription_id > 0),
    status TEXT NOT NULL CHECK (status IN ('PENDING','SENT','EXPIRED','PERMANENT_FAILURE','FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    last_attempt_at TEXT,
    delivered_at TEXT,
    last_error_code TEXT CHECK (last_error_code IS NULL OR (length(trim(last_error_code)) > 0 AND last_error_code = trim(last_error_code) AND length(last_error_code) <= 64)),
    last_error_category TEXT CHECK (last_error_category IS NULL OR last_error_category IN ('RETRYABLE','EXPIRED','PERMANENT')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- A target is either waiting with a schedule, or terminal with none.
    CHECK ((status = 'PENDING' AND next_attempt_at IS NOT NULL)
        OR (status <> 'PENDING' AND next_attempt_at IS NULL)),
    -- Only a delivered target carries a delivery instant.
    CHECK ((status = 'SENT' AND delivered_at IS NOT NULL)
        OR (status <> 'SENT' AND delivered_at IS NULL)),
    -- An attempt and the instant it happened are one fact.
    CHECK ((attempt_count = 0 AND last_attempt_at IS NULL)
        OR (attempt_count > 0 AND last_attempt_at IS NOT NULL)),
    -- A success carries no error, and every failure names one.
    CHECK ((status = 'SENT' AND last_error_code IS NULL AND last_error_category IS NULL)
        OR (status = 'PENDING' AND ((last_error_code IS NULL) = (last_error_category IS NULL)))
        OR (status IN ('EXPIRED','PERMANENT_FAILURE','FAILED')
            AND last_error_code IS NOT NULL AND last_error_category IS NOT NULL)),
    -- A terminal target has been attempted; a never-attempted one is pending.
    CHECK (status = 'PENDING' OR attempt_count > 0),
    UNIQUE (batch_id, subscription_id),
    FOREIGN KEY (batch_id) REFERENCES notification_delivery_batches(id) ON DELETE CASCADE,
    FOREIGN KEY (subscription_id) REFERENCES push_subscriptions(id) ON DELETE CASCADE
);

CREATE INDEX idx_notification_delivery_batches_status ON notification_delivery_batches(status, id);
CREATE INDEX idx_notification_delivery_targets_due ON notification_delivery_targets(status, next_attempt_at, id);
CREATE INDEX idx_notification_delivery_targets_batch ON notification_delivery_targets(batch_id, id);
CREATE INDEX idx_notification_delivery_targets_subscription ON notification_delivery_targets(subscription_id, id);
