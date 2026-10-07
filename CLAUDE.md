# Elephant Edge — Working Context

This file is the entry point for any Claude session working in this repo. Read it first, then
follow the pointers below for depth. This project is not just a codebase — it's the working
context for an FDE (Forward Deployed Engineer) acting as an internal AI consultant for a real
company, Elephant Edge.

## Read HANDOVER.md first

**`HANDOVER.md` is the current-state entry point.** It holds the standing rules that have cost
real money when broken, live infrastructure state (which Render account, which database, how to
tell a DB outage from a stuck deploy), the tenant list, what was built most recently, and the open
items and blockers. Read it before doing anything, so none of it has to be re-explained.

## Who the user is

The user is the FDE at Elephant Edge (elephantedge.ai), reporting to CEO Venkatesh Majji. Hired
initially to build a sales automation tool, then explicitly told by Majji to operate as an
internal AI consultant: understand the whole company, find where work can be made more
effective or cost-efficient, and bring ideas proactively — not just execute whatever is asked
without thinking. Majji was frustrated early on when the user built things without questioning
or exploring alternatives first; that feedback shaped the working approach below.

## How to approach work in this project (the user's explicit instruction)

Before building anything, question it:
- Why this, specifically? What's the actual problem underneath the request?
- What are the other options? Has "just do it manually first, then automate" been considered?
- Is this even the right thing to build, or is there a better way to get the same outcome?
- What already exists (in the Drive research, in progress-log.md, in already-built code) that
  answers this instead of building something new?

Do not blindly execute a request. Think it through, look around for existing context/options,
and bring a reasoned recommendation — evidence + a question, not a finished plan dropped on the
user. This mirrors how Majji wants to be engaged: "here's what I found, here's why it matters,
what do you think" — not silently building and presenting a finished thing.

When doing research or analysis for this user: go one topic at a time, plain language (avoid
unexplained jargon — the user is technical but not from a traditional sales/GTM background),
and don't dump everything at once. Confirm understanding before moving to the next piece.

## Where the product roadmap lives

**`roadmap.md`** — what this tool is actually trying to become: not a lead-enrichment or
email-sending tool, but a system that eventually runs the entirety of what Elephant Edge does
as a company (business development, client delivery/execution, marketing/content, internal
ops, quality/oversight layers — see the file for the real breakdown, which grows as more of the
business is understood, not a fixed list). Critically, it also tracks **which piece is actually
active right now** vs. explicitly deferred, so work stays scoped to the current real stage
instead of jumping ahead. Read this before proposing a new feature or picking a comparison set
for competitive research — the comparison set depends entirely on which piece of the business
is being worked on. Keep this file updated as the active stage changes.

## Where the company understanding lives

**`company-understanding.md`** — the full, step-by-step research record of what Elephant Edge
actually is: what it sells, how it prices (across ~13 real client deals reviewed), team
structure, the pending Velocity Sales Solutions partnership, the "4. AI" folder (what's
already built vs. still a spec), marketing reality (SEO/website/social performance numbers),
named competitors, and how the outbound sales process actually runs. Also contains a running
"Flagged Opportunities/Problems" list and a list of open questions for Majji that haven't been
asked yet. **Read this before assuming anything about the business** — it's grounded in real
documents, not assumption.

**`progress-log.md`** — the technical build log: what's actually been built in this codebase
(jobs-first company discovery, 18-variable scoring, Phase 13 personalized outreach pipeline,
SalesRobot LinkedIn integration, Slack notifications, the autonomous daily cycle, etc.), real
bugs found and fixed, and infrastructure decisions. Read this before assuming what does or
doesn't already exist in the system.

**`company-research-drive/`** — the raw source material: a full export of Elephant Edge's
Google Drive (Company Administration, Products & Content Assets, Marketing Process, Sales
Process, AI folders — ~213 files: proposals, case studies, CRM exports, pricing docs, brand/
positioning drafts, etc.), plus a `converted/` subfolder with plain-text versions of every
.docx/.xlsx for direct reading. This is gitignored — it does not get pushed to GitHub, since
it contains real client/business-sensitive material. `company-understanding.md` is the
distilled, verified summary of this folder; go back to the raw files only when a specific
detail needs re-checking against source, not as the default starting point.

## What NOT to assume

- Do not assume the company's current strategic direction is settled. Multiple parallel
  "next direction" ideas exist in draft form (see `company-understanding.md` Step 6a) — the
  AI-product pivot is the one most explicitly stated as the real plan, but confirm before
  building around any of them as fact.
- Do not assume pricing/positioning documents reflect current reality — some are dated,
  proposals vary widely deal to deal (see Step 7), and a couple of items are explicitly
  flagged as unconfirmed/discrepant in the doc.
- Synefi is a former client/research subject referenced in old material in this repo — the
  company is not currently doing work for or about Synefi. Treat Synefi mentions as historical
  context only, not active scope, unless the user says otherwise.

## Current active focus (as of Aug 2026)

Company-wide, Majji and the content strategist are working two lanes this month: the content
strategist owns inbound (content/blog/SEO conversions), the user owns outbound. The user's
month-1 goal: tighten the existing outbound pipeline's conversion (connection acceptance rate,
reply rate, meetings booked) — target 10-12 booked meetings this month — using the *existing*
targeting/company selection, not a new targeting strategy. The bigger open question ("which
companies should we even be targeting, how do we land bigger/enterprise deals") is a separate,
not-yet-decided strategic question — do not conflate the two when doing this month's work.

Separately, real opportunities exist that are already-researched-but-not-built (see
`progress-log.md` section 9 and `company-understanding.md`'s Flagged Opportunities list):
content-strategy copilot using existing account research, PE/VC portfolio-based prospecting
(directly relevant to the "how do we find the right/bigger companies" question), CRM hygiene
fixes, and the still-unbuilt pieces of the original AI Operating System architecture spec
(Verification Agent, Org Benchmark Agent). None of these are committed work — they're context
for when the user is asked to think about "what's next."
