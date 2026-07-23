# Elephant Edge — Autonomous ABM Agent Architecture

Elephant Edge's own ABM system: identify, qualify, research, prioritize, and engage
Elephant Edge's ideal customers (not a client's). This document is the reusable,
company-agnostic framework — implementation choices (data providers, scoring logic,
messaging) live in a separate doc once the ICP is confirmed.

## Purpose

Design an autonomous AI system that replicates how experienced enterprise sales and
ABM teams identify, qualify, research, prioritize, and engage high-value accounts.

Unlike traditional outreach automation, this system focuses on **decision-making**,
not merely task automation. Each phase is responsible for answering a business
question before progressing to the next.

## Status

- [ ] Phase 0 — Market Intelligence (mostly known; confirm, don't rediscover)
- [ ] Phase 1 — ICP Definition — **BLOCKED on ICP input call**
- [ ] Phase 2 — Company Discovery
- [ ] Phase 3 — Company Qualification
- [ ] Phase 4 — Buying Signal Intelligence
- [ ] Phase 5 — Buying Committee Discovery
- [ ] Phase 6 — Decision Maker Intelligence
- [ ] Phase 7 — Account Research & Context Building
- [ ] Phase 8 — Opportunity Scoring
- [ ] Phase 9 — Engagement Strategy
- [ ] Phase 10 — Personalization
- [ ] Phase 11 — Campaign Execution
- [ ] Phase 12 — Measurement & Learning

---

## High-Level Architecture

```text
Market
   │
   ▼
Market Intelligence
   │
   ▼
Ideal Customer Profile (ICP) Definition
   │
   ▼
Company Discovery
   │
   ▼
Company Qualification
   │
   ▼
Buying Signal Intelligence
   │
   ▼
Buying Committee Discovery
   │
   ▼
Decision Maker Intelligence
   │
   ▼
Account Research & Context Building
   │
   ▼
Opportunity Scoring & Prioritization
   │
   ▼
Engagement Strategy
   │
   ▼
Personalization
   │
   ▼
Campaign Execution
   │
   ▼
Measurement & Learning
```

---

## Phase 0 — Market Intelligence

**Objective:** Understand the market before selecting target accounts.

Answers: *Which markets should we focus on?*

Questions:
- Which industries exist?
- Which industries are growing?
- Which industries have strong purchasing power?
- Which industries are undergoing digital transformation?
- Which industries match our product/service capabilities?
- Which industries have measurable business problems?

Output: Market Segments, Industry Profiles, Market Priorities

---

## Phase 1 — Ideal Customer Profile (ICP)

**Objective:** Define what a good customer looks like — and *why* each criterion exists,
not just store it as a filter.

Business Criteria (examples):
- Industry
- Company Size
- Annual Revenue
- Geographic Presence
- Growth Stage
- Technology Maturity
- Existing Technology Stack
- Digital Transformation Level
- Regulatory Environment
- Procurement Complexity

For each criterion, document:
- Why it matters
- How it is measured
- Expected impact
- Confidence level

Output: A reusable ICP model.

**Elephant Edge status:** Pending ICP input call. See `phase1-icp-notes.md` once the call happens.

---

## Phase 2 — Company Discovery

**Objective:** Identify organizations that satisfy the ICP.

Example discovery sources: company databases, professional networks, funding
databases, job boards, technology intelligence, news, press releases, public
websites, business directories.

**Important principle:** Discovery is not qualification. Output is only a candidate list.

Output: Candidate Accounts

---

## Phase 3 — Company Qualification

**Objective:** Determine whether a discovered company is worth pursuing — not
"does this company exist?" but "is this account likely to become a customer?"

Qualification dimensions:
- **Business Fit** — industry, size, revenue, geographic fit
- **Operational Fit** — technology compatibility, organizational maturity, process complexity
- **Strategic Fit** — growth trajectory, expansion plans, digital initiatives
- **Commercial Fit** — budget likelihood, purchasing capability, sales cycle suitability

Output: Qualified Accounts

---

## Phase 4 — Buying Signal Intelligence

**Objective:** Determine whether *now* is the right time. Accounts may fit the ICP
but not be actively buying.

Typical buying signals: hiring initiatives, funding events, expansion, leadership
changes, product launches, mergers, acquisitions, technology migration, compliance
changes, new partnerships, public strategic announcements, conference participation,
vendor replacement, procurement initiatives.

Each signal evaluated by: signal freshness, business impact, confidence, intent strength.

Output: Buying Intent Score

---

## Phase 5 — Buying Committee Discovery

**Objective:** Understand who participates in purchasing decisions. Enterprise
purchases rarely involve one person.

Identify: Economic Buyer, Technical Buyer, Business Owner, Champion, Influencers,
Procurement, Security, Legal, Finance, End Users.

Output: Buying Committee Map (complete DMU)

---

## Phase 6 — Decision Maker Intelligence

**Objective:** Determine the appropriate stakeholders to engage — not by job title
alone.

Evaluate: organizational role, decision authority, budget ownership, technical
influence, business ownership, procurement responsibility, public activity,
accessibility.

Output: Prioritized Stakeholders (each with a confidence score)

---

## Phase 7 — Account Research & Context Building

**Objective:** Develop a comprehensive understanding of the account.

Analyze: company website, leadership, products, services, news, annual reports,
social activity, hiring trends, technology stack, public interviews, customer
announcements, partnerships, competitive landscape.

Understand: current priorities, challenges, strategic initiatives, business context.

Output: Account Intelligence Profile

---

## Phase 8 — Opportunity Scoring

**Objective:** Estimate the probability of a successful engagement, combining
multiple dimensions (not a single metric).

Scoring categories: ICP Fit, Company Qualification, Buying Intent, Decision Maker
Confidence, Budget Confidence, Timing, Strategic Alignment, Competitive Risk,
Relationship Strength.

Scoring must be transparent and explainable.

Output: Prioritized Opportunity Queue

---

## Phase 9 — Engagement Strategy

**Objective:** Determine the most appropriate engagement plan before generating
outreach.

Decide: which channels, engagement order, number of stakeholders, sequence
strategy, communication timing, multi-threading approach, escalation rules.

Channels: LinkedIn, Email, Events, Webinars, Partner introductions, Referrals,
Direct calls, Community interactions.

Output: Account Engagement Plan

---

## Phase 10 — Personalization

**Objective:** Generate account-specific communication — relevance, not generic
mail-merge personalization.

Inputs: company context, business priorities, buying signals, decision maker
responsibilities, previous interactions.

Output: Personalized Assets

---

## Phase 11 — Campaign Execution

**Objective:** Execute engagement activities autonomously.

Responsibilities: scheduling, rate limiting, multi-channel execution, logging,
retry handling, compliance, response tracking.

Output: Campaign Activity

---

## Phase 12 — Measurement & Learning

**Objective:** Continuously improve future decision making, evaluated across the
full funnel.

Business metrics: Accounts Qualified, Engagement Rate, Response Rate, Meetings
Booked, Opportunities Created, Pipeline Generated, Revenue Influenced.

System metrics: Qualification Accuracy, Signal Accuracy, Decision Maker Accuracy,
Personalization Quality, Channel Effectiveness, Cost per Qualified Account, Cost
per Opportunity.

Learning: continuously update scoring models, signal importance, qualification
logic, channel selection, personalization strategies.

Output: Improved decision models for future campaigns.

---

## Guiding Principles

Every phase should answer these before proceeding:

1. Why does this phase exist?
2. What business decision is being made?
3. What information is required?
4. Where can that information be obtained?
5. What alternative data sources or approaches exist?
6. How reliable is the information?
7. How is confidence measured?
8. What happens if this decision is wrong?
9. What is passed to the next phase?
10. How is success measured?

---

## Relationship to prior work

- This is the **generic ABM Autonomous Agent Framework** — intentionally
  company-agnostic.
- `synefi/` (separate codebase) is a reference for *how to structure an
  autonomous pipeline in code* (phases, orchestration) — not a finished or
  fully ABM-compliant implementation; its logic/scoring is not authoritative here.
- A second doc (`implementation.md`, to be created once Phase 1 is unblocked)
  will map each phase to concrete choices for Elephant Edge: which data
  providers, why, trade-offs, and how each phase is actually built.
