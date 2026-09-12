-- =====================================================================
-- SRS generation: after a plan is vetted, the user may upload their own
-- SRS format (a .docx) and get it back with every vetted story written
-- into the section it belongs in.
--
-- One plan has at most one SRS document; uploading a new format replaces
-- the row rather than adding a second one.
--
-- Unlike db/migrations/V2__*.sql and V4__*.sql, this table is NOT also
-- added to V1__initial_schema.sql: V1's checksum is recorded by Flyway on
-- any database created after Flyway landed here, so editing it now would
-- fail `flyway migrate` on those. A fresh database therefore gets this
-- table from this migration, which runs straight after V1.
--
-- Apply with:
--   psql -h 127.0.0.1 -p 5432 -U postgres -d setu \
--        -f db/migrations/V6__srs_documents.sql
--
-- Safe to run twice.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS srs_documents (
    id                VARCHAR(36)  PRIMARY KEY,
    -- UNIQUE: the plan's one SRS. Replacing the format overwrites this row.
    plan_id           VARCHAR(36)  NOT NULL UNIQUE
                      REFERENCES business_plans(id) ON DELETE CASCADE,
    user_id           VARCHAR(36)  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    template_filename VARCHAR(300) NOT NULL DEFAULT '',
    -- The .docx the user uploaded, kept so the SRS can be regenerated after
    -- more items are vetted without asking them to upload the format again.
    template_bytes    BYTEA        NOT NULL,
    -- The filled-in .docx served by the download endpoint. NULL until a
    -- generation run succeeds.
    output_bytes      BYTEA,
    status            VARCHAR(20)  NOT NULL DEFAULT 'UPLOADED',
    -- JSON: which section each story was written into, for the UI to show.
    placements        TEXT         NOT NULL DEFAULT '[]',
    -- Set when the template had no usable heading outline and the stories
    -- were appended under a new heading at the end instead.
    notice            TEXT         NOT NULL DEFAULT '',
    error_message     TEXT         NOT NULL DEFAULT '',
    created_at        TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    updated_at        TIMESTAMP    NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    CONSTRAINT srs_documents_status_check
        CHECK (status IN ('UPLOADED', 'READY', 'ERROR'))
);

COMMIT;
