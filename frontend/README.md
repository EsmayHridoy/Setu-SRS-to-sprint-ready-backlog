# Setu Frontend (basic API client)

Minimal React (Vite) app that calls the Setu FastAPI backend. No UI styling — just functional forms and lists.

## Prerequisites

- Backend running at `http://localhost:8000`
- Node.js 18+

## Setup

```bash
cd frontend
npm install
npm run dev
```

Opens at `http://localhost:5173` (allowed by backend CORS).

## Config

`.env`:

```
VITE_API_BASE_URL=http://localhost:8000
```

## Flow

1. Pick an account from `GET /api/auth/accounts` (sends `X-User-Id` on later calls)
2. Chat: pick project → list/create conversations → send messages
3. Admin tab (if user has admin role): roles, projects, users, audit

## API helper

See `src/api.js` for all wrapped endpoints.
