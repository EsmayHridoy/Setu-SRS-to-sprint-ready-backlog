-- =====================================================================
-- Links a business plan to the chat conversation it was created from, so
-- the chat agent's business-requirements lookup can be scoped to the
-- current conversation instead of the whole project. Without this, a
-- vague vetting request in one chat could match an item that was only
-- ever discussed in a completely different chat, making the reply read
-- as if it had "carried over" that other conversation's context.
--
-- NULL for plans created outside any chat (the Business tab's direct
-- document upload) -- those are intentionally excluded from chat
-- matching too, not just other chats.
--
-- ON DELETE SET NULL: deleting a conversation must not delete the
-- business items it produced -- they keep existing as project records,
-- just no longer attributed to a chat.
--
-- Apply with:
--   psql -h 127.0.0.1 -p 5432 -U postgres -d setu \
--        -f db/migrations/V9__business_plan_conversation_scope.sql
--
-- Safe to run twice.
-- =====================================================================

BEGIN;

ALTER TABLE business_plans
    ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(36)
        REFERENCES conversations(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_business_plans_conversation_id
    ON business_plans (conversation_id);

COMMIT;
