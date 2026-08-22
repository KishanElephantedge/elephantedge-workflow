"""Source capability registry -- Autonomous Sensing Phase S3 (part 1).

A small, structured registry describing what each REAL existing sensing adapter can and cannot
do -- built from directly inspecting each adapter (app/gtm_os/intelligence/sensing.py), the
scheduler's own SWEEPABLE_SOURCES list (app/gtm_os/orchestration/sweep.py), and the real,
already-documented per-call costs in app/apify_client.py. Nothing here is invented: every field
traces to a real, already-existing fact about the codebase.

`wired_into_hourly_sweep=False` on linkedin_job/web_search/company_website is not a limitation
introduced by this phase -- it reflects the codebase's own existing state (linkedin_job has no
approved search-criteria config yet; web_search/company_website are on-demand tools, never part
of sweep.py's SWEEPABLE_SOURCES). S3 still lists them as selectable strategies (a future S4/S5
could invoke them directly, same as any on-demand tool call already does elsewhere in this
codebase), it just records honestly that today's automatic hourly cycle won't reach them itself.

Cost categories are deliberately NOT invented for sources with no real documented cost --
`cost_category="cost_unknown"` is used, never guessed. See COST section per source below."""

# apify_client.py: LINKEDIN_POST_COST_PER_POST_USD = 0.002 (confirmed real, pay-per-post)
# apify_client.py: COST_PER_JOB_USD = 0.005, COST_PER_RUN_USD = 0.01 (confirmed real)
# apify_client.py: GOOGLE_SEARCH_COST_PER_QUERY_USD = 0.0085 (confirmed real)
# app/deepline_client.py: execute_tool() -- theirstack_job is Deepline-routed, so its real cost
#   guard is the existing Deepline BudgetGuard (app/budget_guard.py), not a per-call $ figure in
#   this codebase -- "budget_guarded", not "cost_unknown": a real control exists, just not a
#   static number.
# app/website_scraper.py: scrape_company_website() is a plain httpx GET, genuinely free (no
#   third-party API) -- confirmed by that file's own docstring.
# app/hackernews_client.py / app/rss_client.py: official free public APIs (confirmed via
#   sense_hackernews_stories()/sense_rss_articles()'s own docstrings).

COST_APIFY_PER_POST = "apify_per_post_0.002usd"
COST_APIFY_PER_JOB_PLUS_RUN = "apify_per_job_0.005usd_plus_run_0.01usd"
COST_APIFY_PER_QUERY = "apify_per_query_0.0085usd"
COST_DEEPLINE_BUDGET_GUARDED = "deepline_budget_guarded"
COST_FREE = "free"
COST_UNKNOWN = "cost_unknown"

SOURCE_CAPABILITIES: dict[str, dict] = {
    "linkedin_post_search": {
        "evidence_type": "person-level free-text problem/question/solution-evaluation mentions",
        "mode": "proactive",
        "supports_company_identity": "indirect",  # via headline parse + company_resolution.py, not guaranteed
        "supports_person_identity": True,
        "cost_category": COST_APIFY_PER_POST,
        "fallback_allowed": True,
        "wired_into_hourly_sweep": True,
    },
    "linkedin_job": {
        "evidence_type": "structured job-posting title (company-published)",
        "mode": "proactive",
        "supports_company_identity": True,  # a job posting is inherently company-scoped
        "supports_person_identity": False,
        "cost_category": COST_APIFY_PER_JOB_PLUS_RUN,
        "fallback_allowed": True,
        "wired_into_hourly_sweep": False,  # confirmed: no approved search-criteria/profile-list config exists yet (sweep.py docstring)
    },
    "theirstack_job": {
        "evidence_type": "structured job-posting (title + real firmographics: revenue/employee/funding)",
        "mode": "proactive",
        "supports_company_identity": True,
        "supports_person_identity": False,
        "cost_category": COST_DEEPLINE_BUDGET_GUARDED,
        "fallback_allowed": True,
        "wired_into_hourly_sweep": True,
    },
    "web_search": {
        "evidence_type": "broad open-web text (Google AI Overview) -- least targeted, most expensive",
        "mode": "proactive",
        "supports_company_identity": "indirect",  # depends entirely on the query, no structured field
        "supports_person_identity": "indirect",
        "cost_category": COST_APIFY_PER_QUERY,
        "fallback_allowed": True,  # explicitly the last-resort fallback per the approved design's own cheapest-first principle
        "wired_into_hourly_sweep": False,  # on-demand tool only, confirmed via sweep.py docstring
    },
    "company_website": {
        "evidence_type": "company-published marketing/product text -- always tied to exactly one known company/domain",
        "mode": "proactive",
        "supports_company_identity": True,  # the ONLY source where company_id is set directly at creation, no resolver involved at all
        "supports_person_identity": False,
        "cost_category": COST_FREE,
        "fallback_allowed": True,
        "wired_into_hourly_sweep": False,  # on-demand tool only, confirmed via sweep.py docstring
    },
    "hackernews_story": {
        "evidence_type": "public tech-community discussion matching configured Content Intelligence topics",
        "mode": "proactive",
        "supports_company_identity": False,
        "supports_person_identity": False,
        "cost_category": COST_FREE,
        "fallback_allowed": True,
        "wired_into_hourly_sweep": True,
    },
    "rss_article": {
        "evidence_type": "published articles matching configured Content Intelligence topics",
        "mode": "proactive",
        "supports_company_identity": False,
        "supports_person_identity": False,
        "cost_category": COST_FREE,
        "fallback_allowed": True,
        "wired_into_hourly_sweep": True,
    },
    "linkedin_reply": {
        "evidence_type": "reply to an existing outreach message (already-known contact)",
        "mode": "reactive",  # per explicit task instruction: not a primary proactive sensing strategy
        "supports_company_identity": True,  # tied to an existing Contact/Company already
        "supports_person_identity": True,
        "cost_category": COST_APIFY_PER_POST,  # same underlying actor as linkedin_post_search
        "fallback_allowed": False,  # reactive-only; cannot be proactively selected as a fallback strategy
        "wired_into_hourly_sweep": True,
    },
}

PROACTIVE_SOURCES = [name for name, cap in SOURCE_CAPABILITIES.items() if cap["mode"] == "proactive"]
