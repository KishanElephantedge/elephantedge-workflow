# Elephant Edge — Signal Framework v2 (from Gokul, in progress — implementation status tracked at bottom)

Status: **collecting requirements, not yet implemented.** This replaces the current ICP (Phase 2)
and Buying Signal (Phase 5) design where overlapping — confirmed by the user this is a full
replacement, not an additive layer. More criteria are still coming from Gokul; this file is
updated as each new piece arrives, and implementation only starts once the user says so.

---

## 1. Primary Signals (replaces Phase 2's ICP hard gates) — UPDATED

- **Revenue**: $2.5M–$5M (unchanged)
- **Headcount**: 11–50 (unchanged)
- **Sales headcount**: **2%–10%** of total company headcount (updated range, was "1–10%")
- **Geography**: United States, tiered:
  - Tier 1 city example given: San Francisco Bay Area (the earlier NYC/LA/Chicago/DC list may
    still apply as additional Tier 1 cities — not explicitly restated in this pass, treated as
    still valid unless corrected)
  - Everything else = Tier 2 (not a hard exclude)
- **Industry**: Tech/Non-tech — all industries in scope, not just SaaS/Agentic AI (confirmed).
- **Offering**: **Product OR Service** — updated from the earlier "Product only" answer.
  Elephant Edge no longer excludes service-based companies at the ICP level.

---

## 2. Secondary Signals (new category — org composition, not currently covered by any phase)

- **Sales headcount** — via team-roster search, title keywords: Head of Sales, Sales
  Development Representative (SDR), Business Development Representative (BDR), Account
  Executive (AE)
- **GTM team headcount** — title keywords: Head of GTM, GTM Engineer, GTM Operations
- **Marketing team headcount** — title keywords: Marketing Manager, Growth Marketing, Head of
  Marketing
- **Team tenure buckets** — refined with explicit logic, not just date ranges:
  - **1–3 months (Recently Joined)**: if 2+ of the roles above (Head of Sales, Director of
    Sales, AE, SDR) joined recently → signals the company is actively **building** a sales team
  - **3–12 months (Existing for a while)**: if 2+ of those roles were opened within this window
  - **>12 months (Matured)**: established team, not newly built
- **Estimated Sales & Marketing spend**: combine salary benchmarks (Glassdoor, AmbitionBox, and
  lookups via Claude/Perplexity) with existing sales+marketing headcount to approximate annual
  spend. **Updated threshold: $300K–$700K/year** (was "must exceed $500K/year" — now a band,
  not a floor).

---

## 3. Hiring Signals (replaces Phase 5's simple "posting exists" check with a richer
classification of *which kind* of hire this is and what it implies)

**Head of Sales**
| Hire type | Context | Intent | Signal Strength | Follow-up question |
|---|---|---|---|---|
| 1st hire | No team or small team exists | Transfer of revenue-generation ownership from founder | Strong | What was the previous revenue motion? |

**SDR**
| Hire type | Context | Intent | Signal Strength | Follow-up question |
|---|---|---|---|---|
| 1st hire | — | Testing outbound | Strong | Cross-check recent funding to corroborate intent |
| 2nd/multiple/parallel hire | Outbound already working | Doubling down on outbound | Medium | Why hire more instead of augmenting the existing team? |

**AE (Account Executive)**
| Hire type | Context | Intent | Signal Strength |
|---|---|---|---|
| 1st hire | An SDR or marketing team may already be present | Confirms a pipeline exists | Medium |
| Multiple hires | With SDR | Needs more closing efficiency | Medium |
| Multiple hires | No SDR or marketing team | AE owns the full sales cycle end-to-end | Strong |

**Head of GTM / GTM Engineer / GTM Operations**
| Hire type | Context | Intent | Signal Strength | Follow-up question |
|---|---|---|---|---|
| Single/multiple | With existing sales team | Focus shift toward augmenting/automating the sales process and marketing (TOFU filling, CRM integration, etc.) | Medium | How much are they spending [on this]? |

