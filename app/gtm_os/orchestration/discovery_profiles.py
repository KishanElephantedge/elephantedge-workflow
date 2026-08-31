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

from sqlalchemy.orm import Session

from app.db.models import Parameter
from app.phases.apify_discovery import (
    APIFY_EMPLOYEE_MAX,
    APIFY_EMPLOYEE_MIN,
    APIFY_INDUSTRY_FILTER,
    APIFY_TITLE_SEARCH,
)

DISCOVERY_PROFILES_PARAMETER_KEY = "v2_discovery_profiles"


class DiscoveryProfileError(ValueError):
    """Raised when a discovery profile fails validation -- never silently coerced."""


DEFAULT_DISCOVERY_PROFILES: list[dict] = [
    {
        "id": "execution",
        "offering_names": ["Execution", "Consulting"],
        "enabled": True,
        # V1's real, proven filters, unchanged -- this is the one profile with a track record.
        "title_search": list(APIFY_TITLE_SEARCH),
        "employee_min": APIFY_EMPLOYEE_MIN,
        "employee_max": APIFY_EMPLOYEE_MAX,
        "industry_filter": list(APIFY_INDUSTRY_FILTER),
    },
    {
        "id": "sales_os",
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
        return param.value
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
