"""Per-offering job-search profiles for V2 sensing.

THE PROBLEM THIS FIXES. V2's job sensing reuses V1's discovery filters verbatim
(apify_discovery.APIFY_TITLE_SEARCH / APIFY_EMPLOYEE_MIN / APIFY_EMPLOYEE_MAX / APIFY_INDUSTRY_FILTER).
Those were written when Elephant Edge sold exactly ONE thing, so they encode exactly one buyer:
"a 25-50 person software company hiring a salesperson" -- the buying signal for Execution
(Fractional VP Sales). Elephant Edge now sells six offerings, and the other five produce
completely different signals, or none in that headcount band at all:
  - Sales OS buyers are building GTM infrastructure (RevOps/sales-ops/GTM-engineer reqs), and
    are routinely larger than 50 people.
  - Workshop buyers are founder-led companies making a FIRST sales hire, routinely smaller
    than 25 people.
  - Sales Products targets mid-market/enterprise -- entirely outside a 25-50 band.
Consequence: five of six offerings had no discovery at all. Not under-performing -- structurally
invisible.

WHAT THIS IS NOT. It does not change how a company is qualified, matched to an ICP, or matched to
an offering -- offering_config.py and icp_matching.py own those and are untouched. This only
decides which job searches are RUN, i.e. what the funnel is allowed to see in the first place.

COST. Each profile is one bounded Apify search, priced exactly like the single search V2 runs
today (~$0.135 at limit=25). Profiles are individually enable-able precisely so cost scales with
deliberate choices rather than with how many offerings happen to exist -- and each one is
budget-checked separately by the caller, so a profile is skipped rather than overrunning.

THE DEFAULTS BELOW NEED A HUMAN REVIEW. The Execution profile is V1's real, proven filter set,
carried over unchanged. The other two are derived from each offering's own stated
target_company_characteristics in offering_config.py, which is real, but the specific job titles
are a reasoned starting point, not validated targeting -- they should be reviewed by whoever owns
GTM before being treated as settled.
"""

import logging

from sqlalchemy.orm import Session

from app.db.models import Parameter
from app.phases.apify_discovery import (
    APIFY_EMPLOYEE_MAX,
    APIFY_EMPLOYEE_MIN,
    APIFY_INDUSTRY_FILTER,
    APIFY_TITLE_SEARCH,
)

logger = logging.getLogger(__name__)

DISCOVERY_PROFILES_PARAMETER_KEY = "v2_discovery_profiles"

# V1 moved its own job search off "24h" on 2026-08-10 after confirming live that a 24-hour window
# "produced only 1 real posting on an otherwise-normal day, well short of the 5-10/day target".
# V2's sensing was still on 24h and reproduced exactly that: 8 signals across three searches in
# run 116. Cost stays bounded either way because Apify bills per RESULT RETURNED and each search
# is capped by its own limit -- a wider window fills that cap with real postings instead of
# leaving it mostly empty. Re-fetched postings already seen are dropped by sensing's own
# source_ref guard, so a wider window never creates duplicate signals.
DEFAULT_TIME_RANGE = "7d"


class DiscoveryProfileError(ValueError):
    """Raised when a discovery profile fails validation -- never silently coerced."""


