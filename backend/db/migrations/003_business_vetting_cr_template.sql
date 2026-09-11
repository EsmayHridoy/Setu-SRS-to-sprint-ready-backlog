-- =====================================================================
-- Business vetting: respond in BRAC IT's own Change Request / Story
-- template vocabulary (User Story, Actors, Pre-condition, Impacted Areas,
-- Requirements, Acceptance Criteria, Exceptions) instead of an invented
-- framework.
--
-- Adds the template-shaped fields. The columns added by
-- 002_business_vetting_steps.sql (current_business, feasibility_notes,
-- integration_approach, related_existing_feature, integration_notes,
-- impacts_other_features, impact_notes) are superseded by these and kept,
-- unpopulated by new vettings, so historical rows are not lost.
--
-- A fresh setu_postgres.sql already includes all of this. Run this only
-- against a database created before it, to keep its data:
--
--   psql -h 127.0.0.1 -p 5432 -d setu -f db/migrations/003_business_vetting_cr_template.sql
--
-- Safe to run twice.
-- =====================================================================

BEGIN;

ALTER TABLE business_items ADD COLUMN IF NOT EXISTS user_story TEXT NOT NULL DEFAULT '';
ALTER TABLE business_items ADD COLUMN IF NOT EXISTS actors TEXT NOT NULL DEFAULT '';
ALTER TABLE business_items ADD COLUMN IF NOT EXISTS pre_condition TEXT NOT NULL DEFAULT '';
ALTER TABLE business_items ADD COLUMN IF NOT EXISTS impacted_areas TEXT NOT NULL DEFAULT '';
ALTER TABLE business_items ADD COLUMN IF NOT EXISTS requirements TEXT NOT NULL DEFAULT '';
ALTER TABLE business_items ADD COLUMN IF NOT EXISTS acceptance_criteria TEXT NOT NULL DEFAULT '';
ALTER TABLE business_items ADD COLUMN IF NOT EXISTS exceptions TEXT NOT NULL DEFAULT '';

COMMIT;
