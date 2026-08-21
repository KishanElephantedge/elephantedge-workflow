"""LinkedIn-post SEARCH-based sensing config -- GTM-OS end-to-end wiring, unblocking the
"zero hypotheses" problem the earlier audit diagnosed. sense_linkedin_posts() (sensing.py) has
always been able to open a new ProblemHypothesis/DemandHypothesis (the only source that can,
per problem_detection.py's own tier map) -- it was never given a real, configured input.

EXPLICITLY NOT the Network/LinkedIn-monitor watch-list (LinkedinMonitorProfile, app/phases/
linkedin_monitor.py) -- per direct user instruction, that table exists to watch KNOWN people
(competitors/partners/ecosystem) and must never be reused for this, a fundamentally different
purpose (finding UNKNOWN companies showing real problem/demand evidence). This module never
imports or queries that table.

DERIVATION, NOT A HARDCODED LIST (per explicit instruction): every phrase below is built from
two real, already-existing sources, combined, never invented:
  1. The exact structural trigger phrases linkedin_post_interpretation.py ALREADY recognizes as
     declared-problem / solution-question / solution-evaluation evidence (imported verbatim from
     that module, never re-typed here) -- so a phrase this module searches for has a real chance
     of ALSO surviving that module's own phrase-match interpretation gate, not just being
     topically related.
  2. The real, lead-authored topic language already sitting in this tenant's own ICP config
     (icp_config.py -- each ICP's own short `name`, e.g. "Stuck in Sales", "Upgrading With AI"),
     offering config (offering_config.py's real offering names), and business_context's
     `icp_bands[].profile` strings (business_context.py, e.g. "Companies stuck in sales",
     "Looking for FDEs in Sales/GTM") -- the lead's own words for who has the problem, not a
     generic "AI sales" keyword list. Longer explanatory fields (an ICP's own `trigger_description`
     sentence) are deliberately NOT used as search phrases -- see `_icp_topic_terms()`.

Config is Parameter-backed (same get/set/validate pattern as every other GTM-OS config in this
codebase) -- fully overridable/tunable from the dashboard without a code change, per instruction.
When no override has been saved yet, get_linkedin_search_phrases() computes the derived default
FRESH from whatever ICP/offering/business-context config is live right now (not a frozen
snapshot) -- so editing the ICP config also naturally updates what this module searches for,
without a separate manual sync step."""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import Parameter
from app.gtm_os.context.business_context import get_business_context
from app.gtm_os.context.business_terms import all_business_topic_terms
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.intelligence.linkedin_post_interpretation import (
    DECLARED_PROBLEM_PHRASES,
    ONGOING_SOLUTION_PHRASES,
    QUESTION_LEAD_PHRASES,
)
from app.gtm_os.opportunity.offering_config import get_offering_config

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


def derive_default_search_phrases(icp_config: list[dict], offering_config: list[dict], business_context: dict) -> list[str]:
    """Pure function -- combines a small, curated subset of the SAME structural trigger phrases
    linkedin_post_interpretation.py already recognizes with real topic terms pulled from ICP/
    offering/business-context config (app/gtm_os/context/business_terms.py, shared with
    content/topics.py's default topic derivation -- same real config, same derivation logic,
    reused rather than duplicated). Deterministic, no LLM, fully inspectable (every phrase traces
    back to one real config field + one real interpretation-layer trigger, see module docstring).
    Deliberately NOT a full cross-product (that would produce dozens of near-duplicate queries for
    no real coverage gain) -- one representative structural trigger per topic term, rotating
    through the three trigger categories so the result covers all three event types
    (problem_statement / solution_question / solution_evaluation_mention), not just one."""
    deduped_topics = all_business_topic_terms(icp_config, offering_config, business_context)
    triggers = [DECLARED_PROBLEM_PHRASES[0], QUESTION_LEAD_PHRASES[0], ONGOING_SOLUTION_PHRASES[0]]

    phrases = []
    for i, topic in enumerate(deduped_topics):
        trigger = triggers[i % len(triggers)]
        phrases.append(f"{trigger} {topic}".strip())
    return phrases


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
    "date_posted_filter": str}. No stored override yet -> phrases are computed FRESH from live
    ICP/offering/business-context config (see module docstring); once a human edits and saves via
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
        config = {"phrases": derive_default_search_phrases(
            get_icp_config(db, tenant_id), get_offering_config(db, tenant_id), get_business_context(db, tenant_id),
        )}
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
