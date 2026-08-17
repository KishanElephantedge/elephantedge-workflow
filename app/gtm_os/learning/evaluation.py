"""Learning/Evaluation readout -- Batch 7 Part M. Pure, read-only observation and reporting.

Explicitly NOT: ML optimization, automated strategy changes, reinforcement learning, revenue
forecasting, or performance scoring across insufficient data (Part M's own boundary) -- this
module only counts and cross-references what already exists, never modifies anything."""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.learning.outcome import SalesOutcome
from app.gtm_os.strategy.strategy import GtmStrategy


def evaluate_learning_readout(db: Session, tenant_id: int) -> dict:
    """Answers the exact questions Part M names, and nothing more. Every count is a real,
    tenant-scoped query -- never estimated or extrapolated."""
    prepared_messages = db.query(MessageDraft).filter(MessageDraft.tenant_id == tenant_id).count()
    approved_messages = db.query(MessageDraft).filter(MessageDraft.tenant_id == tenant_id, MessageDraft.status == "approved").count()
    ready_for_review = db.query(MessageDraft).filter(MessageDraft.tenant_id == tenant_id, MessageDraft.status == "ready_for_review").count()

    outcome_rows = (
        db.query(SalesOutcome.outcome_category, func.count(SalesOutcome.id))
        .filter(SalesOutcome.tenant_id == tenant_id)
        .group_by(SalesOutcome.outcome_category)
        .all()
    )
    outcomes_by_category = {category: count for category, count in outcome_rows}
    total_replies = sum(outcomes_by_category.values())

    outcomes_with_opportunity = db.query(SalesOutcome).filter(SalesOutcome.tenant_id == tenant_id, SalesOutcome.opportunity_id.isnot(None)).count()

    # Which strategy_type produced traceable outcomes -- a real cross-reference, computed via
    # the existing FK chain (SalesOutcome -> Opportunity -> latest GtmStrategy), not stored
    # redundantly anywhere.
    strategy_type_outcome_counts: dict[str, int] = {}
    linked_outcomes = (
        db.query(SalesOutcome)
        .filter(SalesOutcome.tenant_id == tenant_id, SalesOutcome.opportunity_id.isnot(None))
        .all()
    )
    for outcome in linked_outcomes:
        latest_strategy = (
            db.query(GtmStrategy)
            .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id == outcome.opportunity_id)
            .order_by(GtmStrategy.id.desc())
            .first()
        )
        key = latest_strategy.strategy_type if latest_strategy else "unknown"
        strategy_type_outcome_counts[key] = strategy_type_outcome_counts.get(key, 0) + 1

    return {
        "prepared_messages_count": prepared_messages,
        "ready_for_review_count": ready_for_review,
        "approved_messages_count": approved_messages,
        "replies_count": total_replies,
        "outcomes_by_category": outcomes_by_category,
        "outcomes_linked_to_opportunity_count": outcomes_with_opportunity,
        "outcomes_by_strategy_type": strategy_type_outcome_counts,
    }
