"""Link already-purchased signals to companies we already own -- for $0.

WHY THIS EXISTS. Measured on production 2026-09-19: 352 of 506 linkedin_job signals and 634 of
642 linkedin_post signals had no company_id. Most were not genuinely unresolvable -- they were
recorded "unresolved" because company_resolution.py's PAID Deepline tier was budget-blocked
(company_resolution.py:176 returns unresolved on budget_guard_blocked). The evidence was bought,
then stranded.

A large slice of that backlog can be joined with no provider call at all: the signal already
carries company_name_raw, and we already own a company with that exact name. That is the same
free "exact_name_match" tier company_resolution.py itself defines -- this script just applies it
in bulk to the backlog instead of one-at-a-time inside a paid sweep.

SAFETY -- why this does not guess:
  * Exact, case/whitespace-insensitive name equality only. No fuzzy matching, no domain
    inference, no LLM. A wrong link is worse than no link: it attributes real buying evidence to
    the wrong account and can put a real person into the wrong campaign.
  * Production has DUPLICATE company rows for the same name (confirmed 2026-09-19: ATALNT,
    Infisical and Lumion each appear twice, with the contacts on one row and the ICP match on
    the other). When a name matches more than one company, this records "ambiguous" and links
    NOTHING, exactly as company_resolution.py does. Those need de-duplicating first.
  * Never overwrites an existing company_id.
  * Dry-run by default. It prints what it would do and writes nothing unless --apply is passed.

Usage:
    python scripts/backfill_orphaned_signal_links.py                 # dry run, writes nothing
    python scripts/backfill_orphaned_signal_links.py --apply         # perform the writes
    python scripts/backfill_orphaned_signal_links.py --source linkedin_job
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Company
from app.gtm_os.intelligence.signal import GtmSignal


def _norm(name: str | None) -> str | None:
    if not name:
        return None
    return " ".join(name.split()).strip().lower() or None


def plan_backfill(db: Session, source: str | None = None) -> dict:
    """Pure analysis -- performs no writes. Returns the linkable / ambiguous / unmatched split."""
    companies_by_name: dict[str, list[Company]] = defaultdict(list)
    for company in db.query(Company).all():
        key = _norm(company.name)
        if key:
            companies_by_name[key].append(company)

    q = db.query(GtmSignal).filter(GtmSignal.company_id.is_(None), GtmSignal.company_name_raw.isnot(None))
    if source:
        q = q.filter(GtmSignal.source == source)

    linkable: list[tuple[GtmSignal, Company]] = []
    ambiguous: list[tuple[GtmSignal, list[Company]]] = []
    unmatched = 0

    for signal in q.all():
        key = _norm(signal.company_name_raw)
        matches = companies_by_name.get(key, []) if key else []
        if len(matches) == 1:
            linkable.append((signal, matches[0]))
        elif len(matches) > 1:
            ambiguous.append((signal, matches))
        else:
            unmatched += 1

    return {"linkable": linkable, "ambiguous": ambiguous, "unmatched": unmatched}


def apply_backfill(db: Session, plan: dict) -> dict:
    """Writes the unambiguous links, and records 'ambiguous' on the rest so a later pass (or a
    human) can see they were considered and deliberately not guessed."""
    linked = 0
    for signal, company in plan["linkable"]:
        signal.company_id = company.id
        signal.company_resolution_status = "resolved"
        signal.company_resolution_method = "exact_name_match"
        signal.company_resolution_reason = (
            f"backfill: company_name_raw matched exactly one owned company (id={company.id})"
        )
        signal.company_resolved_at = datetime.utcnow()
        linked += 1

    marked = 0
    for signal, matches in plan["ambiguous"]:
        signal.company_resolution_status = "ambiguous"
        signal.company_resolution_method = "exact_name_match"
        signal.company_resolution_reason = (
            f"backfill: name matched {len(matches)} company rows "
            f"(ids={[c.id for c in matches]}) -- duplicates must be merged before this can resolve"
        )
        signal.company_resolved_at = datetime.utcnow()
        marked += 1

    db.commit()
    return {"linked": linked, "marked_ambiguous": marked}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the writes (default: dry run)")
    parser.add_argument("--source", default=None, help="restrict to one signal source, e.g. linkedin_job")
    args = parser.parse_args()

    from app.db.session import SessionLocal, engine

    print(f"database: {engine.url.host or engine.url.database}")
    db = SessionLocal()
    try:
        plan = plan_backfill(db, source=args.source)
        by_source: dict[str, int] = defaultdict(int)
        for signal, _ in plan["linkable"]:
            by_source[signal.source] += 1

        print(f"\nlinkable (exactly one owned company with that name): {len(plan['linkable'])}")
        for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
            print(f"    {src:<24} {n}")
        print(f"ambiguous (name matches >1 company, NOT linked): {len(plan['ambiguous'])}")
        for signal, matches in plan["ambiguous"][:10]:
            print(f"    {signal.company_name_raw!r} -> company ids {[c.id for c in matches]}")
        if len(plan["ambiguous"]) > 10:
            print(f"    ... and {len(plan['ambiguous']) - 10} more")
        print(f"unmatched (no owned company with that name): {plan['unmatched']}")

        if not args.apply:
            print("\nDRY RUN -- nothing written. Re-run with --apply to perform the writes.")
            return 0

        result = apply_backfill(db, plan)
        print(f"\nAPPLIED: linked={result['linked']} marked_ambiguous={result['marked_ambiguous']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
