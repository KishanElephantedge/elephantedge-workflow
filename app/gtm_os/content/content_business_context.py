"""Who the Content Intelligence prompts are actually writing AS -- 2026-09-09.

Every content-generation prompt (opportunity synthesis, draft writing, the content chat's own
system prompt) used to hardcode "Elephant Edge" and its specific "build vs. rent" positioning
directly in the prompt text. That's correct for tenant 2, but wrong for any other tenant: a
partner's content would literally get written as if they WERE Elephant Edge.

Storage: the same Parameter-backed pattern as partner_icp (see app/routes/api.py's
PARTNER_CONTENT_CONTEXT_PARAMETER_KEY) -- one small JSON blob per tenant, editable via
GET/PUT /gtm-os/partner/content-context, no new schema. Elephant Edge's own real values are kept
as the DEFAULT for tenant_id == ELEPHANT_EDGE_TENANT_ID specifically, so its behavior is
byte-for-byte unchanged if that Parameter row is never set. Any OTHER tenant with no row set gets
an honest "not yet configured" placeholder instead of silently inheriting Elephant Edge's own
positioning -- never a fabricated default for a business we don't actually know anything about."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

CONTENT_CONTEXT_PARAMETER_KEY = "partner_content_context"

# Matches app/routes/api.py's own constant -- duplicated rather than imported to avoid a
# content/ -> routes/ import direction (routes already imports from content/, not the reverse).
ELEPHANT_EDGE_TENANT_ID = 2

DEFAULT_ELEPHANT_EDGE_CONTENT_CONTEXT = {
    "business_name": "Elephant Edge",
    "positioning": (
        "most competitors in this space RENT you sales capacity (they do the selling, "
        "capability leaves when the engagement ends); Elephant Edge builds a sales system "
        "the client's own team ends up owning and keeping"
    ),
    "audience": "B2B companies evaluating fractional/outsourced sales leadership",
}

_NOT_CONFIGURED_CONTEXT = {
    "business_name": None,
    "positioning": None,
    "audience": None,
}


def get_content_business_context(db: Session, tenant_id: int) -> dict:
    """Never fabricates a positioning/audience for a tenant we haven't actually been told
    about -- a None field means the caller should degrade gracefully (e.g. the content chat
    should tell the partner to fill in Settings first), not invent generic filler."""
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == CONTENT_CONTEXT_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict) and param.value.get("business_name"):
        return param.value
    if tenant_id == ELEPHANT_EDGE_TENANT_ID:
        return DEFAULT_ELEPHANT_EDGE_CONTENT_CONTEXT
    return dict(_NOT_CONFIGURED_CONTEXT)
