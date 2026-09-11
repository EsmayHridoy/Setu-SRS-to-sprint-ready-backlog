-- =====================================================================
-- Business plans in chat: upgrade an existing database in place.
--
-- A fresh setu_postgres.sql already includes all of this. Run this only
-- against a database created before it, to keep its data:
--
--   psql -h 127.0.0.1 -p 5432 -d setu -f db/migrations/001_business_plans_in_chat.sql
--
-- Safe to run twice.
-- =====================================================================

BEGIN;

-- The assistant message that presents a plan extracted from a document
-- uploaded in chat.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS business_plan_id VARCHAR(36)
        REFERENCES business_plans(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_messages_business_plan_id
    ON messages (business_plan_id);

-- "No" on the review step keeps the plan (and the chat's memory of it)
-- rather than deleting it.
ALTER TABLE business_plans DROP CONSTRAINT IF EXISTS business_plans_status_check;
ALTER TABLE business_plans ADD CONSTRAINT business_plans_status_check
    CHECK (status IN ('DRAFT', 'CONFIRMED', 'DONE', 'DISCARDED'));

COMMIT;
