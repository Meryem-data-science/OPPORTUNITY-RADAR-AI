-- Phase 3.3A: the verified content of a profile, and the evidence behind it.
--
-- A profile fact is a claim about the person that a human has, or has not yet,
-- confirmed. "Verified" has exactly one definition here — `status = 'ACCEPTED'`
-- — so no `verified` column exists: two places to say the same thing is one
-- place too many, and the one that drifts is the one that gets believed.
--
-- Nothing factual is added to `profiles`: a fact belongs next to the evidence
-- that produced it, never in the root where it could be overwritten without a
-- trace. A correction never rewrites a value either: it creates a new fact and
-- points the old one at it, so the previous reading stays readable.

CREATE TABLE profile_facts (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    fact_type TEXT NOT NULL CHECK (
        length(trim(fact_type)) > 0 AND fact_type = trim(fact_type)
    ),
    value TEXT NOT NULL CHECK (length(trim(value)) > 0),
    normalized_value TEXT CHECK (
        normalized_value IS NULL OR length(trim(normalized_value)) > 0
    ),
    status TEXT NOT NULL CHECK (
        status IN ('PROPOSED', 'ACCEPTED', 'CORRECTED', 'REJECTED')
    ),
    replaced_by_fact_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT,
    -- A proposal has not been decided, and a decision is always dated. Only a
    -- corrected fact carries a replacement, and a corrected fact always has
    -- one: that is what makes the old value reachable instead of lost.
    CHECK (
        (status = 'PROPOSED'
            AND decided_at IS NULL
            AND replaced_by_fact_id IS NULL)
        OR (status = 'ACCEPTED'
            AND decided_at IS NOT NULL
            AND replaced_by_fact_id IS NULL)
        OR (status = 'REJECTED'
            AND decided_at IS NOT NULL
            AND replaced_by_fact_id IS NULL)
        OR (status = 'CORRECTED'
            AND decided_at IS NOT NULL
            AND replaced_by_fact_id IS NOT NULL)
    ),
    CHECK (replaced_by_fact_id IS NULL OR replaced_by_fact_id <> id),
    CHECK (decided_at IS NULL OR decided_at >= created_at),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (replaced_by_fact_id) REFERENCES profile_facts(id)
        ON DELETE RESTRICT
);

-- A replacement replaces exactly one fact, so a correction chain stays a chain.
CREATE UNIQUE INDEX idx_profile_facts_replaced_by
    ON profile_facts(replaced_by_fact_id)
    WHERE replaced_by_fact_id IS NOT NULL;

CREATE INDEX idx_profile_facts_profile_status
    ON profile_facts(profile_id, status);
CREATE INDEX idx_profile_facts_profile_type
    ON profile_facts(profile_id, fact_type);

-- Evidence, kept apart from the decision it supports. A provenance row says
-- where a value was read and by which rule; it never says the value is true,
-- and it carries no score, no confidence and no level.
CREATE TABLE profile_fact_provenance (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL,
    source_type TEXT NOT NULL CHECK (
        source_type IN ('CV', 'GITHUB', 'USER_INPUT', 'OTHER_ACCEPTED_EVIDENCE')
    ),
    -- Identifies this piece of evidence for this fact, so the same proof
    -- cannot be recorded twice.
    provenance_key TEXT NOT NULL CHECK (
        length(trim(provenance_key)) > 0 AND provenance_key = trim(provenance_key)
    ),
    source_locator TEXT CHECK (
        source_locator IS NULL OR length(trim(source_locator)) > 0
    ),
    cv_sha256 TEXT CHECK (
        cv_sha256 IS NULL
        OR (length(cv_sha256) = 64 AND cv_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    parser_version TEXT CHECK (
        parser_version IS NULL OR length(trim(parser_version)) > 0
    ),
    extractor_version TEXT CHECK (
        extractor_version IS NULL OR length(trim(extractor_version)) > 0
    ),
    candidate_fingerprint TEXT CHECK (
        candidate_fingerprint IS NULL
        OR (length(candidate_fingerprint) > 0
            AND candidate_fingerprint NOT GLOB '*[^0-9a-f]*')
    ),
    rule_id TEXT CHECK (rule_id IS NULL OR length(trim(rule_id)) > 0),
    -- Canonical JSON array of ascending page numbers, written without spaces:
    -- '[1,2]'. NULL means "this evidence has no page", never "page zero".
    page_numbers TEXT CHECK (
        page_numbers IS NULL
        OR (page_numbers GLOB '[[][0-9]*'
            AND page_numbers GLOB '*[0-9][]]'
            AND page_numbers NOT GLOB '*[^]0-9,[]*'
            AND page_numbers NOT GLOB '*,,*')
    ),
    section_type TEXT CHECK (
        section_type IS NULL OR length(trim(section_type)) > 0
    ),
    section_index INTEGER CHECK (section_index IS NULL OR section_index >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (fact_id, provenance_key),
    FOREIGN KEY (fact_id) REFERENCES profile_facts(id) ON DELETE CASCADE
);

CREATE INDEX idx_profile_fact_provenance_fact
    ON profile_fact_provenance(fact_id);
CREATE INDEX idx_profile_fact_provenance_source_type
    ON profile_fact_provenance(source_type);
