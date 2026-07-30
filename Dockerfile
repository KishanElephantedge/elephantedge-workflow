FROM python:3.11-slim

# Node.js is needed only for the Deepline CLI (an npm package), which this backend now
# calls as a subprocess (Phase 6 Decision Maker search). Same reasoning as synefi's
# Dockerfile: bundle the one cross-runtime dependency via Docker, standard practice for
# "Python app needs a CLI written in another runtime."
#
# redis-server runs IN this same container (started by the CMD below, before uvicorn) --
# a pure cache for GET /batches/{id}, not a separate Render service, so it never adds its
# own cold-start surface. See app/cache.py for the cache-aside logic.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates redis-server && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g deepline@0.1.254 && \
    apt-get purge -y curl && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Deepline auth is env-var based (DEEPLINE_API_KEY, DEEPLINE_HOST_URL) -- set as Render
# environment variables, not baked into this image or committed to the repo.
#
# deepline is pinned to 0.1.254, not "latest" -- 0.1.271 (published 2026-07-24) has a
# regression that fails auth with a valid API key (401 AUTH_ERROR). Found live during this
# service's first deploy, since it was the first build to pull past that version.

EXPOSE 8000
CMD ["sh", "-c", "redis-server --daemonize yes --save '' && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
