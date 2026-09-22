"""Autonomous daily discovery for PARTNER tenants -- 2026-09-16, explicit instruction: "we need
full autonomous daily [run], same for partners as well" -- mirroring how Elephant Edge's own V2
engine runs on a schedule, but per-partner configurable (on/off, what time, how many companies a
day) since every partner's real volume and cadence needs differ.

DELIBERATELY SEPARATE FROM V1's run_daily_autonomous_cycle() (autonomous_orchestrator.py). That
function is V1 machinery, explicitly retired this session ("we dont use the V1 so off and keep
thats it") -- reusing it here would silently resurrect it. This module calls the SAME real,
already-verified partner-discovery pipeline built and proven live this session (Jobo +
verify_jobo_companies, or Apify + enforce_icp_on_companies), nothing new invented.

CONFIG. One Parameter per tenant (PARTNER_DAILY_RUN_PARAMETER_KEY), shape:
    {
        "enabled": bool,               # the on/off toggle -- OFF until a human turns it on
        "schedule_hour_utc": int,      # 0-23, which hour this tenant's run fires
        "daily_target": int,           # how many companies to aim for per day (firmographic)
        "source": "apify" | "jobo",    # which discovery source this tenant's daily run uses
        "pages": int,                  # jobo only, same meaning as elsewhere
        "engagement_mining_enabled": bool,     # the SECOND objective's own on/off, within the
                                                # same daily run -- see ENGAGEMENT MINING below
        "engagement_posts_per_day": int,       # how many LinkedIn posts to check per day
        "engagement_commenters_per_post": int, # minimum commenters harvested PER post (a floor,
                                                # not a shared pool -- see ENGAGEMENT MINING)
        "last_run_date": "YYYY-MM-DD", # UTC date of the last real trigger, prevents double-firing
    }

ENGAGEMENT MINING, 2026-09-22 real correction. This started as a side effect of piggybacking on
main.py's HOURLY tick with no target and no pause control of its own -- an accident, not a
design, caught live when 30 new engagement signals appeared for majji in one day with nobody
having triggered anything ("i naver told that should run every hour it should run daily and
with a target set"). Moved here so it fires exactly once per day, at the SAME gated trigger as
firmographic discovery, under the SAME enabled/pause control, with a real, explicit target:
"5 posts, 2-3 commentators each" (5x2=10 to 5x3=15 raw commenters/day) rather than one shared
harvest pool that lets a single popular post starve the other four (see
select_relevant_post_urls's own docstring for the ranking/filtering this reuses unmodified, and
LINKEDIN_ENGAGEMENT_ACTOR_ID's own comment for why `maxItems` is a SHARED cap across every
post_url in one call -- guaranteeing a PER-POST floor means calling the harvest actor once per
selected post, not once for all of them).

REAL FIX, 2026-09-16 (was wrong before this): title_search does NOT live here. It is a property
of the PARTNER'S OWN ICP (partner_icp Parameter, app/routes/api.py's PUT /gtm-os/partner/icp) --
Amdrodd's hiring-signal titles are literally their own documented "Salesforce job posting"
trigger, an ICP attribute, not a generic scheduling setting every partner would separately
configure the same way. Explicit correction after being told directly: "why we have to give this
separately job titles to search... everyone's ICPs varies based on that we fetch right." The
daily tick reads title_search from get_daily_run_title_search() below (the tenant's own ICP),
never from this config.

No default is invented for schedule_hour_utc/daily_target/source -- same "None until an operator
sets a real value" discipline as discovery.py's own cadence config. `enabled` defaults False
explicitly, matching V1's is_autonomous_enabled() default and every other autonomous-spend toggle
in this codebase: a brand-new tenant's daily run must never fire before someone deliberately
turns it on.

SCHEDULING MODEL. main.py's scheduler ticks this module's check function once an hour (not once a
day, since each tenant picks its own hour) -- see _scheduled_partner_daily_tick there. This module
itself just answers "is tenant X due right now" and, if so, fires the SAME async background
mechanism the manual POST /gtm-os/partner/discover route already uses, so a daily run behaves
identically to a manually-triggered one (same verification, same real ICP enforcement)."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Batch, Company, Parameter

PARTNER_DAILY_RUN_PARAMETER_KEY = "partner_daily_run_config"
PARTNER_ICP_PARAMETER_KEY = "partner_icp"  # duplicated string, not imported, to avoid a
# partner_pipeline.py <-> partner_icp.py <-> here import cycle; see app/phases/partner_icp.py
# for the canonical definition this must stay in sync with.

ELEPHANT_EDGE_TENANT_ID = 2

DEFAULT_CONFIG = {
    "enabled": False,
    "schedule_hour_utc": None,
    "daily_target": None,
    "source": None,
    "pages": 2,
    "engagement_mining_enabled": False,
    "engagement_posts_per_day": None,
    "engagement_commenters_per_post": None,
    "last_run_date": None,
    # Separate from last_run_date's firmographic gate -- engagement mining is opt-in on its own
    # within one daily run (a partner could enable firmographic discovery without ever having
    # configured engagement mining), so it needs its own "already ran today" marker rather than
    # silently piggybacking on firmographic's.
    "engagement_last_run_date": None,
}


def get_daily_run_title_search(db: Session, tenant_id: int) -> list[str] | None:
    """title_search lives on the partner's own ICP, not on the daily-run config -- see this
    module's docstring correction. Returns None (not an empty list) when the ICP has none set,
    so callers can distinguish "no ICP-level signal configured" from "configured as empty"."""
    icp_param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == PARTNER_ICP_PARAMETER_KEY).first()
    if not icp_param or not isinstance(icp_param.value, dict):
        return None
    titles = icp_param.value.get("title_search")
    return titles if isinstance(titles, list) and titles else None


class DailyRunConfigError(ValueError):
    """Raised when a daily-run config fails validation -- never silently coerced."""


def get_daily_run_config(db: Session, tenant_id: int) -> dict:
    param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == PARTNER_DAILY_RUN_PARAMETER_KEY).first()
    if param and isinstance(param.value, dict):
        return {**DEFAULT_CONFIG, **param.value}
    return dict(DEFAULT_CONFIG)


def _validate_config(config: dict) -> None:
    if not isinstance(config.get("enabled"), bool):
        raise DailyRunConfigError("enabled must be true/false")

    hour = config.get("schedule_hour_utc")
    if hour is not None and (not isinstance(hour, int) or isinstance(hour, bool) or not (0 <= hour <= 23)):
        raise DailyRunConfigError("schedule_hour_utc must be an integer 0-23, or null")

    target = config.get("daily_target")
    if target is not None and (not isinstance(target, int) or isinstance(target, bool) or target <= 0):
        raise DailyRunConfigError("daily_target must be a positive integer, or null")

    source = config.get("source")
    if source is not None and source not in ("apify", "jobo"):
        raise DailyRunConfigError("source must be 'apify', 'jobo', or null")

    if config.get("enabled") and (hour is None or target is None or source is None):
        raise DailyRunConfigError("enabled=true requires schedule_hour_utc, daily_target, and source to all be set")

    if not isinstance(config.get("engagement_mining_enabled"), bool):
        raise DailyRunConfigError("engagement_mining_enabled must be true/false")

    posts_per_day = config.get("engagement_posts_per_day")
    if posts_per_day is not None and (not isinstance(posts_per_day, int) or isinstance(posts_per_day, bool) or posts_per_day <= 0):
        raise DailyRunConfigError("engagement_posts_per_day must be a positive integer, or null")

    commenters_per_post = config.get("engagement_commenters_per_post")
    if commenters_per_post is not None and (not isinstance(commenters_per_post, int) or isinstance(commenters_per_post, bool) or commenters_per_post <= 0):
        raise DailyRunConfigError("engagement_commenters_per_post must be a positive integer, or null")

    if config.get("engagement_mining_enabled") and (posts_per_day is None or commenters_per_post is None):
        raise DailyRunConfigError("engagement_mining_enabled=true requires engagement_posts_per_day and engagement_commenters_per_post to both be set")

    # engagement mining is a SEPARATE objective from firmographic discovery, but they share one
    # daily run -- turning either on requires the run itself to be scheduled (schedule_hour_utc),
    # otherwise "enable engagement mining" would silently do nothing (no hour to check it at).
    if config.get("engagement_mining_enabled") and hour is None:
        raise DailyRunConfigError("engagement_mining_enabled=true requires schedule_hour_utc to be set (both objectives share one daily trigger time)")


def set_daily_run_config(db: Session, tenant_id: int, updates: dict) -> dict:
    """Merges `updates` onto the existing config (partial updates -- a caller flipping just
    `enabled` doesn't have to resend everything) and validates the RESULT, not just the delta."""
    current = get_daily_run_config(db, tenant_id)
    merged = {**current, **updates}
    _validate_config(merged)

    if merged.get("enabled") and not get_daily_run_title_search(db, tenant_id):
        raise DailyRunConfigError(
            "enabled=true requires this tenant's own ICP (PUT /gtm-os/partner/icp) to have a "
            "title_search set -- that's the hiring/buying signal driving discovery, and it "
            "belongs on the ICP, not here."
        )

    param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == PARTNER_DAILY_RUN_PARAMETER_KEY).first()
    if param:
        param.value = merged
    else:
        param = Parameter(
            tenant_id=tenant_id, key=PARTNER_DAILY_RUN_PARAMETER_KEY, value=merged,
            description="Autonomous daily discovery schedule for this partner tenant -- see "
            "app/gtm_os/orchestration/partner_daily_run.py for the full contract.",
        )
        db.add(param)
    db.commit()
    return merged


