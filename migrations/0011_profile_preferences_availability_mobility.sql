-- Phase 3.4C: the availability, mobility, preferences and career objectives a
-- person stated themselves, projected.
--
-- Same contract as `0009` and `0010`, and one deliberate difference in where
-- the claims come from. `0009`/`0010` project what a **document** said about a
-- past that already happened; the four tables below project what a **person**
-- said about what they want next. Nothing in this slice can be read out of a
-- CV, and nothing in it is:
--
--   * an availability date is never computed from a CV period, from a diploma
--     year or from the clock. "Available now" and "available from 2026-09-01"
--     are two things somebody typed, not two readings of a document;
--   * a mobility is never deduced from a postal address, a city, a country or
--     a past employer. Living somewhere is not a statement about where one is
--     willing to go, and a nationality is not a statement about visas;
--   * a work mode is never deduced from a past remote experience, and a
--     preferred domain is never deduced from a skill, a project or a section
--     title. Having done data engineering is not wanting to do it next;
--   * a convention (internship agreement) is never assumed available because
--     somebody is a student, and a visa sponsorship need is never assumed from
--     a nationality or a location;
--   * a career objective is never taken from the professional title on a CV.
--
-- So `profile_facts` stays the only source of truth, "verified" keeps its
-- single definition — `status = 'ACCEPTED'` — and, on top of that, every fact
-- these tables project carries `USER_INPUT` provenance: the person stated it.
-- The audit chain stays a chain rather than a copy:
--
--     profile_availability       → profile_facts → profile_fact_provenance
--     profile_mobility           → profile_facts → profile_fact_provenance
--     profile_preferences        → profile_facts → profile_fact_provenance
--     profile_career_objectives  → profile_facts → profile_fact_provenance
--
-- so no table below repeats `source_type`, `provenance_key` or any other
-- evidence column: those live once, in `profile_fact_provenance`, and
-- `fact_id` is the way to them. The only thing a row records on its own behalf
-- is which version of the input contract encoded it.
--
-- **Absence is UNKNOWN, and UNKNOWN has no row.** There is no seeded row, no
-- default row and no "unknown" row created for a profile that stated nothing:
-- a profile with no accepted AVAILABILITY fact simply has no
-- `profile_availability` row, and that is the whole representation of "we do
-- not know". A missing preference is never stored, printed or read as `FALSE`:
-- "this person does not want remote" and "this person never said" are
-- different claims, and only the first is knowledge.
--
-- `convention_status` and `visa_sponsorship_required` do carry an explicit
-- `UNKNOWN` value, and that is not a contradiction: those two exist inside a
-- preference the person *did* state, where "I don't know yet whether I can get
-- a convention" is itself an answer they gave.
--
-- There is no `verified`, `confidence`, `score`, `match_score`, `eligibility`
-- or `ranking` column, for the same reason there is none in `0007`..`0010`.
-- This slice is the profile side of Phases 3.5/3.6 and implements neither: no
-- opportunity constraint is stored here, no profile value is compared to an
-- offer, and nothing below is an eligibility decision.
--
-- Every `*_json` column holds **canonical** JSON — object keys sorted, no
-- insignificant whitespace — as produced by
-- `services/digital_twin/preferences/codec.py`. Canonical means two identical
-- statements are byte-identical, which is what lets the service tell "the same
-- value again" (a no-op) from "a new value" (a correction).
--
-- No row of these tables is ever updated by the projection, so none carries
-- `updated_at`: reconciliation inserts what is missing, deletes what has
-- stopped being justified, and replaces — delete then insert — a row the
-- current facts would now write differently.
--
-- `idx_profile_facts_id_profile`, the composite identity the foreign keys
-- below need, already exists: `0009` created it. `0011` alters no existing
-- table, creates no index on one, and changes nothing about the Phase 3.4A,
-- 3.4B1 or 3.4B2 projections.

