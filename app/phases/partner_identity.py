"""Corrects partner rows whose stored name does not belong to the person the row points at.

The GTM Partners scrape attaches a name to a LinkedIn URL, and on 21 of 183 rows those two
disagree -- some rows carry the company name ("Traction Complete", "Agiloft") instead of the
person's, and some carry a different person entirely ("Isabella (Kalender) Moore" on Samantha
Blaine's profile). Both were spotted by hand; this finds the rest.

The URL is treated as the truth and the name as the mistake, because the URL is what every other
part of the system keys off -- if they disagree, the name is the field that is decorative.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.db.models import LinkedinMonitorProfile

logger = logging.getLogger(__name__)

# Trailing credential/honorific tokens that are part of a slug but not part of a name.
_CREDENTIALS = {"ma", "mba", "phd", "md", "cpa", "cfa", "msc", "mph", "jr", "sr", "ii", "iii", "iv", "esq"}


def _slug_parts(url: str | None) -> list[str]:
    """Alphabetic segments of a /in/ slug, minus LinkedIn's trailing hex disambiguator."""
    m = re.search(r"/in/([^/?#]+)", url or "")
    if not m:
        return []
    parts = [p for p in m.group(1).split("-") if p.isalpha() and len(p) > 1 and not re.fullmatch(r"[0-9a-f]{6,}", p)]
    while parts and parts[-1].lower() in _CREDENTIALS:
        parts.pop()
    return parts


def audit_profile_names(db: Session, tenant_id: int) -> dict:
    """Splits mismatched rows into ones whose slug yields a usable name and ones that don't.

    A slug only overrides the stored name when it is hyphenated into two or more words -- a single
    blob ("woody53", "phmullen") is a vanity handle, and deriving "Woody53" from it would replace a
    correct name with a worse one. Those are reported for a human instead of being rewritten.
    """
    confident: list[dict] = []
    flagged: list[dict] = []
    for p in db.query(LinkedinMonitorProfile).filter(LinkedinMonitorProfile.tenant_id == tenant_id).all():
        parts = _slug_parts(p.linkedin_url)
        stored = [t for t in re.findall(r"[a-z]+", (p.name or "").lower()) if len(t) > 2]
        if not parts or not stored:
            continue
        flat = "".join(parts).lower()
        if any(t in flat for t in stored):
            continue  # some part of the stored name is in the slug -- a nickname or maiden name, leave it
        entry = {"id": p.id, "stored_name": p.name, "linkedin_url": p.linkedin_url}
        if len(parts) >= 2:
            confident.append({**entry, "derived_name": " ".join(w.capitalize() for w in parts)})
        else:
            flagged.append({**entry, "slug": parts[0]})
    return {"confident": confident, "flagged": flagged}


def correct_profile_names(db: Session, tenant_id: int, apply: bool = False) -> dict:
    """Renames the confidently-wrong rows. The previous name is kept, never discarded -- the
    derivation is a judgement about which field to trust, so it has to stay reversible."""
    audit = audit_profile_names(db, tenant_id)
    if not apply:
        return {**audit, "applied": 0}

    applied = 0
    for item in audit["confident"]:
        p = db.get(LinkedinMonitorProfile, item["id"])
        if p is None:
            continue
        data = dict(p.gtm_university_data or {})
        data.setdefault("_name_before_url_correction", p.name)
        p.gtm_university_data = data
        p.name = item["derived_name"]
        db.add(p)
        applied += 1
        logger.info("partner_identity: %r -> %r (id=%s)", item["stored_name"], item["derived_name"], p.id)
    db.commit()
    return {**audit, "applied": applied}