**Head of Marketing / Marketing Manager / Growth Marketing** (NEW row added this pass)
| Hire type | Context | Intent | Signal Strength | Follow-up question |
|---|---|---|---|---|
| Single/multiple | No sales team | Focus shift toward inbound/PLG | Weak | Why hire more? Why not augment? |
| Single/multiple | With sales team | Needs more inbound and top-of-funnel pipeline | Medium | Why hire more? Why not augment? |

**Job Description keyword check** (NEW): look for the specific phrase **"top of the funnel
pipeline generation"** (or close variants) in the JD body — a direct textual signal of intent,
independent of title-based classification.

---

## 4. Additional Qualifying Checks (NEW this pass)

- **Outbound channel requirement**: the company should already be using **LinkedIn and/or
  email** as an outbound channel for marketing/sales — i.e. evidence they run some kind of
  outbound motion at all, not pure inbound-only.
- **AI SDR tooling (bonus, not required)**: better fit if the company is already using an AI
  SDR tool or similar autonomous outbound technology — implies sophistication/receptiveness to
  Elephant Edge's own AI-driven offering.
- **AI-in-sales check**: check whether the company uses any AI system in its sales process at
  all (broader than just AI SDR tools specifically).

---

## Cross-cutting note (from the user, applies to all of the above)

Title alone is often not enough to correctly classify which of the above categories applies —
the actual **job description's responsibilities section** must be read to target accurately.
This confirms the gap already flagged: the current Phase 5 job-posting check only verifies a
posting *exists*, it does not fetch or read the full JD text. Any real implementation of this
framework requires pulling full JD text, not just a title-match existence check.

---

## Flow Inversion (confirmed by the user)

The pipeline order is inverted from the original design: **hiring-signal search comes first,
firmographic Discovery comes second** as a qualifying filter on top of it — not the other way
around. Reasoning: the job posting itself is what reveals a real candidate company (a company
actively hiring for Head of Sales/SDR/BDR/AE/GTM roles is inherently a live signal), whereas
searching by firmographics first and only then checking for a hiring signal was backwards --
most firmographically-matching companies won't have an active posting at all, so starting there
wastes the firmographic search. This also mirrors exactly how the user worked manually: searched
LinkedIn Jobs for "Head of Sales" first, then checked whether the company behind each posting fit
the ICP.

Revised order: **Job Posting Search → extract company identity per posting → Firmographic
Qualification (Discovery's ICP filters, now applied as a confirm/exclude step, not the entry
point) → Secondary Signal enrichment → Hiring-signal classification (much of this already known
from the JD itself) → Scoring → Decision Maker → Personalization → Outreach.**

---

## 5. Proposed Scoring Model (NEW this pass — replaces Phase 9's current rubric if adopted)

Two versions were proposed by an external advisor; both call this an "Autonomous GTM Fit Score" /
ICP scoring formula for ranking companies by fit + buying intent for an AI outbound SDR product.

**Detailed weighted version** (17 variables, each scored 0–5, summed with weights):
```
Score = (P×20) + (Q×15) + (A×10) + (B×15) + (C×10) + (D×10) + (F×10) + (G×15) + (H×15)
      + (I×5) + (J×10) + (K×10) + (L×10) + (M×5) + (N×10) + (O×5) − (R×20)
```
P=sales motion (SMB/Enterprise), Q=LinkedIn+Email as primary outbound, A=company size,
B=hiring SDRs/AEs, C=revenue, D=headcount growth, E=location (filter, not scored), F=recent
funding/growth, G=sales headcount, H=number of open sales roles, I=team maturity,
J=marketing headcount, K=GTM headcount, L=sales & marketing spend, M=existing sales tech
stack (Clay/Apollo/HubSpot/Outreach/etc.), N=product/service fit, O=Tier 1 vs Tier 2 market,
R=negative signals (subtracted).

**Simplified version (recommended by the advisor over the 17-variable one)** — 5 grouped
buying-signal categories summing to 100:

