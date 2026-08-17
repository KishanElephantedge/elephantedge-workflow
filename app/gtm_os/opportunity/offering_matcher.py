"""Offering Matcher framework -- Batch 4 Parts F-H. Consumes an Opportunity + the tenant's
offering configuration (offering_config.py) and returns structured candidate matches. Explicitly
a FRAMEWORK, not final business rules -- per gpt.txt's own constraint, the six known offerings'
real qualification rules haven't been confirmed by the lead/CEO yet (see offering_config.py).

HONESTY ABOUT WHAT CAN ACTUALLY BE MATCHED (Part H's "only use dimensions that actually exist"):
inspected Opportunity/ProblemHypothesis/DemandHypothesis directly -- the only STRUCTURED,
matchable-today dimension either side of this match actually has is `affected_function`
("sales" | "marketing" | "unknown", inherited unchanged from ProblemHypothesis/
InterpretedSignal). There is no `problem_type` taxonomy, no structured company-characteristics
field on Opportunity, and no structured qualification-signal field anywhere in this codebase
today. So even once an offering's `target_problem_types`/`target_company_characteristics`/
`qualification_signals` are filled in by a human, THIS implementation still can't match on them
-- it reports that honestly via `missing_information`, rather than silently ignoring the
configured field or pretending to use it. Only `target_functions` is actually actionable right
now. This is a real, current-system limitation, not a design choice to revisit lightly -- a
future batch would need to add structured problem-type/company-characteristic classification to
ProblemHypothesis or Opportunity itself before those dimensions could genuinely be used.

Deterministic only -- no LLM (Part H's explicit instruction). Never reduces to one score (Part H)
-- every result carries status + fit_reasons + missing_information + exclusion_reasons, so a
human can see exactly why."""

from sqlalchemy.orm import Session

from app.gtm_os.opportunity.offering_config import MATCHING_RELEVANT_FIELDS, get_offering_config
from app.gtm_os.opportunity.opportunity import Opportunity

# The only field, among MATCHING_RELEVANT_FIELDS, this implementation can actually act on today
# -- see module docstring. The others are recognized/reported as configured-but-unusable, never
# silently ignored.
USABLE_MATCHING_FIELD = "target_functions"


def match_offering(offering: dict, opportunity: Opportunity) -> dict:
    """Evaluates ONE offering against ONE opportunity. Always returns a structured result --
    never a bare score, never a forced match."""
    name = offering.get("name", "unknown")
    missing_information: list[str] = []

    configured_relevant_fields = [f for f in MATCHING_RELEVANT_FIELDS if offering.get(f)]
    if not configured_relevant_fields:
        return {
            "offering": name,
            "status": "insufficient_offering_context",
            "fit_reasons": [],
            "missing_information": list(MATCHING_RELEVANT_FIELDS),
            "exclusion_reasons": [],
        }

    # Any configured dimension besides target_functions is real configuration, but not
    # actionable with current Opportunity data -- surfaced explicitly, not silently dropped.
    for field in configured_relevant_fields:
        if field != USABLE_MATCHING_FIELD:
            missing_information.append(
                f"offering has {field!r} configured, but Opportunity has no structured data for "
                "this dimension yet -- cannot be used for matching in this implementation"
            )

    target_functions = offering.get(USABLE_MATCHING_FIELD) or []
    if not target_functions:
        missing_information.append(f"{USABLE_MATCHING_FIELD!r} not configured -- the only dimension currently usable for matching")
        return {
            "offering": name,
            "status": "insufficient_offering_context",
            "fit_reasons": [],
            "missing_information": missing_information,
            "exclusion_reasons": [],
        }

    exclusions = offering.get("exclusions") or []
    if opportunity.affected_function in exclusions:
        return {
            "offering": name,
            "status": "excluded",
            "fit_reasons": [],
            "missing_information": missing_information,
            "exclusion_reasons": [f"affected_function {opportunity.affected_function!r} is in this offering's configured exclusions"],
        }

    if opportunity.affected_function in target_functions:
        return {
            "offering": name,
            "status": "candidate_match",
            "fit_reasons": [f"opportunity's affected_function {opportunity.affected_function!r} matches this offering's configured target_functions {target_functions}"],
            "missing_information": missing_information,
            "exclusion_reasons": [],
        }

    return {
        "offering": name,
        "status": "no_match",
        "fit_reasons": [],
        "missing_information": missing_information,
        "exclusion_reasons": [f"affected_function {opportunity.affected_function!r} not in this offering's configured target_functions {target_functions}"],
    }


def match_offerings(db: Session, tenant_id: int, opportunity: Opportunity) -> list[dict]:
    """Evaluates every configured offering (or the deliberately-incomplete
    DEFAULT_OFFERING_CONFIG's six known names, if nothing has been configured yet) against one
    Opportunity. Read-only -- writes nothing."""
    offerings = get_offering_config(db, tenant_id)
    return [match_offering(offering, opportunity) for offering in offerings]
