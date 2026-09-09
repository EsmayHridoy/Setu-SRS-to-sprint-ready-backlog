"""Setu — application entry point.

The database schema is owned by db/setu_postgres.sql, not by SQLAlchemy.
Nothing is created at startup on purpose: `create_all` would build tables
without the CHECK constraints, cascades and indexes the script defines, and the
two would drift apart silently. Instead the app checks the schema is there and
refuses to start with instructions if it is not.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from . import crypto
from .config import get_settings
from .db import engine
from .routers import admin, auth, chat, projects

log = logging.getLogger("setu")
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

settings = get_settings()

REQUIRED_TABLES = {
    "users", "roles", "user_roles", "projects", "role_projects",
    "artifacts", "conversations", "messages", "citations",
    "audit_events", "jobs",
}


def _check_database() -> None:
    """Fail early and clearly rather than on the first request."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        print(
            f"\nCannot reach the database at {settings.safe_database_url}\n\n"
            f"  {exc.orig}\n"
            "  Check PostgreSQL is running and the host, port and password "
            "in .env are right.\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    missing = REQUIRED_TABLES - set(inspect(engine).get_table_names())
    if missing:
        print(
            f"\nThe database is reachable but the schema is missing "
            f"{len(missing)} table(s): {', '.join(sorted(missing))}\n\n"
            "  Load it with:\n\n"
            "    psql -h 127.0.0.1 -p 5432 -U postgres -d setu "
            "-f db/setu_postgres.sql\n\n"
            "  That script creates every table and inserts the sample data. "
            "See db/POSTGRES.md.\n",
            file=sys.stderr,
        )
        raise SystemExit(1)


# Checked at import so a misconfiguration exits cleanly, before uvicorn has
# started serving. Raising SystemExit inside the async lifespan instead would
# surface as an anyio TaskGroup traceback dozens of lines long.
_check_database()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Connected to %s", settings.safe_database_url)

    if crypto.dev_key_in_use():
        log.warning(
            "SETU_SECRET_KEY is not set - repository tokens are encrypted "
            "with a development key. Set one before storing a real token."
        )
    if settings.use_placeholder_ai:
        log.info("Placeholder answers are on. No model is being called.")
    yield


app = FastAPI(
    title="Setu",
    version="0.1.0",
    summary="Requirement review against the systems we have already built.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(chat.router)
app.include_router(admin.router)


@app.get("/api/health", tags=["system"])
def health():
    return {
        "status": "ok",
        "users": users,
    }