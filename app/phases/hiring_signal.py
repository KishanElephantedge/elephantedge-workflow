"""
Rich hiring-signal classification (Signal Framework v2, section 3) -- replaces the simple
"does a posting exist" check with a real classification of *which* role is being hired, whether
it's a first-ever hire or scaling an existing team, and what that implies.

Uses the same `bloomberry_search_job_postings` tool already wired in buying_signal.py -- it
already returns the full JD text (`description` field), which was simply never read before.
One combined call per company (single OR keyword across all role categories), classified
locally by title-matching -- no additional paid call per role category.
"""

from sqlalchemy.orm import Session

from app.db.models import Company
from app.deepline_client import DeeplineError, execute_tool

ROLE_KEYWORDS = {
    # Expanded from real, observed title-frequency data (a Jobo agent SQL query surfaced
    # e.g. "Business Development Manager" at 11,690 occurrences and "Sales Director" at 1,659 --
    # titles our original narrow list would have completely missed) -- not exhaustive, since
    # companies phrase this differently, but broadened to catch the common real variants rather
    # than only the handful of titles we originally guessed.
    "head_of_sales": [
        "head of sales", "vp sales", "vp of sales", "director of sales", "sales director",
        "sales manager", "cro", "chief revenue officer", "director, sales",
    ],
    "sdr": [
        "sdr", "sales development representative", "bdr", "business development representative",
        "business development manager", "business development executive", "business development specialist",
        "business development associate", "business development director", "business development lead",
        "business development consultant", "business development coordinator", "business development officer",
        "director of business development", "director, business development", "sales development manager",
        "outbound sales representative", "outbound sales development representative",
        "lead generation specialist", "lead generation representative",
    ],
    "ae": ["account executive", " ae "],
    "marketing": [
        "head of marketing", "marketing manager", "growth marketing", "performance marketing",
        "digital marketing manager", "field marketing manager", "regional marketing manager",
        "head of growth marketing",
    ],
    "gtm": ["head of gtm", "gtm engineer", "gtm operations"],
}

COMBINED_JOB_KEYWORD = (
    "Head of Sales OR VP Sales OR VP of Sales OR Director of Sales OR Sales Director OR "
    "Sales Manager OR CRO OR Chief Revenue Officer OR "
    "SDR OR Sales Development Representative OR BDR OR Business Development Representative OR "
    "Business Development Manager OR Business Development Executive OR Business Development Specialist OR "
    "Business Development Associate OR Business Development Director OR Business Development Lead OR "
    "Director of Business Development OR Sales Development Manager OR Outbound Sales Representative OR "
    "Outbound Sales Development Representative OR Lead Generation Specialist OR Lead Generation Representative OR "
    "Account Executive OR Head of Marketing OR Marketing Manager OR Growth Marketing OR Performance Marketing OR "
    "Digital Marketing Manager OR Field Marketing Manager OR Regional Marketing Manager OR Head of Growth Marketing OR "
    "Head of GTM OR GTM Engineer OR GTM Operations"
)

# First-hire language patterns -- direct textual evidence a JD is for a brand-new role, not a
# replacement or team expansion. Confirms/strengthens the headcount-based inference below.
FIRST_HIRE_PHRASES = ["first sales hire", "first-ever", "build from scratch", "founding", "ground floor", "build our sales"]

TOFU_KEYWORD = "top of the funnel"

