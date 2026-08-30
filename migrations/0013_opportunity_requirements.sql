-- Phase 3.5B: the skills and the languages a posting explicitly asks for.
--
-- `0012` read what an opportunity *is* — its kind, its education, its
-- experience, its duration, its start, its places, its work mode, its visa,
-- authorization and agreement statements. It deliberately stopped before
-- skills and languages, and said why: counting every mention of Python or SQL
-- as a requirement would fill a table with the contents of "our stack
-- includes…" paragraphs. This migration adds the tables for the reading that
-- does it properly, and the rules that fill them live in
-- `services/collector/extractors/opportunity_constraints/requirements/`.
--
-- The question this slice answers is still only one:
--
--     WHAT DOES THIS POSTING EXPLICITLY ASK FOR?
--
-- It never asks whether anybody has it. There is no profile here, no
-- comparison to one, no gap, no verdict, no score and no rank; joining the two
-- sides is Phase 3.6 and ranking is Phase 4, and neither exists. Nothing below
-- is a place to put one later by accident.
--
--     opportunities
--       +-> opportunity_constraints                       (3.5A, migration 0012)
--             +-> opportunity_skill_requirements          (0012, filled here)
--             |     +-> opportunity_skill_requirement_evidence
--             +-> opportunity_language_requirements
--             |     +-> opportunity_language_requirement_evidence
--             +-> opportunity_requirement_ambiguities
--             +-> opportunity_requirement_extraction_state
--
-- **The offer side hangs off the 3.5A projection, on purpose.** Every table
-- below points at `opportunity_constraints(opportunity_id)` rather than at
-- `opportunities(id)`, so 3.5B is structurally a continuation of 3.5A and not
-- a second, parallel reading of the same postings. Two consequences, both
-- wanted: a posting with no 3.5A projection cannot receive a 3.5B one, and a
-- 3.5A re-synchronization — which deletes and rebuilds the constraint row —
-- cascades the whole 3.5B reading away, including its extraction state, so the
-- next 3.5B run recomputes instead of trusting a state whose parent was
-- rebuilt underneath it.
--
-- **`opportunity_skill_requirements` is not recreated.** `0012` created it,
-- empty, precisely so this migration would extend one catalogue instead of
-- starting a rival. There is no `opportunity_skill_requirements_v2`, no
-- `offer_skills` and no `required_skills` here: the reserved table is the
-- table, `skills` from `0008` is the vocabulary, and the offer side and the
-- profile side name a technology the same way or they are not comparable at
-- all.
--
-- **A `skills` row is vocabulary, never a claim about a person.** It says the
-- canonical name exists, nothing more. `profile_skills` says a person holds
-- one; `opportunity_skill_requirements` says a posting asks for one; the row
-- they both point at says neither. So this migration **seeds nothing**: the
-- catalogue of terms the extractor can recognise lives in Python, and a
-- vocabulary row appears the first time a real posting is found to require
-- that term. Three hundred inserted names would be three hundred rows nobody
-- observed, and a later reader could not tell them from the ones a posting
-- actually produced.
--
-- **A mention is not a requirement.** That principle is enforced in the rules
-- rather than in the schema, but it is why these tables are small: "our stack
-- includes Python and Spark" produces no row here, "you will build pipelines
-- using Python" produces no row here, and "no prior Python experience is
-- required" produces no row here. The absence of a row is UNKNOWN, and UNKNOWN
-- is not FALSE: a posting that never named SQL has not said SQL is unwanted.
--
-- **An OR is never stored as an AND.** "Python or R required" states one
-- requirement satisfied two ways, and writing it as `Python REQUIRED` plus
-- `R REQUIRED` would turn a choice the employer offered into two obligations
-- it never wrote — and would let a later phase reject somebody the posting
-- would have accepted. v1 asserts neither, and records the refusal in
-- `opportunity_requirement_ambiguities` so that UNKNOWN is a decision on the
-- record rather than a silence.

