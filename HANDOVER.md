# Handover — read this first if you are a new session

*Last updated 2026-10-07 (end of day). Written so that nothing below has to be re-explained from
scratch. If you are a new Claude session: read `CLAUDE.md` for how to work here, then this file
for where things actually stand, then the two design docs it points at.*

---

## 0. The 60-second version

Elephant Edge runs an autonomous GTM pipeline for itself **and as a back office for partners** —
today ~10–15, growing. Each partner has their own ICP, their own enabled features, and their own
data. The product is the platform; nothing may be built for one named partner.

**The provider router (design: `provider-router-design.md`) is fully built — all 9 phases.**
What a hardcoded, single-provider Icypeas search used to do is now: decompose the partner's ICP
into atoms → resolve their wording against a provider's real taxonomy for free → check our own
shared account pool before buying anything → buy only the shortfall → verify the actual rows
against what was asked for → route through a ranked, failover-capable planner → record and learn
from every attempt → one decision-maker resolution path shared by every play. Icypeas is still the
only *executable* adapter (Prospeo/Apollo capabilities are registered but marked `unverified`,
deliberately not promoted from documentation alone) — but the machinery that will fail over to a
second provider the moment one is verified is real and tested, not aspirational.

**The feature registry is also fully built.** Per-partner features (webinars, email campaigns,
LinkedIn content…) are now configuration + an admin screen, not hardcoded tenant-id branches. The
one real violation found (`MAJJI_EMAIL_CAMPAIGNS`, `_require_majji_tenant()`) is gone.

**Push status matters here — it's a mix, not all-or-nothing.** Phases 1–4 of the router and the
whole feature registry ARE already live (through commit `d770a18`). Phases 5–9 (pool, planner
+excludes, composition, scorecards, the shared decision-maker resolver) are committed locally but
**not pushed** — the user's instruction partway through this session was "we will push all together
later," so everything from the pool phase onward is sitting locally. See §4 for the exact commit
list and §5 for what's genuinely blocked until that push happens. **Check `git log origin/main -1`
against local `HEAD` before assuming what's live** — do not trust this paragraph's commit hash once
time has passed; re-verify.

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

