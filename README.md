# Setu

Setu analyzes SRS documents against your existing codebase and produces sprint-ready backlog items. Upload a PDF or DOCX, chat about the requirements, and let Setu vet each business requirement against the code — flagging what's already built, what's feasible, and what needs a story written for it.

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI, Python 3.10+, SQLAlchemy |
| Frontend | React 19, Vite 8 |
| Database | PostgreSQL (schema in `db/setu_postgres.sql`) |
| AI | Google Gemini via ADK + GitHub MCP |
| Auth | JWT (python-jose), bcrypt, slowapi rate limiting |

## Prerequisites

- Python 3.10+
- Node.js 18+
- PostgreSQL running on port 5432

## Quick start

### 1. Database

```bash
psql -h 127.0.0.1 -p 5432 -U postgres -c "CREATE DATABASE setu;"
psql -h 127.0.0.1 -p 5432 -U postgres -d setu -f backend/db/setu_postgres.sql
```

### 2. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
uvicorn app.main:app --reload --port 8000
```

API docs at http://localhost:8000/docs.

### 3. Frontend

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173
```

## Configuration

Copy `backend/.env.example` to `backend/.env`. The key variables:

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | `postgresql+psycopg://postgres:PASSWORD@127.0.0.1:5432/setu` |
| `SETU_SECRET_KEY` | Yes (prod) | Encrypts stored repo tokens — generate with Fernet |
| `GEMINI_API_KEY` | For AI | Get from [Google AI Studio](https://aistudio.google.com/apikey) |
| `GEMINI_MODEL` | No | Default: `gemini-3.5-flash-lite` |
| `GITHUB_PAT` | For GitHub agent | Fine-grained PAT scoped to one repo |
| `GITHUB_REPO` | For GitHub agent | `owner/repo` the PAT is scoped to |
| `USE_PLACEHOLDER_AI` | No | `true` skips the model for local dev |
| `CORS_ORIGINS` | No | Default: `http://localhost:5173` |

Generate a secret key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Default admin account

The SQL seed creates an admin user:

- **Email:** `admin@setu.local`
- **Password:** `abc123$`

Change it after first login.

## How it works

1. **Upload** a PDF or DOCX requirements document in the chat.
2. Setu extracts distinct **business requirements** and shows them as an editable list.
3. **Confirm** the list — Setu vets each requirement one at a time against the connected codebase via the GitHub agent.
4. Each vetted item gets a verdict, feasibility flag, user story, actors, pre-conditions, acceptance criteria, and impacted areas.

## Project structure

```
backend/
  app/
    main.py            entry point, CORS, startup checks
    config.py          settings from .env
    db.py              SQLAlchemy engine and session
    models.py          database tables
    schemas.py         Pydantic API contracts
    security.py        JWT auth, admin guard, project access
    routers/           auth, projects, chat, uploads, admin, agent, business
  db/
    setu_postgres.sql  canonical schema + seed data
    migrations/        incremental upgrade scripts
  requirements.txt

frontend/
  src/
    App.jsx            entire UI (login, chat, admin, business plan cards)
    api.js             typed fetch wrappers for every endpoint
    index.css          styles
  vite.config.js
```

## Access model

A user reaches a project if **any** role they hold grants it. Role grants take effect immediately — no re-login needed. Project-scoped endpoints return 404 (not 403) for unauthorized projects so callers cannot enumerate what they cannot see.

## Development notes

- Set `USE_PLACEHOLDER_AI=true` to develop without a Gemini key. Answers are drawn from sample database rows.
- `db/setu_postgres.sql` owns the schema; SQLAlchemy never runs `create_all`. If the app refuses to start with a missing-table error, re-run the SQL script or apply the relevant migration from `db/migrations/`.
- `project_ids` and `role_ids` in admin payloads **replace** the full list, not append to it.
- `access_token` on projects is write-only; omit the field to keep the stored token, send `""` to clear it. Responses carry only `token_last4`.
