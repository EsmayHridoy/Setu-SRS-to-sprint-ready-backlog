-- =====================================================================
-- BA approval loop: a vetted item's DONE status now means "has a
-- verdict", not "final". The BA can discuss a vetted item -- asking a
-- clarifying question, or giving new information that revises the
-- verdict -- through a per-item comment thread, then explicitly approve
-- it once satisfied. Only approved items are considered backlog-ready.
--
-- Apply with:
--   psql -h 127.0.0.1 -p 5432 -U postgres -d setu \
--        -f db/migrations/V7__item_approval_and_comments.sql
--
-- Safe to run twice.
-- =====================================================================

BEGIN;

ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS business_item_comments (
    id               VARCHAR(36)  PRIMARY KEY,
    business_item_id VARCHAR(36)  NOT NULL
                     REFERENCES business_items(id) ON DELETE CASCADE,
    role             VARCHAR(16)  NOT NULL,  -- USER | ASSISTANT
    content          TEXT         NOT NULL,
    -- Set on an ASSISTANT row when this turn revised the item's verdict
    -- fields, so the thread can show "verdict updated" on that turn.
    changed_verdict  BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    CONSTRAINT business_item_comments_role_check
        CHECK (role IN ('USER', 'ASSISTANT'))
);

CREATE INDEX IF NOT EXISTS ix_business_item_comments_item_id
    ON business_item_comments(business_item_id);

COMMIT;