### Phase 3 — quota-driven fetching (live, commit `8e440d8`)
- `app/gtm_os/sourcing/quota.py` — buy the partner's shortfall against their own configurable
  daily target (`accounts` feature's `daily_account_target`) and nothing more. Page size IS the
  spend decision for a per-result provider. A target already met costs **$0**, where before it
  cost $0.175 to re-discover that every single day.
- Decision-maker resolution (the separately-billed, more expensive step) is capped at the same
  remaining need, not the full page — surplus companies stay persisted for a later run.

### Phase 4 — typed outcomes + sample verification (live, commit `d770a18`)
- `app/gtm_os/sourcing/outcomes.py` — a closed set of outcomes (`ok`, `empty_validated`,
  `empty_suspect`, `quality_fail`, `schema_error`, `auth_error`, `rate_limited`, `unavailable`,
  `budget_blocked`), each with one documented policy (retry? switch provider? count against
  health?) — the outcome drives the next move, not an exception string.
- `app/gtm_os/sourcing/verification.py` — judges the actual returned ROWS against the ICP, not
  the filter we believe we sent. Replays the real 2026-10-03 batch (21 hospitals/law firms/a fire
  department) and catches it on row 1, not row 21. **The one asymmetry that matters**: missing
  data is never a violation — a row only counts against a requirement when the provider gave a
  value AND it clearly breaks it. Industry is only judged when the search actually used a real
  taxonomy value, never when it fell back to free-text keyword (that path never promised a
  classification).

### Local-only from here — NOT pushed (user: "we will push all together later")

- **Excludes + planner** (commit `70a21cb`) — `app/gtm_os/sourcing/exclusions.py` pushes a
  repeatedly-rejected value (government bodies, etc.) back into the provider QUERY so it stops
  being billable at all, with two safeguards (repetition required; the partner's own include list
  is never excluded by our inference). `app/gtm_os/sourcing/planner.py` ranks every registered
  provider for an ICP (coverage first, then observed health, then cost) and auto-switches on a
  typed outcome whose policy says to — bounded (`max_providers`), with a per-provider circuit
  breaker. **Icypeas is still the only executable adapter** — Prospeo/Apollo are registered but
  their capabilities are `unverified`, deliberately not promoted from docs alone.
- **Free provider-schema discovery** (commit `f763794`) — `deepline tools search` / `tools
  describe` wired up as free, read-only admin routes (`/gtm-os/admin/providers/catalog`,
  `/schema`, `/registry`). Registering a new provider no longer requires spending to discover its
  real filter names — three admin-only routes do it for $0. **Needs a push to actually use**: the
  working Deepline key lives on Render; the local CLI key is invalid.
- **Phase 5 — account pool** (commit `b61dc20`) — `company_pool` + `pool_deliveries`: a company
  bought for one partner is free for the next whose ICP also matches it (headcount/revenue/geo
  only — deliberately NOT industry, see the module's own docstring for why). Positive evidence
  required (unknown headcount never "matches"), 30-day freshness window.
- **Phase 7 — composition** (commit `981aadc`) — `app/gtm_os/sourcing/compose.py`. Majji's "no
  dedicated marketing hire" is now actually **enforced**, for the first time, using the free Jobo
  leadership list already fetched for decision-maker resolution — no new paid integration. Same
  asymmetry as phase 4: a title found there disconfirms the requirement; an absence never confirms
  it (Jobo's index is partial). Also `intersect()` for two-provider composition, identity-keyed.
- **Phase 8 — scorecards + drift detection** (commit `f641db0`) — closed a real gap first:
  `run_icp_filters` (the actual production entry point) was calling `search_icypeas()` directly,
  bypassing the planner entirely — `route_attempts` had been empty in production the whole time.
  Now wired through `planner.execute()`. Scorecards are per **ICP shape**
  (`atoms.fingerprint()`), not just per provider — a provider can be healthy on average while one
  specific filter shape has quietly broken, which is exactly what happened on 2026-10-05. Drift
  detection only flags a shape that reliably worked and has since collapsed; the planner's own
  sort key is extracted into a standalone, directly-testable function proving history can only
  break ties between equal coverage, never substitute for it.
- **Phase 9 — one decision-maker path, not two** (commit `653bea2`) — `app/gtm_os/sourcing/
  decision_maker.py`. `icp_filters.py` and `hiring.py` each had their own batched HarvestAPI
  decision-maker resolver and had drifted: icp_filters.py's was fixed (2026-09-28/10-04) for the
  real multi-URL-batching bug; **hiring.py's never was, and was almost certainly returning 0
  decision-makers in production silently**. Both now call one shared resolver. Fixed two real
  bugs in `hiring.py` in one move: the URL-vs-name batching bug, and the "later page fails, earlier
  already-paid pages get discarded" bug.

Test suite: **392 passing** (was 258 before this session).

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

**Feature registry: DONE, live** (`e479e4a`, `0cf3a84`, pushed). `app/gtm_os/features/registry.py`
declares each feature's required config/credentials; `app/gtm_os/features/config.py` computes
readiness; admin routes read/write any partner's config; `synefi/dashboard` has a "Partner
Features" tab in V2 Settings generated from the backend's own schema. `MAJJI_EMAIL_CAMPAIGNS` /
`_require_majji_tenant()` are gone — email campaigns are now `smartlead_campaign_ids` config,
gated on the `email_campaigns` feature flag, usable by any partner.

**Provider router: all 9 phases built** (see design doc §12). What's real vs. what's still a gap:
- Real and tested: atoms, registry, resolution, quota, typed outcomes, verification, exclusions,
  planner/failover, the account pool, composition (enrich-to-decide + intersect), scorecards/drift,
  one shared decision-maker resolver.
- **Still only one executable adapter (Icypeas).** Prospeo and Apollo are registered with
  `unverified` capabilities — the honest next step is pulling their REAL schemas via the new free
  `/gtm-os/admin/providers/schema` route (needs the push — see below) and promoting only what's
  confirmed, never from documentation.
- **Not done, and not attempted**: migrating signal sourcing (hiring/engagement discovery itself,
  as opposed to decision-maker resolution) onto the same router. Phase 9's scope was "decision-maker,
  contact, signals" per the design doc; only decision-maker resolution was actually unified this
  session. Contact-finding and signal sourcing still have their own bespoke paths.

**Blocked / needs the user**
- **The push.** Phases 5–9 (pool, planner+excludes, free schema discovery, composition,
  scorecards, shared decision-maker resolver) are committed locally, not pushed. Until they are:
  Majji's "no dedicated marketing hire" enforcement (phase 7) isn't live, the account pool isn't
  compounding, and the free provider-schema routes can't actually be called (the working Deepline
  key is on Render, not local).
- Frontend repo `synefi/dashboard` rejects pushes from this machine (403). A font-size change
  (`f37ffdf`) and the Partner Features screen (`3e4613d`) are both stuck behind this, on top of
  whatever else accumulates.
- Icypeas taxonomy (`provider_taxonomy_values`) is still thin — it fills from real paid runs, after
  which "Professional Services"-style concepts resolve to real learned values instead of the
  free-text keyword fallback.
- Apollo's department-headcount filter is documented in its product UI but its real API parameter
  name has never been verified — that verification (free, via the new schema-discovery routes) is
  what would let phase 7's enrich-to-decide logic be replaced by an actual structured filter for
  Majji's requirement, rather than the free-Jobo-leadership-list proxy it uses today.

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

Tests: `./venv/bin/python -m pytest tests/ -q` — **392 passing** locally as of this commit.
(`283` was passing in the last version of this file pushed to origin — if you're reading this
from a fresh pull before the pending push lands, that's the number you'll actually see.)

---

## 7. Related documents

| File | What it holds |
|---|---|
| `CLAUDE.md` | How to work in this repo; the user's own instructions |
| `SYSTEM_OVERVIEW.md` | What the whole platform is, its history, every tab and feature, and why each exists |
| `provider-router-design.md` | The multi-provider sourcing architecture and its build order |
| `deployment.md` | Render/Neon infrastructure and incident history |
| `progress-update-2026-09-29.md` | Webinar campaign, and the production-database mixup |
