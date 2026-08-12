# Deployment & Infrastructure

Durable record of how this system is actually hosted, so a future session (or a future
outage) doesn't require re-discovering this from scratch. Read alongside `CLAUDE.md`,
`progress-log.md`, and `roadmap.md`.

**Real secret values (DB password, API keys, JWT secret) are NOT in this file** — they live in
`render.md` in this same directory, which is gitignored (never committed). This file documents
structure and what needs to be set, not the values themselves.

## Architecture

```
Browser
  -> Vercel (dashboard frontend, repo: synefi-workflow)
       -> Render: gateway (repo: workflow-automation)
            -> looks up tenant.backend_url in the shared DB
            -> Render: elephant-edge backend (repo: elephantedge-workflow, this repo)
            -> Render: synefi backend (repo: synefi-workflow's backend, separate service)
  All three backends/gateway share ONE Neon Postgres database (DATABASE_URL identical
  across all of them) -- shared-DB, siloed-compute pattern.
```

The gateway is a real reverse proxy, not just an auth layer: `app.api_route("/api/{tenant_slug}/{path:path}")`
in `gateway/app/main.py` looks up `Tenant.backend_url` from the `tenants` table (columns:
`id`, `slug`, `name`, `backend_url`) and forwards the request there. **To point the whole
system at a different backend, update that one DB row — no gateway code or env var change
needed.**

## Services (as of 2026-08-12, after the free-tier migration below)

| Service | Repo | Render URL | Purpose |
|---|---|---|---|
| Gateway | `KishanElephantedge/workflow-automation` | `https://workflow-automation-1ujz.onrender.com` | Auth (JWT + httpOnly cookie), tenant-scoped reverse proxy |
| Elephant Edge backend | `KishanElephantedge/elephantedge-workflow` (this repo) | `https://elephantedge-workflow-1-7k9d.onrender.com` | All Elephant Edge business logic |
| Synefi backend | `synefi-workflow`'s backend | `https://synefi-workflow.onrender.com` | Synefi tenant (not affected by the 2026-08-12 outage) |
| Dashboard frontend | `KishanElephantedge/synefi-workflow` (dashboard/) | Vercel: `app.elephantedge.ai` | Shared React dashboard for both tenants |

### Elephant Edge backend — config

- **Runtime**: Docker (uses the `Dockerfile` at repo root — installs Node.js + `deepline@0.1.254`
  CLI pinned version, runs Redis in-container, then uvicorn). No build/start command needed in
  Render's UI; the Dockerfile's own `CMD` handles it.
- **Region**: Singapore (Southeast Asia) — matches the Neon DB's region.
- **Required env vars**: `DATABASE_URL`, `DEEPLINE_API_KEY`, `DEEPLINE_HOST_URL` (values in
  `render.md`). Every other credential (Apify, SalesRobot, Smartlead, HubSpot, Jobo, Claude,
  Gemini, Slack) lives in the `credentials` DB table via the `/api/credentials` endpoint, NOT
  as Render env vars — so those survive a service migration automatically, since the DB is
  external to Render.

### Gateway — config

- **Runtime**: Python (native, not Docker).
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Region**: Singapore (Southeast Asia).
- **Required env vars**: `DATABASE_URL`, `COOKIE_SAMESITE=none`, `COOKIE_SECURE=true`,
  `FRONTEND_ORIGIN` (comma-separated allowed origins, currently
  `https://app.elephantedge.ai,https://synefi-workflow.vercel.app`), `JWT_ALGORITHM=HS256`,
  `JWT_EXPIRE_MINUTES=1440`, `JWT_SECRET` (value in `render.md`).

### Dashboard frontend (Vercel) — config

- **Env var**: `VITE_GATEWAY_URL` — must point at the gateway's current Render URL. Baked in
  at build time, so changing it requires a redeploy (Vercel dashboard -> Deployments ->
  Redeploy on latest), not just saving the variable.

## Keep-alive (prevents Render free-tier cold starts)

Render's free tier spins a service down after ~15 min of no inbound HTTP traffic; the next
real request then has to wait through a cold start (30-60s+), which can look like a CORS
error or connection failure in the browser even though the actual cause is just "the service
wasn't warm yet." Two independent keep-alive mechanisms ping all three services every
~5-10 min:

