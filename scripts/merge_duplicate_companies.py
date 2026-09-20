"""Merge a duplicate company row into the one that should survive -- same tenant only.

WHY THIS EXISTS. Production 2026-09-19: Lumion existed twice inside tenant 2, as lumion.ai
(id 710) and hi.switchy.io (id 1016), both carrying the SAME linkedin_url
(linkedin.com/company/joinlumion). The link-shortener host did not match the real domain, so the
already-seen-domain dedup could not tell they were the same company and discovery paid to find
it again. The work then split across the two rows -- 710 held 3 contacts, 8 signals and an
opportunity, 1016 held the ICP match and the headcount -- so neither row was complete and 9
signals were stranded as ambiguous because no single row could be matched.

_normalize_domain() now rejects shortener hosts, so this specific cause cannot recur. This script
repairs what it already created, and stays available for duplicates arriving by other routes.

SAFETY:
  * Same tenant only. Production has 91 duplicate-domain groups and ALL of them are cross-tenant:
    a partner and Elephant Edge legitimately each hold their own copy of a company. Merging
    across tenants would breach the boundary partner tenants exist to enforce, so it is refused.
  * Never merges a company whose contacts have been pushed to a campaign -- reassigning a
    contact that is already in someone's sequence is not a data fix, it is a live-outreach
    change. Refused, loudly, rather than handled cleverly.
  * Moves references rather than deleting them; only fills a field on the survivor that is
    empty, never overwrites one that is set.
  * Dry run by default.

Usage:
    python scripts/merge_duplicate_companies.py --keep 710 --drop 1016
    python scripts/merge_duplicate_companies.py --keep 710 --drop 1016 --apply
"""
import argparse
import sys

from sqlalchemy.orm import Session

from app.db.models import Batch, Company

# Every table with a foreign key to companies.id, enumerated from the production schema
# (information_schema, 2026-09-20). Explicit rather than reflected, so adding a new
# company-referencing table is a deliberate decision here rather than a silent behaviour change.
#
# Completeness matters and is NOT assumed: the first run of this script was refused by a foreign
# key on investigation_objectives.target_company_id, which was missing from an earlier version of
# this list. That refusal is the design working -- apply_merge deletes with raw SQL precisely so
# a missed reference raises instead of being silently cascaded away by the ORM. If this list ever
# falls behind the schema again, the symptom is a loud FK error, not lost data.
_REFERENCING = [
    ("contacts", "company_id"),
    ("icp_matches", "company_id"),
    ("gtm_signals", "company_id"),
    ("interpreted_signals", "company_id"),
    ("problem_hypotheses", "company_id"),
    ("demand_hypotheses", "company_id"),
    ("investigation_objectives", "target_company_id"),
    ("opportunities", "company_id"),
    ("proposals", "company_id"),
    ("partner_company_recommendations", "company_id"),
    ("calendar_bookings", "outcome_company_id"),
    ("scores", "company_id"),
    ("signals", "company_id"),          # V1's own signals table, distinct from gtm_signals
    ("enrichments", "company_id"),
]

# Filled on the survivor only where it is currently empty.
_FILLABLE_FIELDS = [
    "domain", "industry", "employee_count", "location", "linkedin_url",
    "estimated_revenue_lower_usd", "estimated_revenue_higher_usd",
    "hiring_signal_role", "hiring_signal_strength", "hiring_signal_reasoning",
    "resolved_offering_name",
]


def _tenant_of(db: Session, company: Company) -> int | None:
    row = db.query(Batch.tenant_id).filter(Batch.id == company.batch_id).first()
    return row[0] if row else None


