-- Phase 3.4B1: verified EXPERIENCE and PROJECT facts, projected onto rows.
--
-- Nothing here is a new source of truth. `profile_facts` stays the only place
-- a claim about the person is decided, "verified" keeps its single definition
-- — `status = 'ACCEPTED'` — and every row below is derived from a fact that
-- already carries it. The projection reads those facts; it never writes one.
--
-- The audit chain is a chain rather than a copy, exactly as in `0008`:
--
--     profile_experiences → profile_facts → profile_fact_provenance
--     profile_projects    → profile_facts → profile_fact_provenance
--
-- so neither table repeats `source_type`, `cv_sha256`, `parser_version`,
-- `extractor_version` or `provenance_key`: those live once, in
-- `profile_fact_provenance`, and `fact_id` is the way to them. The only thing
-- a row records on its own behalf is what the projection itself decided —
-- which structurer version, which structuring rule.
--
-- Every text column but the two version columns is nullable, and that is the
-- point of the slice: **an absence stays NULL**. No role is deduced from a
-- description, no employer from a sentence, no date from a school year, no
-- duration, no seniority and no level. A fact whose wording carries no
-- structure the closed rules recognise still gets its row, with `NULL`
-- everywhere and the fallback rule id recorded, so no accepted fact is ever
-- dropped silently.
--
-- There is no `verified`, `confidence`, `score`, `proficiency`, `seniority` or
-- `match_score` column, for the same reason there is none in `0007` or `0008`.
--
-- No row of these tables is ever updated by the projection, so neither carries
-- `updated_at`: reconciliation inserts what is missing, deletes what has
-- stopped being justified, and replaces — delete then insert — a row the
-- current rules would now write differently.

-- The composite identity the two foreign keys below need. It is an index, not
-- a column: `0009` alters no existing table. It exists so that "a projection
-- may not point at another profile's fact" is a database rule rather than an
-- application convention — see the composite foreign keys underneath.
CREATE UNIQUE INDEX idx_profile_facts_id_profile
    ON profile_facts(id, profile_id);

-- One ACCEPTED EXPERIENCE fact, read by one structuring rule.
CREATE TABLE profile_experiences (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    -- UNIQUE across the whole table: one accepted fact is projected exactly
    -- once. A fact appearing twice would mean the projection read one claim
    -- two ways, which is a defect rather than corroboration.
    fact_id INTEGER NOT NULL UNIQUE,
    -- Written by the document, never deduced. NULL when the wording carries no
    -- structure the closed rules recognise.
    role_text TEXT CHECK (
        role_text IS NULL
        OR (length(trim(role_text)) > 0 AND role_text = trim(role_text))
    ),
    organization_text TEXT CHECK (
        organization_text IS NULL
        OR (length(trim(organization_text)) > 0
            AND organization_text = trim(organization_text))
    ),
    -- The temporal fragment the document wrote, kept verbatim. Never a
    -- computed date, never a duration, never a converted school year.
    period_text TEXT CHECK (
        period_text IS NULL
        OR (length(trim(period_text)) > 0 AND period_text = trim(period_text))
    ),
    description_text TEXT CHECK (
        description_text IS NULL
        OR (length(trim(description_text)) > 0
            AND description_text = trim(description_text))
    ),
    structurer_version TEXT NOT NULL CHECK (
        length(trim(structurer_version)) > 0
        AND structurer_version = trim(structurer_version)
    ),
    structuring_rule_id TEXT NOT NULL CHECK (
        length(trim(structuring_rule_id)) > 0
        AND structuring_rule_id = trim(structuring_rule_id)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    -- Composite on purpose: the fact must exist *and* belong to this profile.
    -- A projection cannot point at another profile's fact.
    FOREIGN KEY (fact_id, profile_id)
        REFERENCES profile_facts(id, profile_id) ON DELETE CASCADE
);

CREATE INDEX idx_profile_experiences_profile ON profile_experiences(profile_id);

-- One ACCEPTED PROJECT fact, read by one structuring rule.
CREATE TABLE profile_projects (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL UNIQUE,
    title_text TEXT CHECK (
        title_text IS NULL
        OR (length(trim(title_text)) > 0 AND title_text = trim(title_text))
    ),
    period_text TEXT CHECK (
        period_text IS NULL
        OR (length(trim(period_text)) > 0 AND period_text = trim(period_text))
    ),
    description_text TEXT CHECK (
        description_text IS NULL
        OR (length(trim(description_text)) > 0
            AND description_text = trim(description_text))
    ),
    structurer_version TEXT NOT NULL CHECK (
        length(trim(structurer_version)) > 0
        AND structurer_version = trim(structurer_version)
    ),
    structuring_rule_id TEXT NOT NULL CHECK (
        length(trim(structuring_rule_id)) > 0
        AND structuring_rule_id = trim(structuring_rule_id)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (fact_id, profile_id)
        REFERENCES profile_facts(id, profile_id) ON DELETE CASCADE
);

CREATE INDEX idx_profile_projects_profile ON profile_projects(profile_id);