# WHY HEADCOUNT BANDS ARE DERIVED, NOT WRITTEN DOWN.
#
# A profile decides which companies discovery is allowed to SEE; icp_matching.py then decides
# which of them match, and it decides on REVENUE. So a profile whose headcount band does not
# overlap the revenue band of the ICP it feeds cannot ever produce a match -- every company it
# finds is filtered out downstream, and the offering looks like it has no demand when really it
# was never shown any.
#
# That is what the hardcoded bands did. Measured 2026-09-04 against the live ICP config:
#
#   profile    feeds        searched   = revenue    ICP needs           result
#   execution  Execution    25-50      $2.0-4M      icp_3 251-628 emp   NO OVERLAP - impossible
#   workshop   Workshop     5-25       $0.4-2M      icp_1  37-125 emp   NO OVERLAP - impossible
#   sales_os   Sales OS     25-200     $2.0-16M     icp_2 125-251 emp   partial
#
# Two of three offerings were structurally unreachable, and the pipeline had never once searched
# a company large enough for Execution or Sales OS. The bands are now computed from each ICP's own
# revenue_min/max divided by REVENUE_PER_EMPLOYEE_USD -- the same constant icp_matching.py uses to
# estimate revenue from headcount -- so the two can no longer drift apart. Change an ICP's revenue
# band and discovery follows it automatically.
def headcount_band_for_icp(icp: dict) -> tuple[int, int]:
    """The employee range whose ESTIMATED revenue lands inside this ICP's revenue band --
    narrowed further by sales_team_size_max when the ICP configures one (2026-09-07 fix).

    Confirmed live against batch 103: this band was derived from revenue ALONE, so icp_1's
    37-126 range let discovery pay for and decision-maker-enrich 8 real companies (69-126
    employees) that then correctly failed icp_matching.py's OWN sales_team_size_max=10 check
    every time -- at the tenant-median 19.85% sales-headcount rate, that cap only holds up to
    ~50 employees, not 126. Real, wasted spend on companies that could never pass, not a
    matching bug. This does NOT touch sales_team_size_max itself (that's the ICP's own
    business rule, left exactly as configured) -- it only stops discovery from searching past
    the point that rule can ever satisfy, using the SAME constant icp_matching.py's own
    fallback already uses, so the two can never drift back apart."""
    from app.gtm_os.icp.icp_matching import (
        REVENUE_PER_EMPLOYEE_P25_USD,
        REVENUE_PER_EMPLOYEE_P75_USD,
        SALES_HEADCOUNT_PERCENT_P25,
    )

    # p75 for the floor, p25 for the ceiling -- NOT the median for both (fixed 2026-09-10). The
    # median assumed every company in an ICP's revenue band earns exactly the median per head, so
    # icp_1's $3-10M band searched only 37-50 employees when its real span is 23-84. Measured that
    # day: a full discovery run across all three profiles surfaced 5 qualifying companies against a
    # target of 25, having structurally never looked at most of the band. See the constants' own
    # comment in icp_matching.py for why a search range and a point estimate need different figures.
    lo = int(icp["revenue_min_usd"] // REVENUE_PER_EMPLOYEE_P75_USD)
    hi = -(-int(icp["revenue_max_usd"]) // REVENUE_PER_EMPLOYEE_P25_USD)  # ceiling
    sales_team_size_max = icp.get("sales_team_size_max")
    if sales_team_size_max is not None:
        sales_hi = int(sales_team_size_max / (SALES_HEADCOUNT_PERCENT_P25 / 100))
        hi = min(hi, sales_hi)
    # employee_max (2026-09-09, icp_3 only so far): a direct, explicit size ceiling independent
    # of the revenue-derived one -- same "take the more restrictive constraint" rule as
    # sales_team_size_max above, so discovery never pays to find companies icp_matching.py's own
    # employee_max check would reject anyway.
    employee_max = icp.get("employee_max")
    if employee_max is not None:
        hi = min(hi, employee_max)
    return max(lo, 1), hi


def apply_icp_headcount_bands(db: Session, tenant_id: int, profiles: list[dict]) -> list[dict]:
    """Rewrite each profile's employee_min/max from the ICP its offerings map to.

    A profile feeding several offerings takes the union of their ICP bands. Profiles whose
    offerings resolve to no ICP (e.g. Sales Products) are left exactly as configured -- this
    corrects bands, it never invents targeting for an offering that has none.
    """
    from app.gtm_os.icp.icp_offering_matching import get_icps_offerings_overview
    from app.gtm_os.opportunity.offering_config import get_offering_config

    icps = {i["id"]: i for i in get_icps_offerings_overview(db, tenant_id).get("icps", [])}
    offering_icps = {o["name"]: (o.get("applicable_icps") or []) for o in get_offering_config(db, tenant_id)}

    out = []
    for profile in profiles:
        bands = [
            headcount_band_for_icp(icps[icp_id])
            for name in profile.get("offering_names", [])
            for icp_id in offering_icps.get(name, [])
            if icp_id in icps
        ]
        if not bands:
            out.append(profile)
            continue
        updated = dict(profile)
        updated["employee_min"] = min(b[0] for b in bands)
        updated["employee_max"] = max(b[1] for b in bands)
        out.append(updated)
    return out


# TITLES ARE DERIVED FROM THE ICP TRIGGER, FOR THE SAME REASON THE BANDS ARE.
#
# Each ICP declares trigger_hiring_roles plus a trigger_mode of requires_presence or
# requires_absence. That IS the job-title specification, so searching a different set means
# paying for results the matcher is guaranteed to reject:
#
#   icp_1 requires the ABSENCE of gtm/head_of_sales hiring -- yet V1's shared title list searches
#         "Head of Sales", "VP Sales", "CRO" and "GTM Engineer" alongside rep titles. Every company
#         those terms find fails icp_1 by construction. Confirmed live 2026-09-04: Opal Security
#         (hiring a GTM Engineer) and Simple AI (hiring a Head of Sales) were both discovered and
#         both discarded for exactly this reason. Apify bills per RESULT RETURNED, so those were
#         paid for.
#   icp_2 and icp_3 require the PRESENCE of those same roles -- and nothing searched for them.
#
# Deriving titles from the trigger makes each search return companies that can actually pass the
# ICP it feeds, which raises precision and lowers cost at the same time.
ROLE_TITLES: dict[str, list[str]] = {
    "gtm": [
        "GTM Engineer", "Head of GTM", "RevOps", "Revenue Operations", "Sales Operations",
        "Forward Deployed Engineer", "Sales Enablement", "Sales Systems",
    ],
    "head_of_sales": [
        "Head of Sales", "VP Sales", "VP of Sales", "Director of Sales", "Sales Director",
        "CRO", "Chief Revenue Officer",
    ],
}

# What a company hires when it is adding CAPACITY rather than LEADERSHIP -- the observable
# signal for a requires_absence ICP. Strictly individual-contributor: any leadership or GTM title
# here would search for the very thing the ICP requires to be absent.
REP_LEVEL_TITLES: list[str] = [
    "SDR", "BDR", "AE", "Sales Development Representative",
    "Business Development Representative", "Business Development Manager",
    "Account Executive", "Sales Executive", "Sales Representative",
]


def titles_for_icp(icp: dict) -> list[str]:
    """The job titles whose presence (or absence) this ICP is actually defined by."""
    roles = icp.get("trigger_hiring_roles") or []
    if icp.get("trigger_mode") == "requires_absence":
        return list(REP_LEVEL_TITLES)
    titles: list[str] = []
    for role in roles:
        for t in ROLE_TITLES.get(role, []):
            if t not in titles:
                titles.append(t)
    return titles or list(REP_LEVEL_TITLES)


def build_icp_discovery_profiles(db: Session, tenant_id: int) -> list[dict]:
    """One profile PER ICP -- not per offering.

    Discovery is gated by ICP, and several offerings share one ICP (icp_1 feeds Consulting,
    Workshop and Digital Playbook). A profile per offering therefore runs the same search two or
    three times and pays Apify for the same companies repeatedly, while an offering with no
    profile of its own is invisible. One search per ICP covers every offering that ICP feeds,
    exactly once.

    An ICP with no offering mapped to it is skipped: there would be nothing to sell the result.
    """
    from app.gtm_os.icp.icp_offering_matching import get_icps_offerings_overview
    from app.gtm_os.opportunity.offering_config import get_offering_config

    by_icp: dict[str, list[str]] = {}
    for offering in get_offering_config(db, tenant_id):
        for icp_id in (offering.get("applicable_icps") or []):
            by_icp.setdefault(icp_id, []).append(offering["name"])

    profiles = []
    for icp in get_icps_offerings_overview(db, tenant_id).get("icps", []):
        offerings = by_icp.get(icp["id"])
        if not offerings:
            continue
        lo, hi = headcount_band_for_icp(icp)
        profiles.append({
            "id": icp["id"],
            "time_range": DEFAULT_TIME_RANGE,
            "offering_names": offerings,
            "enabled": True,
            "title_search": titles_for_icp(icp),
            "employee_min": lo,
            "employee_max": hi,
            "industry_filter": list(APIFY_INDUSTRY_FILTER),
        })
    return profiles


# Elephant Edge's own tenant id. Declared locally (same pattern as content_business_context.py and
# meeting_intelligence.py) rather than imported from routes/api.py, which would invert the
# dependency direction between orchestration and the route layer.
ELEPHANT_EDGE_TENANT_ID = 2


def _has_own_icp_config(db: Session, tenant_id: int) -> bool:
    """Whether this tenant has a REAL ICP config row, as opposed to inheriting the shared default.

    Deliberately a direct Parameter check and not get_icp_config(), which silently returns
    Elephant Edge's DEFAULT_ICP_CONFIG for any tenant without one -- the exact behaviour that made
    every partner look like it had Elephant Edge's ICPs. Same "check for the real row, don't trust
    the falling-back reader" pattern content topics.py already needed for this reason.
    """
    from app.gtm_os.icp.icp_config import ICP_CONFIG_PARAMETER_KEY

    row = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id, Parameter.key == ICP_CONFIG_PARAMETER_KEY)
        .first()
    )
    return bool(row and row.value)


