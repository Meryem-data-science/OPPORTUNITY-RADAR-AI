CREATE TABLE deduplication_decisions (
    id INTEGER PRIMARY KEY,
    opportunity_a_id INTEGER NOT NULL,
    opportunity_b_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'POSSIBLE_DUPLICATE', 'CONFIRMED_DUPLICATE', 'NOT_DUPLICATE'
    )),
    audit_classification TEXT NOT NULL CHECK (audit_classification IN (
        'STRONG_CANDIDATE', 'POSSIBLE_CANDIDATE', 'WEAK_CANDIDATE'
    )),
    title_similarity REAL NOT NULL,
    organization_similarity REAL NOT NULL,
    title_normalized_exact INTEGER NOT NULL CHECK (title_normalized_exact IN (0, 1)),
    organization_normalized_exact INTEGER NOT NULL CHECK (organization_normalized_exact IN (0, 1)),
    location_signal TEXT NOT NULL,
    shared_source_url INTEGER NOT NULL CHECK (shared_source_url IN (0, 1)),
    shared_application_url INTEGER NOT NULL CHECK (shared_application_url IN (0, 1)),
    shared_canonical_url INTEGER NOT NULL CHECK (shared_canonical_url IN (0, 1)),
    date_distance_days INTEGER,
    reasons_json TEXT NOT NULL,
    first_detected_at TEXT NOT NULL,
    last_detected_at TEXT NOT NULL,
    reviewed_at TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (opportunity_a_id < opportunity_b_id),
    UNIQUE (opportunity_a_id, opportunity_b_id),
    FOREIGN KEY (opportunity_a_id) REFERENCES opportunities(id) ON DELETE RESTRICT,
    FOREIGN KEY (opportunity_b_id) REFERENCES opportunities(id) ON DELETE RESTRICT
);

CREATE INDEX idx_deduplication_decisions_status
    ON deduplication_decisions(status);
