"""The daily run: the active plays, one after another, under the one combined daily budget.

Hiring (Play B) is the only active play for now -- decided 2026-09-27: prove the hiring objective
end to end first, then add post engagement (Play A) back here. Both the scheduler (app/main.py) and
the manual trigger route call this, so they can never do different work."""
from sqlalchemy.orm import Session


def run_active_plays(db: Session, tenant_id: int) -> dict:
    from app.gtm_os.plays.hiring import run_play_b

    result = run_play_b(db, tenant_id)
    return {"status": result.get("status"), "plays": {"hiring": result}}
