CREATE TABLE push_subscriptions (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL CHECK (profile_id > 0),
    endpoint TEXT NOT NULL UNIQUE CHECK (length(trim(endpoint)) > 0 AND endpoint = trim(endpoint) AND endpoint GLOB 'https://?*' AND length(endpoint) <= 2048),
    p256dh TEXT NOT NULL CHECK (length(trim(p256dh)) > 0 AND p256dh = trim(p256dh) AND length(p256dh) <= 256),
    auth TEXT NOT NULL CHECK (length(trim(auth)) > 0 AND auth = trim(auth) AND length(auth) <= 256),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','REVOKED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT,
    CHECK ((status = 'ACTIVE' AND revoked_at IS NULL) OR (status = 'REVOKED' AND revoked_at IS NOT NULL)),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

CREATE INDEX idx_push_subscriptions_profile_status ON push_subscriptions(profile_id, status, id);
