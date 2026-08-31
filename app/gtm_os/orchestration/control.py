"""V2 Control Plane -- Phase 0 of the Autonomous GTM Flow build-out (see progress-log.md).

Same Parameter-backed storage pattern as every other V2 config module (business_context.py/
icp_config.py/pattern_detection_config.py/...). One config object, one authoritative `state`
field (`"running" | "paused" | "stopped"`) -- per the approved architecture's "use ONE
authoritative tenant-level control" instruction, this doubles as the kill switch: there is no
separate boolean flag that could disagree with `state`. `"paused"` and `"stopped"` are treated
identically by `check_can_run()` (both block new autonomous actions) -- the approved decisions
described their required behavior jointly without specifying a functional difference between
them, so none is invented here; the distinction exists only so a human can express "temporary"
vs. "deliberate/indefinite" intent in the UI later.

Limit fields (`max_sends_per_day`, `max_contacts_per_day`, cooldown days, discovery
cadence/target) default to `None` ("unconfigured" / no limit), never a guessed number -- these
are real business/operational decisions nobody has made yet, not technical constants. Only
`retry` gets real seeded defaults (max_attempts/backoff_minutes) -- a technical tuning knob, the
same category as e.g. pattern_detection_config.py's occurrence-threshold defaults, not a
business claim.

This module owns configuration and the run/halt check only. It does NOT implement daily-count
enforcement, cooldown tracking, or duplicate suppression yet -- there is nothing to count or
suppress until a phase that actually performs sends/contact-discovery exists (Phase 3/4/7).
Building that bookkeeping now, with nothing to call it, would be exactly the premature
abstraction this codebase avoids elsewhere; it belongs with the phase that produces the actions
being throttled, reusing this module's config as the limit source at that time.
"""
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.db.models import Parameter

GTM_OS_CONTROL_CONFIG_PARAMETER_KEY = "gtm_os_control_config"

VALID_STATES = ("running", "paused", "stopped")

