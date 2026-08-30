-- Phase 3.5A: what a posting itself requires, extracted and projected.
--
-- The whole slice answers one question — **what does this opportunity ask
-- for?** — and deliberately cannot answer any other. Nothing below mentions a
-- person, a profile, a fit, a verdict or a rank, because a constraint is a
-- property of the posting and a decision is a property of nobody yet.
-- Eligibility is Phase 3.6 and matching is Phase 4; neither exists here, and
-- no column, index or table underneath is a place to put one later by
-- accident. (`opportunities` does carry `eligibility_score`, `match_score` and
-- `priority_score` from `0001`; those are Phase 1 leftovers, this migration
-- adds nothing like them and nothing in Phase 3.5A writes to them.)
--
-- The projection is derived data. `opportunities` stays the source: no row of
-- it is updated, no column is added to it, and a constraint row is deleted and
-- rebuilt whenever the text it was read from changes.
--
--     opportunities
--         +-> opportunity_constraints            (one row per read posting)
--               +-> opportunity_constraint_locations
--               +-> opportunity_education_requirements
--               +-> opportunity_constraint_evidence
--               +-> opportunity_constraint_conflicts
--
-- **NULL is UNKNOWN, and it is the only spelling of it.** Every scalar below
-- is nullable, and a `NULL` means one thing everywhere: no closed rule found
-- an explicit statement. The Python enums carry an `UNKNOWN` member so the
-- extractor's output is total, and `repository.py` maps that member to `NULL`
-- on the way in and back on the way out — one representation in the database,
-- so a `CHECK` can enumerate only the affirmative values and a wrong string is
-- refused by SQLite rather than stored.
--
-- **UNKNOWN is never FALSE.** A posting that says nothing about visas has not
-- refused to sponsor. A posting with an address has not required attendance. A
-- posting written in English has not required English. A posting for an
-- internship has not required a school agreement, has not stated a duration
-- and has not said its candidates are students. None of those inferences
-- exists in this schema or in the code that fills it, and a reader of these
-- tables must not add one: `visa_sponsorship IS NULL` means the posting was
-- silent, and silence is not a policy.
--
-- **A contradiction is recorded, not resolved.** When two strong readings of
-- one posting disagree — "fully remote" three lines above "fully on-site" —
-- the scalar stays `NULL` and a row lands in `opportunity_constraint_conflicts`
-- naming the values and the rules. Choosing between them would turn a defect
-- in the posting into a fact about it.
--
-- There is no `confidence`, no `score`, no `weight`, no `rank` and no
-- `match_score` column. A closed rule either found explicit evidence or it did
-- not, and a number between the two would only invite a threshold.
--
-- Idempotence is `source_fingerprint` plus `extractor_version`, the same
-- pattern `0004` already uses for qualification: the fingerprint is a SHA-256
-- over canonical JSON of exactly the fields the extractor reads, so the same
-- posting read twice writes nothing, an edited description recomputes, and a
-- new extractor version recomputes even when the text is identical.

