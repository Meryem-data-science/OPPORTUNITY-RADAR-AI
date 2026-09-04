-- Phase 6.1: the persistent tracking of a real candidature.
--
-- Everything before this migration is *observation*: the radar collects
-- opportunities, matches them, prioritises them and assembles a Portfolio.
-- None of that is an application. A detected opportunity is not something the
-- user applied to, and this schema refuses to blur the two: no row is ever
-- created here by collection, matching, priority or portfolio work. A row in
-- `applications` exists because a person decided it should — they saved an
-- opportunity, started preparing it, or recorded that they had applied.
--
-- The pair of tables is deliberately asymmetric. `applications` is the current
-- state of one candidature and is meant to be updated; `application_events` is
-- what happened to it and is never updated at all. The current state can
-- always be rebuilt from the history, so the history is the thing that must
-- survive: it is append-only, enforced here rather than in Python, and an
-- event may only ever be written by the same transaction that made the state
-- it describes true.
--
-- Phase 6.1 stops at tracking. There is no adapted CV, no letter, no answer
-- bank, no follow-up engine and no mailbox in this migration — those are 6.2
-- to 6.5 and Phase 7, and each will bring the columns and tables it actually
-- needs rather than finding empty ones waiting here.
CREATE TABLE applications (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL CHECK (profile_id > 0),
    opportunity_id INTEGER NOT NULL CHECK (opportunity_id > 0),
    -- The whole official vocabulary, including the two statuses no Phase 6.1
    -- operation may ask for. DISCOVERED exists so the model stays the model —
    -- it is what an opportunity is before anybody decides anything — and 6.1
    -- never writes it, because a candidature that nobody intended is not a
    -- candidature. READY exists because readiness is a real state, and 6.1
    -- refuses it because nothing yet knows how to compute it: an adapted CV
    -- (6.2), a letter (6.3) and the application's answers (6.4) are what make
    -- an application ready, and none of them exists. The database holds the
    -- vocabulary; the service decides what a user may say today.
    status TEXT NOT NULL CHECK (status IN (
        'DISCOVERED', 'SAVED', 'PREPARING', 'READY', 'SUBMITTED', 'CONFIRMED',
        'ASSESSMENT', 'INTERVIEW', 'REJECTED', 'OFFER', 'WITHDRAWN'
    )),
    -- The instant of the *first* real submission, and nothing else. Every
    -- later movement — confirmed, assessment, interview, offer, rejection —
    -- happens to an application that was already submitted, so none of them
    -- may restate it. `datetime(x) IS x` rejects both a malformed and an
    -- impossible instant: SQLite returns NULL for the second, and `IS` makes
    -- that NULL a failure rather than an unknown a CHECK would let through.
    submitted_at TEXT CHECK (submitted_at IS NULL OR datetime(submitted_at) IS submitted_at),
    last_status_change TEXT NOT NULL CHECK (datetime(last_status_change) IS last_status_change),
    next_action TEXT CHECK (
        next_action IS NULL
        OR (length(trim(next_action)) > 0 AND next_action = trim(next_action)
            AND length(next_action) <= 500)
    ),
    -- Manual tracking only in 6.1: a date the user wrote down, which nothing
    -- reads on a schedule. Recommending or sending a follow-up is Phase 6.5.
    followup_date TEXT CHECK (
        followup_date IS NULL
        OR (followup_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            AND date(followup_date) IS followup_date)
    ),
    notes TEXT CHECK (
        notes IS NULL
        OR (length(trim(notes)) > 0 AND notes = trim(notes) AND length(notes) <= 4000)
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        CHECK (datetime(created_at) IS created_at),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        CHECK (datetime(updated_at) IS updated_at),
    -- Submission is a fact about the row, not a flag beside it. Before an
    -- application has been submitted there is no instant to name; from the
    -- moment it has, every status that can only be reached *through* a
    -- submission has to carry one. WITHDRAWN is the one status that sits on
    -- both sides of that line — a candidature can be abandoned before it was
    -- ever sent, or long after — so it is the only one that says nothing
    -- about submission.
    CHECK (
        (status IN ('DISCOVERED', 'SAVED', 'PREPARING', 'READY')
            AND submitted_at IS NULL)
        OR (status IN ('SUBMITTED', 'CONFIRMED', 'ASSESSMENT', 'INTERVIEW',
                       'REJECTED', 'OFFER')
            AND submitted_at IS NOT NULL)
        OR status = 'WITHDRAWN'
    ),
    -- One candidature per profile per opportunity, held by the database
    -- itself: two "Sauvegarder" clicks racing each other cannot both create a
    -- row, whatever either one decided a moment earlier.
    UNIQUE (profile_id, opportunity_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    -- RESTRICT, not CASCADE: the opportunity is what this candidature is
    -- *about*, and its link is the one the user will click months later. An
    -- opportunity somebody applied to may not be deleted out from under the
    -- application that references it.
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT
);

-- The list surface: this profile's candidatures, newest movement first.
CREATE INDEX idx_applications_profile_status
    ON applications(profile_id, status, id);
CREATE INDEX idx_applications_profile_last_status_change
    ON applications(profile_id, last_status_change, id);
-- "Has this opportunity already been applied to?", and the reverse lookup a
-- RESTRICTed delete needs.
CREATE INDEX idx_applications_opportunity
    ON applications(opportunity_id);
-- Manual follow-up dates, so 6.1 can show them ordered without a scan.
CREATE INDEX idx_applications_profile_followup
    ON applications(profile_id, followup_date);

-- The history. `applications` above answers "where is this candidature now";
-- this answers "how did it get there", and it is the answer that is never
-- allowed to change.
CREATE TABLE application_events (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL CHECK (application_id > 0),
    -- The three things that can happen to a candidature in Phase 6.1. The
    -- vocabulary of later phases — a generated CV, a generated letter, a
    -- status a mailbox inferred, a follow-up that went out, an automated
    -- application — is deliberately absent: an event type that nothing can
    -- yet produce would be a promise this migration cannot keep.
    event_type TEXT NOT NULL CHECK (
        event_type IN ('APPLICATION_CREATED', 'STATUS_CHANGED', 'TRACKING_UPDATED')
    ),
    from_status TEXT CHECK (from_status IS NULL OR from_status IN (
        'DISCOVERED', 'SAVED', 'PREPARING', 'READY', 'SUBMITTED', 'CONFIRMED',
        'ASSESSMENT', 'INTERVIEW', 'REJECTED', 'OFFER', 'WITHDRAWN'
    )),
    to_status TEXT CHECK (to_status IS NULL OR to_status IN (
        'DISCOVERED', 'SAVED', 'PREPARING', 'READY', 'SUBMITTED', 'CONFIRMED',
        'ASSESSMENT', 'INTERVIEW', 'REJECTED', 'OFFER', 'WITHDRAWN'
    )),
    actor_type TEXT NOT NULL CHECK (actor_type IN ('USER', 'SYSTEM')),
    occurred_at TEXT NOT NULL CHECK (datetime(occurred_at) IS occurred_at),
    -- Each event type states exactly the fields it means. A creation comes
    -- from nowhere and names the state it starts in; a status change names
    -- both ends and cannot name the same one twice, because an event that
    -- changed nothing is not an event; a tracking update touched no status at
    -- all and therefore names none.
    CHECK (
        (event_type = 'APPLICATION_CREATED'
            AND from_status IS NULL AND to_status IS NOT NULL)
        OR (event_type = 'STATUS_CHANGED'
            AND from_status IS NOT NULL AND to_status IS NOT NULL
            AND from_status <> to_status)
        OR (event_type = 'TRACKING_UPDATED'
            AND from_status IS NULL AND to_status IS NULL)
    ),
    -- RESTRICT rather than CASCADE, and it is the append-only rule seen from
    -- the other side: the history of a candidature is not a detail of the row
    -- it hangs off, so deleting that row cannot quietly take the history with
    -- it. An application that has a history cannot be deleted, which — since
    -- every application gets an APPLICATION_CREATED event in the transaction
    -- that creates it — means no tracked candidature is ever silently erased.
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE RESTRICT
);

-- One candidature's timeline, in the order it happened. Ids order the stream,
-- not `occurred_at`: two events in the same second are still two events, and
-- the one written second is the one that happened second.
CREATE INDEX idx_application_events_application
    ON application_events(application_id, id);

-- Append-only, enforced where it cannot be forgotten. A repository can be
-- rewritten, a service can be bypassed, a migration can be run by hand; none
-- of them can rewrite what is below, because SQLite refuses the statement
-- itself. Together with the RESTRICT above, an event that has been written
-- has been written.
CREATE TRIGGER application_events_are_append_only_on_update
BEFORE UPDATE ON application_events
BEGIN
    SELECT RAISE(ABORT, 'application_events is append-only: an event cannot be modified');
END;

CREATE TRIGGER application_events_are_append_only_on_delete
BEFORE DELETE ON application_events
BEGIN
    SELECT RAISE(ABORT, 'application_events is append-only: an event cannot be deleted');
END;

-- Atomicity, made structural. An event that names a resulting status may only
-- be written once the application actually *has* that status, so the state
-- and its history can only be written by the same transaction, in that order.
-- A status update committed without its event is caught by review; an event
-- committed without its update cannot be written at all.
CREATE TRIGGER application_events_agree_with_the_application
BEFORE INSERT ON application_events
WHEN NEW.to_status IS NOT NULL
 AND NEW.to_status IS NOT (
    SELECT status FROM applications WHERE id = NEW.application_id
 )
BEGIN
    SELECT RAISE(ABORT, 'application event does not describe the current application state');
END;
