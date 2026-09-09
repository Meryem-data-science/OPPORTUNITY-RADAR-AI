-- Phase 9B.1: append-only storage for already-computed recommendation batches.
--
-- Phase 9A stays a pure function. Nothing below recomputes a score, a
-- disposition or a ranking: this migration only gives an existing
-- `RecommendationBatchResult` somewhere to live, together with the operational
-- identity that says *which* profile, over *which* matching snapshot, in
-- *which* order it was produced.
--
-- Two digests describe a stored run and they are deliberately not the same:
--
--     batch_fingerprint   content identity — Phase 9A's own digest, which holds
--                         no profile id, no opportunity id and no run id
--     run_fingerprint     operational identity — this profile, over this
--                         matching run, over these postings, in this rank order
--
-- The ranking **is** the product of Phase 9A, so `rank_position` is stored, is
-- 1-based, and is unique inside a run. `UNIQUE (run_id, rank_position)` is what
-- makes the stored order a fact rather than a hope: two rows cannot claim the
-- same place, and a reader ordering by it gets exactly the sequence the engine
-- produced.

CREATE TABLE recommendation_runs (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL CHECK (profile_id > 0),
    source_matching_run_id INTEGER NOT NULL CHECK (source_matching_run_id > 0),
    persistence_version TEXT NOT NULL CHECK (length(trim(persistence_version)) > 0 AND persistence_version = trim(persistence_version)),
    input_assembly_version TEXT NOT NULL CHECK (length(trim(input_assembly_version)) > 0 AND input_assembly_version = trim(input_assembly_version)),
    recommendation_engine_version TEXT NOT NULL CHECK (length(trim(recommendation_engine_version)) > 0 AND recommendation_engine_version = trim(recommendation_engine_version)),
    recommendation_rules_version TEXT NOT NULL CHECK (length(trim(recommendation_rules_version)) > 0 AND recommendation_rules_version = trim(recommendation_rules_version)),
    source_matching_run_fingerprint TEXT NOT NULL CHECK (length(source_matching_run_fingerprint) = 64 AND source_matching_run_fingerprint NOT GLOB '*[^0-9a-f]*'),
    batch_fingerprint TEXT NOT NULL CHECK (length(batch_fingerprint) = 64 AND batch_fingerprint NOT GLOB '*[^0-9a-f]*'),
    run_fingerprint TEXT NOT NULL UNIQUE CHECK (length(run_fingerprint) = 64 AND run_fingerprint NOT GLOB '*[^0-9a-f]*'),
    assessment_count INTEGER NOT NULL CHECK (assessment_count > 0),
    batch_payload_json TEXT NOT NULL CHECK (length(trim(batch_payload_json)) > 0 AND batch_payload_json = trim(batch_payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- The pointer `recommendation_profile_state` needs, so that "current" can
    -- only ever name a run this same profile owns.
    UNIQUE (id, profile_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    -- Composite on purpose. A plain `source_matching_run_id` reference would
    -- let one profile's recommendation cite another profile's matching run;
    -- `matching_runs` already carries `UNIQUE (id, profile_id)`, so the pair can
    -- be required and the database refuses the cross-profile row outright.
    --
    -- RESTRICT, not CASCADE, and for the same reason Phase 6 gives its own
    -- upstream references: a matching run that a recommendation cites is part
    -- of that recommendation's provenance, and deleting it would silently
    -- remove recommendation history that nobody asked to delete. Retiring a
    -- profile still works — `profiles` cascades to both sides at once.
    FOREIGN KEY (source_matching_run_id, profile_id) REFERENCES matching_runs(id, profile_id) ON DELETE RESTRICT
);

CREATE TABLE recommendation_assessments (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    opportunity_id INTEGER NOT NULL,
    -- 1-based, and the run's own ranking. Position 0 is not a position.
    rank_position INTEGER NOT NULL CHECK (rank_position > 0),
    disposition TEXT NOT NULL CHECK (disposition IN ('RECOMMENDED', 'UNCERTAIN', 'OUTSIDE_PREFERENCES', 'KNOWN_BLOCKER')),
    recommendation_score REAL CHECK (recommendation_score IS NULL OR (recommendation_score >= 0.0 AND recommendation_score <= 1.0)),
    evidence_coverage REAL NOT NULL CHECK (evidence_coverage >= 0.0 AND evidence_coverage <= 1.0),
    assessment_fingerprint TEXT NOT NULL CHECK (length(assessment_fingerprint) = 64 AND assessment_fingerprint NOT GLOB '*[^0-9a-f]*'),
    assessment_payload_json TEXT NOT NULL CHECK (length(trim(assessment_payload_json)) > 0 AND assessment_payload_json = trim(assessment_payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Phase 9A's contract, restated where it cannot be bypassed: no evidence
    -- means no score, and a score means some evidence. A stored 0.0 with no
    -- coverage would read as "measured and bad" instead of "not measured".
    CHECK ((evidence_coverage = 0.0 AND recommendation_score IS NULL) OR (evidence_coverage > 0.0 AND recommendation_score IS NOT NULL)),
    UNIQUE (run_id, opportunity_id),
    UNIQUE (run_id, rank_position),
    FOREIGN KEY (run_id) REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT
);

-- The **only** mutable pointer in this schema. Runs and their assessments are
-- append-only; what "current" means for a profile is one row, updated in place.
CREATE TABLE recommendation_profile_state (
    profile_id INTEGER PRIMARY KEY,
    -- An absent row reads as NOT_SYNCED. It is not a third value here, because
    -- "nothing has ever run" is the absence of a state, not a state.
    state TEXT NOT NULL CHECK (state IN ('READY', 'INCOMPLETE')),
    current_run_id INTEGER,
    persistence_version TEXT NOT NULL CHECK (length(trim(persistence_version)) > 0 AND persistence_version = trim(persistence_version)),
    input_assembly_version TEXT NOT NULL CHECK (length(trim(input_assembly_version)) > 0 AND input_assembly_version = trim(input_assembly_version)),
    -- Canonical JSON array. `[]` for READY; the readiness issues that stopped a
    -- synchronization go here when Phase 9B.3 introduces INCOMPLETE. The column
    -- exists now so that already-approved contract needs no second migration.
    readiness_issues_json TEXT NOT NULL CHECK (length(trim(readiness_issues_json)) > 0 AND readiness_issues_json = trim(readiness_issues_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- READY names a run; INCOMPLETE names none. There is no half state where a
    -- profile is readable and points nowhere, or is unreadable and points at a
    -- run a reader might still follow.
    CHECK ((state = 'READY' AND current_run_id IS NOT NULL) OR (state = 'INCOMPLETE' AND current_run_id IS NULL)),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (current_run_id, profile_id) REFERENCES recommendation_runs(id, profile_id) ON DELETE CASCADE
);

-- One index per access path this phase actually has, and no more. The run's own
-- ranked read (`WHERE run_id=? ORDER BY rank_position`) is already served by
-- `UNIQUE (run_id, rank_position)`, so it gets no second index here.
CREATE INDEX idx_recommendation_runs_profile ON recommendation_runs(profile_id, id);
-- The child side of the composite matching reference, so enforcing RESTRICT
-- does not scan the table.
CREATE INDEX idx_recommendation_runs_source_matching ON recommendation_runs(source_matching_run_id, profile_id);
CREATE INDEX idx_recommendation_assessments_opportunity ON recommendation_assessments(opportunity_id, run_id);
