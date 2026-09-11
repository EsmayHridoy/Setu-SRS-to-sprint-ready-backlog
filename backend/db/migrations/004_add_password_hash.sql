-- 004_add_password_hash.sql
--
-- Adds JWT-based authentication.
--
-- Apply with:
--   psql -h 127.0.0.1 -p 5432 -U postgres -d setu \
--        -f db/migrations/004_add_password_hash.sql

BEGIN;

-- Add the password column (nullable so existing rows are unaffected).
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(128);

-- Insert the system admin account with password abc123$ (bcrypt-hashed).
-- Skip if an account with that email already exists.
DO $$
DECLARE
    v_admin_id  TEXT;
    v_role_id   TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users WHERE email = 'admin@setu.local') THEN
        v_admin_id := gen_random_uuid()::TEXT;

        INSERT INTO users (id, name, email, job_title, is_active, created_at, password_hash)
        VALUES (
            v_admin_id,
            'Admin',
            'admin@setu.local',
            'System Administrator',
            TRUE,
            NOW(),
            '$2b$12$1j2gjwdfeiULrlaK1pDiie8AOLpQXzt42oAFPObGbPeWHgv2wA1AK'
        );

        -- Assign Platform Admin role if it exists.
        SELECT id INTO v_role_id
        FROM   roles
        WHERE  name = 'Platform Admin'
        LIMIT  1;

        IF v_role_id IS NOT NULL THEN
            INSERT INTO user_roles (user_id, role_id)
            VALUES (v_admin_id, v_role_id);
        END IF;
    END IF;
END $$;

COMMIT;
