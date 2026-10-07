"""Turn what we rejected into what the provider never sends us again.

WHY. Today a government body or a hospital costs us money twice: once to be returned in a page we
paid for, and again in the processing that rejects it -- and then the very next run buys the same
category all over again, because the rejection taught the system nothing. Every provider we have
registered supports exclude as well as include (Icypeas: industry/type/name/keyword/location;
Prospeo: company_industry/keywords/naics/sics/location), so a rejection can be pushed UP into the
query and stop being billable at all.

This is the cheapest possible filter: it costs nothing to send, and it removes rows before the
provider counts them.

TWO SAFEGUARDS, because an over-broad exclude silently shrinks a partner's market and looks
exactly like a thin one:

  1. A value must be rejected REPEATEDLY before it is excluded. One bad company in an industry is
     not evidence about the industry -- `MIN_REJECTIONS` is the whole guard against learning the
     wrong lesson from a single row.
  2. A value the partner explicitly asked to INCLUDE is never excluded, no matter how often rows
     from it were rejected for some unrelated reason. The partner's stated intent outranks our
     inference, always.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.gtm_os.sourcing.models import IcpExclusion
from app.gtm_os.sourcing.resolution import normalize

# How many separate rejections before a value is pushed into the provider query. Two is enough to
# distinguish a pattern from an accident, and cheap to raise if a partner's market looks thin.
MIN_REJECTIONS = 2


def record_rejection(db: Session, tenant_id: int, provider: str, atom: str,
                     value: str | None, reason: str) -> None:
    """Note that a row with this value was rejected. Cheap, free, and never raises -- learning
    must not be able to break a paid run."""
    if not value or not str(value).strip():
        return
    value = str(value).strip()
    key = normalize(value)
    row = (db.query(IcpExclusion)
           .filter(IcpExclusion.tenant_id == tenant_id, IcpExclusion.provider == provider,
                   IcpExclusion.atom == atom, IcpExclusion.normalized_value == key).first())
    if row is None:
        db.add(IcpExclusion(tenant_id=tenant_id, provider=provider, atom=atom, value=value,
                            normalized_value=key, rejection_count=1, reason=reason))
    else:
        row.rejection_count = (row.rejection_count or 0) + 1
        row.reason = reason
        row.last_rejected_at = datetime.utcnow()
    db.commit()


def learned_exclusions(db: Session, tenant_id: int, provider: str, atom: str,
                       protected: set[str] | None = None,
                       min_rejections: int = MIN_REJECTIONS) -> list[str]:
    """Values this partner's runs have rejected often enough to stop paying for.

    `protected` is the partner's own include list -- never excluded, whatever we think we learned.
    """
    protected_norm = {normalize(v) for v in (protected or set())}
    rows = (db.query(IcpExclusion)
            .filter(IcpExclusion.tenant_id == tenant_id, IcpExclusion.provider == provider,
                    IcpExclusion.atom == atom,
                    IcpExclusion.rejection_count >= min_rejections,
                    IcpExclusion.suppressed_at.is_(None))
            .order_by(IcpExclusion.rejection_count.desc()).all())
    return [r.value for r in rows if r.normalized_value not in protected_norm]


def suppress(db: Session, tenant_id: int, provider: str, atom: str, value: str) -> None:
    """Stop excluding a value -- the manual override for when a learned exclusion is wrong.

    Needed because this system infers, and an inference a human disagrees with must be correctable
    without deleting the evidence that produced it.
    """
    row = (db.query(IcpExclusion)
           .filter(IcpExclusion.tenant_id == tenant_id, IcpExclusion.provider == provider,
                   IcpExclusion.atom == atom,
                   IcpExclusion.normalized_value == normalize(value)).first())
    if row is not None:
        row.suppressed_at = datetime.utcnow()
        db.commit()
