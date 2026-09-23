-- Phase 11.3B-R4C-B1b-A: the additive foundation a CV activation will stand on.
--
-- Nothing here is rebuilt. Every statement is an `ALTER TABLE ... ADD COLUMN`,
-- a `CREATE TABLE`, an index or a trigger, so this migration applies inside the
-- ordinary `BEGIN`/`COMMIT` the runner already wraps it in, with
-- `PRAGMA foreign_keys` left on and not one existing row rewritten. `0027` is
-- untouched, and so are the twelve tables whose foreign keys cascade from
-- `profile_facts`.
--
-- Two ideas are introduced and no third one:
--
-- * **currentness**, which is not truth. `status` answers "did a human validate
--   this claim?" and keeps its four values. `retired_at` answers "is that
--   validated claim still part of the active profile?". Absence from a newer CV
--   is UNKNOWN, never evidence that a claim became false, so a fact removed by
--   a CV replacement must not end up looking refused. It stays `ACCEPTED` and
--   gains a retirement date;
--
-- * **activation metadata**, recorded on the replacement itself rather than in
--   a second table that would only repeat what the replacement already knows.
--
-- Both are audit events, so both are made irreversible by triggers rather than
-- by asking every future caller to remember.

-- ---------------------------------------------------------------- currentness

ALTER TABLE profile_facts ADD COLUMN retired_at TEXT
    CHECK (retired_at IS NULL
           OR (length(trim(retired_at)) > 0
               AND retired_at = trim(retired_at)
               -- Only a validated, still-standing fact can be retired. A
               -- proposal was never current, a refusal and a correction are
               -- terminal, and a corrected fact already points at what replaced
               -- it. This is also what stops a retired fact from being moved to
               -- REJECTED or CORRECTED later: the row would stop satisfying it.
               AND status = 'ACCEPTED'
               AND decided_at IS NOT NULL
               AND replaced_by_fact_id IS NULL));

-- The reading every current-state projection now performs, in one index.
CREATE INDEX idx_profile_facts_current
    ON profile_facts(profile_id, fact_type)
    WHERE status = 'ACCEPTED' AND retired_at IS NULL;

-- A retirement is a historical event: it happened, and it keeps having
-- happened. Clearing it would make a fact current again without anybody
-- deciding that, and re-dating it would rewrite when the person's profile
-- changed. Both are refused here rather than trusted to callers.
CREATE TRIGGER trg_profile_facts_retirement_is_final
BEFORE UPDATE OF retired_at ON profile_facts
FOR EACH ROW WHEN OLD.retired_at IS NOT NULL
     AND (NEW.retired_at IS NULL OR NEW.retired_at <> OLD.retired_at)
BEGIN
    SELECT RAISE(ABORT, 'a retirement cannot be cleared or re-dated');
END;

-- --------------------------------------------------------- activation record

-- The order of these three matters: the CHECK on `activated_at` names the other
-- two, so they have to exist first.
ALTER TABLE profile_cv_replacements ADD COLUMN ready_review_digest TEXT
    CHECK (ready_review_digest IS NULL
           OR (length(ready_review_digest) = 64
               AND ready_review_digest NOT GLOB '*[^0-9a-f]*'));

ALTER TABLE profile_cv_replacements ADD COLUMN activation_revision INTEGER
    CHECK (activation_revision IS NULL OR activation_revision > 0);

-- An activation is complete or it does not exist. The three half-states — a
-- date with no revision, a revision with no date, an activation with no review
-- token — are all excluded by this one expression, and rows written by 0027
-- satisfy its first branch because all three columns start NULL.
ALTER TABLE profile_cv_replacements ADD COLUMN activated_at TEXT
    CHECK ((activated_at IS NULL AND activation_revision IS NULL)
           OR (activated_at IS NOT NULL
               AND length(trim(activated_at)) > 0
               AND activated_at = trim(activated_at)
               AND activation_revision IS NOT NULL
               AND ready_review_digest IS NOT NULL
               -- The stored lifecycle vocabulary stays exactly 0027's. An
               -- activated attempt is the one that was READY_TO_ACTIVATE, and
               -- `activated_at IS NOT NULL` is what makes its effective state
               -- ACTIVATED and terminal.
               AND lifecycle = 'READY_TO_ACTIVATE'));

-- One revision per profile, counted only over activated attempts.
CREATE UNIQUE INDEX idx_cv_replacements_revision
    ON profile_cv_replacements(profile_id, activation_revision)
    WHERE activation_revision IS NOT NULL;

