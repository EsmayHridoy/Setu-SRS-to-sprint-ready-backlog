# Multi-stage build: compile wheels in a full image, run in a slim one so the
# published image doesn't carry a C compiler and apt caches.
FROM python:3.12-slim AS builder

WORKDIR /app

# build-essential covers the rare transitive dependency that ships no
# prebuilt wheel for this platform; psycopg[binary] and cryptography already
# bring their own, so this is a safety net, not the common path.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local

COPY app ./app
COPY db ./db

# Runs as an unprivileged user; DATABASE_URL, GEMINI_API_KEY, GITHUB_PAT etc.
# are injected at `docker run` / compose time, never baked into the image.
RUN useradd --create-home --uid 1000 setu && chown -R setu:setu /app
USER setu

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
