CREATE TABLE matching_runs (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    persistence_version TEXT NOT NULL CHECK (length(trim(persistence_version)) > 0 AND persistence_version = trim(persistence_version)),
    selection_version TEXT NOT NULL CHECK (length(trim(selection_version)) > 0 AND selection_version = trim(selection_version)),
    matching_engine_version TEXT NOT NULL CHECK (length(trim(matching_engine_version)) > 0 AND matching_engine_version = trim(matching_engine_version)),
    matching_rules_version TEXT NOT NULL CHECK (length(trim(matching_rules_version)) > 0 AND matching_rules_version = trim(matching_rules_version)),
    semantic_percentile_version TEXT NOT NULL CHECK (length(trim(semantic_percentile_version)) > 0 AND semantic_percentile_version = trim(semantic_percentile_version)),
    corpus_fingerprint TEXT NOT NULL CHECK (length(corpus_fingerprint) = 64 AND corpus_fingerprint NOT GLOB '*[^0-9a-f]*'),
    tfidf_model_fingerprint TEXT NOT NULL CHECK (length(tfidf_model_fingerprint) = 64 AND tfidf_model_fingerprint NOT GLOB '*[^0-9a-f]*'),
    batch_fingerprint TEXT NOT NULL CHECK (length(batch_fingerprint) = 64 AND batch_fingerprint NOT GLOB '*[^0-9a-f]*'),
    run_fingerprint TEXT NOT NULL UNIQUE CHECK (length(run_fingerprint) = 64 AND run_fingerprint NOT GLOB '*[^0-9a-f]*'),
    assessment_count INTEGER NOT NULL CHECK (assessment_count > 0),
    batch_payload_json TEXT NOT NULL CHECK (length(trim(batch_payload_json)) > 0 AND batch_payload_json = trim(batch_payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, profile_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

CREATE TABLE matching_assessments (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    opportunity_id INTEGER NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('PRIMARY', 'UNCERTAIN', 'OUTSIDE_PREFERENCES')),
    match_quality REAL CHECK (match_quality IS NULL OR (match_quality >= 0.0 AND match_quality <= 1.0)),
    evidence_coverage REAL NOT NULL CHECK (evidence_coverage >= 0.0 AND evidence_coverage <= 1.0),
    assessment_fingerprint TEXT NOT NULL CHECK (length(assessment_fingerprint) = 64 AND assessment_fingerprint NOT GLOB '*[^0-9a-f]*'),
    assessment_payload_json TEXT NOT NULL CHECK (length(trim(assessment_payload_json)) > 0 AND assessment_payload_json = trim(assessment_payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((evidence_coverage = 0.0 AND match_quality IS NULL) OR (evidence_coverage > 0.0 AND match_quality IS NOT NULL)),
    UNIQUE (run_id, opportunity_id),
    FOREIGN KEY (run_id) REFERENCES matching_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT
);

CREATE TABLE matching_profile_state (
    profile_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('EMPTY', 'READY')),
    current_run_id INTEGER,
    persistence_version TEXT NOT NULL CHECK (length(trim(persistence_version)) > 0 AND persistence_version = trim(persistence_version)),
    selection_version TEXT NOT NULL CHECK (length(trim(selection_version)) > 0 AND selection_version = trim(selection_version)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((state = 'EMPTY' AND current_run_id IS NULL) OR (state = 'READY' AND current_run_id IS NOT NULL)),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (current_run_id, profile_id) REFERENCES matching_runs(id, profile_id) ON DELETE CASCADE
);

CREATE INDEX idx_matching_runs_profile ON matching_runs(profile_id, id);
CREATE INDEX idx_matching_assessments_run_lane_quality ON matching_assessments(run_id, lane, match_quality, evidence_coverage, opportunity_id);
CREATE INDEX idx_matching_assessments_opportunity ON matching_assessments(opportunity_id, run_id);
