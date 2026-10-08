# Handover — read this first if you are a new session

*Last updated 2026-10-08 (end of day). Written so that nothing below has to be re-explained from
scratch. If you are a new Claude session: read `CLAUDE.md` for how to work here, then this file
for where things actually stand, then the two design docs it points at.*

---

## 0. The 60-second version

Elephant Edge runs an autonomous GTM pipeline for itself **and as a back office for partners** —
today ~10–15, growing. Each partner has their own ICP, their own enabled features, and their own
data. The product is the platform; nothing may be built for one named partner.

**The provider router (design: `provider-router-design.md`) is fully built — all 9 phases, and now
has TWO executable adapters**, not one. Icypeas was the only adapter for weeks; Crustdata (`crustdata-v3`)
went live 2026-10-08, onboarding a new partner (Nora, tenant 16, life-science positioning
consultancy). What a hardcoded, single-provider Icypeas search used to do is now: decompose the
partner's ICP into atoms → resolve their wording against a provider's real taxonomy for free →
check our own shared account pool before buying anything → buy only the shortfall → verify the
actual rows against what was asked for → route through a ranked, failover-capable planner → record
and learn from every attempt → one decision-maker resolution path shared by every play. The planner
genuinely fails over between two live providers now (confirmed live, not just in tests).

**Everything through commit `b5e4cf0` is pushed and live.** The "phases committed locally, not
pushed" state from the previous handover version is resolved — don't trust that paragraph if you
somehow see an old copy of this file; re-verify with `git log origin/main -1` regardless.

**The feature registry is also fully built.** Per-partner features (webinars, email campaigns,
LinkedIn content…) are configuration + an admin screen, not hardcoded tenant-id branches.

---

## 1. Standing rules (violating these has cost real money or real time)

1. **Never conclude — verify.** Check the registry, the docs, or the live schema before asserting
   a capability is absent.
2. **Never spend without stating the cost and getting an explicit yes**, every time. Not once per
   session — per run.
3. **Test live before declaring done.** Local tests passing is not "working" for a NEW provider or
   NEW field/operator combination — see rule 9, this is the big one from 2026-10-08.
4. **Nothing partner-specific in shared code.** No `if tenant_id == N`. Features are flags,
   configuration is data.
5. **Validate against every partner, not the one being discussed.** A fix that looks right for one
   partner's ICP shape can break five others with a different shape (multi-value industries,
   funding criteria, etc.).
6. **A filter seen in a product UI is not an API contract** until the parameter is verified.
7. **No Claude attribution in commit messages.**
8. **Never discard data already paid for.** Persist on receipt, before processing.
9. **Before writing any new provider/tool adapter, get one real, independently-verified-correct
   query+result BEFORE writing code.** From the provider's own UI/AI-assistant output, a worked
   doc example, or one manual raw API test — then diff our planned field names/operators against
   that ground truth. Unit tests built on self-written mocks (inferred from reading schema docs)
   are not verification; they just confirm your own assumption. **Why this is rule 9, not a
   footnote**: onboarding Nora on Crustdata, 8 sequential bugs were found one at a time, each only
   surfacing on a REAL live run, each "fixed" and live-tested again — industry resolver never
   wired, its own autocomplete tool unpriced, keyword fallback too narrow, "Europe" sent as a
   literal (non-existent) country value, technographics using array-equality instead of
   token-match, industry keyword search on the wrong (narrow) field, a Deepline-wrapper-specific
   `fields` requirement undocumented anywhere, and a structured+keyword match ANDed instead of
   ORed. Every one passed tests first. What actually found each bug was comparing our generated
   query against Crustdata's own dashboard-generated query for the identical ICP, then a raw curl
   bypassing our code. If a wrapper layer (Deepline) sits between us and the provider, it may have
   its own undocumented requirements beyond the provider's own docs — verify through the wrapper
   specifically.

---

## 2. Infrastructure, as of today

| Piece | Where | Notes |
|---|---|---|
| Product backend | Render `elephantedge-workflow-1-7k9d.onrender.com` (also `elephantedge-workflow-1.onrender.com`, same commit) | FastAPI, repo `elephantedge-abm` |
| Gateway | Render `workflow-automation-1ujz.onrender.com` | auth + per-tenant proxy, repo `gateway` (NOT `synefi`, despite similar names — see §3a) |
| Frontend | Vercel, `app.fractionalpartner.us` | repo `synefi/dashboard` |
| Database | Neon Postgres, shared, one instance | `tenants.backend_url` routes per tenant |
| Legacy/unrelated | `synefi-workflow.onrender.com` | repo `synefi` — an OLD V1 app ("Synefi Outreach Pipeline"), NOT the gateway, NOT the current backend. Easy to confuse by name; check `/openapi.json`'s `info.title` if unsure which service you're looking at. |

