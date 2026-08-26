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

# 2026-08-26, explicit instruction -- real historical LinkedIn message performance data (the
# "LinkedIn Message Copies" PDF, 2020-2026), fed into MESSAGE_GENERATION_PROMPT/
# FOLLOWUP_GENERATION_PROMPT as style/tone reference for the LLM (see message_draft.py) -- NOT a
# fixed template to copy verbatim. Each offering's messaging_pattern_note is the PDF's own
# real, data-backed observation for that offering (never a generic "be warm" instruction
# invented here). "Sales OS" has none of its own real historical data in the PDF (it's V1's
# newer framing, not covered by the source document) -- deliberately left empty rather than
# fabricated; message generation for that offering falls back to having no reference examples,
# same as before this change.
_PROVEN_MESSAGING: dict[str, dict] = {
    "Digital Playbook": {
        "proven_message_examples": [
            "how's it going? I've designed and launched a sales system to guide founders from zero to 100+ paying customers. I tested it with my close network of founders, business owners, and sales professionals, all of whom experienced surprising growth in sales. Would love to invite you to check it out. The editable version is now available for only $10. Love to hear your thoughts! https://www.elephantedge.co/l/salesplaybook -Majji",
            "I have put together a proven and easy-to-implement playbook that makes founders' jobs easier. https://www.elephantedge.co/l/b2bSales Love to have a chat if this resonates with your SaaS business goals. -Venkatesh",
        ],
        "messaging_pattern_note": "Warm, personal, specific copy consistently outperforms stats/scarcity/credentials-led framing (real reply rates: 37.3%/44.4% for personal-note style vs 5.2% for a bare resource link, 0% for a Dubai-relocation pitch).",
    },
    "Workshop": {
        "proven_message_examples": [
            "how are you? I stumbled upon your profile and found it quite intriguing. After a successful exit, I'm now working on Elephant Edge, where we're building a Sales Academy for B2B/Tech sales training in India & US. We've had some fantastic results with our cohorts and online courses. I'm new to growth marketing and would love to have a quick chat with you to gain some insights and explore potential collaboration opportunities. If you have a moment for a brief call, would love to speak. - Venkatesh",
            "Hi {{firstName}} - How's it going? I wanted to personally share something with you that I'm launching. For sales professionals who are targeting India/US market, I'm hosting a B2B Sales cohort with primary focus to drive the first $1M in revenue and key accounts, a strategy that has been well-tested by early-stage tech founders and their first hires. Would you be interested in joining us for this 1-day cohort. Love to know your thoughts, {{firstName}}. Venkatesh",
        ],
        "messaging_pattern_note": "Your two best performers (52.2% and 33.8% reply) are both simple, personal notes from your earliest cohort era. Your highest-volume recent templates, which lead with credentials/exits before the ask, are your weakest (18-20%) -- warmth and specificity beat scale and credential-leading.",
    },
    "Execution": {
        "proven_message_examples": [
            "Venkatesh here. I'm a new member at GTM Partners and recently joined to accelerate my fractional VP Sales practice. Great connecting with you, and I'd love to learn more about your work in the coming weeks. — Venkatesh",
            "I'm a 2x founder now working as a Fractional VP Sales, helping companies build AI-powered revenue systems to get to $1M without the traditional sales team overhead. Would love to connect.",
        ],
        "messaging_pattern_note": "Your top two performers (69.7% and 38.7% reply) are simple, warm, community-anchored intros with no pitch and no credentials-first framing -- just 'I'm a member here, let's connect.' Your weakest (0-12%) lead with a value prop or ask upfront. Even your most original idea (the 'digital twin of the consultant' pitch) only pulled 21%, likely because it opens with a paragraph of positioning before the ask.",
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
        # 2026-08-26, explicit instruction -- real per-offering outbound campaigns (Playbook,
        # Workshop/"Cohort", Execution/Fractional VP Sales, Sales OS) each need their own
        # SalesRobot/HeyReach campaign, not one tenant-wide default (see app/outreach/*.py --
        # "we should not hardcode that"). {channel_name: campaign_id}, empty until a real
        # campaign is created and its id is supplied -- see set_offering_campaign_id().
        "campaign_ids": {},
        "proven_message_examples": _PROVEN_MESSAGING.get(name, {}).get("proven_message_examples", []),
        "messaging_pattern_note": _PROVEN_MESSAGING.get(name, {}).get("messaging_pattern_note"),
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
        campaign_ids = offering.get("campaign_ids", {})
        if not isinstance(campaign_ids, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in campaign_ids.items()):
            raise OfferingConfigError(f"offering {name!r} field 'campaign_ids' must be a dict of channel name -> campaign id string")
        examples = offering.get("proven_message_examples", [])
        if not isinstance(examples, list) or any(not isinstance(v, str) for v in examples):
            raise OfferingConfigError(f"offering {name!r} field 'proven_message_examples' must be a list of strings")
        pattern_note = offering.get("messaging_pattern_note")
        if pattern_note is not None and not isinstance(pattern_note, str):
            raise OfferingConfigError(f"offering {name!r} field 'messaging_pattern_note' must be a string or null")


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


def get_offering_campaign_id(db: Session, tenant_id: int, offering_name: str, channel: str) -> str | None:
    """Real per-offering routing target for app/outreach/*.py. Returns None if this offering has
    no campaign configured yet for this channel -- callers must NOT fall back to a different
    campaign in that case (see module docstring): a named offering with nothing configured means
    "not ready yet", not "use the tenant default", since that would silently misroute a
    Fractional-VP-Sales-matched prospect into whatever the Sales OS campaign happens to be, etc."""
    for offering in get_offering_config(db, tenant_id):
        if offering.get("name") == offering_name:
            return (offering.get("campaign_ids") or {}).get(channel)
    return None


def get_offering_messaging_reference(db: Session, tenant_id: int, offering_name: str | None) -> dict:
    """Real historical proven_message_examples/messaging_pattern_note for the given offering, for
    message_draft.py's generation prompts to use as style/tone reference (never a template to
    copy verbatim -- see module docstring). Returns {"proven_message_examples": [], "messaging_pattern_note": None}
    when offering_name is None or that offering has none configured (e.g. Sales OS today)."""
    if offering_name:
        for offering in get_offering_config(db, tenant_id):
            if offering.get("name") == offering_name:
                return {
                    "proven_message_examples": offering.get("proven_message_examples") or [],
                    "messaging_pattern_note": offering.get("messaging_pattern_note"),
                }
    return {"proven_message_examples": [], "messaging_pattern_note": None}


def set_offering_campaign_id(db: Session, tenant_id: int, offering_name: str, channel: str, campaign_id: str) -> None:
    """Sets just one offering's campaign id for one channel, leaving the rest of the config
    untouched -- the real update path once a campaign is created in SalesRobot/HeyReach and its
    id is supplied, so this never requires resending the whole offering config by hand."""
    offerings = get_offering_config(db, tenant_id)
    updated = []
    found = False
    for offering in offerings:
        offering = dict(offering)
        if offering.get("name") == offering_name:
            campaign_ids = dict(offering.get("campaign_ids") or {})
            campaign_ids[channel] = campaign_id
            offering["campaign_ids"] = campaign_ids
            found = True
        updated.append(offering)
    if not found:
        raise OfferingConfigError(f"no offering named {offering_name!r} configured")
    set_offering_config(db, tenant_id, updated)


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
