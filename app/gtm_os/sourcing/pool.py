"""Our own shared account pool: a company bought once, usable by any partner whose ICP matches it.

WHY. We have ~10-15 partners today and growing, and B2B ICPs overlap heavily -- a Professional
Services company at 11-50 staff can genuinely fit three different partners' criteria. A provider
query has no memory of that: it is billed again for the same company for every partner who asks.
Our own database does have that memory, for free.

So the first step of sourcing is a query against `company_pool`, not a provider. Only the
shortfall after that gets bought.

STALENESS IS HANDLED HONESTLY, not ignored. A company's headcount drifts, and the same lesson that
shaped the free size-band check elsewhere in this codebase applies here: a pool row too old to
trust is not silently delivered as a fresh match, and it is not silently discarded either -- it is
excluded from pool-first delivery and left for a real run to re-verify and refresh.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.gtm_os.sourcing import atoms as A
from app.gtm_os.sourcing.models import CompanyPool, PoolDelivery

# Beyond this, a pool row's headcount/revenue are not trusted for a fresh delivery without
# re-verification -- the same "self-declared data can be stale" lesson as the free public-page
# size check elsewhere in this codebase (BePresent: declared 2-10, real count was 31).
STALE_AFTER = timedelta(days=30)

_SHORTENER_DOMAINS = {"hubs.li", "switchy.io", "bit.ly", "lnkd.in", "t.co", "tinyurl.com"}


def _normalize_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    domain = domain.lower().strip().removeprefix("www.")
    return None if any(domain == d or domain.endswith(f".{d}") for d in _SHORTENER_DOMAINS) else domain


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def identity_key(linkedin_url: str | None = None, domain: str | None = None,
                 name: str | None = None) -> str | None:
    """linkedin_company_id > domain > normalized name, in that order -- LinkedIn URL is the most
    stable identity both Icypeas and HarvestAPI key their own results on."""
    if linkedin_url:
        slug = linkedin_url.rstrip("/").rsplit("/company/", 1)[-1].split("?")[0].lower()
        if slug and slug != linkedin_url.lower():
            return f"li:{slug}"
    normalized_domain = _normalize_domain(domain)
    if normalized_domain:
        return f"domain:{normalized_domain}"
    normalized_name = _normalize_name(name or "")
    return f"name:{normalized_name}" if normalized_name else None


def record(db: Session, *, linkedin_url: str | None, domain: str | None, name: str,
          industry: str | None, headcount: int | None, revenue_low_usd: int | None,
          revenue_high_usd: int | None, location: str | None, country: str | None,
          source_provider: str, source_endpoint: str | None, cost_usd: float | None) -> None:
    """Add or refresh one real company in the shared pool. Free: called on data already paid for,
    never on its own fetch. Never raises -- pool-building must not be able to break a paid run."""
    key = identity_key(linkedin_url, domain, name)
    if key is None:
        return
    try:
        row = db.query(CompanyPool).filter(CompanyPool.identity_key == key).first()
        if row is None:
            db.add(CompanyPool(
                identity_key=key, linkedin_url=linkedin_url, domain=_normalize_domain(domain),
                name=name, industry_raw=industry, headcount=headcount,
                revenue_low_usd=revenue_low_usd, revenue_high_usd=revenue_high_usd,
                location=location, country=country, source_provider=source_provider,
                source_endpoint=source_endpoint, cost_usd=cost_usd,
            ))
        else:
            # Refresh rather than skip: a company re-surfaced by a later, real paid call is the
            # cheapest possible way to learn it has grown, moved, or changed industry.
            row.headcount = headcount if headcount is not None else row.headcount
            row.revenue_low_usd = revenue_low_usd if revenue_low_usd is not None else row.revenue_low_usd
            row.revenue_high_usd = revenue_high_usd if revenue_high_usd is not None else row.revenue_high_usd
            row.industry_raw = industry or row.industry_raw
            row.last_verified_at = datetime.utcnow()
        db.commit()
    except Exception:  # noqa: BLE001 -- pool-building must never break the paid run it rides on
        db.rollback()


def _headcount_matches(row: CompanyPool, atom: A.Atom) -> bool:
    if row.headcount is None:
        return False          # unknown -- a pool match must be POSITIVE evidence, not an absence
    lo, hi = atom.value
    return (lo is None or row.headcount >= lo) and (hi is None or row.headcount <= hi)


def _revenue_overlaps(row: CompanyPool, atom: A.Atom) -> bool:
    if row.revenue_low_usd is None and row.revenue_high_usd is None:
        return False
    lo, hi = atom.value
    if hi is not None and row.revenue_low_usd is not None and row.revenue_low_usd > hi:
        return False
    if lo is not None and row.revenue_high_usd is not None and row.revenue_high_usd < lo:
        return False
    return True


def _geography_matches(row: CompanyPool, atom: A.Atom) -> bool:
    if not row.location and not row.country:
        return False
    haystack = f"{row.location or ''} {row.country or ''}".lower()
    return any(str(v).lower() in haystack for v in (atom.value or []))


@dataclass
class PoolMatch:
    row: CompanyPool
    fresh: bool


def find_matches(db: Session, tenant_id: int, play: str, icp: dict, limit: int,
                 now: datetime | None = None) -> list[PoolMatch]:
    """Companies already in the pool that match this ICP and have never been delivered to this
    tenant for this play. Free: no provider call, pure database.

    Only MUST-HAVE atoms with a real way to check them are enforced here (headcount, revenue,
    geography) -- industry is deliberately NOT matched on `industry_raw` alone, since that field
    holds whatever a PROVIDER called it, not what this tenant's resolved taxonomy says. A pool
    match on industry without that resolution would repeat exactly the "Professional Services
    matched nothing" mistake, just against our own table instead of a provider's.
    """
    now = now or datetime.utcnow()
    icp_atoms = A.decompose_icp(icp)
    delivered_ids = {d.company_pool_id for d in
                     db.query(PoolDelivery).filter(PoolDelivery.tenant_id == tenant_id,
                                                    PoolDelivery.play == play).all()}

    checks = []
    for atom in icp_atoms.must_haves():
        if atom.key == A.HEADCOUNT:
            checks.append((atom, _headcount_matches))
        elif atom.key == A.REVENUE:
            checks.append((atom, _revenue_overlaps))
        elif atom.key == A.GEOGRAPHY:
            checks.append((atom, _geography_matches))

    if not checks:
        return []        # nothing checkable for free -- do not guess a match

    matches: list[PoolMatch] = []
    # A simple scan rather than a filtered query: the pool is not yet large enough for this to
    # matter, and a scan keeps the matching logic in Python next to the atoms it mirrors rather
    # than duplicated as SQL. Revisit with an indexed query once the pool's real size says to.
    for row in db.query(CompanyPool).order_by(CompanyPool.last_verified_at.desc()).all():
        if row.id in delivered_ids:
            continue
        if not all(check(row, atom) for atom, check in checks):
            continue
        fresh = (now - (row.last_verified_at or row.fetched_at)) <= STALE_AFTER
        matches.append(PoolMatch(row=row, fresh=fresh))
        if len(matches) >= limit:
            break
    return matches


def to_search_row(row: CompanyPool) -> dict:
    """A pool row -> the same shape Icypeas' own search results come in, so a pool-sourced company
    can run through the IDENTICAL per-company processing (_process_icypeas_company) as a freshly
    bought one: same vendor/government checks, same free decision-maker attempt, same revenue
    storage. Delivering from the pool is not a separate code path, just a different source of the
    same shape."""
    out: dict = {"url": row.linkedin_url or "", "name": row.name, "industry": row.industry_raw,
                "numberOfEmployees": row.headcount, "address": row.location}
    if row.revenue_low_usd is not None or row.revenue_high_usd is not None:
        out["estimatedRevenuRange"] = {
            "estimatedMinRevenue": {"amount": row.revenue_low_usd, "unit": "ACTUAL"},
            "estimatedMaxRevenue": {"amount": row.revenue_high_usd, "unit": "ACTUAL"},
        }
    return out


def mark_delivered(db: Session, tenant_id: int, company_pool_id: int, play: str) -> None:
    exists = (db.query(PoolDelivery)
              .filter(PoolDelivery.tenant_id == tenant_id, PoolDelivery.company_pool_id == company_pool_id,
                      PoolDelivery.play == play).first())
    if exists is None:
        db.add(PoolDelivery(tenant_id=tenant_id, company_pool_id=company_pool_id, play=play))
        db.commit()