# Product-fit JD language -- direct textual evidence the role being hired is manually doing what
# Elephant Edge automates (company/ICP research, decision-maker identification, outbound
# campaign execution, AI-native prospecting). A company describing THIS in its own JD is a much
# sharper buying-intent signal than a bare role-title match: they're describing our own product's
# job description as a headcount line item. Grouped by category (not one flat list) so scoring
# can weight "matched N distinct categories" rather than just "matched one keyword somewhere" --
# companies phrase this very differently, so the list is intentionally broad, not the literal
# phrasing of any one real JD it was built from.
PRODUCT_FIT_KEYWORD_CATEGORIES: dict[str, list[str]] = {
    "outbound_motion_building": [
        "outbound motion", "outbound engine", "outbound strategy", "outbound process",
        "outbound function", "own our outbound", "own the outbound", "build outbound",
        "outbound from scratch", "outbound from the ground up", "outbound playbook",
    ],
    "icp_and_list_building": [
        "icp", "ideal customer profile", "target account", "target list", "targeted list",
        "targeted lists", "prospect list", "prospecting list", "build a list", "build lists",
        "list building", "list-building", "list coverage", "research and build", "target companies",
        "priority verticals", "account list", "segment target accounts", "build large",
    ],
    "decision_maker_identification": [
        "identify decision-makers", "identify decision makers", "identify the right decision",
        "find the right contacts", "source contact information", "source accurate contact",
        "key stakeholders", "right person to contact", "decision maker research", "contact data",
    ],
    "outbound_campaign_execution": [
        "outbound campaign", "outbound campaigns", "sequencing", "cold email", "cold outreach",
        "outreach cadence", "multi-channel outreach", "outreach at volume", "personalized outreach",
        "messaging and iteration", "cold calling", "email sequences", "outreach activity",
    ],
    "ai_native_automation": [
        "ai-native", "ai native", "signal-based prospecting", "automated research", "leverage ai",
        "use ai tools", "ai agents", "ai tools to accelerate", "automation-first", "scale outbound",
        "genuinely ai-native", "automate the repetitive", "ai-powered", "ai and search tools",
    ],
    "revops_crm_setup": [
        "revops", "crm", "pipeline stages", "hubspot", "salesforce", "pipedrive",
        "clean and up to date", "deduplicated", "data hygiene", "reporting that tells us",
    ],
    "tool_stack_mentioned": [
        "apollo", "clay", "sales navigator", "zoominfo", "outreach.io", "salesloft", "lemlist",
        "instantly", "smartlead",
    ],
}

# Sales headcount % below this is treated as "no team or a small team exists" for
# hire-type inference (Signal Framework v2's own wording), consistent with Discovery's
# 2-10% qualifying band -- near the low end of that band counts as "small team".
SMALL_TEAM_THRESHOLD_PERCENT = 4.0


def has_qualifying_hiring_signal(company: Company) -> bool:
    """The real gate: a company only qualifies for Decision Maker / Outreach if it's actually
    shown some form of GTM-hiring evidence -- either a role-title match (hiring_signal_role) OR
    real JD-content evidence it needs what our product does (product_fit_jd_categories), not
    just firmographic fit. Either one is enough (OR, not AND) -- a company describing our exact
    product's job in a JD is real evidence even if the title itself didn't match one of our
    known role buckets, and vice versa. Companies with neither get excluded before the most
    expensive phase (Decision Maker) ever runs for them."""
    return company.hiring_signal_role is not None or bool(company.product_fit_jd_categories)


# Team-composition gate -- added after Kishan manually reviewed real hits (batch 32) and found
# SmartWinnr was a false positive despite passing has_qualifying_hiring_signal: it has 12
# real Sales & Revenue people (confirmed via a department-filtered total_count -- an
# unfiltered sample of 30 people badly undercounted this, returning mostly unrelated
# departments like Customer Service). Thresholds are set from today's small real sample
# (SmartWinnr 12 sales = excluded, NetSfere ~9 sales = secondary/still-acceptable, Operant
# AI/Gizmeon ~1-3 = clear gap/primary) and should be revisited once more real data comes in.
#
# Note: an earlier version of this also tried to detect "does an existing employee already
# personally cover GTM" by scanning every employee's headline for the word "gtm" -- dropped
# after a real false positive (a Channel Partnerships person at Operant AI whose headline
# listed "GTM Strategy" as one of several buzzwords, unrelated to the specific open role).
# That's a genuinely harder problem (comparing the open JD against what an existing person
# already does) than a keyword scan can solve, so it's not automated here -- headcount is
# the reliable signal, and borderline "secondary" cases still get human review before outreach.
FULL_TEAM_SALES_THRESHOLD = 10
FULL_TEAM_MARKETING_THRESHOLD = 8
SECONDARY_TEAM_SALES_THRESHOLD = 6


