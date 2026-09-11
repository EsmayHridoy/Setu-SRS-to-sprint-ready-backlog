-- =====================================================================
-- Business vetting: three-step BA/architect framework.
--
-- Adds the richer step-by-step vetting result (current business, requirement
-- clarity, integration strategy) alongside the original columns, which are
-- kept as-is and still populated (derived from the new fields) so nothing
-- that reads them breaks.
--
-- A fresh setu_postgres.sql already includes all of this. Run this only
-- against a database created before it, to keep its data:
--
--   psql -h 127.0.0.1 -p 5432 -d setu -f db/migrations/002_business_vetting_steps.sql
--
-- Safe to run twice.
-- =====================================================================

BEGIN;

-- Step 1 -- the current business the requirement's area implements today.
ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS current_business TEXT NOT NULL DEFAULT '';

-- Step 2 -- feasibility, including whether the requirement itself is clear
-- enough to act on (as opposed to vague/ambiguous and needing the client
-- asked back).
ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS is_requirement_clear BOOLEAN;
ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS is_feasible BOOLEAN;

-- Step 3 -- integration strategy: already served today, extend an existing
-- feature, or build as a new one -- and what that choice touches.
ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS already_supported BOOLEAN;
ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS integration_approach VARCHAR(30);
ALTER TABLE business_items DROP CONSTRAINT IF EXISTS business_items_integration_approach_check;
ALTER TABLE business_items ADD CONSTRAINT business_items_integration_approach_check
    CHECK (integration_approach IS NULL OR integration_approach IN (
        'ALREADY_SUPPORTED', 'EXTEND_EXISTING_FEATURE', 'NEW_FEATURE_OR_ENDPOINT'
    ));
ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS related_existing_feature TEXT NOT NULL DEFAULT '';
ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS integration_notes TEXT NOT NULL DEFAULT '';

COMMIT;
