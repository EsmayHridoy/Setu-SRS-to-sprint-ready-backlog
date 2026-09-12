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

# Flyway's own image bundles a JRE, but it's linked against musl (Alpine) --
# copying it onto this glibc (Debian) base fails with "cannot execute:
# required file not found" even though the file is right there. The
# /flyway directory itself (the launcher script + jars + JDBC drivers) is
# plain Java bytecode and portable, so only it comes from here. Flyway 10
# needs Java 17+.
FROM flyway/flyway:10-alpine AS flyway

# A glibc-linked JRE at the same /opt/java/openjdk path flyway's own script
# already expects -- copied in instead of `apt-get install`ing one, which
# depends on Debian's package mirrors being reachable at build time and
# isn't otherwise needed here.
FROM eclipse-temurin:17-jre AS jre


FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=flyway /flyway /flyway
COPY --from=jre /opt/java/openjdk /opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:${PATH}"

COPY app ./app
COPY db ./db
COPY docker-entrypoint.py .

# Runs as an unprivileged user; DATABASE_URL, GEMINI_API_KEY, GITHUB_PAT etc.
# are injected at `docker run` / compose time, never baked into the image.
RUN useradd --create-home --uid 1000 setu && chown -R setu:setu /app /flyway
USER setu

EXPOSE 8000

# Runs pending Flyway migrations, then execs into uvicorn -- see
# docker-entrypoint.py. Every container start, not just the first: Flyway
# tracks what it already applied and only runs what's new.
CMD ["python3", "docker-entrypoint.py"]