**Three repos that sound alike, are not the same thing** (confirmed the hard way 2026-10-08):
- `elephantedge-abm` (remote: `elephantedge-workflow`) — the GTM-OS backend, this repo.
- `gateway` (remote: `workflow-automation`) — the real auth gateway: `/auth/login`,
  `/api/admin/tenants`, `/api/admin/users`, the tenant-scoped proxy. The "Add User" wizard in
  Settings lives in its frontend code but calls THIS backend's routes.
- `synefi` (remote: `synefi-workflow`) — old V1 pipeline, deployed separately, mostly dead. Also
  happens to contain the `dashboard/` frontend folder (`synefi/dashboard`), which is the real
  frontend — but that frontend's API calls go to the `gateway` service, not to this repo's own
  small legacy backend.

**Render**: 3 free-tier accounts rotated when one is bandwidth-suspended. Switching = update
`tenants.backend_url` + `vercel.json`, not a redeploy.

**Two outages this month, both worth recognising by their signature:**
- *DB-shaped*: health endpoints 200, every DB-touching route fast 500. Both services fail
  identically because they share one database.
- *Deploy-shaped*: `/api/health` keeps serving an **old** commit for a long time — Render silently
  keeps the last good build when a new one crash-loops. **If a deploy "doesn't land", check the
  commit hash in `/api/health` before assuming anything else.**

---

## 3. Tenants

