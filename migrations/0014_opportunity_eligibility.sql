-- Phase 3.6: whether one person can actually apply to one posting.
--
-- Everything before this migration kept the two sides apart on purpose. `0012`
-- and `0013` read what a posting *asks for* and say so in their own comments:
-- "no profile here, no comparison to one, no gap, no verdict, no score and no
-- rank; joining the two sides is Phase 3.6". `0006` through `0011` read what a
-- person *stated* and never looked at a posting. This is the migration where
-- the two meet, and it is the only place in the schema where a row mentions a
-- user and an opportunity at once.
--
-- The question is one question, and it is narrow:
--
--     GIVEN WHAT THIS POSTING EXPLICITLY DEMANDS, AND WHAT THIS PERSON'S
--     RELIABLE FACTS SAY, IS THERE A KNOWN REASON THEY COULD NOT APPLY?
--
-- It is not "would they be hired", not "are they a good fit", and not "how
-- good a fit". There is no score here, no percentage, no rank and no weight,
-- and none of the three columns below can be turned into one: `status` is
-- categorical and the counters are counts of rule outcomes, not a numerator
-- and a denominator. Matching is Phase 4 and does not exist. A posting can be
-- ELIGIBLE and a terrible match, or INELIGIBLE and a superb one.
--
--     users ─┐
--            ├─> opportunity_eligibilities        (one current decision)
--     opportunities ─┘     +-> eligibility_rule_results   (why, rule by rule)
--
-- **UNKNOWN is not INELIGIBLE.** That is the whole design, and the CHECK
-- constraints below exist to make the opposite unstorable rather than merely
-- discouraged. A person whose CV never named a language has not said they do
-- not speak it; a posting that never named a degree has not demanded one. The
-- engine turns silence into UNKNOWN or NOT_APPLICABLE, never into a refusal,
-- and a row claiming otherwise is rejected by SQLite before it reaches disk.
--
-- **A decision belongs to a person.** `UNIQUE (user_id, opportunity_id)`, not
-- `UNIQUE (opportunity_id)`: eligibility is not a property of a posting, and
-- storing it as one would make a second user's answer overwrite the first's.

-- One person, one posting, one current verdict.
--
-- `input_fingerprint` is a SHA-256 over the canonical JSON of **exactly what
-- the engine read** — the posting's education, experience, language, work
-- authorization, convention and skill requirements, the ambiguities 3.5B
-- refused to resolve, this person's projected education, enrolment, languages,
-- skills, stated convention capability and stated sponsorship need — plus the
-- engine version. Nothing else. A changed telephone number, a new GitHub URL,
-- a mobility preference: none of them is read by any rule below, none of them
-- is in the digest, and none of them recomputes anything. A version bump
-- recomputes everything, which is what a version is for.
--
-- The counters are the five rule outcomes, tallied. They explain the verdict;
-- they do not produce it, and dividing one by another produces a number with
-- no meaning. `blocking_unknown_count` is the subset of `unknown_count` that
-- came from a hard rule, because those are the ones that make the verdict
-- UNKNOWN — an advisory UNKNOWN never does.
CREATE TABLE opportunity_eligibilities (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    opportunity_id INTEGER NOT NULL,

    -- Three values, and deliberately only three. There is no
    -- PARTIALLY_ELIGIBLE: a partial answer is either a known blocker
    -- (INELIGIBLE) or a missing fact (UNKNOWN), and inventing a middle would
    -- let a caller treat "we do not know" as "not quite good enough".
    status TEXT NOT NULL CHECK (status IN ('ELIGIBLE', 'INELIGIBLE', 'UNKNOWN')),

    engine_version TEXT NOT NULL CHECK (
        length(trim(engine_version)) > 0 AND engine_version = trim(engine_version)
    ),
    input_fingerprint TEXT NOT NULL CHECK (
        length(input_fingerprint) = 64 AND input_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),

    satisfied_count INTEGER NOT NULL CHECK (satisfied_count >= 0),
    violated_count INTEGER NOT NULL CHECK (violated_count >= 0),
    unknown_count INTEGER NOT NULL CHECK (unknown_count >= 0),
    not_applicable_count INTEGER NOT NULL CHECK (not_applicable_count >= 0),
    not_evaluated_count INTEGER NOT NULL CHECK (not_evaluated_count >= 0),
    blocking_unknown_count INTEGER NOT NULL CHECK (
        blocking_unknown_count >= 0 AND blocking_unknown_count <= unknown_count
    ),

    evaluated_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- The aggregator, written into the schema so a wrong verdict cannot be
    -- stored at all:
    --
    --     violated > 0            -> INELIGIBLE
    --     else blocking unknown>0 -> UNKNOWN
    --     else                    -> ELIGIBLE
    --
    -- Read the three together and they say the thing this phase exists to say:
    -- a decision is INELIGIBLE only when a hard rule was **contradicted**, and
    -- an absent fact produces UNKNOWN instead. Nothing here can promote an
    -- UNKNOWN into a refusal, however many of them there are.
    CHECK (status <> 'INELIGIBLE' OR violated_count > 0),
    CHECK (status <> 'UNKNOWN'
        OR (violated_count = 0 AND blocking_unknown_count > 0)),
    CHECK (status <> 'ELIGIBLE'
        OR (violated_count = 0 AND blocking_unknown_count = 0)),

    -- A decision belongs to one person and to one posting, and there is
    -- exactly one current answer for that pair. History is not kept here: a
    -- recomputation replaces the row, because a stale verdict beside a fresh
    -- one is two answers to one question.
    UNIQUE (user_id, opportunity_id),

    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);