DEFAULT_GTM_OS_CONTROL_CONFIG: dict = {
    "state": "running",
    "limits": {
        "max_sends_per_day": None,
        "max_contacts_per_day": None,
        "company_cooldown_days": None,
        "contact_cooldown_days": None,
        # V2-owned contact-discovery budget (Phase 3/4, 2026-08-21) -- same "dedicated, not
        # shared with V1" principle as discovery.daily_budget_usd above. None (unconfigured)
        # until an operator sets a real number.
        "contact_discovery_daily_budget_usd": None,
    },
    "business_hours": {
        "enabled": False,
        "start_hour": 9,
        "end_hour": 17,
        "timezone": "UTC",
    },
    "discovery": {
        "cadence_hours": None,
        "daily_target": None,
        # V2-owned discovery budget (Phase 1 correction, 2026-08-21) -- deliberately separate
        # from V1's daily_credit_budget_usd, not shared/aggregated with it. Reusing V1's number
        # meant two independent BudgetGuard instances could each spend up to that same figure
        # on the same calendar day with no cross-awareness; a dedicated V2 figure removes that
        # ambiguity without needing real cross-run spend tracking (explicitly out of scope for
        # now). None (unconfigured) until an operator sets a real number -- same "don't invent
        # a business figure" treatment as cadence_hours/daily_target above.
        "daily_budget_usd": None,
    },
    "retry": {
        "max_attempts": 3,
        "backoff_minutes": 15,
    },
    # V2 Phase 8 -- multi-contact outreach sequencing. None (unconfigured) until an operator
    # sets a real number -- same "don't invent a business figure" treatment as every other real
    # business decision in this config (discovery cadence/target, budgets). Unconfigured means
    # the outreach-sequencing stage never advances to a fallback contact; primary-contact
    # outreach (Phase 3-7) is completely unaffected either way. Explicitly NOT seeded with a
    # default interval -- "do not start with aggressive multi-contact sequencing" was given as
    # an explicit instruction, and the safest way to honor that is to require a human to choose
    # a real number before any fallback ever fires, not to guess a "conservative" one.
    "sequencing": {
        "fallback_delay_hours": None,
    },
    # Autonomous Sensing Phase S7 -- the hourly tick's per-cycle bound on how many
    # InvestigationObjective rows S3->S6 may act on this tick (S2 gap-identification itself is
    # free/local and always runs regardless). None (unconfigured) until an operator sets a real
    # number -- same "don't invent a business figure" discipline as every other cap in this
    # config. Unconfigured means S7 explicitly reports "configuration_required" and skips S3-S6
    # entirely for this tick, never treating None as unlimited.
    "investigation": {
        "max_objectives_per_tick": None,
    },
    # Autonomous Sensing Phase S7 -- gates the existing Strategy/Contact-discovery/Message-
    # generation/Send/outreach-sequencing stages to a slower, day-scale cadence, separate from
    # the hourly sensing tick (approved hybrid-cadence architecture: sensing runs every tick,
    # outbound-adjacent stages do not). None (unconfigured) means those stages never run
    # autonomously -- same discipline as discovery.cadence_hours; a human must set a real
    # interval before autonomous outbound execution begins. Opportunity/ICP-matching are
    # deliberately NOT gated by this -- they're pure, read-only-or-additive evidence evaluation
    # (see sweep.py's own docstring), safe and valuable to keep current every hourly tick, not an
    # "outbound" action.
    "outbound": {
        "cadence_hours": None,
    },
    # Autonomous Sensing Phase S8 -- real Apify budget guard (app/apify_budget_guard.py).
    # Deliberately kept as its own dedicated block, same "extend the existing budget-config style,
    # don't build a parallel system" pattern as discovery.daily_budget_usd/outbound.cadence_hours
    # above -- but a DIFFERENT number from discovery.daily_budget_usd (that one caps V2's own
    # company-discovery spend; this one caps Apify-backed autonomous SENSING spend, a distinct
    # real cost source with its own real usage API, see apify_client.get_monthly_usage()). Both
    # None (unconfigured) until an operator sets real numbers -- never invented, never treated as
    # unlimited. monthly_budget_usd is optional (Apify's own account-level cap is a real, separate
    # platform setting an operator can already set directly in Apify's own dashboard/limits API;
    # this field lets V2 additionally self-enforce a monthly ceiling from inside the app if wanted).
    "apify": {
        "daily_budget_usd": None,
        "monthly_budget_usd": None,
    },
    # Daily flow target (2026-08-24 design) -- "a flow" = one Opportunity that has reached a
    # real, usable MessageDraft (status "ready_for_review" or "approved" -- never "draft"/
    # "insufficient_context"), per the approved definition. Wraps the existing, UNCHANGED
    # run_gtm_intelligence_sweep() in an outer loop (see run_gtm_daily_flow_cycle in sweep.py)
    # rather than rewriting any stage into a serial per-company loop. Both None (unconfigured)
    # until an operator sets real numbers -- same "don't invent a business figure" discipline as
    # every other cap in this config. Unconfigured means the daily cycle keeps running exactly
    # one pass, exactly as it always has -- this block changes nothing until explicitly set.
    "flow_target": {
        "daily_flow_target": None,
        # Bounds how many times the outer loop may re-invoke the existing sweep while chasing
        # daily_flow_target -- a genuinely new safety ceiling (nothing existing bounds "how much
        # candidate work happens while chasing an unmet target", per the approved design's own
        # finding). Deliberately NOT given a seeded default the way retry.max_attempts is --
        # this is a real business/cost decision, not a technical tuning constant.
        "max_iterations_per_run": None,
    },
    # Company resolution (2026-08-24) -- makes company_resolution.py's allow_paid_enrichment a
    # real config toggle instead of a hardcoded Python default (confirmed live: the previous
    # hardcoded False meant nothing set anywhere could ever turn it on). Defaults False, same
    # opt-in discipline as every other paid-enrichment gate in this config -- an operator must
    # explicitly enable it. Enabling it activates ALL resolution tiers gated on
    # allow_paid_enrichment (the Deepline-free Apify-based profile lookup, the Deepline
    # search_contact profile lookup, and the existing Deepline name-guess fallback) -- each tier
    # still fails safe and reports its own real reason (including a real Deepline balance block)
    # rather than crashing or silently skipping.
    "company_resolution": {
        "allow_paid_enrichment": False,
    },
}


