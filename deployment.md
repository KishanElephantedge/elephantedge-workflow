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

## Services (as of 2026-09-15 — Account 3 active; see "Three-account rotation" below)

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

**SUPERSEDES the `VITE_GATEWAY_URL` env var described here until 2026-09-15**: as of
2026-08-19 (see `synefi/dashboard/src/api/client.js`'s own comment), production API calls go
through `/gw`, which **`synefi/dashboard/vercel.json`'s `rewrites`** proxies server-side to
the real gateway URL — not a baked-in env var read by the SPA. This was fixed to make the
session cookie first-party (Safari blocks third-party cookies outright, breaking login on
iPhone under the old direct-to-gateway approach). **To point the frontend at a different
gateway: edit the two `destination` URLs in `vercel.json`, commit, push** (`synefi-workflow`
repo, `dashboard/` subfolder) — Vercel auto-deploys on push, no manual redeploy needed. Local
dev (Vite dev server) still talks to the gateway directly, unaffected.

This is a real, static file — Vercel rewrites cannot read an environment variable at request
time, so *some* concrete URL has to live there. The honest way to stop needing a code push on
every rotation would be a stable custom domain (e.g. `gw.elephantedge.ai`) pointed via DNS at
whichever Render account is currently active, with `vercel.json` permanently aimed at that one
name — not yet set up, needs a domain the user controls and Render's Custom Domain feature.

## Keep-alive (prevents Render free-tier cold starts)

Render's free tier spins a service down after ~15 min of no inbound HTTP traffic; the next
real request then has to wait through a cold start (30-60s+), which can look like a CORS
error, a connection failure, or an HTTP 503 in the browser even though the actual cause is
just "the service wasn't warm yet."

**Real gap found 2026-09-15**: this section used to describe two keep-alive mechanisms as
already live. Checked directly against GitHub — `.github/workflows/keepalive.yml` was never
actually committed to the `workflow-automation` repo, on any branch, ever. It only ever
existed as a plan. Whatever gap the cron-job.org side left uncovered (e.g. after a rotation to
a new account whose URLs were never added there) was never caught by a second mechanism, since
the GitHub Actions half didn't exist to catch it.

1. **cron-job.org** (external scheduler, real account, not accessible by any session) — the
   only mechanism that has ever actually been live. Pings `/api/health` (not `/docs` — found
   live that `/docs`'s response was too large for cron-job.org's size limit). **Must be
   updated by hand whenever the active account rotates** — add/update jobs for all SIX known
   URLs below so whichever account is currently idle (and therefore next in line once the
   active one gets suspended) is already warm, not just the active pair:
   - `https://elephantedge-gateway.onrender.com/api/health` / `https://elephantedge-main.onrender.com/api/health` (Account 1)
   - `https://workflow-automation-48nk.onrender.com/api/health` / `https://elephantedge-workflow-1.onrender.com/api/health` (Account 2)
   - `https://workflow-automation-1ujz.onrender.com/api/health` / `https://elephantedge-workflow-1-7k9d.onrender.com/api/health` (Account 3 — active as of 2026-09-15)
2. **GitHub Actions** (`gateway/.github/workflows/keepalive.yml`) — written 2026-09-15,
   pinging the same six URLs every 10 min, but **not pushed**: the git token used by this
   session's pushes lacks the `workflow` OAuth scope GitHub requires specifically for files
   under `.github/workflows/` (confirmed live — every other file in the same commit pushes
   fine). The file exists locally in the `gateway` checkout; add it via GitHub's web UI
   (Add file -> Create new file, paste its contents) or push with a token that has that scope.

## Three-account rotation (explicit decision, 2026-09-15)

Rather than pay for Render's paid tier (~$25/mo) while budget is tight, the working pattern
is: run on Render's free tier, and when the active account hits its 5GB/month bandwidth cap
and gets suspended, switch to whichever of the other two accounts has since had its own cap
reset (Render resets free-tier usage monthly) and is live again. Three accounts exist today,
each with the SAME gateway+backend pair deployed from the SAME two GitHub repos (so all three
auto-deploy every push, regardless of which one is "active"):

| Account | Gateway | Backend | Status as of 2026-09-15 |
|---|---|---|---|
| 1 ("Kishan's workspace") | `elephantedge-gateway.onrender.com` | `elephantedge-main.onrender.com` | Suspended (bandwidth cap) |
| 2 | `workflow-automation-48nk.onrender.com` | `elephantedge-workflow-1.onrender.com` | Live, idle |
| 3 | `workflow-automation-1ujz.onrender.com` | `elephantedge-workflow-1-7k9d.onrender.com` | **Live, active** |

**To switch which account is active, both of these must change together** (mismatching them
means the gateway is reachable but proxies to the wrong/suspended backend, or vice versa):
1. `synefi/dashboard/vercel.json` — both rewrite `destination` URLs, to the new gateway.
2. `tenants.backend_url` (shared DB, `elephant-edge` row, and any partner tenant rows that
   were set from it) — to the new backend. No existing admin API route does this yet (see
   TODO.md); today it needs a direct DB update.

A real, recurring gap this causes: **partner tenants' `backend_url` is copied from Elephant
Edge's row only at tenant-CREATION time** (`gateway/app/main.py`'s `create_tenant`) — it is
not a live reference. Rotating the active account updates Elephant Edge's own tenant fine, but
every existing partner tenant keeps pointing at whatever was active when THEY were created,
silently going stale on the very next rotation unless updated explicitly at the same time.

## Incident log

### 2026-09-15 — Account 3's suspension went unnoticed because nothing was pointed at Account 3

Account 1 (`elephantedge-gateway`/`elephantedge-main`) hit its 5GB/month bandwidth cap and got
suspended — the same real Render-level "This service has been suspended by its owner" page as
2026-08-12 below, not a code issue. But `vercel.json` was STILL pointed at Account 1 even
though Accounts 2 and 3 had already been created and were live (both fully current, having
auto-deployed every commit since) — a past rotation created the new accounts but never
finished repointing the frontend at one of them, so the system silently kept relying on the
one that was about to run out of bandwidth.

Fixed by repointing `vercel.json` at Account 3. Separately hit a real 503 immediately after,
traced to the keep-alive gap documented above (the GitHub Actions half never existed) — a
freshly-pointed-at, previously-idle account is exactly the case with no warm-up traffic yet.

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