-- One posting, read once, at one version.
CREATE TABLE opportunity_constraints (
    opportunity_id INTEGER PRIMARY KEY,

    -- The shared registry of Phase 3.4C, so the profile side and the offer
    -- side speak one vocabulary. NULL when the posting named no type the
    -- closed rules recognise, or when two fields of one tier disagreed.
    opportunity_type TEXT CHECK (opportunity_type IS NULL OR opportunity_type IN (
        'PFA', 'PFE', 'SUMMER_INTERNSHIP', 'PRE_HIRE_INTERNSHIP',
        'ALTERNANCE', 'INTERNSHIP', 'FIRST_JOB', 'JUNIOR_ROLE'
    )),

    -- Months, always, because postings write both years and months and one
    -- unit is one comparison later. A bound is NULL when the posting gave it
    -- no bound: "3+ years" has a floor and no ceiling, and a ceiling invented
    -- here would be a rejection nobody wrote.
    experience_min_months INTEGER CHECK (
        experience_min_months IS NULL OR experience_min_months >= 0
    ),
    experience_max_months INTEGER CHECK (
        experience_max_months IS NULL OR experience_max_months >= 0
    ),
    experience_obligation TEXT CHECK (
        experience_obligation IS NULL
        OR experience_obligation IN ('REQUIRED', 'PREFERRED')
    ),

    -- How long the work lasts. Never derived from the kind of posting: the
    -- word "internship" carries no number.
    duration_min_months INTEGER CHECK (
        duration_min_months IS NULL OR duration_min_months > 0
    ),
    duration_max_months INTEGER CHECK (
        duration_max_months IS NULL OR duration_max_months > 0
    ),

    -- When it begins, at the precision it was written, and no finer. A month
    -- with no year keeps `start_year` NULL: the current year is never
    -- borrowed, from the clock or from anywhere else.
    start_year INTEGER CHECK (start_year IS NULL OR start_year BETWEEN 1900 AND 2999),
    start_month INTEGER CHECK (start_month IS NULL OR start_month BETWEEN 1 AND 12),
    start_day INTEGER CHECK (start_day IS NULL OR start_day BETWEEN 1 AND 31),
    start_precision TEXT CHECK (
        start_precision IS NULL OR start_precision IN ('DATE', 'MONTH', 'YEAR')
    ),

    work_mode TEXT CHECK (
        work_mode IS NULL OR work_mode IN ('ON_SITE', 'HYBRID', 'REMOTE')
    ),

    -- Three different claims, three columns, and no rule turning one into
    -- another. `visa_sponsorship` is what the employer offers to do;
    -- `work_authorization` is what the applicant must already hold; and what
    -- a *person* needs is `profile_preferences.visa_sponsorship_required`, on
    -- the other side of the database entirely.
    visa_sponsorship TEXT CHECK (
        visa_sponsorship IS NULL OR visa_sponsorship IN ('AVAILABLE', 'NOT_AVAILABLE')
    ),
    work_authorization TEXT CHECK (
        work_authorization IS NULL OR work_authorization IN ('REQUIRED', 'NOT_REQUIRED')
    ),
    convention_requirement TEXT CHECK (
        convention_requirement IS NULL
        OR convention_requirement IN ('REQUIRED', 'NOT_REQUIRED')
    ),

    extractor_version TEXT NOT NULL CHECK (
        length(trim(extractor_version)) > 0
        AND extractor_version = trim(extractor_version)
    ),
    source_fingerprint TEXT NOT NULL CHECK (
        length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    extracted_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- The precision says which parts are present, so a row cannot claim a
    -- precision it does not hold. A DATE needs all three; a MONTH needs a
    -- month and no day; a YEAR needs a year and nothing else.
    CHECK (
        (start_precision IS NULL
            AND start_year IS NULL AND start_month IS NULL AND start_day IS NULL)
        OR (start_precision = 'DATE'
            AND start_year IS NOT NULL AND start_month IS NOT NULL
            AND start_day IS NOT NULL)
        OR (start_precision = 'MONTH'
            AND start_month IS NOT NULL AND start_day IS NULL)
        OR (start_precision = 'YEAR'
            AND start_year IS NOT NULL AND start_month IS NULL AND start_day IS NULL)
    ),
    CHECK (
        experience_min_months IS NULL OR experience_max_months IS NULL
        OR experience_min_months <= experience_max_months
    ),
    CHECK (
        duration_min_months IS NULL OR duration_max_months IS NULL
        OR duration_min_months <= duration_max_months
    ),

    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_constraints_type
    ON opportunity_constraints(opportunity_type);
CREATE INDEX idx_opportunity_constraints_version
    ON opportunity_constraints(extractor_version);

-- The places the posting itself named, in the order it named them.
--
-- These are the collected `location` and `country` fields, trimmed and exactly
-- deduplicated. Nothing is geocoded, no country is deduced from a city, no
-- city from a country, and no region is expanded into the places inside it.
-- A row here is a string an employer wrote, not a point on a map.
CREATE TABLE opportunity_constraint_locations (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    location_text TEXT NOT NULL CHECK (
        length(trim(location_text)) > 0 AND location_text = trim(location_text)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (opportunity_id, position),
    UNIQUE (opportunity_id, location_text),
    FOREIGN KEY (opportunity_id)
        REFERENCES opportunity_constraints(opportunity_id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_constraint_locations_opportunity
    ON opportunity_constraint_locations(opportunity_id);

-- Every education level the posting named, and whether it named it as a floor.
--
-- Several rows are several accepted levels, not a contradiction: a posting may
-- genuinely say "Bac+5 or Master". `requirement_mode` keeps "Bac+3 minimum"
-- apart from "Bac+3", because reading the second as the first would admit
-- levels the posting never mentioned.
--
-- `BAC_PLUS_5` and `MASTER` are separate levels on purpose. They are written
-- by different postings in different systems, and merging them here would be
-- an equivalence nobody stated.
CREATE TABLE opportunity_education_requirements (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    level TEXT NOT NULL CHECK (level IN (
        'BAC_PLUS_2', 'BAC_PLUS_3', 'BAC_PLUS_4', 'BAC_PLUS_5',
        'BACHELOR', 'MASTER', 'ENGINEERING_DEGREE', 'PHD'
    )),
    requirement_mode TEXT NOT NULL CHECK (requirement_mode IN ('MINIMUM', 'EXACT')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (opportunity_id, position),
    UNIQUE (opportunity_id, level, requirement_mode),
    FOREIGN KEY (opportunity_id)
        REFERENCES opportunity_constraints(opportunity_id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_education_requirements_opportunity
    ON opportunity_education_requirements(opportunity_id);

-- Why each value was asserted: the rule, the field, and the words.
--
-- `evidence_text` is the **minimal fragment** the rule matched, capped by the
-- extractor. It is a pointer into the posting, not a copy of it: storing whole
-- descriptions here would duplicate the source, inflate the projection and
-- make every audit read text it does not need.
CREATE TABLE opportunity_constraint_evidence (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    constraint_kind TEXT NOT NULL CHECK (constraint_kind IN (
        'OPPORTUNITY_TYPE', 'EDUCATION', 'EXPERIENCE', 'DURATION', 'START',
        'LOCATION', 'WORK_MODE', 'VISA_SPONSORSHIP', 'WORK_AUTHORIZATION',
        'CONVENTION'
    )),
    source_field TEXT NOT NULL CHECK (source_field IN (
        'TITLE', 'DESCRIPTION', 'LOCATION', 'COUNTRY', 'REMOTE_TYPE',
        'QUALIFICATION_TYPE'
    )),
    rule_id TEXT NOT NULL CHECK (
        length(trim(rule_id)) > 0 AND rule_id = trim(rule_id)
    ),
    evidence_text TEXT NOT NULL CHECK (
        length(trim(evidence_text)) > 0 AND length(evidence_text) <= 200
    ),
    normalized_value TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (opportunity_id, position),
    FOREIGN KEY (opportunity_id)
        REFERENCES opportunity_constraints(opportunity_id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_constraint_evidence_opportunity
    ON opportunity_constraint_evidence(opportunity_id);
CREATE INDEX idx_opportunity_constraint_evidence_kind
    ON opportunity_constraint_evidence(constraint_kind);

-- The readings that disagreed, and were therefore not used.
--
-- A row here is the reason a scalar above is NULL. It is not a lesser answer
-- and not a tie to be broken later: it says the posting contradicted itself,
-- names the incompatible values and the rules that produced them, and leaves
-- the field unasserted.
--
-- **A conflict is identified by its slot, not by its kind.** The two are not
-- the same thing: a kind is the subject a reader browses by, a slot is the
-- thing that can actually disagree with itself. `EXPERIENCE` holds two — how
-- much experience a posting wants, and whether it insists — and one posting
-- can contradict itself about both at once:
--
--     "Minimum 3 years of experience required."
--     "At least 5 years of experience preferred."
--
-- That is two contradictions with two answers. Keying the row by kind would
-- refuse the second one outright, and merging them would put `36-` beside
-- `REQUIRED` in a single list of "conflicting values", which describes nothing.
-- `constraint_kind` stays for browsing and is derived from the slot in code, so
-- the two can never drift apart.
--
-- `EDUCATION` and `LOCATION` are absent from the slot list: several levels or
-- several places are several answers, never a disagreement, so neither can
-- appear here at all.
CREATE TABLE opportunity_constraint_conflicts (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL,
    constraint_slot TEXT NOT NULL CHECK (constraint_slot IN (
        'OPPORTUNITY_TYPE', 'EXPERIENCE_BOUNDS', 'EXPERIENCE_OBLIGATION',
        'DURATION', 'START', 'WORK_MODE', 'VISA_SPONSORSHIP',
        'WORK_AUTHORIZATION', 'CONVENTION'
    )),
    constraint_kind TEXT NOT NULL CHECK (constraint_kind IN (
        'OPPORTUNITY_TYPE', 'EDUCATION', 'EXPERIENCE', 'DURATION', 'START',
        'LOCATION', 'WORK_MODE', 'VISA_SPONSORSHIP', 'WORK_AUTHORIZATION',
        'CONVENTION'
    )),
    -- Canonical JSON arrays, sorted by the extractor, so one contradiction has
    -- one representation.
    conflicting_values_json TEXT NOT NULL CHECK (
        json_valid(conflicting_values_json)
        AND json_array_length(conflicting_values_json) >= 2
    ),
    rule_ids_json TEXT NOT NULL CHECK (
        json_valid(rule_ids_json) AND json_array_length(rule_ids_json) >= 1
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (opportunity_id, constraint_slot),
    FOREIGN KEY (opportunity_id)
        REFERENCES opportunity_constraints(opportunity_id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_constraint_conflicts_opportunity
    ON opportunity_constraint_conflicts(opportunity_id);
CREATE INDEX idx_opportunity_constraint_conflicts_kind
    ON opportunity_constraint_conflicts(constraint_kind);

-- Reserved for Phase 3.5B: the skills a posting requires or prefers.
--
-- It is created empty, and **Phase 3.5A never writes a row into it**. It
-- exists now for one reason: to pin the offer side to the `skills` vocabulary
-- `0008` already created, so that 3.5B extends one catalogue instead of
-- starting a second, incompatible one. A test asserts that a 3.5A
-- synchronization leaves it empty.
--
-- Extraction is deliberately not attempted yet. Counting every mention of
-- Python or SQL as a requirement would fill this table with the contents of
-- "our stack includes…" paragraphs and with skills a posting explicitly said
-- it would teach, and a requirement nobody wrote is exactly what this whole
-- slice refuses to invent. Doing it properly needs sentence-level context,
-- which is 3.5B's subject.
--
-- Language requirements have deliberately **no** table yet, for the same
-- reason turned the other way: 3.5A cannot fill one, and an empty table with
-- no writer is schema nobody can trust. `0013` adds it together with the rules
-- that populate it.
CREATE TABLE opportunity_skill_requirements (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    requirement TEXT NOT NULL CHECK (requirement IN ('REQUIRED', 'PREFERRED')),
    extractor_version TEXT NOT NULL CHECK (
        length(trim(extractor_version)) > 0
        AND extractor_version = trim(extractor_version)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (opportunity_id, skill_id),
    FOREIGN KEY (opportunity_id)
        REFERENCES opportunity_constraints(opportunity_id) ON DELETE CASCADE,
    -- `RESTRICT`, like `profile_skills` in `0008`, and for the same reason:
    -- `skills` is shared vocabulary, so a term an offer still requires cannot
    -- be deleted out from under it. `CASCADE` here would let a vocabulary
    -- cleanup silently drop a requirement a posting stated — the constraint
    -- would disappear without anybody deciding it should. Removing the
    -- posting is what removes its requirements, through the cascade above.
    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE RESTRICT
);

CREATE INDEX idx_opportunity_skill_requirements_opportunity
    ON opportunity_skill_requirements(opportunity_id);
CREATE INDEX idx_opportunity_skill_requirements_skill
    ON opportunity_skill_requirements(skill_id);
