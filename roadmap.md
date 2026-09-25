# Elephant Edge Tool — Roadmap & Current Stage

Durable record of what this tool is actually trying to become, and where it really is right
now, so this doesn't get lost or re-litigated every session. Read alongside `CLAUDE.md`
(working approach), `progress-log.md` (technical build log), and `company-understanding.md`
(business research).

## The actual end-state vision

This is not a lead-enrichment tool and not an email-sending tool. Those (Clay, Apollo,
Instantly, Smartlead) solve a narrower problem — data enrichment or send infrastructure — and
are not the comparison set. **The intent is for this tool to eventually run the entirety of
what Elephant Edge does as a company** — not just the sales-pipeline slice of it. Whatever
Elephant Edge does by hand today is fair territory for this tool to eventually take over or
augment. That includes, based on what's actually known about the business so far (see
`company-understanding.md` — this list is not the ceiling, it will grow as more of the
business is understood):

1. **Business development** — finding the right companies, enriching their data, outreaching,
   converting to meetings, progressing and closing deals. (The pipeline that exists today.)
2. **Client delivery / execution** — Elephant Edge's actual paid service isn't just landing a
   deal, it's *running* the engagement afterward: Fractional VP Sales work, the staffing/BOOT
   model (hiring, training, managing SDRs on a client's behalf), the bootcamp program,
   reporting progress back to the client. All real, hands-on work happening today with no
   tooling around it at all.
3. **Marketing & content** — SEO/blog content, social distribution, events/webinars,
   newsletters, personal-brand content — an entire function currently run manually and,
   per the research, badly underperforming its own goals.
4. **Internal operations** — CRM hygiene, proposal generation, pricing/contracts, pipeline
   tracking — all currently manual, spreadsheet/doc-based work per the Drive research.
5. **Quality/oversight layers** — the still-unbuilt pieces of the original AI Operating System
   spec (Verification Agent, Org Benchmark Agent, Collision Critic, Capacity Reasoner) that
   would sit across whichever of the above is automated, catching bad output before it ships.

This list reflects what's known right now, not a fixed roadmap — as more of the business comes
into view (through use, through Majji, through further research), new territory gets added
here rather than treating "content generation" or any other single item as the finish line.

## Where we actually are right now (current real stage)

**Only a slice of item 1 (business development) is active, and even that isn't validated yet.**
Company discovery + enrichment already runs (existing pipeline). Outreach is live via LinkedIn
(SalesRobot). But:

- We don't yet know if the leads being found are actually the right/good-fit companies.
- We don't yet know if the outreach is converting (connect → reply → meeting) at a healthy rate.

**This month's real job, before anything else**: figure out whether outreach is actually
converting, and fix that specifically. Nothing further along item 1 (deal progression/closing),
let alone items 2-5, should be worked on until this stage is genuinely working — not because
those ideas are bad, but because building on top of an unvalidated stage compounds the wrong
things.

## The working principle going forward

At each stage, the standard is: how would a senior GTM engineer / AI consultant / product
person building this for real approach it — not "what feature can we bolt on," but what gap
exists right now, what would the best possible version of this specific stage look like, and
how do genuinely comparable systems (AI-native full-stack outbound/conversion systems — not
plumbing tools) handle the same problem. Research should stay scoped to whatever the *current*
stage actually is, not the whole roadmap at once.

## Parked bug — real, found live 2026-09-24, not yet fixed

**`flow_target`'s outer loop silently spends on the historical backlog, not just today's fresh
discovery.** Setting `discovery.daily_target` low (tested at 2) only caps *new* discovery --
`flow_target.daily_flow_target` then drives a SEPARATE loop that searches the entire backlog of
previously-discovered companies with no decision-maker yet, trying to hit its own flow-count
target. Confirmed live: a "small test" (`daily_target=2`) actually ran `free_decision_maker`
Apify searches against 13 old, unrelated backlog companies (simpat.tech, cassidyai.com, etc. --
none from that day's fresh discovery at all), burning a freshly-rotated $5 Apify account down to
$4.998 spent in one run. This is the exact "flow-target outer loop as a cost/complexity driver"
gap flagged in the very first diagnosis of this system (see the W6 "delete surface" plan) --
never actually fixed. Real next step: either bound the backlog-search the same way
`discovery.daily_target` bounds fresh discovery, or make it a separate, explicitly-approved
step rather than an implicit side effect of any real-run trigger.

## 2026-09-26 — real Deepline budget-guard bug found live and fixed ($8.01 vs a $0.50/day cap)

A SECOND, related symptom of the same underlying gap as the parked `flow_target` bug above,
but a distinct root cause, on the Deepline side rather than Apify. `run_v2_contact_discovery_sweep`
(`app/gtm_os/sales/contact_discovery.py`) processes up to 50 Opportunities per sweep call; each
one's paid-fallback pre-flight check (`_check_paid_fallback_budget`) constructed a FRESH
`BudgetGuard(contact_budget_usd)` per opportunity -- and `BudgetGuard.__init__` re-baselines to
the CURRENT real balance on every construction, so "spent so far today" was always ~$0 no matter
how many opportunities had already been processed. The configured
`limits.contact_discovery_daily_budget_usd: 0.5` was compared against one company's own estimated
max cost (~$0.336), never against real cumulative spend -- the exact "guard recreated fresh each
call" bug already diagnosed and fixed for `check_daily_deepline_budget`'s three other call sites
on 2026-09-15, just never fixed here. Confirmed live via the real billing ledger: 97
`search_contact` charges between 2026-09-24 05:00-08:28 UTC, $8.01 total, 16x the configured cap.

**Fixed**: `_check_paid_fallback_budget` now delegates to `check_daily_deepline_budget` (the
already-correct, persisted-UTC-day-snapshot mechanism), so the daily cap is real cumulative spend,
not a per-call estimate. The redundant per-opportunity `BudgetGuard` (pre- and post-call) was
removed entirely -- it never added real protection once its baseline was shown to reset every
call. Two tests pin the fix (`tests/test_contact_discovery_budget.py`): a second opportunity is
allowed once spend is still under the cap, and blocked once cumulative spend crosses it within
the same run. 185 tests passing.

**V2 autonomous run paused** (`state: running -> paused`) while this was diagnosed -- not yet
resumed; that's a separate decision from this fix landing.

**The original Apify-side `flow_target` backlog-search bug above is still open** -- different
code path (`free_decision_maker`/Apify vs. `search_contact`/Deepline), not touched by this fix.

## Non-goals right now (explicitly deferred, not forgotten)

- Deal progression / closing automation (rest of item 1) — not started, not the current focus.
- Client delivery/execution tooling (item 2), marketing/content (item 3), internal ops (item 4),
  quality/oversight layers (item 5) — all real, all eventually in scope, none started. Content
  generation specifically was already researched conceptually (progress-log.md section 9).
- Broader targeting/ICP changes — this month's outreach work uses the *existing* targeting;
  "which companies should we target" is a separate, undecided strategic question
  (see `CLAUDE.md`).
- Jobo pipeline verification, HeyReach cleanup, Render/Deepline connectivity issue — real, but
  explicitly deprioritized for now per direct instruction.

## 2026-09-24 — real strategic direction from Majji: differentiation, not manual replication

Direct feedback, verbatim: *"We need to show some differentiation man.. whatever we did so far
even Sandy can do if she put in some efforts.. it's low effort works. We need to solve a complex
workflow that they can't imagine or can execute. That creates wow moment."*

This is the real reason the Sandy Yu engagement (manual CSV filtering across Sales Nav/Clay/
SalesIntel, see `sandy/CONTEXT.md`) is explicitly NOT the bar to build toward — it's necessary
right now only because V2's autonomous capability isn't reliable enough yet to run it hands-off.
The manual work is a stopgap, not the target state.

**Two concrete directions that came out of this:**

1. **Event-attendee prospecting as a real differentiator.** Majji's own framing: *"if we can
   find CROs who attend any event... that's a right one with learning mindset... if we identify
   events done for Revenue leaders, CROs, CXOs and scrape that data, that's a good prospect
   list."* Not a one-off pull — a standing capability (find revenue-leader/CXO events happening
   across the US this quarter, scrape attendee/interested lists) that most competitors can't
   easily replicate by hand.

2. **Self-learning/autonomous agent adoption — look at Hermes Agent (nousresearch.com/
   github.com/nousresearch/hermes-agent) and comparable frameworks.** The reasoning (user's own
   synthesis, confirmed as the right read): today's LLM interactions only know what's fed into
   one prompt each time (e.g. "write LinkedIn content for this profile"); a system with
   persistent, compounding memory across everything the team does and thinks would have
   materially better context over time, and could start *suggesting* ideas/direction rather than
   just executing tasks. This is explicitly framed as upgrading/strengthening V2's own autonomy,
   not replacing it — V2 already IS meant to be this kind of system; it just isn't reliable
   enough yet, which is the actual gap to close. Not yet researched in depth — a real next step,
   not a decision made.

**Also decided**: starting with Remy (the next client after Sandy), Elephant Edge moves to
**owned email infrastructure** per client (e.g. `remy@fractionalpartner.us`) via sending.ac
(chosen over zapmail.ai — comparable products, sending.ac's continuous inbox-placement
monitoring + mailbox-burn insurance wins for a pattern meant to scale across many future
clients). Sandy's own campaign stays on outsourced sending (already delegated externally due to
timeline) — this owned-infra pattern starts fresh with Remy and extends to each client after.