CREATE INDEX idx_opportunity_eligibilities_opportunity
    ON opportunity_eligibilities(opportunity_id);
CREATE INDEX idx_opportunity_eligibilities_user_status
    ON opportunity_eligibilities(user_id, status);

-- Why the verdict is what it is: one row per rule the engine ran.
--
-- Every decision keeps its whole reasoning, including the rules that passed and
-- the ones that had nothing to look at. Stopping at the first contradiction
-- would give an operator one reason where the posting gave three, so the engine
-- evaluates everything and this table stores everything.
--
-- Five statuses, and the difference between the last three is the point:
--
-- * `SATISFIED`   — an applicable requirement exists and reliable facts meet it;
-- * `VIOLATED`    — a hard applicable requirement exists and reliable facts
--                   **contradict** it. Only ever a contradiction, never a gap;
-- * `UNKNOWN`     — a hard applicable requirement exists and the facts needed
--                   to answer are not there. This is a question, not a refusal;
-- * `NOT_APPLICABLE` — the posting asked for nothing on this dimension;
-- * `NOT_EVALUATED`  — something was asked and `eligibility-rules-v1`
--                   deliberately declines to decide it. An ambiguity 3.5B
--                   refused to resolve, a skill requirement whose alternatives
--                   are not representable, a dimension this version defers.
--
-- `is_blocking` is what the aggregator counts, and it is narrow by
-- construction. A rule is blocking only when it belongs to one of the six hard
-- dimensions **and** the posting stated the requirement as REQUIRED. The CHECKs
-- below enforce that, so the invariants this phase promises are properties of
-- the schema rather than of the code that happens to fill it:
--
-- * a skill requirement can never block — not even a REQUIRED one, because
--   "Python or R" is stored as two REQUIRED rows and treating both as
--   obligations would reject somebody the posting would have accepted;
-- * an ambiguity can never block — 3.5B recorded a refusal to represent a
--   demand, and a refusal is not a demand;
-- * mobility, location, availability, duration, start date and work mode can
--   never block, because this version does not evaluate them;
-- * a PREFERRED requirement can never block, on any dimension;
-- * nothing that cannot block can ever be VIOLATED.
CREATE TABLE eligibility_rule_results (
    id INTEGER PRIMARY KEY,
    eligibility_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),

    dimension TEXT NOT NULL CHECK (dimension IN (
        -- The six that may block.
        'EDUCATION', 'ENROLLMENT', 'EXPERIENCE', 'LANGUAGE',
        'WORK_AUTHORIZATION', 'CONVENTION',
        -- Advisory: read, reported, never decisive.
        'SKILL', 'AMBIGUITY',
        -- Deferred by v1: the data exists, the comparison does not.
        'MOBILITY', 'LOCATION', 'AVAILABILITY', 'DURATION',
        'START_DATE', 'WORK_MODE'
    )),
    rule_code TEXT NOT NULL CHECK (
        length(trim(rule_code)) > 0 AND rule_code = trim(rule_code)
    ),
    status TEXT NOT NULL CHECK (status IN (
        'SATISFIED', 'VIOLATED', 'UNKNOWN', 'NOT_APPLICABLE', 'NOT_EVALUATED'
    )),
    is_blocking INTEGER NOT NULL CHECK (is_blocking IN (0, 1)),

    -- How the posting stated it, when it stated it at all. NULL when the rule
    -- had no requirement to classify — a dimension the posting is silent on, a
    -- dimension this version defers.
    requirement_kind TEXT CHECK (
        requirement_kind IS NULL OR requirement_kind IN ('REQUIRED', 'PREFERRED')
    ),

    -- The machine-readable truth. `explanation` is a deterministic rendering of
    -- this code and the same inputs, generated by a template; it is a
    -- convenience for a reader and nothing downstream should parse it.
    reason_code TEXT NOT NULL CHECK (
        length(trim(reason_code)) > 0 AND reason_code = trim(reason_code)
    ),
    explanation TEXT NOT NULL CHECK (
        length(trim(explanation)) > 0 AND length(explanation) <= 400
    ),

    -- Where each side of the comparison lives, as a stable logical pointer:
    -- `LANGUAGE#english`, `SKILL#python`, `profile_preferences#convention_status`.
    -- Not a row id, because 3.5B rebuilds its rows whole and an id would dangle
    -- after the next extraction; not a copy of the evidence, because Phase 3.4
    -- and Phase 3.5B already store the words and duplicating them here would
    -- create a second, divergent record of the same proof. Neither column ever
    -- holds a posting's text or a person's.
    requirement_ref TEXT CHECK (
        requirement_ref IS NULL
        OR (length(trim(requirement_ref)) > 0
            AND requirement_ref = trim(requirement_ref)
            AND length(requirement_ref) <= 200)
    ),
    profile_ref TEXT CHECK (
        profile_ref IS NULL
        OR (length(trim(profile_ref)) > 0
            AND profile_ref = trim(profile_ref)
            AND length(profile_ref) <= 200)
    ),

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Only a blocking rule can contradict. A gap is never a contradiction.
    CHECK (status <> 'VIOLATED' OR is_blocking = 1),
    -- Only the six hard dimensions can block.
    CHECK (is_blocking = 0 OR dimension IN (
        'EDUCATION', 'ENROLLMENT', 'EXPERIENCE', 'LANGUAGE',
        'WORK_AUTHORIZATION', 'CONVENTION'
    )),
    -- A PREFERRED requirement never blocks, whatever dimension it sits on.
    CHECK (requirement_kind IS NULL OR requirement_kind <> 'PREFERRED'
        OR is_blocking = 0),
    -- A blocking rule is a rule about a requirement the posting made explicit.
    CHECK (is_blocking = 0 OR requirement_kind = 'REQUIRED'),
    -- Advisory and deferred dimensions can neither block nor contradict, and
    -- they also cannot claim a hard outcome by using the UNKNOWN status: an
    -- unverified skill is NOT_EVALUATED, which no aggregator counts.
    CHECK (dimension NOT IN (
            'SKILL', 'AMBIGUITY', 'MOBILITY', 'LOCATION',
            'AVAILABILITY', 'DURATION', 'START_DATE', 'WORK_MODE'
        )
        OR (is_blocking = 0 AND status IN (
            'SATISFIED', 'NOT_APPLICABLE', 'NOT_EVALUATED'
        ))),

    UNIQUE (eligibility_id, position),
    FOREIGN KEY (eligibility_id)
        REFERENCES opportunity_eligibilities(id) ON DELETE CASCADE
);

CREATE INDEX idx_eligibility_rule_results_eligibility
    ON eligibility_rule_results(eligibility_id);
CREATE INDEX idx_eligibility_rule_results_dimension_status
    ON eligibility_rule_results(dimension, status);
CREATE INDEX idx_eligibility_rule_results_reason
    ON eligibility_rule_results(reason_code);