def build_partner_discovery_profiles(db: Session, tenant_id: int) -> list[dict]:
    """One discovery profile built from a PARTNER tenant's own stated ICP (the `partner_icp`
    Parameter they edit in their dashboard), so the same daily engine that serves Elephant Edge
    can serve a partner without a second pipeline.

    Reuses partner_pipeline's existing, already-proven mapping primitives rather than
    reimplementing them: map_icp_to_linkedin_industries (free-text industry -> the actor's real
    LinkedIn taxonomy), _geographies_to_locations, and headcount_band_for_partner_icp. Those
    encode real, expensive lessons about what this actor actually accepts; a second copy here
    would drift from them silently.

    Returns [] -- never a default profile -- when the partner's ICP cannot be sized into a real
    search. A partner with no revenue band and no headcount band (Jeff Ballard, Jeff Platt as of
    2026-09-11) genuinely cannot be searched for on firmographics alone, and inventing a band
    would spend their budget on companies nobody chose.
    """
    from app.phases.partner_icp import get_partner_icp
    from app.phases.partner_pipeline import (
        _geographies_to_locations,
        headcount_band_for_partner_icp,
        map_icp_to_linkedin_industries,
    )

    icp = get_partner_icp(db, tenant_id)
    if not icp:
        return []

    emp_min, emp_max, _ = headcount_band_for_partner_icp(icp)
    if emp_min is None and emp_max is None:
        logger.warning(
            "tenant %s has a partner ICP with neither a headcount nor a revenue band -- "
            "cannot build a discovery profile from it", tenant_id,
        )
        return []

    # The actor needs both bounds. A one-sided ICP ("under 100 employees", "$20M+") keeps its own
    # stated bound and takes the shared APIFY_* default for the side it left open -- a documented
    # floor/ceiling, not a guess at what the partner meant.
    emp_min = APIFY_EMPLOYEE_MIN if emp_min is None else max(1, int(emp_min))
    emp_max = APIFY_EMPLOYEE_MAX if emp_max is None else int(emp_max)
    if emp_min > emp_max:
        logger.warning("tenant %s ICP produced employee_min %s > employee_max %s", tenant_id, emp_min, emp_max)
        return []

    # The LLM industry mapping is billed to Elephant Edge, not the partner -- our key, our cost,
    # same rule run_partner_discovery already follows for this call.
    industries, _rejected = map_icp_to_linkedin_industries(db, ELEPHANT_EDGE_TENANT_ID, icp)
    locations = _geographies_to_locations(icp)

    profile = {
        "id": f"partner_icp_{tenant_id}",
        "time_range": DEFAULT_TIME_RANGE,
        "offering_names": [],
        "enabled": True,
        # The hiring signal that actually indicates THIS partner's buyer, when their ICP states
        # one. Defaulting every partner to APIFY_TITLE_SEARCH encodes Elephant Edge's own
        # hypothesis -- "a company hiring sellers has a sales problem" -- as if it were universal,
        # and for most partners it simply is not. Amy Phillips sells to manufacturers with no CMO,
        # so her buyer shows up hiring a marketing manager, not an SDR; Jeff Ballard fixes
        # underperforming partner programs, so his shows up hiring a channel/alliances role.
        # Searching either of them on sales titles finds plausible-looking companies that have
        # nothing to do with what they sell. See build_discovery_plan's own note on the same point.
        "title_search": list(icp.get("title_search") or APIFY_TITLE_SEARCH),
        "employee_min": emp_min,
        "employee_max": emp_max,
        "industry_filter": industries or list(APIFY_INDUSTRY_FILTER),
    }
    if locations:
        profile["location_search"] = locations
    return [profile]


