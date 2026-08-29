-- Phase 3.4A: verified SKILL facts, projected onto normalized skills.
--
-- Nothing here is a new source of truth. `profile_facts` stays the only place
-- a claim about the person is decided, "verified" keeps its single definition
-- — `status = 'ACCEPTED'` — and every row below is derived from facts that
-- already carry it. The projection reads those facts; it never writes one.
--
-- The audit chain is deliberately a chain rather than a copy:
--
--     profile_skills → profile_skill_evidence → profile_facts
--                                             → profile_fact_provenance
--
-- so `profile_skill_evidence` records only what the projection itself decided
-- (which normalizer version, which normalization rule) and points at the fact
-- for everything else. Duplicating the provenance columns here would create a
-- second answer to "where did this come from", and the copy that drifts is
-- the copy that gets believed.
--
-- No level of any kind exists in this schema: no level, proficiency, score,
-- confidence or seniority column, and no occurrence count that could be read
-- as one. Two facts proving the same skill mean two pieces of evidence, never
-- "more" of that skill. There is no `verified` column either, for the same
-- reason there is none in `0007`.
--
-- No row is ever updated by this slice, so no table carries `updated_at`: a
-- projection either holds a row or does not, and reconciliation inserts what
-- is missing and deletes what has stopped being justified.

-- The canonical identity of a skill, shared by every profile. A row here is
-- vocabulary, not a claim: it says "this canonical name exists under this
-- key", never "somebody has this skill". Only `profile_skills` says that.
CREATE TABLE skills (
    id INTEGER PRIMARY KEY,
    -- The conservative technical key the normalizer computes: NFKC, trimmed,
    -- inner space runs collapsed, casefolded. Punctuation, accents and symbols
    -- survive it, so `c`, `c++` and `c#` are three keys and three skills.
    canonical_key TEXT NOT NULL UNIQUE CHECK (
        length(trim(canonical_key)) > 0 AND canonical_key = trim(canonical_key)
    ),
    canonical_name TEXT NOT NULL CHECK (
        length(trim(canonical_name)) > 0 AND canonical_name = trim(canonical_name)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One profile holds one skill, or does not. The association carries nothing
-- else: what justifies it lives in `profile_skill_evidence`, and how good the
-- person is at it lives nowhere, because no evidence in this project states it.
CREATE TABLE profile_skills (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- One profile holds one skill at most once. Two verified facts naming the
    -- same skill are two evidences of this single row, never a second row.
    UNIQUE (profile_id, skill_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    -- A skill still held by a profile cannot be deleted from the vocabulary;
    -- an unused vocabulary row may stay, and means nothing on its own.
    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE RESTRICT
);

CREATE INDEX idx_profile_skills_profile ON profile_skills(profile_id);
CREATE INDEX idx_profile_skills_skill ON profile_skills(skill_id);

-- Why one profile holds one skill: the ACCEPTED fact that justifies it, plus
-- the version and the rule of the normalization that read it that way.
CREATE TABLE profile_skill_evidence (
    id INTEGER PRIMARY KEY,
    profile_skill_id INTEGER NOT NULL,
    -- UNIQUE, and not per profile_skill: one verified fact justifies exactly
    -- one skill of one profile. A fact appearing under two skills would mean
    -- the projection read one claim two ways, which is a defect, not evidence.
    fact_id INTEGER NOT NULL UNIQUE,
    normalizer_version TEXT NOT NULL CHECK (
        length(trim(normalizer_version)) > 0
        AND normalizer_version = trim(normalizer_version)
    ),
    normalization_rule_id TEXT NOT NULL CHECK (
        length(trim(normalization_rule_id)) > 0
        AND normalization_rule_id = trim(normalization_rule_id)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (profile_skill_id) REFERENCES profile_skills(id)
        ON DELETE CASCADE,
    -- Evidence cannot outlive the fact it points at, and it never keeps a copy
    -- of it: the value, the status and the provenance stay on the fact side.
    FOREIGN KEY (fact_id) REFERENCES profile_facts(id) ON DELETE CASCADE
);

CREATE INDEX idx_profile_skill_evidence_profile_skill
    ON profile_skill_evidence(profile_skill_id);