1. **GitHub Actions** (`gateway/.github/workflows/keepalive.yml`, repo `workflow-automation`)
   — pings `/docs` on all three services every 10 min. Editing this file requires a GitHub
   token with `workflow` scope; the default PAT used by this session's git pushes does NOT
   have that scope, so this file may need editing directly on GitHub's web UI instead of via
   a normal `git push` if the token isn't upgraded.
2. **cron-job.org** (external scheduler, real account, not accessible by this session) —
   pings `/api/health` (not `/docs` — found live that `/docs`'s response was too large for
   cron-job.org's size limit) on all three services independently, roughly every 5 min.

**Both must be updated whenever a service's URL changes** (e.g., after a migration like the
one below) — updating only one leaves a real gap where cold starts can still happen.

## Incident log

### 2026-08-12 — Render free-tier suspension, full migration

Both the gateway (`workflow-automation`) and the Elephant Edge backend
(`elephantedge-workflow-1`) were suspended by Render for exceeding free-tier limits (not a
code bug, not a billing/payment failure -- a real free-tier usage cap). Both showed "This
service has been suspended" (a real Render-level page, not an app-level error) on every
request.

**Decision**: rather than upgrading to a paid Render plan or moving to another platform,
recreated both services fresh on a second, separate Render account (still free tier), and
repointed the system at the new URLs. No code changes were needed to the backend or gateway
themselves -- only:
1. Both services recreated on the new account with the same repo/branch/build config (see
   tables above).
2. `tenants.backend_url` row for `elephant-edge` updated (via direct `psql` against the shared
   Neon DB) to the new backend URL.
3. Vercel's `VITE_GATEWAY_URL` updated to the new gateway URL, frontend redeployed.
4. Both keep-alive mechanisms (GitHub Actions cron + cron-job.org) updated to the new URLs.

**Real gotcha hit during migration**: right after cutover, the dashboard showed CORS errors
in the browser console on every API call. This looked like a CORS misconfiguration but
wasn't -- confirmed live via a direct `curl` OPTIONS preflight that the correct
`Access-Control-Allow-Origin` header was already being returned correctly. The real cause was
the brand-new service being cold (just created, no warm-up traffic yet) -- a request that
fails at the connection level before the app container is ready shows up in a browser as a
CORS error even when CORS itself is fine. Resolved on its own within a minute once the
service warmed up; confirmed via a direct `curl` request returning a proper `401 Not
authenticated` (correct behavior for an unauthenticated request) instead of a raw
connection failure.

**Follow-up decision (2026-08-12)**: move to AWS next, funded by the free-tier credit --
research findings and the plan are below.

## Planned: move to AWS (not yet started)

Researched real, current AWS free-tier terms before deciding (AWS changed this July 2025 --
it is no longer a classic "12 months of free EC2 hours" offer):

- A new AWS account gets **$100 in credit immediately**, up to **$200 total** after
  completing a few onboarding tasks (launch/terminate an EC2 instance, set up RDS, deploy a
  Lambda, etc.).
- Credits **expire 12 months after account creation**, or sooner if fully spent.
- After credits run out, the account auto-closes unless explicitly converted to a paid plan
  -- no risk of a surprise bill, but also no ongoing free hosting past that.
- Real ongoing cost estimate once credits run out, for both services (gateway + backend)
  running continuously: roughly **$10-20/month total** (EC2 t3.micro/small or Lightsail,
  Singapore region to match the Neon DB) -- comparable to just paying for Render's own small
  paid tier (~$7/service/month), so AWS isn't meaningfully cheaper long-term, but the ~12
  months of credit is real free runway and avoids the free-tier-suspension problem for that
  period.

**Decision**: use the AWS credit to run both services for the next several months at no cost,
deferring the eventual pay-or-not decision until closer to when the credit runs out. Not
started yet -- when this happens, it's the same migration shape as the 2026-08-12 Render
migration above (recreate both services on AWS, update `tenants.backend_url`, update
`VITE_GATEWAY_URL`, update both keep-alive mechanisms), just onto EC2/Lightsail instead of a
second Render account, and no longer needing the keep-alive workaround at all since a real
EC2/Lightsail instance doesn't cold-start the way Render's free tier does.