class ControlPlaneConfigError(ValueError):
    """Raised when control-plane configuration fails validation -- never silently coerced."""


class ControlPlaneHalted(Exception):
    """Raised by check_can_run() when the control plane's state is not "running". Callers decide
    what "halted" means for them (skip a scheduled tick, reject a manual trigger, etc.) -- this
    module never itself skips work, it only reports the state."""

    def __init__(self, state: str):
        self.state = state
        super().__init__(f"gtm_os control plane is '{state}' -- new autonomous actions are blocked")


def _validate_positive_int_or_none(value, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ControlPlaneConfigError(f"'{field_name}' must be a positive integer or null")


def _validate_non_negative_int_or_none(value, field_name: str) -> None:
    """Like _validate_positive_int_or_none but admits 0, for fields where 0 is a real, meaningful
    setting rather than a mistake.

    Only the cooldown fields use this. A cooldown of 0 means "no cooldown", which is a deliberate
    choice here: reaching several contacts at the SAME company on the same day is intended
    behaviour (multi-contact outreach), and null cannot express it -- send.py treats an
    unconfigured limit as "not configured" and refuses to send at all, precisely so an unset
    value is never silently read as unlimited. 0 says "configured, and the answer is none".

    max_sends_per_day deliberately still requires >= 1: a 0 there would mean "send nothing",
    which the control-plane state (paused/stopped) already expresses far more clearly.
    """
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControlPlaneConfigError(f"'{field_name}' must be a non-negative integer or null")


def _validate_positive_number_or_none(value, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ControlPlaneConfigError(f"'{field_name}' must be a positive number or null")


def _validate_control_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise ControlPlaneConfigError("Control config must be an object")

    state = config.get("state")
    if state not in VALID_STATES:
        raise ControlPlaneConfigError(f"'state' must be one of {VALID_STATES}")

    limits = config.get("limits")
    if not isinstance(limits, dict):
        raise ControlPlaneConfigError("'limits' must be an object")
    for field_name in ("max_sends_per_day", "max_contacts_per_day"):
        _validate_positive_int_or_none(limits.get(field_name), f"limits.{field_name}")
    for field_name in ("company_cooldown_days", "contact_cooldown_days"):
        _validate_non_negative_int_or_none(limits.get(field_name), f"limits.{field_name}")
    _validate_positive_number_or_none(limits.get("contact_discovery_daily_budget_usd"), "limits.contact_discovery_daily_budget_usd")

    business_hours = config.get("business_hours")
    if not isinstance(business_hours, dict):
        raise ControlPlaneConfigError("'business_hours' must be an object")
    if not isinstance(business_hours.get("enabled"), bool):
        raise ControlPlaneConfigError("'business_hours.enabled' must be a boolean")
    start_hour = business_hours.get("start_hour")
    end_hour = business_hours.get("end_hour")
    if not isinstance(start_hour, int) or isinstance(start_hour, bool) or not (0 <= start_hour <= 23):
        raise ControlPlaneConfigError("'business_hours.start_hour' must be an integer 0-23")
    if not isinstance(end_hour, int) or isinstance(end_hour, bool) or not (0 <= end_hour <= 23):
        raise ControlPlaneConfigError("'business_hours.end_hour' must be an integer 0-23")
    if not isinstance(business_hours.get("timezone"), str) or not business_hours.get("timezone"):
        raise ControlPlaneConfigError("'business_hours.timezone' must be a non-empty string")

    discovery = config.get("discovery")
    if not isinstance(discovery, dict):
        raise ControlPlaneConfigError("'discovery' must be an object")
    _validate_positive_int_or_none(discovery.get("cadence_hours"), "discovery.cadence_hours")
    _validate_positive_int_or_none(discovery.get("daily_target"), "discovery.daily_target")
    _validate_positive_number_or_none(discovery.get("daily_budget_usd"), "discovery.daily_budget_usd")

    retry = config.get("retry")
    if not isinstance(retry, dict):
        raise ControlPlaneConfigError("'retry' must be an object")
    max_attempts = retry.get("max_attempts")
    backoff_minutes = retry.get("backoff_minutes")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise ControlPlaneConfigError("'retry.max_attempts' must be a positive integer")
    if not isinstance(backoff_minutes, int) or isinstance(backoff_minutes, bool) or backoff_minutes < 1:
        raise ControlPlaneConfigError("'retry.backoff_minutes' must be a positive integer")

    investigation = config.get("investigation")
    if not isinstance(investigation, dict):
        raise ControlPlaneConfigError("'investigation' must be an object")
    _validate_positive_int_or_none(investigation.get("max_objectives_per_tick"), "investigation.max_objectives_per_tick")

    outbound = config.get("outbound")
    if not isinstance(outbound, dict):
        raise ControlPlaneConfigError("'outbound' must be an object")
    _validate_positive_int_or_none(outbound.get("cadence_hours"), "outbound.cadence_hours")

    apify = config.get("apify")
    if not isinstance(apify, dict):
        raise ControlPlaneConfigError("'apify' must be an object")
    _validate_positive_number_or_none(apify.get("daily_budget_usd"), "apify.daily_budget_usd")
    _validate_positive_number_or_none(apify.get("monthly_budget_usd"), "apify.monthly_budget_usd")

    sequencing = config.get("sequencing")
    if not isinstance(sequencing, dict):
        raise ControlPlaneConfigError("'sequencing' must be an object")
    _validate_positive_int_or_none(sequencing.get("fallback_delay_hours"), "sequencing.fallback_delay_hours")

    flow_target = config.get("flow_target")
    if not isinstance(flow_target, dict):
        raise ControlPlaneConfigError("'flow_target' must be an object")
    _validate_positive_int_or_none(flow_target.get("daily_flow_target"), "flow_target.daily_flow_target")
    _validate_positive_int_or_none(flow_target.get("max_iterations_per_run"), "flow_target.max_iterations_per_run")

    company_resolution = config.get("company_resolution")
    if not isinstance(company_resolution, dict):
        raise ControlPlaneConfigError("'company_resolution' must be an object")
    if not isinstance(company_resolution.get("allow_paid_enrichment"), bool):
        raise ControlPlaneConfigError("'company_resolution.allow_paid_enrichment' must be a boolean")


def get_control_config(db: Session, tenant_id: int) -> dict:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == GTM_OS_CONTROL_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict):
        return param.value
    return DEFAULT_GTM_OS_CONTROL_CONFIG


def set_control_config(db: Session, tenant_id: int, config: dict) -> None:
    _validate_control_config(config)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == GTM_OS_CONTROL_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = config
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=GTM_OS_CONTROL_CONFIG_PARAMETER_KEY,
            value=config,
            description="V2 GTM-OS control plane (state, safety limits, business hours, discovery "
            "cadence, retry policy) -- see app/gtm_os/orchestration/control.py",
        )
        db.add(param)
    db.commit()


