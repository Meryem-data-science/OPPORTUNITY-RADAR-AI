CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    category TEXT,
    country TEXT,
    frequency_minutes INTEGER CHECK (frequency_minutes IS NULL OR frequency_minutes > 0),
    status TEXT NOT NULL,
    last_run_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE opportunities (
    id INTEGER PRIMARY KEY,
    canonical_title TEXT NOT NULL,
    organization TEXT NOT NULL,
    opportunity_type TEXT,
    employment_type TEXT,
    location TEXT,
    country TEXT,
    remote_type TEXT,
    description TEXT,
    published_at TEXT,
    deadline TEXT,
    discovered_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    application_url TEXT,
    canonical_url TEXT,
    status TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    relevance_score REAL,
    eligibility_score REAL,
    match_score REAL,
    priority_score REAL,
    interview_potential_score REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE opportunity_sources (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    application_url TEXT,
    canonical_url TEXT,
    discovered_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunities_canonical_url ON opportunities(canonical_url);
CREATE INDEX idx_opportunities_organization ON opportunities(organization);
CREATE INDEX idx_opportunities_discovered_at ON opportunities(discovered_at);
CREATE INDEX idx_opportunity_sources_source_id ON opportunity_sources(source_id);
