-- =====================================================================
-- Adds a `scope` field to a vetted item: the boundary of the specific
-- requirement (what is in scope and what is explicitly out of scope for
-- it), distinct from `impacted_areas` (which existing system areas it
-- touches). Written by both the initial vetting pass and by the item
-- discussion/re-vet loop (V7).
--
-- Apply with:
--   psql -h 127.0.0.1 -p 5432 -U postgres -d setu \
--        -f db/migrations/V8__business_item_scope.sql
--
-- Safe to run twice.
-- =====================================================================

BEGIN;

ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT '';

COMMIT;
