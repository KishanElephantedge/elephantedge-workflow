"""Offering configuration -- Batch 4 Part F. Same Parameter-backed storage pattern as
topics.py/business_context.py -- own key, own shape, no new schema/CMS.

2026-08-24 UPDATE -- real offering definitions confirmed directly by the user (pillar/mode/
audience/depth table + real product names), resolving the ICP<->offering mapping that had
DRIFTED OUT OF SYNC between this file and icp_offering_matching.py's own docstring (that file
already documented the correct, lead-confirmed mapping; this file's own `_APPLICABLE_ICPS_BY_
OFFERING` dict had never been updated to match -- a real bug, not a design choice). Fixed here:
    Execution (Fractional VP Sales, Leadership/Embed, "I run it for you")
        -> icp_3 ("Needs Fractional Leadership", triggers on hiring a VP/Head of Sales) --
           was icp_2, now corrected.
    Sales OS (Agentic layers for sales / AI SDR & Sales Automation, Automation/Build,
        "I build the system that runs it") -> icp_2 ("Upgrading With AI", triggers on hiring a
           GTM-engineer-type role) -- was unmapped, now corrected.
    Consulting/Workshop/Digital Playbook -> icp_1 ("Stuck in Sales") -- unchanged, was already
        correct in both files.
    Sales Products (AI Sales/GTM products, Technology/Sell, Mid-market/Enterprise audience,
        "I hand you the tool that does it") -> deliberately left UNMAPPED to icp_1/2/3: those
        three ICPs are all SMB revenue bands ($3-50M); Sales Products targets a different segment
        (Mid-market/Enterprise), so forcing it onto one of the three would misrepresent the real
        audience, not just fill a gap.

Still deliberately incomplete beyond what was actually confirmed: qualification_signals,
target_problem_types, exclusions, typical_objections, delivery_constraints remain empty/None --
description/positioning_messaging/target_company_characteristics below are filled in ONLY with
what the user explicitly stated (pillar, mode, audience, depth, real product names/links), not
extrapolated further. offering_matcher.py's "insufficient_offering_context" reporting (see its own
docstring) still applies to whatever remains unconfirmed."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

OFFERING_CONFIG_PARAMETER_KEY = "gtm_os_offering_config"

# Corrected 2026-08-24 -- see module docstring for the real, user-confirmed mapping and why the
# previous version of this dict had drifted out of sync with icp_offering_matching.py.
_APPLICABLE_ICPS_BY_OFFERING: dict[str, list[str]] = {
    "Consulting": ["icp_1"],
    "Workshop": ["icp_1"],
    "Digital Playbook": ["icp_1"],
    "Execution": ["icp_3"],
    "Sales OS": ["icp_2"],
    "Sales Products": [],  # deliberately unmapped -- different (Mid-market/Enterprise) segment, see module docstring
}

# Real, user-confirmed descriptions/positioning/audience (2026-08-24) -- everything else per
# offering stays empty/None until further confirmed (see module docstring).
_OFFERING_DETAILS: dict[str, dict] = {
    "Consulting": {
        "description": "Sales Consulting.",
        "target_company_characteristics": [],
        "positioning_messaging": None,
    },
    "Execution": {
        "description": "Fractional VP Sales -- Elephant Edge embeds and runs the sales function directly for the client, rather than just advising.",
        "target_company_characteristics": ["India SMB founders"],
        "positioning_messaging": "Execution -- I run it for you.",
    },
    "Workshop": {
        "description": "Sales workshops -- teaches founders how sales should work. Real product: "
        "udemy.com/course/b2b-sales-for-founders-learn-the-playbook-in-48-minutes/",
        "target_company_characteristics": ["India SMB founders"],
        "positioning_messaging": "Awareness -- here's how sales should work.",
    },
    "Sales OS": {
        "description": "AI SDR and Sales Automation (\"Agentic layers for sales\") -- Elephant Edge builds the automated sales infrastructure/system that runs the client's sales process.",
        "target_company_characteristics": ["US SMBs"],
        "positioning_messaging": "Infrastructure -- I build the system that runs it.",
    },
    "Sales Products": {
        "description": "AI Sales/GTM products -- a packaged technology tool sold to the buyer, not a delivered service engagement.",
        "target_company_characteristics": ["Mid-market/Enterprise"],
        "positioning_messaging": "Scale -- I hand you the tool that does it.",
    },
    "Digital Playbook": {
        "description": "Lead magnets -- downloadable sales playbooks/guides used as top-of-funnel content. Real products: "
        "elephantedge.gumroad.com/l/AIsalesplaybook, elephantedge.gumroad.com/l/partner-ledplaybook",
        "target_company_characteristics": [],
        "positioning_messaging": None,
    },
}

DEFAULT_OFFERING_CONFIG: list[dict] = [
    {
        "name": name,
        "description": _OFFERING_DETAILS[name]["description"],
        "applicable_icps": _APPLICABLE_ICPS_BY_OFFERING[name],
        "target_problem_types": [],
        "target_functions": ["sales", "marketing"] if name == "Digital Playbook" else ["sales"],
        "target_company_characteristics": _OFFERING_DETAILS[name]["target_company_characteristics"],
        "qualification_signals": [],
        "exclusions": [],
        "typical_objections": [],
        "delivery_constraints": None,
        "positioning_messaging": _OFFERING_DETAILS[name]["positioning_messaging"],
    }
    for name in ["Consulting", "Execution", "Workshop", "Sales OS", "Sales Products", "Digital Playbook"]
]

# The fields offering_matcher.py checks to decide whether an offering has ENOUGH configuration to
# even attempt a match -- if all of these are empty for a given offering, matching is impossible,
# not just weak (see offering_matcher.py). Deliberately excludes purely descriptive fields
# (description, positioning_messaging, delivery_constraints, typical_objections) -- those don't
# affect whether a MATCH DECISION can be made, only how it's explained/delivered once made.
MATCHING_RELEVANT_FIELDS = ("target_problem_types", "target_functions", "target_company_characteristics", "qualification_signals")


class OfferingConfigError(ValueError):
    """Raised when offering configuration fails validation -- never silently coerced."""


def _validate_offering_config(offerings: list[dict]) -> None:
    if not isinstance(offerings, list):
        raise OfferingConfigError("offering config must be a list")
    seen_names: set[str] = set()
    for i, offering in enumerate(offerings):
        if not isinstance(offering, dict):
            raise OfferingConfigError(f"offering at index {i} must be an object")
        name = offering.get("name")
        if not isinstance(name, str) or not name.strip():
            raise OfferingConfigError(f"offering at index {i} has an empty or missing name")
        normalized = name.strip().lower()
        if normalized in seen_names:
            raise OfferingConfigError(f"duplicate offering name: {name!r}")
        seen_names.add(normalized)
        for list_field in ("applicable_icps", "target_problem_types", "target_functions", "target_company_characteristics", "qualification_signals", "exclusions", "typical_objections"):
            value = offering.get(list_field, [])
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise OfferingConfigError(f"offering {name!r} field {list_field!r} must be a list of strings")


def get_offering_config(db: Session, tenant_id: int) -> list[dict]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == OFFERING_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, list):
        return param.value
    return DEFAULT_OFFERING_CONFIG


def set_offering_config(db: Session, tenant_id: int, offerings: list[dict]) -> None:
    _validate_offering_config(offerings)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == OFFERING_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = offerings
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=OFFERING_CONFIG_PARAMETER_KEY,
            value=offerings,
            description="Offering definitions for Offering Matcher (name, target functions/problem "
            "types, qualification signals, exclusions, ...) -- pending lead/CEO confirmation, see "
            "app/gtm_os/opportunity/offering_config.py",
        )
        db.add(param)
    db.commit()
