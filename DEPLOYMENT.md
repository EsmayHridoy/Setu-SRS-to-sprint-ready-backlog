# Setu — Deployment Guide (GitHub + Hostinger VPS + Dokploy)

Same architecture pattern as SohozDesk / InvoicePro: two isolated services +
Postgres, all as separate Dokploy apps on the same VPS.

**Status: live.**
- Frontend: https://setu.<vps-ip>.nip.io
- Backend:  https://api.setu.<vps-ip>.nip.io/api/health

## Architecture

```
                 ┌─────────────────────────── Hostinger VPS ───────────────────────────┐
                 │                        Dokploy  (Traefik + Docker)                    │
   Browser  ───► │  https://setu.<vps-ip>.nip.io           →  FRONTEND (Nginx + React)       │
        │        │                                    │  calls API over HTTPS            │
        └──────► │  https://api.setu.<vps-ip>.nip.io       →  BACKEND (FastAPI:8000)          │
                 │                                    │                                  │
                 │                         POSTGRES (Dokploy DB, private)                │
                 └──────────────────────────────────────────────────────────────────────┘
```

- **Frontend** = Nginx serving the React SPA (container port **80**). Own domain + HTTPS.
- **Backend** = FastAPI/uvicorn (container port **8000**). Own domain + HTTPS.
- They talk over the **public API URL**. Backend `CORS_ORIGINS` must allow the frontend origin.
- **No manual schema step** — the backend image runs Flyway automatically on
  container start; a brand-new empty database gets the full schema and seed
  data on first boot, no `psql -f ...` needed.

Hostnames (nip.io — swap for a real domain if you have one, same VPS pattern as SohozDesk):
- Frontend: `setu.<vps-ip>.nip.io`
- Backend:  `api.setu.<vps-ip>.nip.io`

---

## Part 1 — GitHub images already building

Tag from **staging** or **main**:
```bash
git tag setu-production-1.0.0
git push origin setu-production-1.0.0
```

Images (private, on GitHub Container Registry):
- `ghcr.io/esmayhridoy/setu-srs-to-sprint-ready-backlog-backend:<tag>`
- `ghcr.io/esmayhridoy/setu-srs-to-sprint-ready-backlog-frontend:<tag>`

---

## Part 2 — GitHub PAT for Dokploy (once)

Images are private, so Dokploy needs a credential to pull them — GitHub's
equivalent of GitLab's Deploy Token.

GitHub → your account (or a dedicated bot account) → **Settings → Developer
settings → Personal access tokens → Tokens (classic)**
- Scope: **`read:packages`** only
- Generate → save the token

Save alongside it: your GitHub **username** — Dokploy's registry login wants both.

---

## Part 3 — Database in Dokploy

1. Dokploy → **Create Project** → `setu` (separate from SohozDesk/InvoicePro).
2. **Create Service → Database → PostgreSQL**
   - Name: `setu-db`
   - DB / user / strong password, image `postgres:16`
3. Deploy → note the **internal host** (e.g. `setu-db`), port `5432`.

No schema to load here — the backend does it on first start (Part 4).

---

## Part 4 — Backend app (Dokploy)

1. Project → **Application** → `setu-backend`
2. **Source: Docker Image**
   - Image: `ghcr.io/esmayhridoy/setu-srs-to-sprint-ready-backlog-backend:latest`
   - Registry: `ghcr.io` + the PAT from Part 2 (username = your GitHub username, password = the token)
3. **Environment** — see `backend/.env.example` for the full, current list; the ones that matter for a real deployment:
   ```
   DATABASE_URL=postgresql+psycopg://setu:<db password>@setu-db:5432/setu
   SETU_SECRET_KEY=<generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
   CORS_ORIGINS=https://setu.<vps-ip>.nip.io
   USE_PLACEHOLDER_AI=false
   GEMINI_MODEL=gemini-3.5-flash-lite
   GEMINI_API_KEY=<from aistudio.google.com/apikey>
   GEMINI_THINKING_LEVEL=medium
   GITHUB_PAT=<fine-grained PAT scoped to the ONE repo Setu should read>
   GITHUB_REPO=<owner/repo the PAT above is scoped to>
   GITHUB_BRANCH=<branch to read, blank = default branch>
   ```
4. **Domain:** `api.setu.<vps-ip>.nip.io`
   - **Container Port:** `8000`
   - Enable **HTTPS** (Let's Encrypt)
5. Deploy → watch the logs for the Flyway migration report, then check
   `https://api.setu.<vps-ip>.nip.io/api/health`

> If the deploy log shows a Flyway connection error, double-check
> `DATABASE_URL` matches Part 3's internal host/port/password exactly.

---

## Part 5 — Frontend app (Dokploy) — Nginx

1. **Application** → `setu-frontend`
2. **Source: Docker Image**
   - Image: `ghcr.io/esmayhridoy/setu-srs-to-sprint-ready-backlog-frontend:latest`
   - Same registry credentials as Part 4
3. **Domain:** `setu.<vps-ip>.nip.io`
   - **Container Port:** `80` ← Nginx inside the image
   - Enable **HTTPS**
4. Deploy → open `https://setu.<vps-ip>.nip.io`

The SPA's API URL is **baked in at build time**, not read at container start
(see Part 6) — it must already be correct in the image you deploy here.

---

## Part 6 — Wire-up checklist (order matters)

Unlike the backend, the frontend can't pick up its API URL from a Dokploy
environment variable — Vite bakes `VITE_API_BASE_URL` into the JS bundle
*when the image is built* in GitHub Actions, not when the container starts.
So the backend's real URL must be known **before** the frontend image is
built:

1. Deploy the backend first (Part 4), confirm `api.setu.<vps-ip>.nip.io` responds.
2. Set the GitHub repo variable: **Settings → Secrets and variables →
   Actions → Variables → `VITE_API_BASE_URL`** = `https://api.setu.<vps-ip>.nip.io`
3. Cut a new tag so the frontend rebuilds with that URL baked in:
   ```bash
   git tag setu-production-1.0.1
   git push origin setu-production-1.0.1
   ```
4. Deploy the frontend (Part 5) using that new tag's image.

| Setting | Where | Value |
|---|---|---|
| SPA API URL | GitHub Actions variable `VITE_API_BASE_URL` | `https://api.setu.<vps-ip>.nip.io` |
| CORS | backend env `CORS_ORIGINS` | `https://setu.<vps-ip>.nip.io` |

Mismatch on either → browser CORS errors or the SPA calling the wrong host.

---

## Part 7 — CI → Dokploy auto-redeploy

The `deploy` job in `.github/workflows/docker-publish.yml` is already there —
it curls each app's Dokploy Deploy Webhook right after both images finish
building. It only needs the webhook URLs, stored as GitHub secrets:

1. Dokploy → `setu-backend` app → **Deployments** tab → copy the **Webhook URL**.
2. Same for `setu-frontend`.
3. GitHub → **Settings → Secrets and variables → Actions → Secrets** → **New repository secret**:
   ```
   DOKPLOY_BACKEND_WEBHOOK  = <backend webhook URL>
   DOKPLOY_FRONTEND_WEBHOOK = <frontend webhook URL>
   ```

Until both secrets are set, the `deploy` job just prints a note and skips —
it won't fail the build. Once they're set, every `setu-production-*` tag
redeploys both apps automatically; no more manual "Rebuild" click.

---

## Everyday

```bash
# develop
git push origin staging

# ship
git tag setu-production-x.y.z
git push origin setu-production-x.y.z
# then click Deploy on the Dokploy app(s) that changed
```
