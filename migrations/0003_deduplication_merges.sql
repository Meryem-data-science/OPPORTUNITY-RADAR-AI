CREATE TABLE deduplication_merges (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL,
    canonical_opportunity_id INTEGER NOT NULL,
    merged_opportunity_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('APPLIED', 'ROLLED_BACK')),
    canonical_before_json TEXT NOT NULL,
    canonical_after_json TEXT NOT NULL,
    merged_before_json TEXT NOT NULL,
    fields_filled_json TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    rolled_back_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (canonical_opportunity_id != merged_opportunity_id),
    FOREIGN KEY (decision_id) REFERENCES deduplication_decisions(id) ON DELETE RESTRICT,
    FOREIGN KEY (canonical_opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT,
    FOREIGN KEY (merged_opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_deduplication_merges_applied_decision
    ON deduplication_merges(decision_id) WHERE status = 'APPLIED';
CREATE UNIQUE INDEX uq_deduplication_merges_applied_merged
    ON deduplication_merges(merged_opportunity_id) WHERE status = 'APPLIED';
CREATE INDEX idx_deduplication_merges_canonical_status
    ON deduplication_merges(canonical_opportunity_id, status);

CREATE TABLE deduplication_merge_source_moves (
    id INTEGER PRIMARY KEY,
    merge_id INTEGER NOT NULL,
    opportunity_source_id INTEGER NOT NULL,
    from_opportunity_id INTEGER NOT NULL,
    to_opportunity_id INTEGER NOT NULL,
    moved_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (from_opportunity_id != to_opportunity_id),
    UNIQUE (merge_id, opportunity_source_id),
    FOREIGN KEY (merge_id) REFERENCES deduplication_merges(id) ON DELETE RESTRICT,
    FOREIGN KEY (opportunity_source_id) REFERENCES opportunity_sources(id) ON DELETE RESTRICT,
    FOREIGN KEY (from_opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT,
    FOREIGN KEY (to_opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT
);

CREATE INDEX idx_deduplication_merge_source_moves_merge
    ON deduplication_merge_source_moves(merge_id);
