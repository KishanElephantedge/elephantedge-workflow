"""What each partner-facing feature is, and what it needs configured to actually work.

WHY THIS EXISTS (2026-10-07). We run the back office for a growing number of partners, and they do
not all want the same things: one asks for webinars, another for email marketing, a third for
LinkedIn content -- and two who want "the same" feature usually want it pointed at different
campaigns, mailboxes or cadences. There are only three honest ways to handle that, and two of them
are wrong:

    hardcode per partner      -> every new partner is a code change and a deploy. This had already
                                 started: MAJJI_EMAIL_CAMPAIGNS hardcoded two Smartlead campaign
                                 ids and _require_majji_tenant() 404'd everyone else, even though
                                 the per-tenant `smartlead_campaign_id` key already existed.
    branch inside the feature -> `if tenant_id == N` spreads until nobody can reason about it.
    CONFIGURATION + ADAPTERS  -> this file.

The three layers, two of which already existed and only needed declaring:

    1. tenants.enabled_features   which modules a partner sees              (already live)
    2. Parameter(tenant_id, key)  what those modules are pointed at         (already live, 22 uses)
    3. THIS REGISTRY              what each feature REQUIRES, so onboarding is data entry, a
                                  partner's readiness is computable before a run rather than
                                  discovered during one, and the admin settings screen can be
                                  generated instead of hand-written per feature.

Rules this encodes:
  - Same feature wanted differently -> add a ConfigKey with a sensible default. Never a branch.
  - Structurally different (a different vendor entirely) -> an adapter chosen by config, the way
    app/outreach/selector.py already picks SalesRobot vs HeyReach vs Smartlead.
  - Build a feature when a real partner asks for it, then offer it to everyone as an add-on.
    Nothing here is speculative -- every feature below exists because someone asked.
"""
from __future__ import annotations

from dataclasses import dataclass

# Value types the admin UI renders, and that validation checks against.
STRING = "string"
INTEGER = "integer"
URL = "url"
LIST = "list"
OBJECT_LIST = "object_list"


@dataclass(frozen=True)
class ConfigKey:
    """One per-tenant setting, stored as a Parameter row keyed by `key`."""

    key: str
    label: str
    type: str = STRING
    required: bool = True
    help: str | None = None
    example: str | None = None


@dataclass(frozen=True)
class Feature:
    key: str                       # the value that appears in tenants.enabled_features
    label: str
    description: str
    config: tuple[ConfigKey, ...] = ()
    credentials: tuple[str, ...] = ()      # credential rows the feature needs for this tenant
    # Features whose data is produced by the pipeline rather than configured, e.g. Accounts.
    note: str | None = None


ACCOUNTS = Feature(
    key="accounts",
    label="Accounts",
    description="Companies sourced against the partner's ICP, plus engagement-sourced leads, in "
                "one list behind a source filter.",
    config=(
        ConfigKey("partner_icp", "ICP", OBJECT_LIST,
                  help="The partner's ideal customer profile. Edited on the partner's own Settings "
                       "page or parsed from a document; drives every sourcing run.",
                  example='{"employee_min": 11, "employee_max": 50, "geographies": ["United States"]}'),
    ),
)

CONTENT = Feature(
    key="content",
    label="LinkedIn Content",
    description="The partner's own LinkedIn content and posting activity.",
    config=(
        ConfigKey("own_linkedin_profile_url", "LinkedIn profile URL", URL,
                  help="The partner's own profile, used to track what they actually post.",
                  example="https://www.linkedin.com/in/username/"),
        ConfigKey("partner_content_context", "Content context", OBJECT_LIST, required=False,
                  help="Voice, themes and audience notes used when drafting content."),
    ),
)

CRM = Feature(
    key="crm",
    label="Data (CRM)",
    description="Per-lead outreach tracking for a partner running their own campaign. Built for "
                "Sandy Yu's stated need: a structured view of where each target sits in the process.",
    note="Rows arrive by import; no per-tenant configuration is required to switch it on.",
)

WEBINARS = Feature(
    key="webinars",
    label="Webinars",
    description="Real invite performance for a partner's webinar -- sent and clicked counts read "
                "from the same table the send script writes to.",
    note="Event metadata lives in the webinars table, created per event rather than configured here.",
)

EMAIL_CAMPAIGNS = Feature(
    key="email_campaigns",
    label="Email Campaigns",
    description="Live Smartlead campaign stats for the partner's own campaigns.",
    config=(
        ConfigKey("smartlead_campaign_ids", "Smartlead campaigns", OBJECT_LIST,
                  help="Which campaigns belong to this partner. Required because one Smartlead API "
                       "key can see every campaign in the account, so the partner must only ever be "
                       "shown their own.",
                  example='[{"id": 4037761, "label": "Cost Saving Angle"}]'),
    ),
    credentials=("smartlead_api_key",),
)


FEATURES: tuple[Feature, ...] = (ACCOUNTS, CONTENT, CRM, WEBINARS, EMAIL_CAMPAIGNS)


def get_feature(key: str) -> Feature | None:
    return next((f for f in FEATURES if f.key == key), None)


def feature_keys() -> list[str]:
    return [f.key for f in FEATURES]
