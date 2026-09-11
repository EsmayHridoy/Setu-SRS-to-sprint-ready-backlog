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
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from . import crypto
from .config import get_settings
from .db import SessionLocal, engine
from .limiter import limiter
from .routers import admin, agent, auth, business, chat, projects, uploads

log = logging.getLogger("setu")
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

settings = get_settings()

REQUIRED_TABLES = {
    "users", "roles", "user_roles", "projects", "role_projects",
    "artifacts", "conversations", "messages", "citations",
    "audit_events", "jobs", "business_plans", "business_items",
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

    message_columns = {c["name"] for c in inspect(engine).get_columns("messages")}
    if "business_plan_id" not in message_columns:
        print(
            "\nThe database schema is out of date: messages.business_plan_id "
            "is missing.\n\n"
            "  Upgrade it in place (keeps your data) with:\n\n"
            "    psql -h 127.0.0.1 -p 5432 -d setu "
            "-f db/migrations/001_business_plans_in_chat.sql\n",
            file=sys.stderr,
        )
        raise SystemExit(1)


# Checked at import so a misconfiguration exits cleanly, before uvicorn has
# started serving. Raising SystemExit inside the async lifespan instead would
# surface as an anyio TaskGroup traceback dozens of lines long.
_check_database()


def _load_db_settings() -> None:
    """Apply any admin-saved settings from the DB over the env-based defaults."""
    from . import crypto as _crypto
    from .models import AppSetting
    from .routers.admin import _SETTINGS_META

    try:
        db = SessionLocal()
        try:
            rows = db.query(AppSetting).all()
        finally:
            db.close()
    except Exception:
        return  # table may not exist yet on first run

    for row in rows:
        meta = _SETTINGS_META.get(row.key)
        if not meta or not row.value:
            continue
        value = _crypto.decrypt(row.value) if meta["sensitive"] else row.value
        if value is None:
            continue
        attr = meta["attr"]
        if attr == "github_mcp_readonly":
            setattr(settings, attr, value.lower() == "true")
        else:
            setattr(settings, attr, value)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Connected to %s", settings.safe_database_url)
    _load_db_settings()

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

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
app.include_router(uploads.router)
app.include_router(admin.router)
app.include_router(agent.router)
app.include_router(business.router)


@app.get("/api/health", tags=["system"])
def health():
    return {"status": "ok"}