DEFAULT_DISCOVERY_PROFILES: list[dict] = [
    {
        "id": "consulting",
        "time_range": DEFAULT_TIME_RANGE,
        "offering_names": ["Consulting"],
        "enabled": True,
        # V1's proven title set -- this is the profile that actually produced the 2026-09-04 batch
        # of 10 opportunities, all of which matched icp_1.
        "title_search": list(APIFY_TITLE_SEARCH),
        "employee_min": APIFY_EMPLOYEE_MIN,
        "employee_max": APIFY_EMPLOYEE_MAX,
        "industry_filter": list(APIFY_INDUSTRY_FILTER),
    },
    {
        "id": "execution",
        "time_range": DEFAULT_TIME_RANGE,
        # Split from Consulting on 2026-09-04: Execution is icp_3 ($20-50M) and Consulting is
        # icp_1 ($3-10M). One profile could not serve both -- the union band would have been
        # 37-628 employees, which is not a target, it is the absence of one.
        "offering_names": ["Execution"],
        "enabled": True,
        # V1's real, proven filters, unchanged -- this is the one profile with a track record.
        "title_search": list(APIFY_TITLE_SEARCH),
        "employee_min": APIFY_EMPLOYEE_MIN,
        "employee_max": APIFY_EMPLOYEE_MAX,
        "industry_filter": list(APIFY_INDUSTRY_FILTER),
    },
    {
        "id": "sales_os",
        "time_range": DEFAULT_TIME_RANGE,
        "offering_names": ["Sales OS"],
        "enabled": True,
        # Sales OS is "agentic layers for sales" -- the buying signal is a company BUILDING GTM
        # infrastructure, which shows up as ops/engineering reqs rather than quota-carrying ones.
        "title_search": [
            "RevOps", "Revenue Operations", "Sales Operations", "GTM Engineer",
            "Marketing Operations", "Sales Enablement", "Sales Systems", "CRM Administrator",
        ],
        # Deliberately wider than Execution's 25-50: a company big enough to need sales
        # infrastructure is routinely past 50 people, which the V1 band silently excluded.
        "employee_min": 25,
        "employee_max": 200,
        "industry_filter": list(APIFY_INDUSTRY_FILTER),
    },
    {
        "id": "workshop",
        "time_range": DEFAULT_TIME_RANGE,
        "offering_names": ["Workshop"],
        "enabled": True,
        # Workshop teaches FOUNDERS how sales should work -- the signal is a founder-led company
        # making an early/first sales hire, which sits BELOW V1's 25-person floor.
        "title_search": [
            "Sales Executive", "Business Development", "Account Executive",
            "Sales Representative", "Head of Sales",
        ],
        "employee_min": 5,
        "employee_max": 25,
        "industry_filter": list(APIFY_INDUSTRY_FILTER),
    },
]