| Tenant | Who | Notes |
|---|---|---|
| 2 | Elephant Edge | Own pipeline; also the **billing tenant** — all partner spend reserves against its ledger and its control-plane pause |
| 5 | Jeff Ballard | Channel/alliances consultant |
| 9 | Remy (GTM University) | |
| 12 | Jeff Platt | RevOps diagnostic — *different person from tenant 5*, easy to confuse |
| 15 | Majji / Fractional Partner | The most-exercised partner. Webinars + email-campaign tabs are gated to this tenant. `department_headcount.marketing` max 0 ("no dedicated marketing hire") |
| 16 | Nora Volger (`partner:nora-volger`) | Onboarded 2026-10-08. Life-science/health-tech positioning consultancy — very different ICP shape from every other partner: funding stage/recency, leadership-change timing, technographics (HubSpot/Marketo/Salesforce), and a marketing-headcount **minimum** of 4 (the inverse of Majji's maximum of 0). Login `nora@uptakegtm.com`. Crustdata is her winning provider (`daily_account_target: 5`). |
| 3, 6, 10, 11, 13, 14 | other partners | each with their own ICP |

Control plane pause state changes often — check before assuming a tenant is running.

---

## 4. The provider router — current real state

### Atom vocabulary (`app/gtm_os/sourcing/atoms.py`)
`headcount`, `revenue`, `geography`, `industry`, `department_headcount` (qualified by department,
supports both a floor and a ceiling), `decision_maker_title`, `company_type`, plus four added
2026-10-08 onboarding Nora: `funding_stage`, `funding_recency` (days since last round),
`leadership_change` (titles + days), `technographics` (tools a company runs, include/exclude).

### Registry (`app/gtm_os/sourcing/registry.py`) — 10 endpoints registered
Icypeas (verified, executable), Crustdata v3 (verified, **executable**, second real adapter),
Prospeo, Apollo (`requires_own_credential=True` — Deepline has no managed access, needs a partner
Apollo account), PredictLeads company search + financing-events + news-events discovery, Dropleads
(people search, used for free department-presence checks, deliberately NOT in the company-search
waterfall), PeopleDataLabs (left mostly `unverified` — Deepline's own schema for it never
disclosed real field names), Forager (person/role search with a confirmed real `funding_types`
enum — NOT in the company-search waterfall either, since it returns people not companies;
`leadership_change`'s date field has no confirmed range-pair, stays `unverified` for a reason, see
registry.py's own comment).

**Support is three-state** (`supported` / `absent` / `unverified`) — never boolean, never inferred
from one provider's behavior.

### Resolution (`app/gtm_os/sourcing/resolution.py`)
Two levels: (1) which filter expresses the concept — registry.py. (2) what VALUE that filter
accepts — this file, in order: exact match against values already confirmed → the provider's own
free resolver endpoint (`Capability.resolver_fetch`, added 2026-10-08 — this was previously just
metadata nobody called) → LLM restricted to confirmed values only → a MIXED resolution combining
whatever resolved structurally with a keyword fallback for the rest (**OR'd together**, not ANDed
— a real, confirmed-live bug fixed 2026-10-08, see `_or_combine_fragments`) → free-text keyword
alone → unresolved. An LLM can never invent a value.

### Planner (`app/gtm_os/sourcing/planner.py`)
Ranks every registered `company_search`-job provider for an ICP (coverage first, then observed
health, then cost), auto-switches on a typed outcome whose policy says to, bounded
(`max_providers`), circuit breaker. `aggregate_coverage()` (added 2026-10-08) reports what would
ACTUALLY happen across the FULL registry — replaced the old `icp_coverage()` which hardcoded
Icypeas alone and would have stayed silently wrong forever as providers were added. Has a
`pending_adapter` bucket distinct from `needs_provider_research`: an atom some registered provider
can enforce but has no adapter yet is a BUILD gap, not a RESEARCH gap.

### Free debug tooling
- `GET /gtm-os/partner/icp-preview` — full multi-provider coverage for the calling partner, free.
- `GET /gtm-os/admin/providers/catalog` / `/schema` / `/registry` — free Deepline tool-catalog
  lookups, for researching a new provider without spending.
- `GET /gtm-os/admin/debug/crustdata-filters?partner_tenant_id=N` — the EXACT payload
  `search_crustdata()` would send for that partner's ICP, built but never executed. Added
  2026-10-08 after the planner's own failover hid which provider's payload was actually sent
  (Crustdata ran first, failed to Icypeas, and the API response only ever surfaced the LAST
  attempted provider's result) — made a real bug impossible to diagnose from the response alone.

### Known real gaps
- Apollo, Prospeo, PeopleDataLabs, PredictLeads, Forager: registered, **no adapter function
  written** — `pending_adapter` in coverage reports, not executable.
- `funding_stage` has no confirmed company-search-job provider yet (Forager's confirmed enum is on
  a people/role endpoint, out of the waterfall).
- `leadership_change` and `decision_maker_title` have no company-search-job provider either —
  these are structurally person/role-search concepts; the SHOULD_HAVE necessity on these atoms
  already assumes a later signal-detection pass handles them, not search-time filtering.
- Signal sourcing (hiring/engagement discovery itself) is still NOT on this router — only
  decision-maker resolution was unified (phase 9, prior session).

---

## 5. How to check the system is healthy, for free

```bash
# backend + which commit is actually live (not what you pushed)
curl -s https://elephantedge-workflow-1-7k9d.onrender.com/api/health

# DB path works (health alone does NOT prove this)
curl -s https://elephantedge-workflow-1-7k9d.onrender.com/api/gtm-os/partner/icp -H "X-Tenant-Id: 15"

# gateway DB path: 401 is healthy, 500 means the database is down
curl -s -X POST "https://workflow-automation-1ujz.onrender.com/auth/login?email=x@y.z&password=x"

# what an ICP would actually search for, across EVERY registered provider — free, no provider call
curl -s https://elephantedge-workflow-1-7k9d.onrender.com/api/gtm-os/partner/icp-preview -H "X-Tenant-Id: 15"

# the exact Crustdata payload a partner's ICP would produce — free, never executed
curl -s "https://elephantedge-workflow-1-7k9d.onrender.com/api/gtm-os/admin/debug/crustdata-filters?partner_tenant_id=16" -H "X-Tenant-Id: 2"
```

Tests: `./venv/bin/python -m pytest tests/ -q` — **445 passing** as of commit `b5e4cf0`.

---

## 6. Related documents

| File | What it holds |
|---|---|
| `CLAUDE.md` | How to work in this repo; the user's own instructions |
| `SYSTEM_OVERVIEW.md` | What the whole platform is, its history, every tab and feature, and why each exists |
| `provider-router-design.md` | The multi-provider sourcing architecture and its build order |
| `deployment.md` | Render/Neon infrastructure and incident history |
| `progress-update-2026-09-29.md` | Webinar campaign, and the production-database mixup |
