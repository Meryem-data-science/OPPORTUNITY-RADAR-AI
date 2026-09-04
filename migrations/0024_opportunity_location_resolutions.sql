-- Phase 7A.1: where a posting's own words place it, resolved to a country.
--
-- The product this radar serves has narrowed: **Morocco only**. That is a
-- decision about one person's search, and it changes nothing about what a
-- posting says. So this migration adds exactly one thing — a derived,
-- versioned, auditable reading of the location strings `0012` already stores —
-- and refuses to add the other half of the question:
--
--     opportunity_constraint_locations   what the employer wrote (evidence)
--         +-> opportunity_location_resolutions   which country that names
--
-- **No verdict is stored here.** MATCH / OUT_OF_TARGET / UNKNOWN is a
-- statement about one *profile* against one posting, and a profile's declared
-- mobility can change this afternoon. Persisting a verdict would freeze a
-- comparison whose left-hand side moved, so the verdict is computed on demand
-- by `services/geography/evaluator.py` and stored nowhere. There is therefore
-- no `profile_id`, no `eligibility`, no `match_score`, no `priority` and no
-- `rank` column below, and none of them belongs here later.
--
-- **`opportunity_constraint_locations` is untouched.** It keeps the raw
-- strings — `Capital Tower, Boulevard Mly Youssef, Casablanca, Maroc` — exactly
-- as they were collected, because they are the evidence this projection is
-- read *from*. No row of it is updated, no column is added to it, and it never
-- becomes a canonical/ISO table: a resolution that overwrote its own source
-- could no longer be audited against it.
--
-- **UNKNOWN is never OUT_OF_TARGET.** A segment nobody could resolve is a
-- question, not a contradiction. `resolution_status` has three values and only
-- one of them asserts a country; the other two assert that this row cannot be
-- used to place the posting anywhere. A reader of this table must not read
-- `country_code IS NULL` as "not in Morocco".
--
-- **Nothing here says how the work is done.** `opportunity_constraints.work_mode`
-- already answers remote / hybrid / on-site, and an address is not evidence of
-- attendance. This migration adds no second work-mode resolver and no column
-- that could become one.
--
-- One source string may name several places — `Doha, Qatar; London, UK; Dubai,
-- UAE` is a real shape in the collected corpus — so one
-- `opportunity_constraint_locations` row produces **one row per segment**,
-- ordered by `segment_position`. Several places are several answers, never a
-- disagreement, exactly as `0012` already treats several locations.
--
-- Idempotence is `source_fingerprint` plus `resolver_version`, the same pair
-- `0004` and `0012` already use: the fingerprint is a SHA-256 over canonical
-- JSON of exactly the text the resolver read, so an unchanged string under an
-- unchanged resolver rewrites nothing, an edited string recomputes, and a new
-- resolver version recomputes even when the string is identical.
--
-- The resolver behind these rows is local, deterministic and closed: an
-- explicit alias registry, no network, no geocoding service, no model and no
-- fuzzy matching. `resolution_rule_id` names the rule that produced each row,
-- so every country code here can be traced to one enumerated reason.

CREATE TABLE opportunity_location_resolutions (
    id INTEGER PRIMARY KEY,

    -- Denormalised on purpose, and safe: `source_location_id` already implies
    -- an opportunity, but every reader of this projection groups by posting,
    -- and a join through the evidence table to learn which posting a row
    -- belongs to would make the most common read the most expensive one. The
    -- pair is kept honest by both foreign keys cascading from the same delete.
    opportunity_id INTEGER NOT NULL,
    source_location_id INTEGER NOT NULL,

    -- The order the source string named its places in, preserved. `0` is the
    -- first segment of that string, never a rank and never a preference.
    segment_position INTEGER NOT NULL CHECK (segment_position >= 0),

    -- The segment as it was read, trimmed and nothing else: no case folding,
    -- no accent stripping, no canonical spelling. The normalisation the rules
    -- apply lives in the resolver and is deliberately not stored, so this
    -- column stays comparable with the evidence it came from.
    raw_segment TEXT NOT NULL CHECK (
        length(trim(raw_segment)) > 0 AND raw_segment = trim(raw_segment)
    ),

    -- ISO 3166-1 alpha-2, upper case, and NULL whenever no closed rule named
    -- one. NULL is UNKNOWN — it is not "somewhere else".
    country_code TEXT CHECK (
        country_code IS NULL
        OR (length(country_code) = 2 AND country_code GLOB '[A-Z][A-Z]')
    ),

    -- Optional, and secondary. The product targets the whole of Morocco, so no
    -- decision depends on this column; it exists because a city is sometimes
    -- the *only* thing that named the country, and a reader auditing such a
    -- row needs to see which catalogue entry did it. A city is never stored
    -- without the country it implied.
    city_key TEXT CHECK (
        city_key IS NULL
        OR (length(trim(city_key)) > 0 AND city_key = trim(city_key))
    ),

    resolution_status TEXT NOT NULL CHECK (
        resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNKNOWN')
    ),

    -- Which enumerated rule produced this row, including the rules that
    -- produced no country: an unresolved segment that cannot say *why* it is
    -- unresolved is not auditable.
    resolution_rule_id TEXT NOT NULL CHECK (
        length(trim(resolution_rule_id)) > 0
        AND resolution_rule_id = trim(resolution_rule_id)
    ),
    resolver_version TEXT NOT NULL CHECK (
        length(trim(resolver_version)) > 0
        AND resolver_version = trim(resolver_version)
    ),
    source_fingerprint TEXT NOT NULL CHECK (
        length(source_fingerprint) = 64 AND source_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- The status and the country say the same thing, so they cannot disagree:
    -- RESOLVED is exactly "a country was determined", and the two other
    -- statuses are exactly "one was not". A RESOLVED row with no country would
    -- be a resolution that resolved nothing; an AMBIGUOUS row carrying a
    -- country would be a guess wearing a warning label.
    CHECK (
        (resolution_status = 'RESOLVED' AND country_code IS NOT NULL)
        OR (resolution_status <> 'RESOLVED' AND country_code IS NULL)
    ),
    -- A city that implied no country implies nothing at all here.
    CHECK (city_key IS NULL OR country_code IS NOT NULL),

    -- One reading per segment of one source string.
    UNIQUE (source_location_id, segment_position),

    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE,
    -- The evidence row is the source. When `0012` replaces a posting's
    -- locations — which it does by deleting and reinserting them — every
    -- resolution read from the old strings disappears with them, and the next
    -- synchronization reads the new ones. A stale projection is worse than a
    -- missing one.
    FOREIGN KEY (source_location_id)
        REFERENCES opportunity_constraint_locations(id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_location_resolutions_opportunity
    ON opportunity_location_resolutions(opportunity_id);
CREATE INDEX idx_opportunity_location_resolutions_source
    ON opportunity_location_resolutions(source_location_id);
CREATE INDEX idx_opportunity_location_resolutions_country
    ON opportunity_location_resolutions(country_code);
CREATE INDEX idx_opportunity_location_resolutions_version
    ON opportunity_location_resolutions(resolver_version);
