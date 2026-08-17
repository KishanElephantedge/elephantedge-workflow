"""The company's own operating knowledge -- goals, offerings, ICP, TAM, GTM motions, sales
methodology, messaging/objections -- stored as structured, machine-readable data instead of a
markdown file or a hardcoded `if company_has_sales_job: sell_ai_sales()` rule. This is the
direct implementation of the founder's own framing: "the notes shouldn't simply be stored as
random prompts, they define the business ontology the AI operates from."

Storage choice: reuses the existing `Parameter` table (tenant_id, key, value JSON) rather than
a new table. Same pattern already proven in app/phases/linkedin_monitor.py's
get_keyword_tiers/set_keyword_tiers (a Parameter-backed, dashboard-editable JSON blob with a
seed default). No new schema is justified yet -- this is one structured document per tenant,
not a set of relational entities with their own querying needs.

DEFAULT_BUSINESS_CONTEXT is a first transcription of the founder's handwritten notes
(2026-08-14/15). It is a starting point, not a finished spec -- several fields are marked
UNCONFIRMED where the handwriting was ambiguous (the TAM chart's axis/units in particular).
Treat this default as seed data only; the real source of truth from the first save onward is
the Parameter row itself, editable without a code change."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

BUSINESS_CONTEXT_PARAMETER_KEY = "gtm_os_business_context"

DEFAULT_BUSINESS_CONTEXT: dict = {
    "goals": {
        "revenue_goal": None,  # not yet quantified in the notes beyond "currently $0, must increase"
        "partnership_goals": None,
        "meetings_goals": None,
        "long_term_goal": None,
        "short_term_goal": None,
    },
    "offerings": [
        {"name": "Consulting", "description": None},
        {"name": "Execution", "description": None},
        {"name": "Workshop", "description": None},
        {"name": "Sales OS", "description": None},
        {"name": "Sales Products", "description": None},
        {"name": "Digital Playbook", "description": None},
    ],
    "icp_bands": [
        {
            "band": 1,
            "revenue_range_usd": "3M-10M",
            "profile": "Companies stuck in sales",
        },
        {
            "band": 2,
            "revenue_range_usd": "10M-20M",
            "profile": "Looking for FDEs in Sales/GTM",
        },
        {
            "band": 3,
            "revenue_range_usd": "20M-50M",
            "profile": "Looking for FDEs in GTM",
        },
    ],
    "tam": {
        "segments": ["SMB", "Midmarket", "Enterprise"],
        # UNCONFIRMED: handwritten chart showed 100 / 50 / 250 against these three segments,
        # but the Y-axis label and units (company count? $ TAM? something else?) were not
        # legible -- needs the founder's confirmation before this is trusted for anything.
        "segment_values_unconfirmed": {"SMB": 100, "Midmarket": 50, "Enterprise": 250},
        "unit": "UNCONFIRMED",
    },
    "gtm_motions": [
        "Partnership motion",
        "Outbound motion",
        "LinkedIn content",
        "Inbound / SEO / webinars",
    ],
    "sales_methodology": ["MEDDIC", "AIDA"],
    "messaging_objections": [
        "We're not interested",
        "We use a competitor",
        "We build in-house",
    ],
    "integrations_mentioned": ["HubSpot", "Newsletter"],
    "strategic_flow": [
        "Demand / market movement",
        "ICP comparison",
        "AI Sales (current positioning)",
        "Offering",
        "Revenue goal",
        "GTM motions",
    ],
    "business_model_history": [
        "Playbook sales (Zero to $1M B2B Sales & GTM Playbook; B2B Sales for Founders)",
        "Workshops",
        "Sales consultancy",
        "GTM sales / fractional VP of Sales",
        "AI-powered sales (current)",
    ],
}


def get_business_context(db: Session, tenant_id: int) -> dict:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == BUSINESS_CONTEXT_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict) and param.value:
        return param.value
    return DEFAULT_BUSINESS_CONTEXT


def set_business_context(db: Session, tenant_id: int, context: dict) -> None:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == BUSINESS_CONTEXT_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = context
    else:
        param = Parameter(
            tenant_id=tenant_id, key=BUSINESS_CONTEXT_PARAMETER_KEY, value=context,
            description="GTM OS business context (goals, offerings, ICP, TAM, GTM motions, sales methodology, messaging) -- see app/gtm_os/context/business_context.py",
        )
        db.add(param)
    db.commit()
