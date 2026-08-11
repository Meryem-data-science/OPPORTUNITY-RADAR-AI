CREATE TABLE opportunity_qualifications (
    opportunity_id INTEGER PRIMARY KEY,
    qualification TEXT NOT NULL CHECK (qualification IN (
        'CORE_TARGET', 'ADJACENT_TARGET', 'OUT_OF_SCOPE', 'UNCERTAIN'
    )),
    primary_domain TEXT NOT NULL CHECK (primary_domain IN (
        'DATA_ENGINEERING', 'DATA_SCIENCE', 'MACHINE_LEARNING_AI', 'GENAI_LLM',
        'MLOPS_ML_PLATFORM', 'BI_ANALYTICS', 'DATA_QUALITY_GOVERNANCE',
        'OTHER_DATA_AI', 'NON_TARGET', 'UNKNOWN'
    )),
    opportunity_type TEXT NOT NULL CHECK (opportunity_type IN (
        'PFE', 'INTERNSHIP', 'GRADUATE', 'APPRENTICESHIP', 'JOB', 'UNKNOWN'
    )),
    employment_type TEXT NOT NULL CHECK (employment_type IN (
        'FULL_TIME', 'PART_TIME', 'CONTRACT', 'TEMPORARY', 'UNKNOWN'
    )),
    listing_quality TEXT NOT NULL CHECK (listing_quality IN (
        'NORMAL_LISTING', 'POSSIBLE_NON_JOB_PAGE', 'INSUFFICIENT_CONTENT'
    )),
    matched_domains_json TEXT NOT NULL,
    matched_title_signals_json TEXT NOT NULL,
    matched_description_signals_json TEXT NOT NULL,
    matched_exclusion_signals_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL CHECK (
        length(input_fingerprint) = 64 AND input_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    classified_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_qualifications_qualification
    ON opportunity_qualifications(qualification);
CREATE INDEX idx_opportunity_qualifications_primary_domain
    ON opportunity_qualifications(primary_domain);
CREATE INDEX idx_opportunity_qualifications_opportunity_type
    ON opportunity_qualifications(opportunity_type);