def check_can_run(db: Session, tenant_id: int) -> None:
    """The single gate every V2 autonomous stage/scheduler entry point must call before starting
    NEW work. Raises ControlPlaneHalted when state != "running". Never blocks an already-started
    atomic operation (per the approved "an in-flight operation may finish" rule) -- callers only
    ever call this before beginning a new unit of work, never mid-operation."""
    state = get_control_config(db, tenant_id).get("state", "running")
    if state != "running":
        raise ControlPlaneHalted(state)


def is_within_business_hours(config: dict, now: datetime | None = None) -> bool:
    """Pure check against the configured business-hours window. Returns True when the window is
    disabled (no restriction configured).

    V2 Phase 7 fix: now genuinely timezone-aware, per the explicit Phase 0 follow-up
    requirement ("It MUST be timezone-correct before Phase 7 uses it for real sends. Do not
    silently use server-local now.hour for production send decisions."). `now` (UTC, naive or
    aware) is converted into `business_hours.timezone` (a real IANA zone name, e.g.
    "America/New_York") via the stdlib `zoneinfo` -- no new dependency -- before extracting the
    hour to compare. An invalid/unknown timezone string fails safe to UTC rather than raising
    and blocking the whole send stage on a bad config value."""
    business_hours = config.get("business_hours", {})
    if not business_hours.get("enabled"):
        return True
    now = now or datetime.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    tz_name = business_hours.get("timezone") or "UTC"
    try:
        local_now = now.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        local_now = now.astimezone(ZoneInfo("UTC"))
    start_hour = business_hours.get("start_hour", 0)
    end_hour = business_hours.get("end_hour", 23)
    return start_hour <= local_now.hour < end_hour


