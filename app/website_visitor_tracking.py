"""Company-level website visitor identification -- receives a page-view beacon from the
marketing site's own tracking snippet, resolves the visitor's IP to a company via Deepline's
deepline_ip_to_company_find_company_by_ip tool, and logs the result.

DELIBERATELY COMPANY-LEVEL ONLY, NOT PERSON-LEVEL: an IP address identifies a network (an
office, a VPN, an ISP block), not an individual -- there is no way to derive "this specific
person is browsing" from an IP alone, with any provider. Tools that claim person-level
identification (RB2B, Warmly, 6sense) do it differently: their own JS pixel captures browser
cookies/device fingerprints, cross-referenced against a separate ad-tech identity graph -- a
different, more invasive integration with real legal/compliance exposure (GDPR/CCPA), not
something layered on top of this one. If person-level identification is ever wanted, that's a
new, separate integration decision, not an extension of this module.

Uses the SAME Deepline execute_tool() plumbing as every other Deepline call in this backend
(app/deepline_client.py) -- no new HTTP client, no new auth pattern."""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import WebsiteVisitor
from app.deepline_client import DeeplineError, execute_tool

IP_TO_COMPANY_TOOL_ID = "deepline_ip_to_company_find_company_by_ip"

# How long a resolved company lookup for a given IP is considered still fresh -- avoids calling
# Deepline again for every single page view from the same visitor's browsing session, without
# needing a separate cache table (the WebsiteVisitor rows themselves double as the cache).
IP_LOOKUP_CACHE_HOURS = 24

PRIVATE_IP_PREFIXES = ("10.", "127.", "192.168.", "169.254.")


def _is_private_ip(ip: str) -> bool:
    """Private/loopback/link-local ranges never resolve to a real company (confirmed by the
    tool's own schema description) -- skip the Deepline call entirely for these rather than
    spending a call (even a free one) on a guaranteed no-match, and rather than logging a
    confusing "lookup_failed" for what's actually an expected local/internal request."""
    if ip.startswith(PRIVATE_IP_PREFIXES):
        return True
    if ip.startswith("172."):
        try:
            second_octet = int(ip.split(".")[1])
            return 16 <= second_octet <= 31
        except (IndexError, ValueError):
            return False
    return ip in ("localhost", "::1")


def extract_client_ip(headers: dict, fallback_ip: str | None) -> str:
    """Render (and most PaaS hosts) sit behind a proxy -- the real visitor IP arrives via
    X-Forwarded-For (first entry in the comma-separated chain, per standard proxy convention),
    not the raw connection IP, which would just be the proxy's own address."""
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return fallback_ip or "unknown"


def _lookup_company(ip: str) -> dict:
    """Real Deepline call, grounded -- never fabricates a company. Returns a dict with
    company_lookup_status always set, so a caller never has to guess whether a null company
    means "no match" or "we couldn't check."""
    if _is_private_ip(ip):
        return {"company_lookup_status": "no_match"}
    try:
        result = execute_tool(IP_TO_COMPANY_TOOL_ID, {"ip": ip})
    except DeeplineError:
        return {"company_lookup_status": "lookup_failed"}

    company = result.get("company") or {}
    geo = result.get("geoIP") or {}
    if not company.get("name"):
        return {"company_lookup_status": "no_match"}

    return {
        "company_lookup_status": "resolved",
        "company_name": company.get("name"),
        "company_domain": company.get("domain"),
        "company_website": company.get("website"),
        "company_industry": company.get("industry"),
        "company_employee_range": company.get("employee_range"),
        "company_city": geo.get("city"),
        "company_state": geo.get("state"),
        "company_country": geo.get("country"),
        "is_fuzzy_match": bool(result.get("fuzzy")),
    }


def record_visit(db: Session, tenant_id: int, ip_address: str, page_path: str | None, referrer: str | None, user_agent: str | None) -> WebsiteVisitor:
    """The single entry point the /inbound/visitor-ping route calls. Reuses a recent resolution
    for this exact IP if one exists (see IP_LOOKUP_CACHE_HOURS) rather than re-querying Deepline
    for every page view in the same browsing session -- the visit itself is still logged as its
    own row either way, only the company lookup is reused."""
    cutoff = datetime.utcnow() - timedelta(hours=IP_LOOKUP_CACHE_HOURS)
    recent = (
        db.query(WebsiteVisitor)
        .filter(WebsiteVisitor.tenant_id == tenant_id)
        .filter(WebsiteVisitor.ip_address == ip_address)
        .filter(WebsiteVisitor.created_at >= cutoff)
        .filter(WebsiteVisitor.company_lookup_status.isnot(None))
        .order_by(WebsiteVisitor.created_at.desc())
        .first()
    )
    if recent and recent.company_lookup_status in ("resolved", "no_match"):
        lookup = {
            "company_lookup_status": recent.company_lookup_status,
            "company_name": recent.company_name,
            "company_domain": recent.company_domain,
            "company_website": recent.company_website,
            "company_industry": recent.company_industry,
            "company_employee_range": recent.company_employee_range,
            "company_city": recent.company_city,
            "company_state": recent.company_state,
            "company_country": recent.company_country,
            "is_fuzzy_match": recent.is_fuzzy_match,
        }
    else:
        lookup = _lookup_company(ip_address)

    visit = WebsiteVisitor(
        tenant_id=tenant_id, ip_address=ip_address, page_path=page_path,
        referrer=referrer, user_agent=user_agent, **lookup,
    )
    db.add(visit)
    db.commit()
    return visit


def get_recent_visitors(db: Session, tenant_id: int, limit: int = 50, resolved_only: bool = True) -> list[WebsiteVisitor]:
    query = db.query(WebsiteVisitor).filter(WebsiteVisitor.tenant_id == tenant_id)
    if resolved_only:
        query = query.filter(WebsiteVisitor.company_lookup_status == "resolved")
    return query.order_by(WebsiteVisitor.created_at.desc()).limit(limit).all()