-- An activated attempt stops being open, so the next replacement can be
-- prepared. The index is what says so; no caller has to remember it.
DROP INDEX idx_cv_replacements_one_open;
CREATE UNIQUE INDEX idx_cv_replacements_one_open
    ON profile_cv_replacements(profile_id)
    WHERE lifecycle IN ('PREPARED', 'REVIEWING', 'READY_TO_ACTIVATE')
      AND activated_at IS NULL;

-- What was activated stays activated, and stays what it was.
--
-- Two groups of columns are frozen. The first says *that* it was activated and
-- under what: being cancelled, re-opened for review, re-dated, given another
-- revision, or having its review token rewritten are all refused. The second
-- says *what* was activated: the row's own identity, the profile it belongs
-- to, the reading campaign it applied, the document it was opened against, and
-- when it was opened. Letting any of those move would leave an activation
-- whose record describes something other than what actually happened.
--
-- `updated_at` is deliberately left mutable: it is a technical touch stamp, not
-- part of the historical event. `closed_at` needs no rule here — 0027 already
-- ties it to `lifecycle = 'CANCELLED'`, and the lifecycle is frozen above, so
-- an activated attempt can never be closed as cancelled.
CREATE TRIGGER trg_cv_replacement_activation_is_final
BEFORE UPDATE ON profile_cv_replacements
FOR EACH ROW WHEN OLD.activated_at IS NOT NULL
     AND (NEW.activated_at IS NULL
          OR NEW.activated_at <> OLD.activated_at
          OR NEW.activation_revision IS NULL
          OR NEW.activation_revision <> OLD.activation_revision
          OR NEW.ready_review_digest IS NULL
          OR NEW.ready_review_digest <> OLD.ready_review_digest
          OR NEW.lifecycle <> OLD.lifecycle
          OR NEW.id <> OLD.id
          OR NEW.profile_id <> OLD.profile_id
          OR NEW.extraction_id <> OLD.extraction_id
          -- Nullable, so `IS NOT` rather than `<>`: a baseline that was NULL
          -- becoming a document, or the reverse, is a change like any other.
          OR NEW.baseline_document_id IS NOT OLD.baseline_document_id
          OR NEW.created_at <> OLD.created_at)
BEGIN
    SELECT RAISE(ABORT, 'an activated replacement cannot be reopened or rewritten');
END;

-- The answers an activation applied are the answers it applied. Editing them
-- afterwards would make the stored review describe something the activation
-- never saw. Before activation, the ordinary B1a review is untouched by these
-- three triggers, because each fires only when the parent is already activated.
CREATE TRIGGER trg_cv_decisions_frozen_after_activation_insert
BEFORE INSERT ON profile_cv_replacement_decisions
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM profile_cv_replacements AS r
     WHERE r.id = NEW.replacement_id AND r.activated_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'the review of an activated replacement is closed');
END;

-- Both sides of the move are checked. Refusing only `OLD` would keep an
-- activated review from being edited while still letting a decision be carried
-- *into* it from an attempt still open — which would add an answer to a review
-- that was already applied.
--
-- Naming both sides by id alone also covers a change of `profile_id`: moving a
-- decision into another person's activated attempt matches on `NEW`, and
-- moving one out of an activated attempt matches on `OLD`, whichever profile is
-- written. The composite foreign key `(replacement_id, profile_id)` remains the
-- second layer underneath.
CREATE TRIGGER trg_cv_decisions_frozen_after_activation_update
BEFORE UPDATE ON profile_cv_replacement_decisions
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM profile_cv_replacements AS r
     WHERE r.activated_at IS NOT NULL
       AND (r.id = OLD.replacement_id OR r.id = NEW.replacement_id)
)
BEGIN
    SELECT RAISE(ABORT, 'the review of an activated replacement is closed');
END;

CREATE TRIGGER trg_cv_decisions_frozen_after_activation_delete
BEFORE DELETE ON profile_cv_replacement_decisions
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM profile_cv_replacements AS r
     WHERE r.id = OLD.replacement_id AND r.activated_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'the review of an activated replacement is closed');
END;

-- ------------------------------------------------------- downstream currency

-- How far each downstream phase has been synchronized, in CV activation
-- revisions. Nothing advances these rows yet: B1b-A creates the record, and the
-- phases learn to keep it in a later slice. A missing row reads as 0, which is
-- also the revision of a profile that has never activated a CV — so a database
-- that never replaces a CV behaves exactly as it does today.
CREATE TABLE profile_downstream_sync_watermark (
    profile_id INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN (
        'SKILLS',
        'STRUCTURED_PROFILE',
        'ELIGIBILITY',
        'MATCHING',
        'RECOMMENDATION',
        'PRIORITY',
        'PORTFOLIO'
    )),
    synced_revision INTEGER NOT NULL CHECK (synced_revision >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (profile_id, phase),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
