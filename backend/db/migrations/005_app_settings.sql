-- App-level settings editable by admins through the UI.
-- Sensitive values (API keys, PATs) are stored encrypted via the app's
-- crypto module, not in plain text here.
CREATE TABLE IF NOT EXISTS app_settings (
    key         VARCHAR(80)  PRIMARY KEY,
    value       TEXT         NOT NULL DEFAULT '',
    is_sensitive BOOLEAN     NOT NULL DEFAULT false,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
