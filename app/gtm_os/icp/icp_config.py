"""ICP configuration -- Batch 8 Part (config structure). Same Parameter-backed storage pattern
as offering_config.py/opportunity_config.py -- own key, own shape, no new schema/CMS.

UNLIKE offering_config.py (deliberately empty pending lead confirmation), DEFAULT_ICP_CONFIG
below IS seeded with real content -- because, per gpt.txt's own explicit instruction ("where the
lead has given an explicit rule, represent it"), the lead's reference material (the PDF + the
gpt.txt planning discussion) already contains three concrete, named ICPs with real revenue
ranges and real hiring triggers:

    ICP 1 -- $3-10M, <=5 sales reps, no dedicated GTM/head-of-sales hiring -- "stuck in sales"
    ICP 2 -- $10-20M, hiring GTM engineers / forward-deployed engineers -- "upgrading with AI"
    ICP 3 -- $20-50M, hiring VP/Head of Sales (wants fractional, not full-time) -- "needs
             fractional leadership"

These are transcribed directly from that material, not invented. Still fully editable/replaceable
via set_icp_config() the moment the lead formally confirms or changes them -- this is configuration,
not hardcoded business logic, per gpt.txt's own "make it configuration, not a hardcoded
assumption" instruction."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

ICP_CONFIG_PARAMETER_KEY = "gtm_os_icp_config"

# Real hiring_signal_role vocabulary, per app/db/models.py's own Company.hiring_signal_role
# comment -- reused verbatim, not re-invented: "head_of_sales | sdr | ae | marketing | gtm".
HIRING_SIGNAL_ROLES = {"head_of_sales", "sdr", "ae", "marketing", "gtm"}

# Transcribed directly from the lead's reference material (see module docstring) -- not invented.
# trigger_mode: "requires_presence" (hiring_signal_role must be one of trigger_hiring_roles) or
# "requires_absence" (hiring_signal_role must NOT be one of trigger_hiring_roles).
DEFAULT_ICP_CONFIG: list[dict] = [
    {
        "id": "icp_1",
        "name": "Stuck in Sales",
        "description": "$3-10M revenue, up to 5 sales reps, generic marketing with no dedicated "
        "GTM function -- growth has plateaued and no active GTM/sales-leadership hiring is underway.",
        "revenue_min_usd": 3_000_000,
        "revenue_max_usd": 10_000_000,
        "sales_team_size_max": 5,
        "trigger_mode": "requires_absence",
        "trigger_hiring_roles": ["gtm", "head_of_sales"],
        "trigger_description": "No active GTM engineer or Head-of-Sales hiring detected -- "
        "consistent with a team that has plateaued rather than actively upgrading.",
    },
    {
        "id": "icp_2",
        "name": "Upgrading With AI",
        "description": "$10-20M revenue, actively upgrading their sales team with AI -- hiring "
        "GTM engineers or forward-deployed engineers (FDEs).",
        "revenue_min_usd": 10_000_000,
        "revenue_max_usd": 20_000_000,
        "sales_team_size_max": None,
        "trigger_mode": "requires_presence",
        "trigger_hiring_roles": ["gtm"],
        "trigger_description": "Actively hiring for a GTM-engineer-type role.",
    },
    {
        "id": "icp_3",
        "name": "Needs Fractional Leadership",
        "description": "$20-50M revenue, needs sales leadership rather than more headcount -- "
        "hiring a VP/Head of Sales role they may prefer to fill fractionally rather than full-time.",
        "revenue_min_usd": 20_000_000,
        "revenue_max_usd": 50_000_000,
        "sales_team_size_max": None,
        "trigger_mode": "requires_presence",
        "trigger_hiring_roles": ["head_of_sales"],
        "trigger_description": "Actively hiring for a VP/Head of Sales role.",
    },
]


class IcpConfigError(ValueError):
    """Raised when ICP configuration fails validation -- never silently coerced."""


def _validate_icp_config(icps: list[dict]) -> None:
    if not isinstance(icps, list):
        raise IcpConfigError("ICP config must be a list")
    seen_ids: set[str] = set()
    for i, icp in enumerate(icps):
        if not isinstance(icp, dict):
            raise IcpConfigError(f"ICP at index {i} must be an object")
        icp_id = icp.get("id")
        if not isinstance(icp_id, str) or not icp_id.strip():
            raise IcpConfigError(f"ICP at index {i} has an empty or missing id")
        if icp_id in seen_ids:
            raise IcpConfigError(f"duplicate ICP id: {icp_id!r}")
        seen_ids.add(icp_id)

        name = icp.get("name")
        if not isinstance(name, str) or not name.strip():
            raise IcpConfigError(f"ICP {icp_id!r} has an empty or missing name")

        for revenue_field in ("revenue_min_usd", "revenue_max_usd"):
            value = icp.get(revenue_field)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise IcpConfigError(f"ICP {icp_id!r} field {revenue_field!r} must be a non-negative integer or null")

        sales_team_size_max = icp.get("sales_team_size_max")
        if sales_team_size_max is not None and (not isinstance(sales_team_size_max, int) or isinstance(sales_team_size_max, bool) or sales_team_size_max < 0):
            raise IcpConfigError(f"ICP {icp_id!r} field 'sales_team_size_max' must be a non-negative integer or null")

        trigger_mode = icp.get("trigger_mode")
        if trigger_mode not in ("requires_presence", "requires_absence"):
            raise IcpConfigError(f"ICP {icp_id!r} has an invalid trigger_mode: {trigger_mode!r}")

        trigger_hiring_roles = icp.get("trigger_hiring_roles")
        if not isinstance(trigger_hiring_roles, list) or not all(isinstance(r, str) for r in trigger_hiring_roles):
            raise IcpConfigError(f"ICP {icp_id!r} field 'trigger_hiring_roles' must be a list of strings")
        unknown_roles = set(trigger_hiring_roles) - HIRING_SIGNAL_ROLES
        if unknown_roles:
            raise IcpConfigError(f"ICP {icp_id!r} references unknown hiring_signal_role values: {unknown_roles}")


def get_icp_config(db: Session, tenant_id: int) -> list[dict]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == ICP_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, list):
        return param.value
    return DEFAULT_ICP_CONFIG


def set_icp_config(db: Session, tenant_id: int, icps: list[dict]) -> None:
    _validate_icp_config(icps)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == ICP_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = icps
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=ICP_CONFIG_PARAMETER_KEY,
            value=icps,
            description="ICP definitions + hiring triggers for ICP + Trigger Intelligence -- "
            "seeded from the lead's own reference material, editable via set_icp_config() (see "
            "app/gtm_os/icp/icp_config.py)",
        )
        db.add(param)
    db.commit()