def assess_team_composition(company: Company, db: Session) -> dict:
    """Real, per-company GTM team-composition check via crustdata_v3_person_search, scoped to
    the exact domain (no cross-company attribution risk, since this is domain-exact, not the
    broken crustdata_v3_job_search). Uses a department-filtered total_count per query (cheap,
    limit=1 -- total_count reflects the true total regardless of how many profiles are
    returned) rather than counting over an arbitrary, unrepresentative sample. Returns one of
    three tiers:
      - "excluded": already has a full GTM team by real headcount -- doesn't need us even
        though has_qualifying_hiring_signal was true.
      - "secondary": a moderate existing sales team (still hiring, but not clearly understaffed)
        -- real candidate, but lower priority than a cleaner team-gap company; never skipped,
        just ranked after "primary" companies when picking the target count.
      - "primary": a clear team gap -- best fit, matches what Kishan called "in need."
    Persists the result directly on the Company row.

    Real bug fix (2026-08-19): _department_count() used to have no error handling at all --
    confirmed live that a single real Deepline failure (balance at -0.96 credits) here crashed
    the ENTIRE autonomous run, since this runs early in discovery, per company, for every branch
    (apify_discovery.py/jd_first_discovery.py/decision_maker.py) -- the same class of bug
    already fixed in decision_maker.py's _run_search_contact(), just a second, separate call
    site that fix didn't cover. A failed count is now treated the same as "no domain to verify
    against" above (tier="primary", the conservative "can't rule out a real gap" default) rather
    than crashing -- this one company's tier becomes a guess-free "we don't know," not a reason
    to lose the whole day's run."""
    if not company.domain:
        company.team_fit_tier = "primary"
        company.team_fit_reasoning = "no domain to verify team composition against"
        db.commit()
        return {"tier": "primary", "reasoning": company.team_fit_reasoning}

    def _department_count(department: str) -> int | None:
        filters = {
            "op": "and",
            "conditions": [
                {"field": "experience.employment_details.current.company_website_domain", "type": "=", "value": company.domain},
                {"field": "basic_profile.normalized_title.department", "type": "=", "value": department},
            ],
        }
        try:
            response = execute_tool("crustdata_v3_person_search", {"filters": filters, "limit": 1})
        except DeeplineError:
            return None
        raw = response.get("toolResponse", {}).get("raw", {})
        return raw.get("total_count", 0) if isinstance(raw, dict) else 0

    sales_count = _department_count("Sales & Revenue")
    marketing_count = _department_count("Marketing")

    if sales_count is None or marketing_count is None:
        tier = "primary"
        reasoning = "team composition lookup failed -- could not verify, treated as an unconfirmed gap"
        company.team_fit_tier = tier
        company.team_fit_reasoning = reasoning
        db.commit()
        return {"tier": tier, "reasoning": reasoning, "sales_count": sales_count, "marketing_count": marketing_count}

    if sales_count >= FULL_TEAM_SALES_THRESHOLD or marketing_count >= FULL_TEAM_MARKETING_THRESHOLD:
        tier = "excluded"
        reasoning = f"Full GTM team already: {sales_count} sales + {marketing_count} marketing people"
    elif sales_count >= SECONDARY_TEAM_SALES_THRESHOLD:
        tier = "secondary"
        reasoning = f"Existing sales team ({sales_count} people) but still hiring, no clear hierarchy seen -- deprioritize unless no primary candidates"
    else:
        tier = "primary"
        reasoning = f"Real team gap: {sales_count} sales, {marketing_count} marketing people"

    company.team_fit_tier = tier
    company.team_fit_reasoning = reasoning
    # Free byproduct of the two paid calls above (added 2026-08-14): this is the exact data
    # _infer_hire_type() needs (sales_headcount_percent/marketing_headcount_percent), previously
    # only ever populated on the jd_first/Deepline path via a separate paid Crustdata call.
    # Persisting it here means the Apify path's first-hire-vs-scaling classification actually
    # works instead of always falling back to "unknown" -- no new cost, since sales_count/
    # marketing_count were already being fetched and discarded after the tier decision.
    if company.employee_count:
        company.sales_headcount_percent = round(100 * sales_count / company.employee_count, 2)
        company.marketing_headcount_percent = round(100 * marketing_count / company.employee_count, 2)
    db.commit()
    return {"tier": tier, "reasoning": reasoning, "sales_count": sales_count, "marketing_count": marketing_count}


