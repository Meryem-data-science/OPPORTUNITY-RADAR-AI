CREATE TABLE source_runs (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
    pages_checked INTEGER CHECK (pages_checked IS NULL OR pages_checked >= 0),
    items_found INTEGER CHECK (items_found IS NULL OR items_found >= 0),
    new_items INTEGER CHECK (new_items IS NULL OR new_items >= 0),
    relevant_items INTEGER CHECK (relevant_items IS NULL OR relevant_items >= 0),
    http_status INTEGER CHECK (
        http_status IS NULL OR (http_status >= 100 AND http_status <= 599)
    ),
    error_type TEXT,
    error_message TEXT,
    parser_version TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (finished_at >= started_at),
    CHECK (status = 'SUCCESS' OR error_type IS NOT NULL),
    CHECK (status = 'FAILED' OR (error_type IS NULL AND error_message IS NULL)),
    CHECK (new_items IS NULL OR items_found IS NULL OR new_items <= items_found),
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX idx_source_runs_source_started
    ON source_runs(source_id, started_at);
