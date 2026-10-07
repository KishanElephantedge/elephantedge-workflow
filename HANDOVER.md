# Handover — read this first if you are a new session

*Last updated 2026-10-07. Written so that nothing below has to be re-explained from scratch.
If you are a new Claude session: read `CLAUDE.md` for how to work here, then this file for where
things actually stand, then the two design docs it points at.*

---

## 0. The 60-second version

Elephant Edge runs an autonomous GTM pipeline for itself **and as a back office for partners** —
today ~10–15, growing. Each partner has their own ICP, their own enabled features, and their own
data. The product is the platform; nothing may be built for one named partner.

Right now we are part-way through replacing the hardcoded, single-provider account-sourcing layer
with a **provider router** (design: `provider-router-design.md`). Phases 1 and 2 are live. Phase 3
is next. Separately, a **feature registry** for per-partner feature configuration is agreed and
not yet started.

---

## 1. Standing rules (violating these has cost real money)

1. **Never conclude — verify.** I claimed "no provider supports department headcount" from the two
   providers in front of me. Apollo, Sales Navigator and Crustdata all do. Check the registry, the
   docs, or the live schema before asserting absence.
2. **Never spend without stating the cost and getting an explicit yes**, every time. Not once per
   session — per run.
3. **Test live before declaring done.** Local tests passing is not "working". Several "fixed"
   claims this month were wrong because nothing was run against production.
4. **Nothing partner-specific in shared code.** No `if tenant_id == N`. Features are flags,
   configuration is data. (One violation remains — see §5.)
5. **Validate against every partner, not the one being discussed.** On 2026-10-07 a keyword bug
   broke six of seven partners; the one we were testing with happened to look fine.
6. **A filter seen in a product UI is not an API contract** until the parameter is verified.
7. **No Claude attribution in commit messages.**
8. **Never discard data already paid for.** Persist on receipt, before processing.

---

## 2. Infrastructure, as of today

| Piece | Where | Notes |
|---|---|---|
| Product backend | Render `elephantedge-workflow-1-7k9d.onrender.com` | FastAPI, repo `elephantedge-abm` |
| Gateway | Render `workflow-automation-1ujz.onrender.com` | auth + per-tenant proxy, repo `gateway` |
| Frontend | Vercel, `app.fractionalpartner.us` | repo `synefi/dashboard` — **pushes rejected (403) from this machine** |
| Database | Neon Postgres, shared, one instance | `tenants.backend_url` routes per tenant |

**Render**: 3 free-tier accounts rotated when one is bandwidth-suspended. Accounts 1 and 2 are
suspended; Account 3 is live. Switching = update `tenants.backend_url` + `vercel.json`, not a
redeploy.

**Two outages this month, both worth recognising by their signature:**
- *DB-shaped*: health endpoints 200, every DB-touching route fast 500. Both services fail
  identically because they share one database. Neon was suspended; a DB switch followed.
- *Deploy-shaped*: `/api/health` keeps serving an **old** commit for a long time. Render silently
  keeps the last good build when a new one crash-loops. On 2026-10-04 every deploy had been
  crash-looping for a day on `ModuleNotFoundError: No module named 'psycopg'` — the Neon switch
  moved `DATABASE_URL` to the `postgresql+psycopg` (v3) dialect while only psycopg2 was installed.
  Fixed by adding `psycopg[binary]` to both repos. **If a deploy "doesn't land", check the commit
  hash in `/api/health` before assuming anything else.**

---

## 3. Tenants

| Tenant | Who | Notes |
|---|---|---|
| 2 | Elephant Edge | Own pipeline; also the **billing tenant** — all partner spend reserves against its ledger and its control-plane pause |
| 5 | Jeff Ballard | Channel/alliances consultant. ICP replaced 2026-10-07 from his own PX Practice doc |
| 9 | Remy (GTM University) | |
| 12 | Jeff Platt | RevOps diagnostic — *different person from tenant 5*, easy to confuse |
| 15 | Majji / Fractional Partner | The most-exercised partner. Webinars + email-campaign tabs are gated to this tenant |
| 3, 6, 10, 11, 13, 14 | other partners | each with their own ICP |

Control plane is **paused** for tenants 2 and 15 right now. Un-pause both to run; re-pause after.

---

## 4. What was built today (2026-10-07)

### Provider router — design
`provider-router-design.md`. The core idea: an ICP decomposes into **atoms** (one testable
requirement each), and every atom must end a run in exactly one state — enforced by the provider,
checked by us after fetch, needing research, or verified unsupported. Silently dropping one is
structurally impossible. Researched against Clay's waterfall, Deepline's own published doctrine,
and Explorium/adaptive-routing patterns.

### Phase 1 — registry + atoms (live, commit `2a170f4`)
- `app/gtm_os/sourcing/atoms.py` — ICP → atoms, keeping the partner's original wording.
- `app/gtm_os/sourcing/registry.py` — per-provider capability: the **exact** filter expression each
  accepts. Icypeas (verified), Prospeo, Apollo. Support is **three-state**: `supported` / `absent`
  (verified) / `unverified` (needs research) — never boolean.
- Icypeas' payload is now **rendered from the registry**, not hand-built. Output byte-identical.
- Every run reports `icp_coverage`.

