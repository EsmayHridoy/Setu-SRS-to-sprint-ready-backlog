"""Read agent-level settings from the database at request time.

DB is always the source of truth. If a required setting is missing a
RuntimeError is raised with a message the caller can surface to the user.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import crypto
from .models import AppSetting

_LABELS = {
    "gemini_api_key": "Google / Gemini API Key",
    "gemini_model": "Gemini Model",
    "github_pat": "GitHub Personal Access Token",
    "github_mcp_url": "GitHub MCP URL",
    "github_mcp_readonly": "GitHub MCP Read-only",
}

_SENSITIVE = {"gemini_api_key", "github_pat"}


def _read(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    if not row or not row.value:
        return ""
    return crypto.decrypt(row.value) if key in _SENSITIVE else row.value


def require(db: Session, key: str) -> str:
    """Return the value or raise a user-friendly RuntimeError."""
    value = _read(db, key)
    if not value:
        label = _LABELS.get(key, key)
        raise RuntimeError(
            f"{label} is not configured. "
            "Go to Admin → Configuration to set it."
        )
    return value


def get_runtime_config(db: Session) -> dict:
    """Return all agent settings from DB, raising if any required key is missing."""
    return {
        "gemini_api_key": require(db, "gemini_api_key"),
        "gemini_model":   require(db, "gemini_model"),
        "github_pat":     require(db, "github_pat"),
        "github_mcp_url": require(db, "github_mcp_url"),
        "github_mcp_readonly": _read(db, "github_mcp_readonly") != "false",
    }
