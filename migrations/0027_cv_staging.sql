-- Phase 11.3A-R4C-B1a: staging for a CV replacement, and nothing that applies one.
--
-- Everything here is inert. Not one table below is read by the skill
-- projection, the structured profile, eligibility, Matching, Recommendation,
-- Priority or Portfolio, and no row in `profile_facts` or
-- `profile_fact_provenance` changes because something was written here. A
-- replacement that is prepared, reviewed and then cancelled leaves the Digital
-- Twin exactly as it found it; that is the property this slice exists to make
-- true before any activation is designed.
--
-- The staging tables do hold extracted CV text, in `profile_cv_candidates`.
-- That is the same class of data `profile_facts.value` already holds, kept in
-- the same local database, and it is what lets a person review a replacement
-- after closing their laptop. It never reaches a log, a summary, an error
-- message or a report: the code that owns these tables exposes counters,
-- canonical types, ordinals and digests, and there is no flag that prints a
-- value.

-- The composite foreign key on the decision table references
-- `profile_facts(id, profile_id)`. Migration 0009 already created the unique
-- index `idx_profile_facts_id_profile` that SQLite needs for that, and 0010 and
-- 0011 already reference the same pair, so this migration adds nothing to any
-- existing table: it creates its own and stops there.

-- One CV document, for one profile. Identity is the content digest, never a
-- filename and never a path: a CV filename usually carries the person's name,
-- and a path says nothing about which document this is.
CREATE TABLE profile_cv_documents (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (
        length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    -- UPLOADED: this project read the bytes. LEGACY_DECLARED: an operator
    -- stated which document the existing facts came from, without the file.
    origin TEXT NOT NULL CHECK (origin IN ('UPLOADED', 'LEGACY_DECLARED')),
    byte_size INTEGER CHECK (byte_size IS NULL OR byte_size > 0),
    page_count INTEGER CHECK (page_count IS NULL OR page_count > 0),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('KNOWN', 'ACTIVE', 'HISTORICAL')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at TEXT,
    retired_at TEXT,
    -- A document whose bytes were read knows its shape; a declared one may not.
    CHECK (origin <> 'UPLOADED' OR (byte_size IS NOT NULL AND page_count IS NOT NULL)),
    CHECK ((lifecycle = 'ACTIVE') = (activated_at IS NOT NULL AND retired_at IS NULL)),
    CHECK ((lifecycle = 'HISTORICAL') = (activated_at IS NOT NULL AND retired_at IS NOT NULL)),
    CHECK (lifecycle <> 'KNOWN' OR (activated_at IS NULL AND retired_at IS NULL)),
    UNIQUE (profile_id, content_sha256),
    UNIQUE (id, profile_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

-- At most one active CV per profile, enforced by the database rather than by
-- whichever caller happens to run next.
CREATE UNIQUE INDEX idx_cv_documents_one_active
    ON profile_cv_documents(profile_id) WHERE lifecycle = 'ACTIVE';
CREATE INDEX idx_cv_documents_profile_lifecycle
    ON profile_cv_documents(profile_id, lifecycle);

-- One reading campaign of one document: these parser and extractor versions,
-- this attempt. `attempt_no` exists so that a manifest found corrupt can be
-- left exactly where it is — never rewritten, never reused — while a fresh,
-- correctly keyed campaign is recorded beside it.
CREATE TABLE profile_cv_extractions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    profile_id INTEGER NOT NULL,
    parser_version TEXT NOT NULL CHECK (
        length(trim(parser_version)) > 0 AND parser_version = trim(parser_version)
    ),
    extractor_version TEXT NOT NULL CHECK (
        length(trim(extractor_version)) > 0 AND extractor_version = trim(extractor_version)
    ),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    -- The chain digest the manifest must reproduce. It is computed from the
    -- extraction in memory, before the first candidate row is written, and the
    -- whole manifest is written in the same transaction as this row: a row here
    -- with a partially populated manifest cannot be produced by this code.
    manifest_chain_digest TEXT NOT NULL CHECK (
        length(manifest_chain_digest) = 64
        AND manifest_chain_digest NOT GLOB '*[^0-9a-f]*'
    ),
    -- COMPLETE is the only state this code writes. CORRUPT is set once a
    -- verification fails, and it is terminal: the rows stay readable and the
    -- campaign is never reused.
    manifest_state TEXT NOT NULL CHECK (manifest_state IN ('COMPLETE', 'CORRUPT')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    corrupted_at TEXT,
    CHECK ((manifest_state = 'CORRUPT') = (corrupted_at IS NOT NULL)),
    UNIQUE (document_id, parser_version, extractor_version, attempt_no),
    UNIQUE (id, profile_id),
    FOREIGN KEY (document_id, profile_id)
        REFERENCES profile_cv_documents(id, profile_id) ON DELETE CASCADE
);

CREATE INDEX idx_cv_extractions_document
    ON profile_cv_extractions(document_id, parser_version, extractor_version, attempt_no);

-- The manifest: every candidate the extraction produced, in document order,
-- with the identity it will be recognised by later. `value` is CV text and is
-- protected as such; nothing in this project prints it from here.
CREATE TABLE profile_cv_candidates (
    id INTEGER PRIMARY KEY,
    extraction_id INTEGER NOT NULL,
    profile_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    candidate_type TEXT NOT NULL CHECK (length(trim(candidate_type)) > 0),
    fact_type TEXT NOT NULL CHECK (length(trim(fact_type)) > 0),
    candidate_fingerprint TEXT NOT NULL CHECK (
        length(candidate_fingerprint) > 0
        AND candidate_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    -- The evidence key this reading would carry as a fact. It is derived from
    -- the evidence itself — document digest, versions, rule, place — exactly as
    -- `ProvenanceInput.resolved_provenance_key` derives it, and never from a
    -- replacement attempt: a second attempt over the same reading is the same
    -- proof, not a new one.
    provenance_key TEXT NOT NULL CHECK (
        length(trim(provenance_key)) > 0 AND provenance_key = trim(provenance_key)
    ),
    value TEXT NOT NULL CHECK (length(trim(value)) > 0),
    normalized_value TEXT CHECK (
        normalized_value IS NULL OR length(trim(normalized_value)) > 0
    ),
    rule_id TEXT NOT NULL CHECK (length(trim(rule_id)) > 0),
    -- Canonical JSON array of ascending page numbers, the encoding 0007 uses.
    page_numbers TEXT CHECK (
        page_numbers IS NULL
        OR (page_numbers GLOB '[[][0-9]*'
            AND page_numbers GLOB '*[0-9][]]'
            AND page_numbers NOT GLOB '*[^]0-9,[]*'
            AND page_numbers NOT GLOB '*,,*')
    ),
    section_type TEXT CHECK (section_type IS NULL OR length(trim(section_type)) > 0),
    section_index INTEGER CHECK (section_index IS NULL OR section_index >= 0),
    -- Running digest: SHA-256 over the previous digest and this candidate's
    -- canonical payload. A removed, reordered or edited row breaks the chain
    -- from its own position onward, which is what makes completeness provable
    -- after a restart without trusting a counter.
    chain_digest TEXT NOT NULL CHECK (
        length(chain_digest) = 64 AND chain_digest NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE (extraction_id, ordinal),
    UNIQUE (extraction_id, candidate_fingerprint),
    UNIQUE (extraction_id, provenance_key),
    UNIQUE (id, profile_id),
    FOREIGN KEY (extraction_id, profile_id)
        REFERENCES profile_cv_extractions(id, profile_id) ON DELETE CASCADE
);

CREATE INDEX idx_cv_candidates_extraction ON profile_cv_candidates(extraction_id, ordinal);

-- One attempt at replacing the active CV: which campaign it reads, which
-- document it was opened against, and where the review stands.
CREATE TABLE profile_cv_replacements (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL,
    extraction_id INTEGER NOT NULL,
    -- The active CV when the attempt was opened. NULL means there was none.
    baseline_document_id INTEGER,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN (
        'PREPARED', 'REVIEWING', 'READY_TO_ACTIVATE', 'CANCELLED'
    )),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    CHECK ((lifecycle = 'CANCELLED') = (closed_at IS NOT NULL)),
    UNIQUE (id, profile_id),
    FOREIGN KEY (extraction_id, profile_id)
        REFERENCES profile_cv_extractions(id, profile_id) ON DELETE CASCADE,
    FOREIGN KEY (baseline_document_id, profile_id)
        REFERENCES profile_cv_documents(id, profile_id) ON DELETE RESTRICT
);

-- One open attempt per profile. A cancelled attempt leaves the index, so
-- re-uploading after a cancellation is an ordinary new attempt rather than a
-- conflict.
CREATE UNIQUE INDEX idx_cv_replacements_one_open
    ON profile_cv_replacements(profile_id)
    WHERE lifecycle IN ('PREPARED', 'REVIEWING', 'READY_TO_ACTIVATE');
CREATE INDEX idx_cv_replacements_profile ON profile_cv_replacements(profile_id, id);

-- A human decision, recorded and held. Nothing here changes a fact: these rows
-- are what an activation would later read, and B1a implements no activation.
CREATE TABLE profile_cv_replacement_decisions (
    id INTEGER PRIMARY KEY,
    replacement_id INTEGER NOT NULL,
    profile_id INTEGER NOT NULL,
    -- INCOMING names a manifest candidate, which has no fact yet. EXISTING
    -- names a fact of this profile. Exactly one of the two, never both.
    role TEXT NOT NULL CHECK (role IN ('INCOMING', 'EXISTING')),
    candidate_id INTEGER,
    fact_id INTEGER,
    difference TEXT NOT NULL CHECK (difference IN (
        'NEW',
        'UNCHANGED_STILL_SUPPORTED',
        'ABSENT_FROM_NEW_CV',
        'INDEPENDENTLY_SUPPORTED',
        'USER_INPUT_REPLACEMENT',
        'ALREADY_PROPOSED',
        'ALREADY_ACCEPTED',
        'BLOCKED_TERMINAL_REJECTED',
        'BLOCKED_TERMINAL_CORRECTED'
    )),
    decision TEXT NOT NULL CHECK (decision IN (
        'UNDECIDED', 'ACCEPT', 'REJECT', 'CORRECT', 'SKIP_BLOCKED', 'KEEP', 'RETIRE'
    )),
    -- A correction is staged as a value, not applied: no fact is created and
    -- no fact is marked CORRECTED while a review is open.
    staged_value TEXT CHECK (staged_value IS NULL OR length(trim(staged_value)) > 0),
    staged_normalized_value TEXT CHECK (
        staged_normalized_value IS NULL OR length(trim(staged_normalized_value)) > 0
    ),
    -- What the fact was when the person answered. An activation compares this
    -- with the fact's status at that moment and refuses a stale decision.
    fact_status_at_decision TEXT,
    -- Everything the answer depended on, as one digest: the classification,
    -- the fact's status, whether it is protected, the evidence it rests on and
    -- which manifest reading it is. Completeness is *not* decided from target
    -- ids: the review recomputes this digest and fails closed when it moved,
    -- so a fact that gained USER_INPUT or GITHUB evidence after somebody chose
    -- RETIRE has to be looked at again.
    review_state_digest TEXT NOT NULL CHECK (
        length(review_state_digest) = 64
        AND review_state_digest NOT GLOB '*[^0-9a-f]*'
    ),
    decided_at TEXT,
    CHECK ((role = 'INCOMING') = (candidate_id IS NOT NULL)),
    CHECK ((role = 'EXISTING') = (fact_id IS NOT NULL)),
    CHECK ((candidate_id IS NULL) <> (fact_id IS NULL)),
    CHECK ((decision = 'UNDECIDED') = (decided_at IS NULL)),
    CHECK ((decision = 'CORRECT') = (staged_value IS NOT NULL)),
    CHECK (staged_normalized_value IS NULL OR staged_value IS NOT NULL),
    -- An incoming reading can be accepted, refused, corrected or skipped
    -- because it is blocked; an existing fact can only be kept or retired.
    CHECK (
        (role = 'INCOMING'
            AND decision IN ('UNDECIDED', 'ACCEPT', 'REJECT', 'CORRECT', 'SKIP_BLOCKED'))
        OR (role = 'EXISTING' AND decision IN ('UNDECIDED', 'KEEP', 'RETIRE'))
    ),
    -- A blocked reading is only ever skipped: turning a terminal REJECTED or
    -- CORRECTED fact back into a proposal is a separate workflow nobody has
    -- designed, so this fails closed instead of guessing.
    CHECK (
        difference NOT IN ('BLOCKED_TERMINAL_REJECTED', 'BLOCKED_TERMINAL_CORRECTED')
        OR decision IN ('UNDECIDED', 'SKIP_BLOCKED')
    ),
    CHECK (
        decision <> 'SKIP_BLOCKED'
        OR difference IN ('BLOCKED_TERMINAL_REJECTED', 'BLOCKED_TERMINAL_CORRECTED')
    ),
    -- A fact the person stated themselves, or that another source supports, is
    -- not this document's to retire.
    CHECK (
        difference NOT IN ('INDEPENDENTLY_SUPPORTED', 'USER_INPUT_REPLACEMENT')
        OR decision <> 'RETIRE'
    ),
    UNIQUE (replacement_id, candidate_id),
    UNIQUE (replacement_id, fact_id),
    FOREIGN KEY (replacement_id, profile_id)
        REFERENCES profile_cv_replacements(id, profile_id) ON DELETE CASCADE,
    FOREIGN KEY (candidate_id, profile_id)
        REFERENCES profile_cv_candidates(id, profile_id) ON DELETE RESTRICT,
    FOREIGN KEY (fact_id, profile_id)
        REFERENCES profile_facts(id, profile_id) ON DELETE RESTRICT
);

CREATE INDEX idx_cv_decisions_replacement
    ON profile_cv_replacement_decisions(replacement_id, decision);