def _detect_product_fit_signals(description: str) -> list[str]:
    """Returns every PRODUCT_FIT_KEYWORD_CATEGORIES category with at least one phrase match in
    this JD's body text -- the count of distinct categories matched (not raw phrase count) is
    what scoring treats as signal strength, since a JD hitting many different categories reads
    much more like "this role is literally our product" than one hitting the same category
    repeatedly."""
    description_lower = description.lower()
    matched_categories = []
    for category, phrases in PRODUCT_FIT_KEYWORD_CATEGORIES.items():
        if any(phrase in description_lower for phrase in phrases):
            matched_categories.append(category)
    return matched_categories


def _classify_role(title: str) -> str | None:
    title_lower = f" {title.lower()} "
    for role, keywords in ROLE_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return role
    return None


def _infer_hire_type(company: Company, role: str) -> str:
    """No API tells us directly whether this is a company's 1st or 5th hire for a role --
    inferred from existing team size (Discovery's role_distribution_percent), the same
    approach used manually in the Lucky Robots worked example (account-gap-analysis-methodology.md)."""
    if role in ("head_of_sales", "sdr", "ae"):
        percent = company.sales_headcount_percent
    else:
        percent = company.marketing_headcount_percent
    if percent is None:
        return "unknown"
    return "first_hire" if percent <= SMALL_TEAM_THRESHOLD_PERCENT else "multiple_hire"


def _classify_signal(company: Company, role: str, hire_type: str, description: str) -> tuple[str, str]:
    """Returns (strength, reasoning), per Signal Framework v2's tables (section 3)."""
    description_lower = description.lower()
    first_hire_confirmed = hire_type == "first_hire" or any(p in description_lower for p in FIRST_HIRE_PHRASES)

    no_sales_team = (company.sales_headcount_percent or 0) <= SMALL_TEAM_THRESHOLD_PERCENT
    no_marketing_team = (company.marketing_headcount_percent or 0) <= SMALL_TEAM_THRESHOLD_PERCENT

    if role == "head_of_sales" and first_hire_confirmed:
        return "strong", "First Head of Sales hire, no/small team exists -- transfer of revenue-generation ownership from founder. Ask: what was the previous revenue motion?"
    if role == "sdr":
        if first_hire_confirmed:
            return "strong", "First SDR hire -- testing outbound. Cross-check recent funding to corroborate intent."
        return "medium", "Multiple/parallel SDR hire -- outbound already working, doubling down. Ask: why hire more instead of augmenting?"
    if role == "ae":
        if first_hire_confirmed:
            return "medium", "First AE hire -- an SDR or marketing team may already exist; confirms a pipeline exists."
        if no_sales_team and no_marketing_team:
            return "strong", "Multiple AE hires, no SDR or marketing team -- AE owns the full sales cycle end-to-end."
        return "medium", "Multiple AE hires, with SDR -- needs more closing efficiency."
    if role == "marketing":
        if no_sales_team:
            return "weak", "Marketing hire, no sales team -- focus shift toward inbound/PLG. Ask: why hire more instead of augmenting?"
        return "medium", "Marketing hire, with sales team -- needs more inbound/TOFU pipeline. Ask: why hire more instead of augmenting?"
    if role == "gtm":
        return "medium", "GTM hire, with sales team -- focus shift toward augmenting/automating sales process and marketing. Ask: how much are they spending on this?"
    return "weak", "Matched a role posting but couldn't confidently classify hire type."