def run_partner_discovery_now(batch_id: int, tenant_id: int, source: str, target: int,
                              title_search: list[str] | None, pages: int = 2) -> None:
    """THE ONE REAL DISCOVERY-RUNNER, shared by the manual API route (routes/api.py) and the
    daily scheduler below -- moved here 2026-09-16 so both callers run the exact same, already-
    verified pipeline instead of two copies drifting apart. Runs entirely on its own db session
    (never share a session across threads/requests) -- every outcome (success, a real ICP
    budget block, or a real exception) is written onto the Batch row, since there's no HTTP
    response or caller left to return anything to once this runs in the background."""
    import logging

    from app.db.session import SessionLocal

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        batch = db.get(Batch, batch_id)
        icp_param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == PARTNER_ICP_PARAMETER_KEY).first()
        icp = dict(icp_param.value)

        if source == "jobo":
            from app.phases.partner_pipeline_jobo import run_tenant_discovery_jobo, verify_jobo_companies

            discovery_result = run_tenant_discovery_jobo(batch.id, db, tenant_id, icp, title_search, target=target, pages=pages)
            new_ids = [c.id for c in db.query(Company).filter(Company.batch_id == batch.id).all()]
            verify_result = verify_jobo_companies(
                db, tenant_id, new_ids, icp.get("revenue_min_usd"), icp.get("revenue_max_usd"),
                icp.get("employee_min"), icp.get("employee_max"), enrichment_tenant_id=ELEPHANT_EDGE_TENANT_ID,
            )
            batch.discovery_result = {"source": "jobo", "discovery": {k: v for k, v in discovery_result.items() if k != "kept"}, "verification": verify_result}
        else:
            from app.phases.apify_discovery import run_apify_discovery
            from app.phases.partner_pipeline import build_discovery_plan, enforce_icp_on_companies
            from app.apify_budget_guard import STATUS_ALLOWED as _APIFY_BUDGET_ALLOWED, check_apify_budget as _check_apify_budget

            plan = build_discovery_plan(db, ELEPHANT_EDGE_TENANT_ID, f"tenant_{tenant_id}", icp, target=target, title_search=title_search)
            budget = _check_apify_budget(db, ELEPHANT_EDGE_TENANT_ID, plan["estimated_max_cost_usd"])
            if budget["status"] != _APIFY_BUDGET_ALLOWED:
                batch.status = "blocked"
                batch.discovery_result = {"source": "apify", "status": "blocked", "reason": budget["reason"], "plan": plan}
                db.commit()
                return

            s = plan["search"]
            discovery_result = run_apify_discovery(
                batch.id, db, tenant_id, target=target, time_range=s["time_range"],
                location_search=s["location_search"], title_search=s["title_search"],
                employee_min=s["employee_min"], employee_max=s["employee_max"],
                industry_filter=s["industry_filter"], limit=s["limit"], budget_tenant_id=ELEPHANT_EDGE_TENANT_ID,
            )
            new_companies = db.query(Company).filter(Company.batch_id == batch.id).all()
            enforced = enforce_icp_on_companies(db, tenant_id, new_companies, icp, enrich_revenue=True, enrichment_tenant_id=ELEPHANT_EDGE_TENANT_ID)

            # REAL FIX, 2026-09-16: this used to stop at discovery -- Jobo's own path bundles
            # free leadership automatically, but the Apify path never found decision-makers at
            # all. find_decision_makers() is the SAME free-first cascade (Jobo leadership ->
            # Apify people-search resolve -> Google AI Overview) already proven live all session.
            # allow_paid_fallback=False here deliberately: this runs unattended and daily, so it
            # never silently reaches Deepline's paid search_contact (~$0.17-0.57/company) on its
            # own -- a company that misses every free layer just gets fewer contacts, not a
            # bigger bill nobody approved.
            from app.phases.decision_maker import find_decision_makers

            contacts_found = 0
            for company in enforced["kept"]:
                try:
                    new_contacts, _ = find_decision_makers(company, db, tenant_id, allow_paid_fallback=False)
                    contacts_found += len(new_contacts)
                except Exception:
                    logger.exception("decision-maker resolution failed for company_id=%s in batch %s", company.id, batch_id)

            batch.discovery_result = {
                "source": "apify", "plan": plan, "discovery": discovery_result,
                "kept": [c.name for c in enforced["kept"]], "dropped": enforced["dropped"],
                "revenue_enrichment": enforced["revenue_enrichment"], "contacts_found": contacts_found,
            }
        batch.status = "completed"
        db.commit()
    except Exception as e:  # noqa: BLE001 -- nothing left to raise TO; record it, never lose it
        db.rollback()
        batch = db.get(Batch, batch_id)
        if batch:
            batch.status = "failed"
            batch.discovery_error = f"{type(e).__name__}: {e}"
            db.commit()
        logger.exception("partner discovery run failed for batch %s", batch_id)
    finally:
        db.close()