def plan_merge(db: Session, keep_id: int, drop_id: int) -> dict:
    """Analysis only -- no writes. Raises ValueError on anything unsafe."""
    keep = db.query(Company).filter(Company.id == keep_id).first()
    drop = db.query(Company).filter(Company.id == drop_id).first()
    if not keep or not drop:
        raise ValueError(f"company not found (keep={keep_id} drop={drop_id})")
    if keep.id == drop.id:
        raise ValueError("keep and drop are the same company")

    keep_tenant, drop_tenant = _tenant_of(db, keep), _tenant_of(db, drop)
    if keep_tenant != drop_tenant:
        raise ValueError(
            f"refusing to merge across tenants (keep is tenant {keep_tenant}, drop is tenant "
            f"{drop_tenant}) -- cross-tenant copies of a company are legitimate and separate"
        )

    pushed = db.execute(
        __import__("sqlalchemy").text(
            "SELECT COUNT(*) FROM contacts ct JOIN campaign_pushes p ON p.contact_id = ct.id "
            "WHERE ct.company_id = :cid"
        ),
        {"cid": drop.id},
    ).scalar()
    if pushed:
        raise ValueError(
            f"refusing to merge: company {drop.id} has {pushed} contact(s) already pushed to a "
            f"campaign -- reassigning a contact mid-sequence is a live-outreach change, not a "
            f"data fix"
        )

    moves = {}
    for table, column in _REFERENCING:
        try:
            n = db.execute(
                __import__("sqlalchemy").text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :cid"),
                {"cid": drop.id},
            ).scalar()
        except Exception:
            db.rollback()
            continue
        if n:
            moves[table] = n

    fills = {
        field: getattr(drop, field)
        for field in _FILLABLE_FIELDS
        if getattr(keep, field, None) in (None, "") and getattr(drop, field, None) not in (None, "")
    }

    return {"keep": keep, "drop": drop, "tenant_id": keep_tenant, "moves": moves, "fills": fills}


def apply_merge(db: Session, plan: dict) -> dict:
    from sqlalchemy import text

    keep, drop = plan["keep"], plan["drop"]
    for table, _n in plan["moves"].items():
        column = dict(_REFERENCING)[table]
        db.execute(
            text(f"UPDATE {table} SET {column} = :keep WHERE {column} = :drop"),
            {"keep": keep.id, "drop": drop.id},
        )
    for field, value in plan["fills"].items():
        setattr(keep, field, value)

    drop_id = drop.id
    db.flush()
    # Raw DELETE rather than db.delete(): the ORM cascade would quietly pull in every mapped
    # relationship on Company (including V1's `signals` table) and delete rows this script never
    # examined. A direct delete is precise, and if some table still references this company the
    # foreign key raises -- which is the outcome we want, because it means a reference is missing
    # from _REFERENCING and should be added deliberately rather than silently destroyed.
    db.expunge(drop)
    db.execute(text("DELETE FROM companies WHERE id = :cid"), {"cid": drop_id})
    db.commit()
    return {"moved": plan["moves"], "filled": list(plan["fills"]), "deleted_company_id": drop_id}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=int, required=True, help="company id that survives")
    parser.add_argument("--drop", type=int, required=True, help="company id merged away")
    parser.add_argument("--apply", action="store_true", help="perform the writes (default: dry run)")
    args = parser.parse_args()

    from app.db.session import SessionLocal, engine

    print(f"database: {engine.url.host or engine.url.database}")
    db = SessionLocal()
    try:
        plan = plan_merge(db, args.keep, args.drop)
        keep, drop = plan["keep"], plan["drop"]
        print(f"\ntenant {plan['tenant_id']}")
        print(f"  KEEP  {keep.id}  {keep.name!r}  domain={keep.domain!r}")
        print(f"  DROP  {drop.id}  {drop.name!r}  domain={drop.domain!r}")
        print(f"\nreferences to move: {plan['moves'] or 'none'}")
        print(f"fields to fill on the survivor: {plan['fills'] or 'none'}")

        if not args.apply:
            print("\nDRY RUN -- nothing written. Re-run with --apply.")
            return 0

        result = apply_merge(db, plan)
        print(f"\nAPPLIED: {result}")
        return 0
    except ValueError as e:
        print(f"\nREFUSED: {e}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
