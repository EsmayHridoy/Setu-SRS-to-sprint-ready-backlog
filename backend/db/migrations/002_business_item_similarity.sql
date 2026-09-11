-- =====================================================================
-- Similarity percentage on vetted business items: upgrade an existing
-- database in place.
--
-- A fresh setu_postgres.sql already includes all of this. Run this only
-- against a database created before it, to keep its data:
--
--   psql -h 127.0.0.1 -p 5432 -d setu -f db/migrations/002_business_item_similarity.sql
--
-- Safe to run twice. Items vetted before this keep a NULL percentage until
-- they are vetted again.
-- =====================================================================

BEGIN;

ALTER TABLE business_items
    ADD COLUMN IF NOT EXISTS similarity_percent INTEGER,
    ADD COLUMN IF NOT EXISTS similar_feature    TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS similarity_notes   TEXT NOT NULL DEFAULT '';

ALTER TABLE business_items
    DROP CONSTRAINT IF EXISTS business_items_similarity_percent_check;
ALTER TABLE business_items ADD CONSTRAINT business_items_similarity_percent_check
    CHECK (similarity_percent BETWEEN 0 AND 100);

COMMIT;