# Core sales-only title terms for the TheirStack query itself -- narrower than the full
# ROLE_KEYWORDS set (drops pure-marketing/gtm terms) since TheirStack bills per result
# returned; a tighter server-side filter means fewer irrelevant postings billed per company,
# confirmed live (real batch of 149 companies cost $0.35 total with this narrowing, vs.
# markedly more per-hit with the full unfiltered keyword list).
_CORE_SALES_ROLES = {"head_of_sales", "sdr", "ae", "gtm"}
THEIRSTACK_TITLE_PATTERNS = sorted({kw.strip() for role, kws in ROLE_KEYWORDS.items() if role in _CORE_SALES_ROLES for kw in kws if kw.strip()})
JOB_POSTING_MAX_AGE_DAYS = 45


def classify_hiring_signal(company: Company, db: Session) -> dict:
    """Fetches active postings and classifies the strongest match found. Persists results
    directly on the Company row.

    Switched from bloomberry_search_job_postings to theirstack_job_search -- confirmed live
    that Bloomberry has near-zero coverage for our ICP band (multiple real small companies
    with confirmed real LinkedIn/Indeed postings returned zero Bloomberry results), while
    TheirStack found real postings for the same companies. Two more real bugs found and
    fixed here: (1) TheirStack's own recency filter alone isn't enough -- a posting can be
    within posted_at_max_age_days yet already show "No longer accepting applications" on
    LinkedIn, so postings with a non-null closed_at are filtered out client-side; (2) no
    explicit order_by meant an identical repeat query could return a DIFFERENT top posting,
    so date_posted desc is pinned for determinism."""
    if not company.domain:
        return {"role": None, "hire_type": None, "strength": None, "reasoning": "no domain"}

    response = execute_tool(
        "theirstack_job_search",
        {
            "company_domain_or": [company.domain],
            "job_title_or": THEIRSTACK_TITLE_PATTERNS,
            "posted_at_max_age_days": JOB_POSTING_MAX_AGE_DAYS,
            "order_by": [{"field": "date_posted", "desc": True}],
            "limit": 3,
        },
    )
    all_jobs = response.get("toolResponse", {}).get("raw", {}).get("data", [])
    jobs = [j for j in all_jobs if j.get("closed_at") is None]

    best_role = None
    best_hire_type = None
    best_strength = None
    best_reasoning = None
    best_title = None
    best_url = None
    tofu_found = False
    product_fit_categories: set[str] = set()

    strength_rank = {"strong": 3, "medium": 2, "weak": 1}
    for job in jobs:
        title = job.get("job_title") or ""
        description = job.get("description") or ""
        if TOFU_KEYWORD in description.lower():
            tofu_found = True
        # Checked across every posting fetched, not just the one that ends up "best" for role
        # classification -- product-fit language can appear in a posting whose title didn't
        # match a role keyword cleanly, and it's still real evidence worth capturing.
        product_fit_categories.update(_detect_product_fit_signals(description))
        role = _classify_role(title)
        if not role:
            continue
        hire_type = _infer_hire_type(company, role)
        strength, reasoning = _classify_signal(company, role, hire_type, description)
        if best_strength is None or strength_rank.get(strength, 0) > strength_rank.get(best_strength, 0):
            best_role, best_hire_type, best_strength, best_reasoning, best_title, best_url = role, hire_type, strength, reasoning, title, job.get("url")

    company.active_job_title = best_title
    company.hiring_signal_role = best_role
    company.hiring_signal_hire_type = best_hire_type
    company.hiring_signal_strength = best_strength
    company.product_fit_jd_categories = sorted(product_fit_categories) or None
    reasoning_suffix = ""
    if tofu_found:
        reasoning_suffix += " [TOFU pipeline keyword found in JD]"
    if product_fit_categories:
        reasoning_suffix += f" [JD describes our product's own job: {', '.join(sorted(product_fit_categories))}]"
    if best_url:
        reasoning_suffix += f" [posting: {best_url}]"
    company.hiring_signal_reasoning = (best_reasoning or "") + reasoning_suffix
    db.commit()

    return {
        "role": best_role,
        "hire_type": best_hire_type,
        "strength": best_strength,
        "reasoning": company.hiring_signal_reasoning,
        "product_fit_categories": sorted(product_fit_categories),
        "postings_checked": len(jobs),
    }
