CREATE TABLE notification_policy_state (
    profile_id INTEGER PRIMARY KEY,
    baseline_portfolio_run_id INTEGER NOT NULL CHECK (baseline_portfolio_run_id > 0),
    last_processed_portfolio_run_id INTEGER NOT NULL CHECK (last_processed_portfolio_run_id > 0),
    highest_seen_portfolio_run_id INTEGER NOT NULL CHECK (highest_seen_portfolio_run_id > 0),
    policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0 AND policy_version=trim(policy_version)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (highest_seen_portfolio_run_id >= last_processed_portfolio_run_id),
    CHECK (highest_seen_portfolio_run_id >= baseline_portfolio_run_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (baseline_portfolio_run_id, profile_id) REFERENCES portfolio_runs(id, profile_id) ON DELETE CASCADE,
    FOREIGN KEY (last_processed_portfolio_run_id, profile_id) REFERENCES portfolio_runs(id, profile_id) ON DELETE CASCADE,
    FOREIGN KEY (highest_seen_portfolio_run_id, profile_id) REFERENCES portfolio_runs(id, profile_id) ON DELETE CASCADE
);

CREATE TABLE notification_events (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL CHECK (profile_id > 0),
    event_type TEXT NOT NULL CHECK (event_type IN ('NEW_ACTIONABLE_OPPORTUNITY','ATTENTION_ESCALATED')),
    opportunity_id INTEGER NOT NULL CHECK (opportunity_id > 0),
    previous_portfolio_run_id INTEGER NOT NULL CHECK (previous_portfolio_run_id > 0),
    portfolio_run_id INTEGER NOT NULL CHECK (portfolio_run_id > 0),
    policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0 AND policy_version=trim(policy_version)),
    event_fingerprint TEXT NOT NULL UNIQUE CHECK (length(event_fingerprint)=64 AND event_fingerprint NOT GLOB '*[^0-9a-f]*'),
    payload_json TEXT NOT NULL CHECK (length(trim(payload_json)) > 0 AND payload_json=trim(payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (previous_portfolio_run_id <> portfolio_run_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT,
    FOREIGN KEY (previous_portfolio_run_id, profile_id) REFERENCES portfolio_runs(id, profile_id) ON DELETE RESTRICT,
    FOREIGN KEY (portfolio_run_id, profile_id) REFERENCES portfolio_runs(id, profile_id) ON DELETE RESTRICT
);

CREATE TABLE notification_outbox (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL UNIQUE CHECK (event_id > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (event_id) REFERENCES notification_events(id) ON DELETE CASCADE
);

CREATE INDEX idx_notification_events_profile ON notification_events(profile_id, id);
CREATE INDEX idx_notification_events_opportunity ON notification_events(opportunity_id);
CREATE INDEX idx_notification_events_run ON notification_events(portfolio_run_id);
CREATE INDEX idx_notification_events_type ON notification_events(event_type);
CREATE UNIQUE INDEX idx_notification_events_new_actionable_once ON notification_events(profile_id, opportunity_id) WHERE event_type = 'NEW_ACTIONABLE_OPPORTUNITY';
CREATE INDEX idx_notification_outbox_created ON notification_outbox(created_at, id);
