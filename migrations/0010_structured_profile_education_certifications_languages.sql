-- Phase 3.4B2: verified EDUCATION, CERTIFICATION and LANGUAGE facts, projected.
--
-- Same contract as `0009`, extended to three more fact types. Nothing here is a
-- new source of truth: `profile_facts` stays the only place a claim about the
-- person is decided, "verified" keeps its single definition — `status =
-- 'ACCEPTED'` — and every row below is derived from a fact that already carries
-- it. The projection reads those facts; it never writes one.
--
-- The audit chain stays a chain rather than a copy:
--
--     profile_educations     → profile_facts → profile_fact_provenance
--     profile_certifications → profile_facts → profile_fact_provenance
--     profile_languages      → profile_facts → profile_fact_provenance
--
-- so no table below repeats `source_type`, `cv_sha256`, `parser_version`,
-- `extractor_version` or `provenance_key`: those live once, in
-- `profile_fact_provenance`, and `fact_id` is the way to them. The only thing a
-- row records on its own behalf is what the projection itself decided — which
-- structurer version, which structuring rule.
--
-- Every text column but the two version columns is nullable, and that is the
-- whole point of the slice: **an absence stays NULL**. In particular:
--
--   * a diploma is never deduced from an institution, and an institution is
--     never deduced from a diploma. Punctuation proves that segments exist; it
--     never proves what they are about, so a pipe-delimited education line is
--     read as "diploma then school" only when a closed, documented rule can
--     tell the two apart — and when it cannot, the period alone is kept and
--     both stay NULL;
--   * no `Bac+N` and no study level is computed from the word "Master", and no
--     calendar date is computed from a school year;
--   * a certification is never assumed obtained. A stated intention —
--     "préparation à", "objectif" — is not an obtained certification, no issuer
--     is invented, and no obtention or expiry date is invented;
--   * a language level is stored exactly as written. "courant" never becomes
--     `C1`, "fluent" never becomes `C2`, no CEFR level is computed, and no
--     language is deduced from the fact that a project was written in English.
--
-- There is no `verified`, `confidence`, `score`, `seniority`, `match_score` or
-- `inferred_level` column, for the same reason there is none in `0007`, `0008`
-- or `0009`.
--
-- No row of these tables is ever updated by the projection, so none carries
-- `updated_at`: reconciliation inserts what is missing, deletes what has
-- stopped being justified, and replaces — delete then insert — a row the
-- current rules would now write differently.
--
-- `idx_profile_facts_id_profile`, the composite identity the foreign keys below
-- need, already exists: `0009` created it. `0010` alters no existing table and
-- creates no index on one.

-- One ACCEPTED EDUCATION fact, read by one structuring rule.
CREATE TABLE profile_educations (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    -- UNIQUE across the whole table: one accepted fact is projected exactly
    -- once. A fact appearing twice would mean the projection read one claim
    -- two ways, which is a defect rather than corroboration.
    fact_id INTEGER NOT NULL UNIQUE,
    -- Written by the document, never deduced. NULL — and it often will be —
    -- whenever the closed rules cannot tell the school from the programme
    -- without choosing, because choosing would be inventing.
    institution_text TEXT CHECK (
        institution_text IS NULL
        OR (length(trim(institution_text)) > 0
            AND institution_text = trim(institution_text))
    ),
    program_text TEXT CHECK (
        program_text IS NULL
        OR (length(trim(program_text)) > 0 AND program_text = trim(program_text))
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

CREATE INDEX idx_profile_educations_profile ON profile_educations(profile_id);

-- One ACCEPTED CERTIFICATION fact, read by one structuring rule.
--
-- The columns say what a document wrote, never what a person holds: there is no
-- `obtained_at`, no `expires_at` and no `credential_id`, because a CV line that
-- names a certification does not state when it was obtained, whether it was
-- obtained at all, or when it lapses.
CREATE TABLE profile_certifications (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL UNIQUE,
    certification_text TEXT CHECK (
        certification_text IS NULL
        OR (length(trim(certification_text)) > 0
            AND certification_text = trim(certification_text))
    ),
    issuer_text TEXT CHECK (
        issuer_text IS NULL
        OR (length(trim(issuer_text)) > 0 AND issuer_text = trim(issuer_text))
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

CREATE INDEX idx_profile_certifications_profile
    ON profile_certifications(profile_id);

-- One ACCEPTED LANGUAGE fact, read by one structuring rule.
--
-- `proficiency_text` is the wording the document used, stored verbatim. It is
-- deliberately **not** a CEFR column: there is no `cefr_level`, no enumeration
-- and no check constraining it to `A1..C2`, because storing "courant" as `C1`
-- would be a translation nobody made. Comparison against the closed registry
-- happens in the structurer; what is stored is what was written.
CREATE TABLE profile_languages (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL UNIQUE,
    language_text TEXT CHECK (
        language_text IS NULL
        OR (length(trim(language_text)) > 0
            AND language_text = trim(language_text))
    ),
    proficiency_text TEXT CHECK (
        proficiency_text IS NULL
        OR (length(trim(proficiency_text)) > 0
            AND proficiency_text = trim(proficiency_text))
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

CREATE INDEX idx_profile_languages_profile ON profile_languages(profile_id);