-- Every language the posting asked for, and how well, if it said.
--
-- One row per posting and language. `REQUIRED` outranks `PREFERRED` when a
-- posting says both, and the losing mention survives as evidence carrying its
-- own `observed_requirement`, so nothing claims a preference was a demand.
--
-- `proficiency_text` is **what the posting wrote**, and only that: `B2` stays
-- `B2`, `Fluent` stays `Fluent`, `Native` stays `Native`. Nothing here maps
-- `Fluent` to `C1` or `Native` to `C2`; those equivalences are somebody's
-- convention, not the employer's sentence, and `profile_languages` in `0010`
-- keeps proficiency as written for exactly the same reason. `NULL` means the
-- posting named the language and no level — or named two incompatible levels
-- for one demand, in which case the language stays required, the level becomes
-- UNKNOWN, and an ambiguity row says so.
--
-- A language is never inferred. Not from the country, not from the city, not
-- from the currency, not from the company's name, and not from the language
-- the advertisement itself happens to be written in. A posting written in
-- English has not required English.
CREATE TABLE opportunity_language_requirements (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL,
    -- The stable key the extractor's closed registry assigns, so `French`,
    -- `français` and `francais` are one language rather than three.
    language_key TEXT NOT NULL CHECK (
        length(trim(language_key)) > 0 AND language_key = trim(language_key)
    ),
    language_name TEXT NOT NULL CHECK (
        length(trim(language_name)) > 0 AND language_name = trim(language_name)
    ),
    proficiency_text TEXT CHECK (
        proficiency_text IS NULL
        OR (length(trim(proficiency_text)) > 0
            AND proficiency_text = trim(proficiency_text)
            AND length(proficiency_text) <= 60)
    ),
    requirement TEXT NOT NULL CHECK (requirement IN ('REQUIRED', 'PREFERRED')),
    extractor_version TEXT NOT NULL CHECK (
        length(trim(extractor_version)) > 0
        AND extractor_version = trim(extractor_version)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (opportunity_id, language_key),
    FOREIGN KEY (opportunity_id)
        REFERENCES opportunity_constraints(opportunity_id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_language_requirements_opportunity
    ON opportunity_language_requirements(opportunity_id);
CREATE INDEX idx_opportunity_language_requirements_language
    ON opportunity_language_requirements(language_key);

-- Why one skill requirement was asserted: the words, the rule, and the
-- section the words sat in.
--
-- Several rows for one requirement is the normal case, not a defect. A posting
-- may name a skill under "Nice to have" and again under "Requirements", and
-- the projected row is then `REQUIRED` — but the preferred mention is still
-- something the posting said, so it keeps its own row and its own
-- `observed_requirement`. That column is what stops an audit from reading a
-- preference as a demand: the requirement above is the projection, each row
-- here is one observation, and the two are allowed to differ.
--
-- `evidence_text` is the **minimal fragment** the rule matched, capped. It is
-- a pointer into the posting, never a copy of it.
--
-- `context_heading_text` is the heading the fragment sat under, stored
-- **separately** and verbatim. A posting whose "Required Qualifications"
-- section lists "Python" is explained by two texts, not by one invented
-- sentence reading "Required Qualifications > Python": the employer wrote the
-- heading and wrote the item, and welding them together would quote a sentence
-- nobody wrote. `NULL` when the fragment sat under no heading at all.
CREATE TABLE opportunity_skill_requirement_evidence (
    id INTEGER PRIMARY KEY,
    opportunity_skill_requirement_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    -- One legal value, because v1 reads one field. The column exists so an
    -- audit never has to assume where a fragment came from, and widening it is
    -- a migration — which is exactly the review a new input deserves.
    source_field TEXT NOT NULL CHECK (source_field IN ('DESCRIPTION')),
    observed_requirement TEXT NOT NULL CHECK (
        observed_requirement IN ('REQUIRED', 'PREFERRED')
    ),
    rule_id TEXT NOT NULL CHECK (
        length(trim(rule_id)) > 0 AND rule_id = trim(rule_id)
    ),
    evidence_text TEXT NOT NULL CHECK (
        length(trim(evidence_text)) > 0 AND length(evidence_text) <= 200
    ),
    context_heading_text TEXT CHECK (
        context_heading_text IS NULL
        OR (length(trim(context_heading_text)) > 0
            AND length(context_heading_text) <= 200)
    ),
    extractor_version TEXT NOT NULL CHECK (
        length(trim(extractor_version)) > 0
        AND extractor_version = trim(extractor_version)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (opportunity_skill_requirement_id, position),
    FOREIGN KEY (opportunity_skill_requirement_id)
        REFERENCES opportunity_skill_requirements(id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_skill_requirement_evidence_requirement
    ON opportunity_skill_requirement_evidence(opportunity_skill_requirement_id);

-- The same, for languages. Same philosophy, same cap, same separation of the
-- fragment from the heading it sat under.
CREATE TABLE opportunity_language_requirement_evidence (
    id INTEGER PRIMARY KEY,
    opportunity_language_requirement_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    source_field TEXT NOT NULL CHECK (source_field IN ('DESCRIPTION')),
    observed_requirement TEXT NOT NULL CHECK (
        observed_requirement IN ('REQUIRED', 'PREFERRED')
    ),
    -- What this observation said about the level, as written. It may differ
    -- from the projected `proficiency_text` above — that is the point of
    -- keeping it.
    observed_proficiency_text TEXT CHECK (
        observed_proficiency_text IS NULL
        OR (length(trim(observed_proficiency_text)) > 0
            AND observed_proficiency_text = trim(observed_proficiency_text)
            AND length(observed_proficiency_text) <= 60)
    ),
    rule_id TEXT NOT NULL CHECK (
        length(trim(rule_id)) > 0 AND rule_id = trim(rule_id)
    ),
    evidence_text TEXT NOT NULL CHECK (
        length(trim(evidence_text)) > 0 AND length(evidence_text) <= 200
    ),
    context_heading_text TEXT CHECK (
        context_heading_text IS NULL
        OR (length(trim(context_heading_text)) > 0
            AND length(context_heading_text) <= 200)
    ),
    extractor_version TEXT NOT NULL CHECK (
        length(trim(extractor_version)) > 0
        AND extractor_version = trim(extractor_version)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (opportunity_language_requirement_id, position),
    FOREIGN KEY (opportunity_language_requirement_id)
        REFERENCES opportunity_language_requirements(id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_language_requirement_evidence_requirement
    ON opportunity_language_requirement_evidence(opportunity_language_requirement_id);

-- The requirements the extractor understood and deliberately refused to store.
--
-- Without this table, "Python or R required" and a posting that never
-- mentioned either would look identical: no rows, nothing to see. They are not
-- the same thing at all. One is silence; the other is an explicit demand that
-- this version cannot represent without corrupting it, and the difference is
-- what an operator needs in order to decide whether v2 should learn to
-- represent alternative groups.
--
-- Three reasons, and no more than the code can actually raise:
--
-- * `ALTERNATIVE_GROUP_UNSUPPORTED` — the posting offered a choice ("Python or
--   R", "one of Python, R or Julia", "English or French"). Storing each
--   alternative as its own requirement would turn OR into AND: a later phase
--   would look for somebody holding *all* of them, and reject a candidate the
--   posting would have accepted with any one. So none of them is stored;
-- * `COMPOUND_SKILL_EXPRESSION_UNSUPPORTED` — the posting wrote a slashed
--   expression naming **one** thing: `AI/ML engineering`, `ML/LLM-powered
--   system`. It is not a choice between the halves, so reporting it as one
--   would describe an offer nobody made; and it is not two demands either, so
--   storing both halves would invent two obligations out of one noun phrase.
--   The registry of such expressions is closed and lives in
--   `requirements/skill_catalog.py`; an unregistered slash — `Python/R`,
--   `TensorFlow/PyTorch` — stays the conservative reading above;
-- * `CONFLICTING_LANGUAGE_PROFICIENCY` — one posting demanded one language at
--   two incompatible levels ("English B2 required" and "English C1 required").
--   The language stays `REQUIRED`, because that part is not in dispute; only
--   the level becomes UNKNOWN, because choosing B2 or C1 would be this
--   extractor settling a contradiction in somebody's advertisement.
--
-- The rows are deduplicated per posting on everything they hold — kind, reason,
-- rule, fragment and heading — because the group's terms are deliberately not
-- stored, so two identical rows from two runs of one sentence would carry no
-- information the first does not.
--
-- This is an audit trail, not a queue and not a lesser answer: nothing reads
-- these rows to make a decision, and no phase downstream is allowed to treat
-- one as a weak requirement.
CREATE TABLE opportunity_requirement_ambiguities (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('SKILL', 'LANGUAGE')),
    reason TEXT NOT NULL CHECK (reason IN (
        'ALTERNATIVE_GROUP_UNSUPPORTED',
        'COMPOUND_SKILL_EXPRESSION_UNSUPPORTED',
        'CONFLICTING_LANGUAGE_PROFICIENCY'
    )),
    rule_id TEXT NOT NULL CHECK (
        length(trim(rule_id)) > 0 AND rule_id = trim(rule_id)
    ),
    evidence_text TEXT NOT NULL CHECK (
        length(trim(evidence_text)) > 0 AND length(evidence_text) <= 200
    ),
    context_heading_text TEXT CHECK (
        context_heading_text IS NULL
        OR (length(trim(context_heading_text)) > 0
            AND length(context_heading_text) <= 200)
    ),
    extractor_version TEXT NOT NULL CHECK (
        length(trim(extractor_version)) > 0
        AND extractor_version = trim(extractor_version)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (opportunity_id, position),
    FOREIGN KEY (opportunity_id)
        REFERENCES opportunity_constraints(opportunity_id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_requirement_ambiguities_opportunity
    ON opportunity_requirement_ambiguities(opportunity_id);
CREATE INDEX idx_opportunity_requirement_ambiguities_reason
    ON opportunity_requirement_ambiguities(reason);

-- That a posting **was read**, at which version, from which text.
--
-- This table exists because zero is an answer. A posting whose description
-- names no skill and no language produces no requirement row, no evidence row
-- and no ambiguity row — and so does a posting nobody has run the extractor
-- over yet. Without a state row those two are indistinguishable, and a
-- coverage figure computed from the requirement tables would silently count
-- unread postings as postings with nothing to require.
--
-- Idempotence is `source_fingerprint` plus `extractor_version`, the pair
-- `0004` and `0012` already use. The fingerprint is a SHA-256 over canonical
-- JSON of **exactly the fields 3.5B reads**, which in v1 is the description
-- and nothing else: the title is not read, because a title is a name and a
-- name is not a demand, and putting an unread field in the fingerprint would
-- make an edit nobody's rules looked at recompute every posting.
--
-- The row lives and dies with the 3.5A constraint row it hangs off. A 3.5A
-- re-synchronization rebuilds that row and takes this one with it, so a stale
-- state can never outlive the projection it was computed beside.
CREATE TABLE opportunity_requirement_extraction_state (
    opportunity_id INTEGER PRIMARY KEY,
    source_fingerprint TEXT NOT NULL CHECK (
        length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    extractor_version TEXT NOT NULL CHECK (
        length(trim(extractor_version)) > 0
        AND extractor_version = trim(extractor_version)
    ),
    extracted_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opportunity_id)
        REFERENCES opportunity_constraints(opportunity_id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_requirement_extraction_state_version
    ON opportunity_requirement_extraction_state(extractor_version);