def run_partner_engagement_mining_now(tenant_id: int, posts_per_day: int, commenters_per_post: int) -> None:
    """THE ENGAGEMENT-MINING RUNNER -- one call per day (see run_partner_daily_tick_for_tenant),
    own db session, same "never share a session across threads" discipline as
    run_partner_discovery_now. Guarantees a PER-POST floor rather than one shared harvest pool:
    calls the harvest actor once per selected post (each with its own commenters_per_post cap),
    instead of once for all posts combined -- see this module's own docstring and
    LINKEDIN_ENGAGEMENT_ACTOR_ID's comment for why a shared call lets one popular post starve
    the rest. Spend is bounded per call by check_apify_budget, same as every other paid path in
    this codebase -- a day with a low/exhausted Apify budget simply harvests fewer posts, never
    raises, never overspends."""
    import logging

    from app.db.session import SessionLocal

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        from app.apify_budget_guard import STATUS_ALLOWED, check_apify_budget
        from app.apify_client import LINKEDIN_ENGAGEMENT_COST_PER_ENGAGER_USD
        from app.gtm_os.intelligence.engagement_intent import select_relevant_post_urls
        from app.gtm_os.intelligence.sensing import sense_linkedin_post_engagement, sense_linkedin_post_search

        signals = sense_linkedin_post_search(db, tenant_id)
        post_urls = select_relevant_post_urls(signals, posts_per_day)

        harvested = 0
        for post_url in post_urls:
            budget = check_apify_budget(
                db, ELEPHANT_EDGE_TENANT_ID, commenters_per_post * LINKEDIN_ENGAGEMENT_COST_PER_ENGAGER_USD,
                operation="linkedin_engagement",
            )
            if budget["status"] != STATUS_ALLOWED:
                logger.info("partner engagement mining: budget exhausted for tenant_id=%s after %s/%s posts", tenant_id, harvested, len(post_urls))
                break
            sense_linkedin_post_engagement(db, tenant_id, [post_url], max_results=commenters_per_post, budget_tenant_id=ELEPHANT_EDGE_TENANT_ID)
            harvested += 1
    except Exception:  # noqa: BLE001 -- one objective's failure must never look like a crash with nothing recorded
        logger.exception("partner engagement mining failed for tenant_id=%s", tenant_id)
    finally:
        db.close()