def _validate(profiles: list[dict]) -> None:
    if not isinstance(profiles, list):
        raise DiscoveryProfileError("discovery profiles must be a list")
    seen: set[str] = set()
    for i, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            raise DiscoveryProfileError(f"profile at index {i} must be an object")
        pid = profile.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise DiscoveryProfileError(f"profile at index {i} has an empty or missing id")
        if pid in seen:
            raise DiscoveryProfileError(f"duplicate profile id: {pid!r}")
        seen.add(pid)
        titles = profile.get("title_search")
        if not isinstance(titles, list) or not titles:
            raise DiscoveryProfileError(f"profile {pid!r} needs a non-empty title_search")
        for bound in ("employee_min", "employee_max"):
            value = profile.get(bound)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise DiscoveryProfileError(f"profile {pid!r} field {bound!r} must be a non-negative integer")
        if profile["employee_min"] > profile["employee_max"]:
            raise DiscoveryProfileError(f"profile {pid!r} has employee_min > employee_max")


def get_discovery_profiles(db: Session, tenant_id: int) -> list[dict]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id, Parameter.key == DISCOVERY_PROFILES_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, list):
        # An explicit human override always wins -- only its bands are corrected.
        try:
            return apply_icp_headcount_bands(db, tenant_id, param.value)
        except Exception:  # noqa: BLE001 -- a config read failure must not stop discovery
            logger.warning("could not derive ICP headcount bands; using the stored profiles as-is", exc_info=True)
            return param.value
    # ONLY for a tenant whose ICP config is genuinely its own. get_icp_config() and
    # get_offering_config() both fall back to Elephant Edge's DEFAULT_* seeds for ANY tenant that
    # has no row of its own -- and as of 2026-09-11 no tenant has one, Elephant Edge included. So
    # calling build_icp_discovery_profiles() unconditionally built Elephant Edge's three ICP
    # profiles for every partner tenant: confirmed live that tenants 5/6/9/10/11/12 and even an
    # unconfigured tenant 3 all returned the identical icp_1/icp_2/icp_3 profiles, ignoring the
    # real partner_icp each of them has on file. The defaults ARE Elephant Edge's real config
    # (icp_config.py transcribes them from its own reference material), so they stay correct for
    # tenant 2 and are wrong for everyone else.
    if tenant_id == ELEPHANT_EDGE_TENANT_ID or _has_own_icp_config(db, tenant_id):
        try:
            profiles = build_icp_discovery_profiles(db, tenant_id)
            if profiles:
                return profiles
            logger.warning("no ICP has an offering mapped to it; falling back to the static profiles")
        except Exception:  # noqa: BLE001
            logger.warning("could not build ICP discovery profiles; falling back to the static ones", exc_info=True)

    # A partner tenant has no gtm_os_icp_config and no offerings, so the branch above always
    # yields nothing for them -- their ICP lives in `partner_icp` instead.
    try:
        partner_profiles = build_partner_discovery_profiles(db, tenant_id)
        if partner_profiles:
            return partner_profiles
    except Exception:  # noqa: BLE001 -- same rule as above: a config read failure must not stop discovery
        logger.warning("could not build partner discovery profiles for tenant %s", tenant_id, exc_info=True)

    # DEFAULT_DISCOVERY_PROFILES is Elephant Edge's OWN static profile set (its titles, its
    # industries, its bands), so returning it for any other tenant hands that tenant Elephant
    # Edge's ICP under their own name -- the search would look successful and produce companies
    # nobody chose for them. Real risk as of 2026-09-11: every partner tenant reaches this line,
    # because none has an offering-mapped ICP. An empty list makes the caller stop instead, which
    # is the honest outcome for "we do not know who this tenant sells to".
    if tenant_id != ELEPHANT_EDGE_TENANT_ID:
        logger.warning(
            "tenant %s has no usable discovery profile (no ICP config, no partner_icp) -- "
            "returning none rather than defaulting to Elephant Edge's own profiles", tenant_id,
        )
        return []
    return DEFAULT_DISCOVERY_PROFILES


def get_enabled_discovery_profiles(db: Session, tenant_id: int) -> list[dict]:
    return [p for p in get_discovery_profiles(db, tenant_id) if p.get("enabled", True)]


def set_discovery_profiles(db: Session, tenant_id: int, profiles: list[dict]) -> list[dict]:
    _validate(profiles)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id, Parameter.key == DISCOVERY_PROFILES_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = profiles
    else:
        db.add(Parameter(
            tenant_id=tenant_id, key=DISCOVERY_PROFILES_PARAMETER_KEY, value=profiles,
            description="V2 per-offering job-search profiles (titles + headcount band per offering)",
        ))
    db.commit()
    return get_discovery_profiles(db, tenant_id)