def get_control_status(db: Session, tenant_id: int) -> dict:
    """Current run status/failure state -- reuses GtmIntelligenceRun as-is (no new run-tracking
    table), per the approved Phase 0 reuse instruction."""
    from app.gtm_os.orchestration.sweep import GtmIntelligenceRun

    config = get_control_config(db, tenant_id)
    latest_run = (
        db.query(GtmIntelligenceRun)
        .filter(GtmIntelligenceRun.tenant_id == tenant_id)
        .order_by(GtmIntelligenceRun.started_at.desc())
        .first()
    )
    return {
        "state": config.get("state", "running"),
        "latest_run": None
        if latest_run is None
        else {
            "id": latest_run.id,
            "status": latest_run.status,
            "error_summary": latest_run.error_summary,
            "started_at": latest_run.started_at,
            "completed_at": latest_run.completed_at,
        },
    }


GTM_INTELLIGENCE_SCHEDULE_PARAMETER_KEY = "gtm_intelligence_schedule_utc"

# V1's own daily autonomous cycle fires at 9:00 UTC (see autonomous_orchestrator.py's
# DEFAULT_SCHEDULE_HOUR_UTC/MINUTE_UTC) -- this cycle defaults to an hour later so the two never
# compete for the same DB/Apify budget window, not because the hour itself is meaningful.
DEFAULT_INTELLIGENCE_SCHEDULE_HOUR_UTC = 10
DEFAULT_INTELLIGENCE_SCHEDULE_MINUTE_UTC = 0


def get_intelligence_schedule_utc(db: Session, tenant_id: int) -> tuple[int, int]:
    """Fixed wall-clock time (UTC) the GTM intelligence sensing cycle fires at -- one run per
    day, same CronTrigger-at-a-fixed-time pattern as V1's autonomous_schedule_utc (see that
    module's own docstring for why a fixed time, not an interval timer, is used: an interval's
    countdown restarts on every deploy, so its actual fire time silently drifts). Replaces the
    old hardcoded IntervalTrigger(minutes=60) -- this cycle used to run hourly with no way to
    change that without a code change/redeploy."""
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == GTM_INTELLIGENCE_SCHEDULE_PARAMETER_KEY)
        .first()
    )
    if param and param.value and "hour" in param.value and "minute" in param.value:
        return int(param.value["hour"]), int(param.value["minute"])
    return DEFAULT_INTELLIGENCE_SCHEDULE_HOUR_UTC, DEFAULT_INTELLIGENCE_SCHEDULE_MINUTE_UTC


def set_intelligence_schedule_utc(db: Session, tenant_id: int, hour: int, minute: int) -> None:
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise ControlPlaneConfigError("hour must be 0-23, minute must be 0-59")
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == GTM_INTELLIGENCE_SCHEDULE_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = {"hour": hour, "minute": minute}
    else:
        param = Parameter(
            tenant_id=tenant_id, key=GTM_INTELLIGENCE_SCHEDULE_PARAMETER_KEY, value={"hour": hour, "minute": minute},
            description="Fixed daily UTC time the GTM intelligence sensing cycle fires at",
        )
        db.add(param)
    db.commit()
