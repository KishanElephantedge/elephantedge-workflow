"""Offering configuration -- Batch 4 Part F. Same Parameter-backed storage pattern as
topics.py/business_context.py -- own key, own shape, no new schema/CMS.

CRITICAL, per gpt.txt's own explicit constraint: the six known offering names (Consulting,
Execution, Workshop, Sales OS, Sales Products, Digital Playbook) are real (from
business_context.py), but their actual qualification rules, target functions, target company
characteristics, exclusions, objections, delivery constraints, and positioning have NOT been
confirmed by the lead/CEO. DEFAULT_OFFERING_CONFIG below is DELIBERATELY INCOMPLETE for those
fields -- every offering has only a `name`, everything else is null/empty. This is not a bug or
an oversight: it is what forces offering_matcher.py to honestly report "insufficient_offering_context"
for every offering until a human fills these fields in (via set_offering_config), rather than the
system inventing business rules to fill the gap. See offering_matcher.py's own module docstring
for how "insufficient" is detected from exactly this shape.

BATCH 9 ADDITION -- `applicable_icps`: unlike the fields above, the lead's reference material
DOES explicitly map most of the 6 offerings to specific ICPs (see icp_offering_matching.py's own
module docstring for the exact mapping and one deliberately-unmapped exception). Per this batch's
own "where the lead has given an explicit rule, represent it" instruction, `applicable_icps` is
seeded with those real mappings (a list of icp_config.py `id` values, e.g. ["icp_1"]) -- still
just configuration, fully editable via set_offering_config() the moment the lead confirms/changes
it."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

OFFERING_CONFIG_PARAMETER_KEY = "gtm_os_offering_config"

# The six known offering names, per business_context.py's own DEFAULT_BUSINESS_CONTEXT
# ("offerings" list) -- reused, not re-invented. `applicable_icps` is seeded from the lead's
# reference material (see module docstring); every other field remains None/[] -- seed structure
# only. A human confirms real definitions later via set_offering_config(); nothing here should
# ever be treated as a fully confirmed qualification rule.
#
# Mapping source (see icp_offering_matching.py for the full reasoning): ICP 1 ("Stuck in Sales")
# -> GTM Consulting/Workshop/Digital Playbook; ICP 2 ("Upgrading With AI") -> Sales Execution
# Build/Sales Tech Product; ICP 3 ("Needs Fractional Leadership") -> deliberately left UNMAPPED
# here -- see the explicit ambiguity note in icp_offering_matching.py's module docstring.
_APPLICABLE_ICPS_BY_OFFERING: dict[str, list[str]] = {
    "Consulting": ["icp_1"],
    "Workshop": ["icp_1"],
    "Digital Playbook": ["icp_1"],
    "Execution": ["icp_2"],
    "Sales Products": ["icp_2"],
    "Sales OS": [],  # deliberately unmapped -- see icp_offering_matching.py module docstring
}

DEFAULT_OFFERING_CONFIG: list[dict] = [
    {
        "name": name,
        "description": None,
        "applicable_icps": _APPLICABLE_ICPS_BY_OFFERING[name],
        "target_problem_types": [],
        "target_functions": [],
        "target_company_characteristics": [],
        "qualification_signals": [],
        "exclusions": [],
        "typical_objections": [],
        "delivery_constraints": None,
        "positioning_messaging": None,
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
