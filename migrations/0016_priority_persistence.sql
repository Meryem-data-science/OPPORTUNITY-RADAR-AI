CREATE TABLE priority_runs (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL CHECK (profile_id > 0),
    user_id INTEGER NOT NULL CHECK (user_id > 0),
    matching_run_id INTEGER NOT NULL CHECK (matching_run_id > 0),
    persistence_version TEXT NOT NULL CHECK (length(trim(persistence_version)) > 0 AND persistence_version = trim(persistence_version)),
    input_assembly_version TEXT NOT NULL CHECK (length(trim(input_assembly_version)) > 0 AND input_assembly_version = trim(input_assembly_version)),
    priority_engine_version TEXT NOT NULL CHECK (length(trim(priority_engine_version)) > 0 AND priority_engine_version = trim(priority_engine_version)),
    priority_rules_version TEXT NOT NULL CHECK (length(trim(priority_rules_version)) > 0 AND priority_rules_version = trim(priority_rules_version)),
    freshness_version TEXT NOT NULL CHECK (length(trim(freshness_version)) > 0 AND freshness_version = trim(freshness_version)),
    quality_version TEXT NOT NULL CHECK (length(trim(quality_version)) > 0 AND quality_version = trim(quality_version)),
    evaluation_date TEXT NOT NULL CHECK (evaluation_date = date(evaluation_date) AND length(evaluation_date) = 10),
    matching_run_fingerprint TEXT NOT NULL CHECK (length(matching_run_fingerprint) = 64 AND matching_run_fingerprint NOT GLOB '*[^0-9a-f]*'),
    run_fingerprint TEXT NOT NULL UNIQUE CHECK (length(run_fingerprint) = 64 AND run_fingerprint NOT GLOB '*[^0-9a-f]*'),
    assessment_count INTEGER NOT NULL CHECK (assessment_count > 0),
    run_payload_json TEXT NOT NULL CHECK (length(trim(run_payload_json)) > 0 AND run_payload_json = trim(run_payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, profile_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (matching_run_id) REFERENCES matching_runs(id) ON DELETE CASCADE
);

CREATE TABLE priority_assessments (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    opportunity_id INTEGER NOT NULL,
    priority_score REAL CHECK (priority_score IS NULL OR (priority_score >= 0.0 AND priority_score <= 1.0)),
    priority_evidence_coverage REAL NOT NULL CHECK (priority_evidence_coverage >= 0.0 AND priority_evidence_coverage <= 1.0),
    priority_category TEXT CHECK (priority_category IS NULL OR priority_category IN ('URGENT','HIGH','MEDIUM','LOW','IGNORE')),
    eligibility_status TEXT NOT NULL CHECK (eligibility_status IN ('ELIGIBLE','UNKNOWN','INELIGIBLE')),
    matching_lane TEXT NOT NULL CHECK (matching_lane IN ('PRIMARY','UNCERTAIN','OUTSIDE_PREFERENCES')),
    assessment_fingerprint TEXT NOT NULL CHECK (length(assessment_fingerprint) = 64 AND assessment_fingerprint NOT GLOB '*[^0-9a-f]*'),
    assessment_payload_json TEXT NOT NULL CHECK (length(trim(assessment_payload_json)) > 0 AND assessment_payload_json = trim(assessment_payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, opportunity_id),
    FOREIGN KEY (run_id) REFERENCES priority_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT
);

CREATE TABLE priority_profile_state (
    profile_id INTEGER PRIMARY KEY,
    current_run_id INTEGER NOT NULL,
    persistence_version TEXT NOT NULL CHECK (length(trim(persistence_version)) > 0 AND persistence_version = trim(persistence_version)),
    input_assembly_version TEXT NOT NULL CHECK (length(trim(input_assembly_version)) > 0 AND input_assembly_version = trim(input_assembly_version)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (current_run_id, profile_id) REFERENCES priority_runs(id, profile_id) ON DELETE CASCADE
);

CREATE INDEX idx_priority_runs_profile ON priority_runs(profile_id, id);
CREATE INDEX idx_priority_assessments_run_category_score ON priority_assessments(run_id, priority_category, priority_score, opportunity_id);
CREATE INDEX idx_priority_assessments_opportunity ON priority_assessments(opportunity_id, run_id);