-- The one ACCEPTED AVAILABILITY fact of a profile, decoded.
--
-- Singleton by construction: `profile_id` is the primary key, so a profile has
-- at most one availability. Two accepted AVAILABILITY facts is an integrity
-- failure the repository refuses out loud rather than resolving by picking the
-- most recent one.
CREATE TABLE profile_availability (
    profile_id INTEGER PRIMARY KEY,
    -- UNIQUE across the whole table: one accepted fact is projected exactly
    -- once, and never for two profiles.
    fact_id INTEGER NOT NULL UNIQUE,
    availability_status TEXT NOT NULL CHECK (
        availability_status IN ('AVAILABLE_NOW', 'AVAILABLE_FROM')
    ),
    -- A real ISO date the person typed, `YYYY-MM-DD`, or nothing at all. Never
    -- a date derived from a CV, a school year or the current time.
    available_from TEXT CHECK (
        available_from IS NULL
        OR available_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    ),
    input_version TEXT NOT NULL CHECK (
        length(trim(input_version)) > 0 AND input_version = trim(input_version)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- The two forms are exclusive: "available now" carries no date, and
    -- "available from" is meaningless without one. Neither is ever completed
    -- with a plausible value.
    CHECK (
        (availability_status = 'AVAILABLE_NOW' AND available_from IS NULL)
        OR (availability_status = 'AVAILABLE_FROM' AND available_from IS NOT NULL)
    ),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    -- Composite on purpose: the fact must exist *and* belong to this profile.
    -- A projection cannot point at another profile's fact.
    FOREIGN KEY (fact_id, profile_id)
        REFERENCES profile_facts(id, profile_id) ON DELETE CASCADE
);

-- The one ACCEPTED MOBILITY fact of a profile, decoded.
--
-- `locations_json` is a canonical JSON array of the locations the person typed,
-- kept verbatim beyond a trim: no geocoding, no country inferred from a city,
-- no city inferred from a country, no region expanded into the cities it
-- contains, and no case folding. `RESTRICTED` with an empty array is refused —
-- a restriction naming nowhere would restrict nothing — while `OPEN` with an
-- empty array is the ordinary form.
CREATE TABLE profile_mobility (
    profile_id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL UNIQUE,
    mobility_scope TEXT NOT NULL CHECK (mobility_scope IN ('OPEN', 'RESTRICTED')),
    locations_json TEXT NOT NULL CHECK (json_valid(locations_json)),
    input_version TEXT NOT NULL CHECK (
        length(trim(input_version)) > 0 AND input_version = trim(input_version)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        mobility_scope = 'OPEN'
        OR json_array_length(locations_json) > 0
    ),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (fact_id, profile_id)
        REFERENCES profile_facts(id, profile_id) ON DELETE CASCADE
);

-- The one ACCEPTED PREFERENCE fact of a profile, decoded.
--
-- Two of the six fields are closed registries — `opportunity_types` and
-- `work_modes` — and both must name at least one value: a preference that
-- wants no kind of opportunity and no way of working is not a preference. The
-- other four are the person's own words or an explicit `UNKNOWN`, and all four
-- may legitimately be empty or unknown.
--
-- `preferred_domains_json` is never populated from skills, projects or CV
-- sections, and `constraints_json` is never populated from anything at all:
-- both hold exactly the strings the person typed.
CREATE TABLE profile_preferences (
    profile_id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL UNIQUE,
    opportunity_types_json TEXT NOT NULL CHECK (
        json_valid(opportunity_types_json)
        AND json_array_length(opportunity_types_json) > 0
    ),
    work_modes_json TEXT NOT NULL CHECK (
        json_valid(work_modes_json) AND json_array_length(work_modes_json) > 0
    ),
    preferred_domains_json TEXT NOT NULL CHECK (json_valid(preferred_domains_json)),
    convention_status TEXT NOT NULL CHECK (
        convention_status IN ('UNKNOWN', 'AVAILABLE', 'NOT_AVAILABLE')
    ),
    visa_sponsorship_required TEXT NOT NULL CHECK (
        visa_sponsorship_required IN ('UNKNOWN', 'YES', 'NO')
    ),
    constraints_json TEXT NOT NULL CHECK (json_valid(constraints_json)),
    input_version TEXT NOT NULL CHECK (
        length(trim(input_version)) > 0 AND input_version = trim(input_version)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (fact_id, profile_id)
        REFERENCES profile_facts(id, profile_id) ON DELETE CASCADE
);

-- The one ACCEPTED CAREER_OBJECTIVE fact of a profile, decoded.
--
-- `objectives_json` holds at least one objective, in the person's own words. A
-- profile that stated none has no row here, which is UNKNOWN — never an empty
-- array, and never an objective read off a CV title.
CREATE TABLE profile_career_objectives (
    profile_id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL UNIQUE,
    objectives_json TEXT NOT NULL CHECK (
        json_valid(objectives_json) AND json_array_length(objectives_json) > 0
    ),
    input_version TEXT NOT NULL CHECK (
        length(trim(input_version)) > 0 AND input_version = trim(input_version)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (fact_id, profile_id)
        REFERENCES profile_facts(id, profile_id) ON DELETE CASCADE
);