def run_partner_daily_tick_for_tenant(db: Session, tenant_id: int, now: datetime | None = None) -> dict:
    """Checked once an hour, per partner tenant, by the scheduler (main.py). Fires the real
    discovery run(s) (each in its own background thread, same as the manual route -- this
    function itself must return quickly, never block the hourly tick on a multi-minute run)
    exactly once per UTC day, at the tenant's own configured hour.

    TWO INDEPENDENT OBJECTIVES, one shared trigger hour, 2026-09-22 -- firmographic discovery
    and engagement mining are gated and tracked separately (own enabled flag, own
    last_run_date), since a tenant could have one configured without the other, but both check
    the SAME schedule_hour_utc so a partner who wants "both, once a day" gets exactly that. Real
    correction after engagement mining was found firing every hour with no target: "it should
    run daily and with a target set."

    KNOWN LIMITATION, not silently hidden: a discovery run can occasionally hang (confirmed live
    2026-09-16, root cause not yet found) with no watchdog to kill it. A hung run from one day
    does not block the next day's trigger (each check is independent, gated only by
    last_run_date), but it does mean a stuck run's Batch row can sit "in_progress" indefinitely
    until someone notices. Real follow-up needed, not solved here."""
    import threading

    config = get_daily_run_config(db, tenant_id)
    if not config["enabled"] and not config["engagement_mining_enabled"]:
        return {"status": "skipped", "reason": "daily run disabled for this tenant"}

    now = now or datetime.now(timezone.utc)
    if now.hour != config["schedule_hour_utc"]:
        return {"status": "skipped", "reason": f"not this tenant's scheduled hour ({config['schedule_hour_utc']} UTC)"}

    today_str = now.strftime("%Y-%m-%d")
    result: dict = {"firmographic": None, "engagement": None}

    if config["enabled"] and config.get("last_run_date") != today_str:
        title_search = get_daily_run_title_search(db, tenant_id)
        if not title_search:
            result["firmographic"] = {"status": "skipped", "reason": "enabled, but this tenant's ICP has no title_search set -- fix the ICP, not this config"}
        else:
            batch = Batch(
                tenant_id=tenant_id, name=f"Partner daily run ({config['source']}) — {now:%Y-%m-%d %H:%M} UTC",
                source="partner_discovery", current_phase="signal_discovery", status="in_progress",
            )
            db.add(batch)
            db.commit()
            db.refresh(batch)

            # Mark last_run_date BEFORE the thread starts, not after it finishes -- a run that
            # hangs (see the known limitation above) must not cause the same tenant to fire again
            # every hour for the rest of the day while the earlier run is still stuck.
            set_daily_run_config(db, tenant_id, {"last_run_date": today_str})

            thread = threading.Thread(
                target=run_partner_discovery_now,
                args=(batch.id, tenant_id, config["source"], config["daily_target"], title_search, config["pages"]),
                daemon=True,
            )
            thread.start()
            result["firmographic"] = {"status": "started", "batch_id": batch.id}
    elif config["enabled"]:
        result["firmographic"] = {"status": "skipped", "reason": "already ran today"}

    if config["engagement_mining_enabled"] and config.get("engagement_last_run_date") != today_str:
        set_daily_run_config(db, tenant_id, {"engagement_last_run_date": today_str})
        thread = threading.Thread(
            target=run_partner_engagement_mining_now,
            args=(tenant_id, config["engagement_posts_per_day"], config["engagement_commenters_per_post"]),
            daemon=True,
        )
        thread.start()
        result["engagement"] = {"status": "started"}
    elif config["engagement_mining_enabled"]:
        result["engagement"] = {"status": "skipped", "reason": "already ran today"}

    if result["firmographic"] is None and result["engagement"] is None:
        return {"status": "skipped", "reason": "neither objective is enabled for this tenant"}
    return {"status": "started" if "started" in (result["firmographic"] or {}).get("status", "") or "started" in (result["engagement"] or {}).get("status", "") else "skipped", **result}
