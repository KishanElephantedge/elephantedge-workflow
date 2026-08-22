"""LinkedIn-post SEARCH-based sensing config -- GTM-OS end-to-end wiring, unblocking the
"zero hypotheses" problem the earlier audit diagnosed. sense_linkedin_posts() (sensing.py) has
always been able to open a new ProblemHypothesis/DemandHypothesis (the only source that can,
per problem_detection.py's own tier map) -- it was never given a real, configured input.

EXPLICITLY NOT the Network/LinkedIn-monitor watch-list (LinkedinMonitorProfile, app/phases/
linkedin_monitor.py) -- per direct user instruction, that table exists to watch KNOWN people
(competitors/partners/ecosystem) and must never be reused for this, a fundamentally different
purpose (finding UNKNOWN companies showing real problem/demand evidence). This module never
imports or queries that table.

REDESIGNED (2026-08-22, real-production-sample audit): the original derivation mechanically
built queries as `f"{trigger} {label}"`, concatenating a fixed interpretation-layer trigger
phrase with an internal ICP/offering CONFIG LABEL (icp_config.py's own `name` field, e.g. "Stuck
in Sales"; offering_config.py's own `name` field, e.g. "Sales OS") -- producing real, live
production queries like "struggling to Stuck in Sales" and "how are other teams sales os". These
are internal taxonomy labels, not language a real prospect would ever write or that would match
one -- confirmed against a real 29-post production sample: zero of those posts were a genuine
prospect describing their own situation; every on-topic hit was a coincidental keyword overlap
inside otherwise-irrelevant thought-leadership/vendor content.

This module is now intentionally DECOUPLED from business_terms.py's label-derivation functions
(icp_topic_terms/offering_topic_terms/business_context_topic_terms/all_business_topic_terms) --
those remain completely unmodified and untouched here, since they're also used by
content/topics.py for a genuinely different purpose (Market Intelligence topic matching against
external content, not Problem/Demand sensing search input; conflating the two was itself part of
what produced label-shaped queries here). See QUERY_CATALOG below: a small, hand-curated,
CONCEPTUALLY grounded (not string-derived) set of real prospect-language search patterns, each
one a plausible sentence a real operator/founder would actually write, tagged with which ICP/
offering concept it represents (by id, not by label-matching a config string) purely for
provenance/traceability -- never used to construct the query text itself.

Config is STILL Parameter-backed (same get/set/validate pattern as every other GTM-OS config in
this codebase) -- fully overridable/tunable from the dashboard without a code change. When no
override has been saved yet, get_linkedin_search_config() computes the curated default fresh."""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import Parameter

