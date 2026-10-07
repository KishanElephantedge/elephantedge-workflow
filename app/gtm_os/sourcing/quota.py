"""How many accounts this partner still needs today, and therefore how much we are willing to buy.

WHY THIS EXISTS (2026-10-07). Clay and Apollo own a database, so returning every match costs them
nothing. We BUY each row: Icypeas bills $0.007 per REQUESTED result, so a page of 25 costs $0.175
whether we needed 25 or 3. The old code asked for a fixed `pages=N` at a fixed size of 25 with no
idea what the partner's target was, which meant:

  - paying for 25 rows to deliver 3,
  - and paying again the next day even when the partner's quota was already met.

So the executor is quota-driven, not page-driven:

    needed      = this partner's daily target - what they have already been delivered today
    page_size   = needed x attrition, capped by the provider's own maximum
    spend       = nothing at all when needed <= 0

Page size IS the spend decision for a per-result provider. That is the whole point of this module.

The attrition multiplier exists because the pipeline loses rows between fetching a company and
delivering a usable lead: already-known companies, vendor/government rejects, failed decision-maker
resolution, qualifier rejects. Deepline's own guidance is to over-provision ~1.4x at the top.
That constant is a starting point, not a measurement -- phase 8's route scorecards are what will
replace it with a real per-route, per-ICP number. It is deliberately NOT tuned by guesswork here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy.orm import Session

from app.gtm_os.plays.lead import GtmLead

# Used only when a partner has no explicit target configured. Small on purpose: over-delivering
# costs money, under-delivering costs a day, and the second is the cheaper mistake to correct.
DEFAULT_DAILY_TARGET = 10

# Until scorecards measure it per route (phase 8). See the module docstring.
DEFAULT_ATTRITION_MULTIPLIER = 1.4

# Never buy a page so small that provider overhead dominates, and never a single row at a time.
MIN_PAGE_SIZE = 5


@dataclass
class Quota:
    target: int
    delivered_today: int
    remaining: int
    page_size: int
    reason: str | None = None

    @property
    def satisfied(self) -> bool:
        return self.remaining <= 0


def daily_target(db: Session, tenant_id: int) -> int:
    """The partner's own target, configured per tenant -- it is not the same for everyone.

    Read through the feature-config layer so it is set from the admin Partner Features screen
    like every other per-partner setting, rather than being another bespoke parameter lookup.
    """
    from app.gtm_os.features import config as feature_config

    try:
        configured = feature_config.get_config(db, tenant_id, "accounts").get("daily_account_target")
    except Exception:  # noqa: BLE001 -- a config read must never stop a run; fall back to the default
        configured = None
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_TARGET
    return value if value > 0 else DEFAULT_DAILY_TARGET


def delivered_today(db: Session, tenant_id: int, play: str, now: datetime | None = None) -> int:
    """Leads actually created for this partner today by this play.

    Counts GtmLead rows rather than companies fetched: a company we fetched but rejected was not
    delivered to the partner, and must not count against their target.
    """
    now = now or datetime.utcnow()
    start = datetime.combine(now.date(), time.min)
    return (db.query(GtmLead)
            .filter(GtmLead.tenant_id == tenant_id, GtmLead.play == play,
                    GtmLead.created_at >= start)
            .count())


def plan(db: Session, tenant_id: int, play: str, provider_page_size_max: int,
         multiplier: float = DEFAULT_ATTRITION_MULTIPLIER, now: datetime | None = None) -> Quota:
    """What to buy right now, if anything."""
    target = daily_target(db, tenant_id)
    delivered = delivered_today(db, tenant_id, play, now=now)
    remaining = max(0, target - delivered)

    if remaining <= 0:
        return Quota(target=target, delivered_today=delivered, remaining=0, page_size=0,
                     reason=f"daily target of {target} already met ({delivered} delivered today)")

    wanted = math.ceil(remaining * multiplier)
    page_size = max(MIN_PAGE_SIZE, min(wanted, provider_page_size_max))
    return Quota(target=target, delivered_today=delivered, remaining=remaining, page_size=page_size)
