CREATE TABLE portfolio_runs (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL CHECK (profile_id > 0),
    priority_run_id INTEGER NOT NULL CHECK (priority_run_id > 0),
    matching_run_id INTEGER NOT NULL CHECK (matching_run_id > 0),
    persistence_version TEXT NOT NULL,
    input_assembly_version TEXT NOT NULL,
    portfolio_engine_version TEXT NOT NULL,
    portfolio_rules_version TEXT NOT NULL,
    priority_run_fingerprint TEXT NOT NULL CHECK (length(priority_run_fingerprint)=64 AND priority_run_fingerprint NOT GLOB '*[^0-9a-f]*'),
    matching_run_fingerprint TEXT NOT NULL CHECK (length(matching_run_fingerprint)=64 AND matching_run_fingerprint NOT GLOB '*[^0-9a-f]*'),
    run_fingerprint TEXT NOT NULL UNIQUE CHECK (length(run_fingerprint)=64 AND run_fingerprint NOT GLOB '*[^0-9a-f]*'),
    assessment_count INTEGER NOT NULL CHECK (assessment_count > 0),
    included_count INTEGER NOT NULL CHECK (included_count >= 0),
    excluded_count INTEGER NOT NULL CHECK (excluded_count >= 0),
    safe_count INTEGER NOT NULL CHECK (safe_count >= 0),
    target_count INTEGER NOT NULL CHECK (target_count >= 0),
    ambitious_count INTEGER NOT NULL CHECK (ambitious_count >= 0),
    run_payload_json TEXT NOT NULL CHECK (length(trim(run_payload_json)) > 0 AND run_payload_json=trim(run_payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (included_count + excluded_count = assessment_count),
    CHECK (safe_count + target_count + ambitious_count = included_count),
    UNIQUE (id, profile_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (priority_run_id) REFERENCES priority_runs(id) ON DELETE RESTRICT,
    FOREIGN KEY (matching_run_id) REFERENCES matching_runs(id) ON DELETE RESTRICT
);

CREATE TABLE portfolio_assessments (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    opportunity_id INTEGER NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('INCLUDED','EXCLUDED')),
    bucket TEXT CHECK (bucket IS NULL OR bucket IN ('SAFE','TARGET','AMBITIOUS')),
    priority_category TEXT CHECK (priority_category IS NULL OR priority_category IN ('URGENT','HIGH','MEDIUM','LOW','IGNORE')),
    eligibility_status TEXT NOT NULL CHECK (eligibility_status IN ('ELIGIBLE','UNKNOWN','INELIGIBLE')),
    matching_lane TEXT NOT NULL CHECK (matching_lane IN ('PRIMARY','UNCERTAIN','OUTSIDE_PREFERENCES')),
    required_skill_score REAL CHECK (required_skill_score IS NULL OR (required_skill_score >= 0.0 AND required_skill_score <= 1.0)),
    required_skill_matched_count INTEGER NOT NULL CHECK (required_skill_matched_count >= 0),
    required_skill_total_count INTEGER NOT NULL CHECK (required_skill_total_count >= 0),
    safe_cap_applied INTEGER NOT NULL CHECK (safe_cap_applied IN (0,1)),
    assessment_fingerprint TEXT NOT NULL CHECK (length(assessment_fingerprint)=64 AND assessment_fingerprint NOT GLOB '*[^0-9a-f]*'),
    assessment_payload_json TEXT NOT NULL CHECK (length(trim(assessment_payload_json)) > 0 AND assessment_payload_json=trim(assessment_payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (required_skill_matched_count <= required_skill_total_count),
    CHECK ((required_skill_total_count=0 AND required_skill_matched_count=0 AND required_skill_score IS NULL) OR (required_skill_total_count>0 AND required_skill_score IS NOT NULL)),
    CHECK ((disposition='INCLUDED' AND bucket IS NOT NULL) OR (disposition='EXCLUDED' AND bucket IS NULL)),
    UNIQUE (run_id, opportunity_id),
    FOREIGN KEY (run_id) REFERENCES portfolio_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT
);

CREATE TABLE portfolio_profile_state (
    profile_id INTEGER PRIMARY KEY,
    current_run_id INTEGER NOT NULL,
    persistence_version TEXT NOT NULL,
    input_assembly_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (current_run_id, profile_id) REFERENCES portfolio_runs(id, profile_id) ON DELETE CASCADE
);

CREATE INDEX idx_portfolio_runs_profile ON portfolio_runs(profile_id);
CREATE INDEX idx_portfolio_runs_priority ON portfolio_runs(priority_run_id);
CREATE INDEX idx_portfolio_runs_matching ON portfolio_runs(matching_run_id);
CREATE INDEX idx_portfolio_assessments_run ON portfolio_assessments(run_id);
CREATE INDEX idx_portfolio_assessments_opportunity ON portfolio_assessments(opportunity_id);
CREATE INDEX idx_portfolio_assessments_disposition ON portfolio_assessments(disposition);
CREATE INDEX idx_portfolio_assessments_bucket ON portfolio_assessments(bucket);