# Each entry: a real, natural sentence a prospect/operator might actually write, never an
# internal ICP/offering label. `source` and `intent_family` are provenance metadata only --
# purely for a human auditing why a query exists, never fed back into query construction (which
# would risk reintroducing the exact label-echoing bug this catalog replaces). `pattern` names
# which interpretation-layer trigger family this sentence is shaped like (declared_problem /
# question_lead / ongoing_evaluation / hiring_trigger) -- hiring_trigger is a real fourth shape
# this module didn't previously have a category for at all: a person announcing they're actively
# hiring for a role is a genuine trigger signal in its own right, distinct from a declared
# personal problem statement or a solution-evaluation mention.
QUERY_CATALOG: list[dict] = [
    # ICP1 "Stuck in Sales" -- first-person growth-plateau / capacity pain, never the label itself.
    {"text": "our sales team is stretched thin", "source": "icp:icp_1", "intent_family": "capacity_problem", "pattern": "declared_problem"},
    {"text": "struggling to scale our sales", "source": "icp:icp_1", "intent_family": "growth_plateau", "pattern": "declared_problem"},
    {"text": "sales growth has stalled", "source": "icp:icp_1", "intent_family": "growth_plateau", "pattern": "declared_problem"},
    {"text": "can't keep up with sales demand", "source": "icp:icp_1", "intent_family": "capacity_problem", "pattern": "declared_problem"},

    # ICP2 "Upgrading With AI" -- the real hiring trigger (a GTM engineer role) and real
    # sales-automation evaluation language, never "Upgrading With AI" or "sales os" themselves.
    {"text": "hiring our first gtm engineer", "source": "icp:icp_2", "intent_family": "gtm_engineer_hiring", "pattern": "hiring_trigger"},
    {"text": "looking for a gtm engineer", "source": "icp:icp_2", "intent_family": "gtm_engineer_hiring", "pattern": "hiring_trigger"},
    {"text": "building an ai sdr", "source": "icp:icp_2", "intent_family": "sales_automation", "pattern": "declared_problem"},
    {"text": "evaluating sales automation tools", "source": "icp:icp_2", "intent_family": "sales_automation", "pattern": "ongoing_evaluation"},

    # ICP3 "Needs Fractional Leadership" -- the real hiring trigger (VP/Head of Sales, fractional
    # preference), never "Needs Fractional Leadership" itself.
    {"text": "looking for a fractional vp of sales", "source": "icp:icp_3", "intent_family": "fractional_leadership_hiring", "pattern": "hiring_trigger"},
    {"text": "hiring a head of sales", "source": "icp:icp_3", "intent_family": "fractional_leadership_hiring", "pattern": "hiring_trigger"},
    {"text": "need a fractional sales leader", "source": "icp:icp_3", "intent_family": "fractional_leadership_hiring", "pattern": "declared_problem"},
    {"text": "considering a fractional sales leader", "source": "icp:icp_3", "intent_family": "fractional_leadership_hiring", "pattern": "ongoing_evaluation"},

    # Offering-derived -- ONLY where real, distinct prospect intent exists beyond the ICP queries
    # above. Execution/Sales OS have no distinct prospect language beyond ICP3/ICP2's own hiring-
    # trigger queries (Execution literally IS the fractional-VP-Sales hiring need already
    # covered) -- deliberately not duplicated. Workshop/Sales Products/Digital Playbook have no
    # real, observed prospect-language pattern to ground a query in ("I need a digital playbook"
    # is not a real sentence anyone writes) -- deliberately excluded per the explicit "do not
    # generate noise just to create more queries" instruction, not an oversight.
    {"text": "looking for a sales consultant", "source": "offering:Consulting", "intent_family": "external_advisory_need", "pattern": "hiring_trigger"},
    {"text": "need outside help with our sales process", "source": "offering:Consulting", "intent_family": "external_advisory_need", "pattern": "declared_problem"},
]

SEARCH_CONFIG_PARAMETER_KEY = "gtm_os_linkedin_search_config"

# Bounded, real cost-containment values -- same "small number, explicit, tunable" convention as
# PROFILES_PER_BATCH/every other sweep limit in this codebase. Real cost: ~$0.002/post scraped
# (search_linkedin_posts, same actor/pricing as the Network monitor) -- MAX_PHRASES_PER_CYCLE x
# POSTS_PER_PHRASE bounds the absolute worst case per sweep tick to a small, known number.
DEFAULT_MAX_PHRASES_PER_CYCLE = 8
DEFAULT_POSTS_PER_PHRASE = 10
DEFAULT_DATE_POSTED_FILTER = "past-week"  # same value reverse_discovery.py already uses for the same actor/purpose

# A SEARCH query has no natural incremental cutoff the way a WATCHED PROFILE does (there's no
# per-profile last_checked_at to pass as scrape_until) -- searching the same phrase again inside
# the "past week" window would keep re-scraping (and re-billing for) largely the same results on
# every hourly tick. Real cost control: track each phrase's own last-searched time (in this same
# config, not a new table) and skip re-searching it until this many hours have passed --
# independent of how often the overall sweep runs.
DEFAULT_MIN_HOURS_BETWEEN_SEARCHES = 6


class SearchConfigError(ValueError):
    """Raised when search config fails validation -- never silently coerced."""


def derive_default_search_phrases() -> list[str]:
    """Returns the curated QUERY_CATALOG's query text only, in catalog order -- no longer takes
    icp_config/offering_config/business_context params (kept as a no-arg function since the
    catalog is hand-curated, not derived live from those configs anymore -- see module docstring
    for why). Callers needing the provenance metadata (source/intent_family/pattern) should read
    QUERY_CATALOG directly; this function exists only for the 'phrases' list shape
    select_due_phrases()/sense_linkedin_post_search() already expect, unchanged."""
    return [entry["text"] for entry in QUERY_CATALOG]


def get_query_metadata(query_text: str) -> dict | None:
    """Looks up the provenance (source/intent_family/pattern) for one query's text, for
    reporting/debugging only -- never used to construct or alter the query itself. Returns None
    for a query not in the current catalog (e.g. a human-edited override phrase saved via
    set_linkedin_search_config that isn't one of the curated defaults)."""
    for entry in QUERY_CATALOG:
        if entry["text"] == query_text:
            return {"source": entry["source"], "intent_family": entry["intent_family"], "pattern": entry["pattern"]}
    return None


