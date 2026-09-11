"""Seed app_settings from the current .env values.

Run from the backend/ directory:
    python db/seed_app_settings.py

Sensitive values are encrypted with the same key the app uses (SETU_SECRET_KEY
from .env, or the dev fallback if that is blank), so the running app can
decrypt them transparently.
"""
import sys
from pathlib import Path

# Allow `from app.xxx import ...` without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings          # loads .env
from app import crypto
from app.db import SessionLocal
from app.models import AppSetting
from datetime import datetime

SETTINGS = [
    # (db_key,            env_value,                              is_sensitive)
    ("gemini_api_key",    get_settings().gemini_api_key or "",    True),
    ("gemini_model",      get_settings().gemini_model or "",      False),
    ("github_pat",        get_settings().github_pat or "",        True),
    ("github_mcp_url",    get_settings().github_mcp_url or "",    False),
    ("github_mcp_readonly",
     "true" if get_settings().github_mcp_readonly else "false",  False),
]


def main() -> None:
    db = SessionLocal()
    try:
        inserted = 0
        skipped = 0
        for key, value, sensitive in SETTINGS:
            if not value:
                print(f"  skip  {key}  (empty in .env)")
                skipped += 1
                continue

            stored = crypto.encrypt(value) if sensitive else value
            row = db.get(AppSetting, key)
            if row is None:
                row = AppSetting(key=key, is_sensitive=sensitive,
                                 value=stored, updated_at=datetime.utcnow())
                db.add(row)
                print(f"  insert {key}")
            else:
                row.value = stored
                row.is_sensitive = sensitive
                row.updated_at = datetime.utcnow()
                print(f"  update {key}")
            inserted += 1

        db.commit()
        print(f"\nDone — {inserted} setting(s) written, {skipped} skipped.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
