#!/usr/bin/env python3
"""Container entrypoint: run pending Flyway migrations, then start the app.

Runs on every container start, not just the first one -- Flyway records what
it has already applied (in its own flyway_schema_history table) and only
runs anything new, so this is safe to repeat on every restart/redeploy.

baselineOnMigrate handles both cases with one flag: a brand-new empty
database just gets every V1..Vn migration run in order, normally; a database
that already has V1-V5 applied by hand (from before Flyway existed here)
gets baselined at V5 -- marked as already done without re-running V1's
destructive DROP TABLE statements -- and only V6+ actually executes.

subprocess with an argv list is used instead of a shell one-liner so a
password containing shell-special characters (quotes, $, backticks) can
never be misinterpreted or injected.
"""
from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import urlparse

FLYWAY_BASELINE_VERSION = "5"  # last migration applied by hand before Flyway


def _flyway_connection_args() -> list[str]:
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlparse(url)
    jdbc_url = f"jdbc:postgresql://{parsed.hostname}:{parsed.port or 5432}{parsed.path}"
    return [f"-url={jdbc_url}", f"-user={parsed.username}", f"-password={parsed.password}"]


def main() -> None:
    flyway_cmd = [
        "/flyway/flyway",
        *_flyway_connection_args(),
        "-locations=filesystem:/app/db/migrations",
        "-baselineOnMigrate=true",
        f"-baselineVersion={FLYWAY_BASELINE_VERSION}",
        "-baselineDescription=Pre-Flyway manual migrations",
        "migrate",
    ]
    subprocess.run(flyway_cmd, check=True)

    os.execvp("uvicorn", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"])


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"Flyway migration failed (exit {exc.returncode}); not starting the app.",
              file=sys.stderr)
        sys.exit(exc.returncode)