### Phase 2 — resolution (live, commits `42f503c`, `b0339d3`)
- `app/gtm_os/sourcing/resolution.py` — **two levels in order**: (1) which filter in this tool
  expresses the concept, (2) what values that filter accepts. Collapsing them caused the $0.175
  empty page.
- Value resolution order: provider's own resolver → values we've confirmed → LLM **restricted to
  confirmed values** → free-text field → unresolved. An LLM cannot invent a value.
- Two tables (`provider_taxonomy_values`, `icp_term_resolutions`), created via `ensure_indexes()`.
  Icypeas' published taxonomy 404s, so we **learn its real values from rows we already paid for**.
- `GET /gtm-os/partner/icp-preview` — **free**: shows the filters that would be sent and what
  happens to each requirement, with no provider call. Use this instead of a paid run to inspect.

### Bugs found and fixed today
| Bug | How it was found |
|---|---|
| Partner's `industries` never reached the search — 21/21 wrong companies | live run |
| `"Professional Services"` isn't an Icypeas value; matched zero, cost $0.175 | live run |
| Government bodies and school districts passed `type.exclude` | live run |
| Keyword fallback sent one comma-joined phrase — **broke 6 of 7 partners** | previewing all tenants |
| Every deploy crash-looping for a day on psycopg | the deploy log the user supplied |

### Other work today
- Majji's ICP updated (11–50 staff, $2.5–5M, US, Professional Services); Jeff Ballard's replaced
  from his PDF via the parse endpoint. Both done **through the describe/parse flow**, deliberately,
  to prove that feature works rather than writing the DB directly.
- Revenue Pace: Sandy Yu's $1,500 offline deal recorded on booking #47; the calculation now counts
  won deals with no company for Elephant Edge's own bookings. Figure reads $1,500 / $83,333.
- Monid researched: 87 providers / 2,442 endpoints, public catalog, no key needed. **Includes**
  Apollo, PDL, Hunter, ContactOut, Crunchbase, Apify. **Excludes** Icypeas. Not free — data calls
  need a funded key.

---

## 5. Open items

**Agreed, not started — feature registry + admin feature config.** Partners get different features
(webinars, email marketing, LinkedIn content) and sometimes the same feature configured
differently. Decision: **configuration over code, adapters over branches.**
- Three layers: `enabled_features` flag (exists) → per-tenant `Parameter` config (exists, 22 uses)
  → **feature registry** declaring each feature's required config keys/credentials (missing).
- Configuration UI goes in **Elephant Edge V2 admin → Settings, per partner** for now. Partner
  self-serve comes later, when the model becomes subscription-based.
- Build features **as requirements actually arrive**, then offer them to everyone as add-ons. Do
  not build speculatively.
- **The one hardcoding violation to remove**: `MAJJI_EMAIL_CAMPAIGNS` and `_require_majji_tenant()`
  in `api.py` (~line 6978). The config key `smartlead_campaign_id` already exists in
  `app/outreach/smartlead.py` — the route simply didn't use it.

**Provider router, remaining phases** (see design doc §12): 3 quota-driven fetching → 4 typed
outcomes + sample verification → 5 account pool → 6 planner/failover → 7 composition →
8 scorecards → 9 migrate other stages.

**Blocked / needs the user**
- Frontend repo `synefi/dashboard` rejects pushes (403). A font-size change (`f37ffdf`) and any
  Settings-form work are stuck behind this.
- `department_headcount` on the ICP is read by the code but **not shippable** until the Settings
  form carries it — the ICP save is a full replace, so a UI save would wipe it.
- Majji's "marketing < 1" is still enforced by nothing; it is correctly surfaced as a note rather
  than pretending to be a filter. Apollo is the route that could enforce it.
- Icypeas taxonomy is still empty; it fills from the next paid run, after which "Professional
  Services" maps onto real values instead of using the keyword fallback.

---

## 6. How to check the system is healthy, for free

```bash
# backend + which commit is actually live (not what you pushed)
curl -s https://elephantedge-workflow-1-7k9d.onrender.com/api/health

# DB path works (health alone does NOT prove this)
curl -s https://elephantedge-workflow-1-7k9d.onrender.com/api/gtm-os/partner/icp -H "X-Tenant-Id: 15"

# gateway DB path: 401 is healthy, 500 means the database is down
curl -s -X POST "https://workflow-automation-1ujz.onrender.com/auth/login?email=x@y.z&password=x"

# what an ICP would actually search for — free, no provider call
curl -s https://elephantedge-workflow-1-7k9d.onrender.com/api/gtm-os/partner/icp-preview -H "X-Tenant-Id: 15"
```

Tests: `./venv/bin/python -m pytest tests/ -q` — **283 passing** as of this commit.

---

## 7. Related documents

| File | What it holds |
|---|---|
| `CLAUDE.md` | How to work in this repo; the user's own instructions |
| `SYSTEM_OVERVIEW.md` | What the whole platform is, its history, every tab and feature, and why each exists |
| `provider-router-design.md` | The multi-provider sourcing architecture and its build order |
| `deployment.md` | Render/Neon infrastructure and incident history |
| `progress-update-2026-09-29.md` | Webinar campaign, and the production-database mixup |
