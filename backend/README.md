# Setu — backend

FastAPI service. Role-based project access, an administration API, and a chat
surface. **The AI is not connected**: answers come from `placeholder_ai.py`,
which reads sample rows out of the database so the API can be built and tested
before any model is involved.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Docs at http://localhost:8000/docs. The database is SQLite and seeds itself on
first start — delete `setu.db` to start over.

## Authentication

A placeholder. Send the id of a seeded account as a header:

```
X-User-Id: <id from GET /api/auth/accounts>
```

Missing header gives 401. A non-admin calling `/api/admin/*` gives 403.

When SSO arrives, only `current_user` in `security.py` changes. Every
permission check reads from the database and stays as it is.

## Access rule

One rule, applied in one place:

> A person can reach a project if **any** role they hold grants it.

`security.py::projects_for_user` recomputes that union from the database on
every request. The client never states which projects it may see, so changing a
grant takes effect immediately with no re-login.

`authorised_project` is the dependency every project-scoped endpoint uses. It
returns **404, not 403**, for a project you were not granted — someone who
cannot reach a project should not learn that it exists.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | No auth |
| GET | `/api/auth/accounts` | No auth. Seeded accounts |
| GET | `/api/auth/session` | You, admin flag, your projects |
| GET | `/api/projects` | Projects your roles grant |
| POST | `/api/conversations` | `{"project_id": "..."}` |
| GET | `/api/conversations` | Optional `?project_id=` |
| GET | `/api/conversations/{id}` | Full history with citations |
| DELETE | `/api/conversations/{id}` | |
| POST | `/api/conversations/{id}/messages` | `{"content": "..."}` |
| GET POST | `/api/admin/projects` | |
| PUT DELETE | `/api/admin/projects/{id}` | |
| POST | `/api/admin/projects/{id}/reindex` | Stamps a timestamp for now |
| GET POST | `/api/admin/roles` | |
| PUT DELETE | `/api/admin/roles/{id}` | |
| GET POST | `/api/admin/users` | |
| PUT DELETE | `/api/admin/users/{id}` | |
| GET | `/api/admin/audit` | Optional `?limit=` |

Two things that surprise people: `project_ids` and `role_ids` **replace** the
whole list rather than appending, and `access_token` is write-only — omit it to
keep the stored token, send `""` to clear it. Responses carry only
`token_last4`.

## Files

```
app/
  main.py            app, CORS, startup, seeding
  config.py          settings from the environment
  db.py              engine, session, declarative base
  models.py          every table
  schemas.py         the API contract (Pydantic)
  security.py        identity, admin guard, project access
  crypto.py          access-token encryption
  seed.py            sample roles, projects, people, artifacts
  placeholder_ai.py  stand-in for the analysis engine
  routers/
    auth.py          sign-in and session
    projects.py      projects the caller can reach
    chat.py          conversations and messages
    admin.py         roles, projects, people, activity
```

## Connecting the real engine

Everything fake is in `placeholder_ai.py`, called from one marked block in
`routers/chat.py`:

```python
# --- the seam ---
drafted = placeholder_ai.answer(db, conversation.project, question)
```

Rewrite `answer()` against the real pipeline and keep the return type:

1. Expand the question into likely table, column and status-code names
2. Run the hybrid search (keyword + vector, fused by rank), scoped to
   `conversation.project_id`
3. Send the retrieved chunks to the model
4. **Discard any citation whose quoted text is not actually present in the
   artifact it claims to come from**

Step 4 is not optional. A model can attach a real file path to an invented
claim and the path will check out. The `verified` column on `citations` exists
so a checked answer is distinguishable from an unchecked one; the placeholder
sets it to `false` because nothing has been verified.

`artifacts` and `citations` are already shaped for this. When embeddings land,
move to Postgres with pgvector and add the `vector` and `tsvector` columns:

```
DATABASE_URL=postgresql+psycopg://setu:setu@localhost:5432/setu
```

Then set `USE_PLACEHOLDER_AI=false`.

## Configuration

Copy `.env.example` to `.env`. Everything has a working default.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./setu.db` | Postgres when embeddings land |
| `SETU_SECRET_KEY` | *(empty)* | Encrypts access tokens. Set before storing a real one |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma separated |
| `USE_PLACEHOLDER_AI` | `true` | Flags replies as placeholder |
| `SEED_ON_START` | `true` | Only seeds when the users table is empty |

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Known gaps

Deliberate, not oversights.

- **No real authentication.** Anyone can send any `X-User-Id`. Fine on a
  laptop, unacceptable anywhere else.
- **No indexing.** Reindex stamps a timestamp; sample artifacts are seeded.
- **Replies are not streamed.** Worth adding once a model is generating tokens.
- **SQLite.** Move to Postgres before more than one person uses it, and it is
  required for pgvector regardless.
- **No per-project filter on retrieval yet.** Once real code is indexed, every
  retrieval query must filter by the caller's granted projects. The
  `project_id` column on `artifacts` is there for exactly that. Getting it
  wrong means a BA on one project can pull another project's source through
  the chat.