| Category | Weight | Made up of |
|---|---|---|
| **Need** | 30 | Hiring SDRs/AEs/BDRs/GTM, growing sales team |
| **Ability to Pay** | 20 | Revenue, funding, ARR, employee count |
| **Outbound Maturity** | 20 | Uses LinkedIn, Apollo, Clay, HubSpot, Outreach, Salesloft |
| **Product Fit** | 20 | B2B SaaS, high ACV, enterprise sales, long sales cycle, founder-led sales |
| **Buying Intent** | 10 | Recent funding, expansion, new VP Sales/CRO, new market launch, hiring across multiple geographies |

`ICP Score = Need + Ability to Pay + Outbound Maturity + Product Fit + Buying Intent` (max 100)

**Tiers**: 90–100 Tier A (contact immediately), 80–89 Tier B (high priority), 70–79 Tier C
(good fit), <70 low priority/nurture. Note: this tier scale (needing a score ≥70 to even be
"low priority" territory, ≥90 for top tier) is a much higher bar than Phase 9's current
Hot/Warm/Cool thresholds (≥80/≥40/≥1 out of a max of 110) — these are not directly comparable
without re-deriving thresholds against the new max-100 scale.

**Advisor's suggested 6th signal for Elephant Edge specifically**: **Urgency** — companies
hiring 5–20 SDRs/AEs at once are spending heavily on pipeline-building already; Elephant Edge's
autonomous engine directly substitutes/augments that spend, making high-volume sales hiring the
single strongest signal, ahead of revenue/funding.

---

## Implementation Status (checked against the actual codebase)

**✅ Implemented:**
- Revenue $2.5M–$5M, Headcount 11–50 (Phase 3 Discovery query)
- Recent funding + headcount growth signals (Phase 5, free from Discovery's response)
- Generic "does an active Head of Sales/VP Sales/SDR/AE/Director-of-Sales job posting exist"
  check (Phase 5/8, via `bloomberry_search_job_postings` — title/keyword match only, existence
  check only)
- Basic 3-signal scoring with stacking bonus + decision-maker-match precision (Phase 9,
  0–110 scale, Hot/Warm/Cool/Excluded tiers)

**❌ Not implemented (real gaps against this framework):**
- Sales headcount as a % of total headcount (2–10% ratio) — not checked at all
- Geography tiering (Tier 1 cities vs. Tier 2) — Discovery only checks `hq_country = USA`, no
  city-level tiering
- Industry as "Tech/Non-tech" (all industries) — Discovery still filters to
  `["Software Development", "Artificial Intelligence"]` only, not yet widened
- "Offering: Product or Service" — no detection mechanism exists; Discovery doesn't check this
  axis at all
- **All Secondary Signals** — sales/GTM/marketing team headcount counts, team tenure bucketing,
  estimated Sales & Marketing spend ($300K–$700K threshold) — none of this exists in code
- **Rich hiring-signal classification** — the AE/Marketing/GTM tables, and the 1st-hire vs.
  multi-hire distinction for Head of Sales/SDR — not implemented; current code only checks
  existence of *any* matching posting, doesn't read JD text or classify hire type
- **JD full-text reading** at all — the cross-cutting requirement (title alone isn't enough,
  must read responsibilities) is not built; no code fetches full job description text
- **JD keyword check** ("top of the funnel pipeline generation") — not implemented
- **Outbound channel check** (LinkedIn/email usage) — not implemented
- **AI SDR tooling / AI-in-sales checks** — not implemented
- **Flow inversion** (hiring-signal search first, firmographics second) — current code still
  runs Discovery (firmographics) first; the job-posting check only runs afterward, per-company,
  on already-discovered companies — the inverted flow (search job postings broadly across all
  companies first) has not been rebuilt
- **New scoring model** (either the 17-variable weighted version or the simplified 5-category
  Need/Ability-to-Pay/Outbound-Maturity/Product-Fit/Buying-Intent version) — Phase 9's current
  rubric is a simpler, different design entirely; adopting either proposed model would be a
  substantial rewrite, not an incremental change

## Status
This is now a large, multi-part gap between the documented framework and the running code.
Waiting on the user's direction on whether to begin implementing this (and in what order),
or continue collecting more criteria first.
