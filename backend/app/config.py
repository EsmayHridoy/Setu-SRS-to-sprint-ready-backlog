"""Application settings, read from the environment.

A .env file sitting next to this package is loaded first, so values there
behave exactly like real environment variables. A variable already set in the
shell always wins, which is what makes a one-off override work:

    DATABASE_URL=postgresql+psycopg://... uvicorn app.main:app

This application runs on PostgreSQL only. There is no SQLite fallback, on
purpose: a fallback turns a misconfigured connection into a silent switch to a
different, empty database, and you find out hours later when the data you
expected is not there.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# backend/.env — one level up from backend/app/
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_FILE)


class ConfigError(RuntimeError):
    """Raised when the application cannot start with the given settings."""


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()

    if not url:
        raise ConfigError(
            "DATABASE_URL is not set.\n\n"
            f"  Create {ENV_FILE} containing:\n\n"
            "    DATABASE_URL=postgresql+psycopg://postgres:PASSWORD"
            "@127.0.0.1:5432/setu\n\n"
            "  See db/POSTGRES.md for the full setup."
        )

    if url.startswith("sqlite"):
        raise ConfigError(
            "DATABASE_URL points at SQLite. This application runs on "
            "PostgreSQL only.\n\n"
            "    DATABASE_URL=postgresql+psycopg://postgres:PASSWORD"
            "@127.0.0.1:5432/setu"
        )

    if not url.startswith("postgresql"):
        raise ConfigError(
            f"DATABASE_URL must be a PostgreSQL URL, got: {url.split('://')[0]}"
            "://…\n\n"
            "    DATABASE_URL=postgresql+psycopg://postgres:PASSWORD"
            "@127.0.0.1:5432/setu"
        )

    # psycopg2 is not installed; steer the common typo somewhere useful.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    return url


class Settings:
    """Built inside __init__ rather than as class attributes.

    Class-body assignments run at import time, which would let a ConfigError
    escape as a raw traceback before get_settings() could turn it into a
    readable message.
    """

    def __init__(self) -> None:
        self.database_url = _database_url()

        # Encrypts repository access tokens at rest. Generate one with:
        #   python -c "from cryptography.fernet import Fernet; \
        #              print(Fernet.generate_key().decode())"
        self.secret_key = os.getenv("SETU_SECRET_KEY", "")

        self.cors_origins = os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")

        # Placeholder answers instead of a real model. Flip to false once the
        # analysis engine is wired up.
        self.use_placeholder_ai = (
            os.getenv("USE_PLACEHOLDER_AI", "true").lower() == "true"
        )

        # Connection pool. Raise these when more than a handful of people use it.
        self.pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
        self.max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))

        # ADK agents that talk to a live repository through GitHub's hosted
        # MCP server, backed by a local Ollama model via LiteLLM (see
        # adk_runner.build_model). No API key: Ollama runs locally. Left
        # unset, app/github_agent.py refuses with a clear error at call time
        # rather than the app failing to start -- this feature is opt-in,
        # unlike DATABASE_URL above.
        self.ollama_model = os.getenv("OLLAMA_MODEL", "qwen3.6:27b")
        self.ollama_api_base = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
        # A fine-grained PAT scoped to a single repo. That scope, set when the
        # token is created on GitHub, is what limits the agent -- not this app.
        self.github_pat = os.getenv("GITHUB_PAT", "")
        # "owner/repo" the PAT above is actually scoped to, e.g. "acme/api".
        # Purely for prompting: without it, agents have no way to know which
        # repository they can reach and waste turns guess-searching GitHub by
        # a human-readable project name that may not match the repo slug.
        self.github_repo = os.getenv("GITHUB_REPO", "")
        self.github_mcp_url = os.getenv(
            "GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/"
        )
        self.github_mcp_readonly = (
            os.getenv("GITHUB_MCP_READONLY", "true").lower() == "true"
        )
        # "all" (21 toolsets) pushes every request's tool-schema payload past
        # 12k tokens before any real content -- easily blows a free-tier
        # tokens-per-minute cap on its own. GitHub's own "default" toolset
        # (context, repos, issues, pull_requests, users) already covers what
        # these agents need: reading code, issues and PRs.
        self.github_mcp_toolsets = os.getenv("GITHUB_MCP_TOOLSETS", "default")

    @property
    def safe_database_url(self) -> str:
        """The URL with the password removed, for logs and error messages."""
        url = self.database_url
        if "@" not in url:
            return url
        scheme, rest = url.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ConfigError as exc:
        print(f"\nConfiguration error\n\n{exc}\n", file=sys.stderr)
        raise SystemExit(1) from exc