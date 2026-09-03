-- Phase 5.3C2: a subscription can never be told what happened before it existed.
--
-- 0018 gave a subscription a lifecycle; 0019 gave a profile an append-only
-- stream of notification events; 0020 froze the recipients of each event at
-- the moment its outbox row is materialized. What none of them settles is the
-- case where the outbox row is materialized *after* a device opts in: the
-- snapshot would then legitimately include a device that was not there when
-- the movement happened, and the user's first notification would be history.
--
-- The watermark closes that. It is the highest notification event id the
-- profile had at the instant this subscription became ACTIVE, and delivery
-- only ever targets a subscription for an event strictly above it. Because it
-- is an id and not an instant, the boundary is decided by SQLite's own
-- serialization rather than by two clocks: an event committed before the
-- activation is at or below the watermark and is never delivered here, and one
-- committed after it is above and is deliverable.
--
-- Every subscription that already exists is initialized to its profile's
-- current maximum event id, so upgrading a live database backfills nothing.
ALTER TABLE push_subscriptions
    ADD COLUMN notification_event_watermark INTEGER NOT NULL DEFAULT 0
    CHECK (notification_event_watermark >= 0);

UPDATE push_subscriptions
SET notification_event_watermark = (
    SELECT COALESCE(MAX(notification_events.id), 0)
    FROM notification_events
    WHERE notification_events.profile_id = push_subscriptions.profile_id
);

-- SQLite can only add a column with a constant default, and the only constant
-- available is 0 — the value that would deliver a profile's whole history to a
-- new device. These two triggers make that default unusable rather than
-- merely discouraged.
--
-- Both sides of the line are wrong, and both are refused. A watermark below
-- the stream replays history the device never asked for. A watermark above it
-- is quieter and worse: it is not a boundary at all but a mute, and it lasts
-- until the profile's ids happen to climb past whatever number was written —
-- an outage that looks exactly like a working subscription. An activation is
-- therefore the profile's current highest event id and nothing else: equal,
-- not merely not-behind, whoever writes the row.
CREATE TRIGGER push_subscriptions_watermark_matches_the_event_stream
BEFORE INSERT ON push_subscriptions
WHEN NEW.notification_event_watermark <> (
    SELECT COALESCE(MAX(id), 0) FROM notification_events
    WHERE profile_id = NEW.profile_id
)
BEGIN
    SELECT RAISE(ABORT, 'push subscription watermark is not the profile current notification event id');
END;

-- Only a REVOKED -> ACTIVE transition is an activation, so only that
-- transition is checked. A key rotation on a live subscription keeps the
-- watermark it already earned — it never stopped being subscribed, so there
-- is no new line to draw — and revoking never moves it either.
CREATE TRIGGER push_subscriptions_reactivation_matches_the_event_stream
BEFORE UPDATE OF status ON push_subscriptions
WHEN OLD.status = 'REVOKED' AND NEW.status = 'ACTIVE'
 AND NEW.notification_event_watermark <> (
    SELECT COALESCE(MAX(id), 0) FROM notification_events
    WHERE profile_id = NEW.profile_id
)
BEGIN
    SELECT RAISE(ABORT, 'reactivated push subscription watermark is not the profile current notification event id');
END;

-- A batch with no recipients was already terminal on the spot in 0020, but it
-- could only say "nobody was subscribed". There is now a second, entirely
-- different reason to have nobody: every active device activated at or after
-- this event and is deliberately not being told about it. Both remain
-- NO_ACTIVE_SUBSCRIPTIONS batches — 0020's vocabulary is not rewritten — and
-- this column is what tells an audit which of the two happened.
ALTER TABLE notification_delivery_batches
    ADD COLUMN empty_reason TEXT DEFAULT NULL
    CHECK (empty_reason IS NULL
        OR (status = 'NO_ACTIVE_SUBSCRIPTIONS'
            AND empty_reason IN ('NO_ACTIVE_SUBSCRIPTIONS', 'NO_ELIGIBLE_SUBSCRIPTIONS')));

UPDATE notification_delivery_batches
SET empty_reason = 'NO_ACTIVE_SUBSCRIPTIONS'
WHERE status = 'NO_ACTIVE_SUBSCRIPTIONS';

-- The UPDATE above already gave every empty batch that predates 0021 the only
-- reason it could have had, so NULL now means exactly one thing: a batch that
-- has recipients. This trigger keeps it that way — a row written from here on
-- states its reason when, and only when, it is an empty batch.
CREATE TRIGGER notification_delivery_batches_empty_reason_is_stated
BEFORE INSERT ON notification_delivery_batches
WHEN (NEW.status = 'NO_ACTIVE_SUBSCRIPTIONS') <> (NEW.empty_reason IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'an empty delivery batch names its reason, and only an empty one does');
END;

CREATE INDEX idx_push_subscriptions_profile_status_watermark
    ON push_subscriptions(profile_id, status, notification_event_watermark, id);