def _validate_search_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise SearchConfigError("search config must be an object")
    phrases = config.get("phrases")
    if not isinstance(phrases, list) or any(not isinstance(p, str) or not p.strip() for p in phrases):
        raise SearchConfigError("'phrases' must be a list of non-empty strings")
    for key in ("max_phrases_per_cycle", "posts_per_phrase"):
        if key in config and (not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] < 1):
            raise SearchConfigError(f"{key!r} must be a positive integer")
    if "date_posted_filter" in config and not isinstance(config["date_posted_filter"], str):
        raise SearchConfigError("'date_posted_filter' must be a string")


def get_linkedin_search_config(db: Session, tenant_id: int) -> dict:
    """Returns {"phrases": [...], "max_phrases_per_cycle": int, "posts_per_phrase": int,
    "date_posted_filter": str}. No stored override yet -> phrases are the curated QUERY_CATALOG
    defaults (see module docstring); once a human edits and saves via
    set_linkedin_search_config(), that saved list is the source of truth going forward."""
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == SEARCH_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict) and param.value.get("phrases"):
        config = dict(param.value)
    else:
        config = {"phrases": derive_default_search_phrases()}
    config.setdefault("max_phrases_per_cycle", DEFAULT_MAX_PHRASES_PER_CYCLE)
    config.setdefault("posts_per_phrase", DEFAULT_POSTS_PER_PHRASE)
    config.setdefault("date_posted_filter", DEFAULT_DATE_POSTED_FILTER)
    config.setdefault("min_hours_between_searches", DEFAULT_MIN_HOURS_BETWEEN_SEARCHES)
    config.setdefault("phrase_last_searched_at", {})  # phrase -> ISO timestamp string
    return config


def set_linkedin_search_config(db: Session, tenant_id: int, config: dict) -> None:
    _validate_search_config(config)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == SEARCH_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = config
    else:
        param = Parameter(
            tenant_id=tenant_id, key=SEARCH_CONFIG_PARAMETER_KEY, value=config,
            description="LinkedIn-post search phrases for GTM-OS problem/demand sensing -- editable without a code change (see app/gtm_os/intelligence/linkedin_search_config.py)",
        )
        db.add(param)
    db.commit()


def select_due_phrases(config: dict, now: datetime | None = None) -> list[str]:
    """Real rate control (see DEFAULT_MIN_HOURS_BETWEEN_SEARCHES above): a phrase searched more
    recently than `min_hours_between_searches` ago is skipped this cycle, even if it's in the
    configured list -- prevents re-billing Apify for largely the same "past week" results every
    single hourly tick. Bounded to `max_phrases_per_cycle` due phrases, oldest-searched (or never
    searched) first, so cost per cycle has a hard, known ceiling regardless of how many total
    phrases are configured."""
    now = now or datetime.utcnow()
    min_gap = timedelta(hours=config.get("min_hours_between_searches", DEFAULT_MIN_HOURS_BETWEEN_SEARCHES))
    last_searched = config.get("phrase_last_searched_at") or {}

    def _due(phrase: str) -> bool:
        raw = last_searched.get(phrase)
        if not raw:
            return True
        try:
            return now - datetime.fromisoformat(raw) >= min_gap
        except ValueError:
            return True

    due = [p for p in config.get("phrases", []) if _due(p)]
    due.sort(key=lambda p: last_searched.get(p) or "")
    return due[: config.get("max_phrases_per_cycle", DEFAULT_MAX_PHRASES_PER_CYCLE)]


def record_phrases_searched(db: Session, tenant_id: int, config: dict, phrases: list[str], now: datetime | None = None) -> None:
    """The only writer of phrase_last_searched_at -- called once per sweep after the real search
    calls for `phrases` have completed (success or failure both count as "searched," since a
    failed Apify call still means we shouldn't hammer the same query again next tick)."""
    now = now or datetime.utcnow()
    last_searched = dict(config.get("phrase_last_searched_at") or {})
    for phrase in phrases:
        last_searched[phrase] = now.isoformat()
    config = dict(config)
    config["phrase_last_searched_at"] = last_searched
    set_linkedin_search_config(db, tenant_id, config)
