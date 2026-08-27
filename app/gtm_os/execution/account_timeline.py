"""Account Event Timeline (2026-08-27) -- a chronological, per-company view composed entirely
from existing, already-real, append-only tables. Deterministic, read-only, no LLM -- same
"compose, never re-derive" discipline as execution_readiness.py's _build_pipeline_item().

FIELD AUDIT (this session): a real timeline is buildable today for signal detection, signal
interpretation, problem/demand hypothesis evidence, strategy versions (each change is its own
row, never overwritten), message drafts, campaign pushes/events, sales outcomes, and meeting
outcomes -- every one of these is genuinely append-only and joinable to a company.

WHAT THIS DELIBERATELY DOES NOT DO: it does not reconstruct Opportunity/MessageDraft
status-TRANSITION history (only the latest status is ever stored; earlier values are overwritten
in place, not logged), and it does not fabricate the three deck-example events that have zero
real backing in this schema -- phone-number enrichment (Contact has no phone field), an explicit
"decision-maker matched" moment (only inferable from Contact.created_at, not a real named event),
and an account-attributed escalation (Notification exists but is scoped to batch_id/run_id, not
company_id). UNTRACKED_EVENT_TYPES below names these explicitly so the UI can say "not tracked
yet" instead of silently looking complete."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import CampaignEvent, CampaignPush, CalendarBooking, Contact
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis, DemandHypothesisEvidence
from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis, ProblemHypothesisEvidence
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.learning.outcome import SalesOutcome
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.strategy.strategy import GtmStrategy

# Named here so the UI can render an explicit "not tracked" note rather than a fabricated entry
# or a silent gap -- see module docstring for why each of these has zero real backing today.
UNTRACKED_EVENT_TYPES = [
    {"event_type": "phone_number_enrichment", "reason": "Contact has no phone field in this schema yet."},
    {"event_type": "decision_maker_matched", "reason": "Only inferable from Contact.created_at, not a real named event."},
    {"event_type": "account_escalation", "reason": "Notification exists but is scoped to batch_id/run_id, not company_id, so it can't be attributed to this account yet."},
]

# Real status-transition history is not stored anywhere for these -- only the LATEST value
# survives an update. The timeline still shows the record's own creation as a real event; it
# never implies the current status is when that status was reached.
_NO_TRANSITION_HISTORY_NOTE = "only the current value is known; earlier changes were not logged"


def get_account_event_timeline(db: Session, tenant_id: int, company_id: int) -> dict:
    """Returns {"events": [...], "untracked_event_types": [...]}, events sorted newest first.
    Each event: {event_type, occurred_at, title, detail, source_type, source_id}. `occurred_at`
    is None only when the real underlying source has no timestamp of its own -- never invented."""
    events = []

    for signal in db.query(GtmSignal).filter(GtmSignal.tenant_id == tenant_id, GtmSignal.company_id == company_id):
        events.append({
            "event_type": "signal_detected",
            "occurred_at": signal.observed_at or signal.captured_at,
            "title": f"Signal detected from {signal.source}",
            "detail": (signal.extracted_info or {}).get("title") or signal.signal_type,
            "source_type": "gtm_signal",
            "source_id": signal.id,
        })

    interpreted_ids_for_company = []
    for interpreted in db.query(InterpretedSignal).filter(InterpretedSignal.tenant_id == tenant_id, InterpretedSignal.company_id == company_id):
        interpreted_ids_for_company.append(interpreted.id)
        events.append({
            "event_type": "signal_interpreted",
            "occurred_at": interpreted.observed_at or interpreted.created_at,
            "title": f"Interpreted as {interpreted.event_type}",
            "detail": interpreted.business_change,
            "source_type": "interpreted_signal",
            "source_id": interpreted.id,
        })

    for problem in db.query(ProblemHypothesis).filter(ProblemHypothesis.tenant_id == tenant_id, ProblemHypothesis.company_id == company_id):
        events.append({
            "event_type": "problem_hypothesis_opened",
            "occurred_at": problem.first_observed_at,
            "title": "Problem hypothesis opened",
            "detail": problem.problem_statement,
            "source_type": "problem_hypothesis",
            "source_id": problem.id,
        })
        for evidence in db.query(ProblemHypothesisEvidence).filter(ProblemHypothesisEvidence.tenant_id == tenant_id, ProblemHypothesisEvidence.problem_hypothesis_id == problem.id):
            events.append({
                "event_type": "problem_evidence_linked",
                "occurred_at": evidence.added_at,
                "title": f"Evidence linked ({evidence.role})",
                "detail": evidence.note,
                "source_type": "problem_hypothesis_evidence",
                "source_id": evidence.id,
            })

    for demand in db.query(DemandHypothesis).filter(DemandHypothesis.tenant_id == tenant_id, DemandHypothesis.company_id == company_id):
        events.append({
            "event_type": "demand_hypothesis_opened",
            "occurred_at": demand.first_observed_at,
            "title": "Demand hypothesis opened",
            "detail": demand.demand_statement,
            "source_type": "demand_hypothesis",
            "source_id": demand.id,
        })
        for evidence in db.query(DemandHypothesisEvidence).filter(DemandHypothesisEvidence.tenant_id == tenant_id, DemandHypothesisEvidence.demand_hypothesis_id == demand.id):
            events.append({
                "event_type": "demand_evidence_linked",
                "occurred_at": evidence.added_at,
                "title": f"Evidence linked ({evidence.role})",
                "detail": evidence.note,
                "source_type": "demand_hypothesis_evidence",
                "source_id": evidence.id,
            })

    opportunity_ids_for_company = []
    for opp in db.query(Opportunity).filter(Opportunity.tenant_id == tenant_id, Opportunity.company_id == company_id):
        opportunity_ids_for_company.append(opp.id)
        events.append({
            "event_type": "opportunity_created",
            "occurred_at": opp.created_at,
            "title": "Opportunity created",
            "detail": f"{opp.opportunity_statement} (current status: {opp.status} -- {_NO_TRANSITION_HISTORY_NOTE})",
            "source_type": "opportunity",
            "source_id": opp.id,
        })

    if opportunity_ids_for_company:
        for strategy in db.query(GtmStrategy).filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id.in_(opportunity_ids_for_company)):
            events.append({
                "event_type": "strategy_set",
                "occurred_at": strategy.created_at,
                "title": f"Strategy: {strategy.strategy_type}",
                "detail": strategy.recommended_next_step,
                "source_type": "gtm_strategy",
                "source_id": strategy.id,
            })

        for draft in db.query(MessageDraft).filter(MessageDraft.tenant_id == tenant_id, MessageDraft.opportunity_id.in_(opportunity_ids_for_company)):
            events.append({
                "event_type": "message_drafted",
                "occurred_at": draft.created_at,
                "title": f"Message drafted ({draft.channel or 'unknown channel'}, {draft.message_role})",
                "detail": f"current status: {draft.status} -- {_NO_TRANSITION_HISTORY_NOTE}",
                "source_type": "message_draft",
                "source_id": draft.id,
            })

        for outcome in db.query(SalesOutcome).filter(SalesOutcome.tenant_id == tenant_id, SalesOutcome.opportunity_id.in_(opportunity_ids_for_company)):
            events.append({
                "event_type": "sales_outcome_observed",
                "occurred_at": outcome.observed_at or outcome.created_at,
                "title": f"Sales outcome: {outcome.outcome_category}",
                "detail": outcome.reasoning_note,
                "source_type": "sales_outcome",
                "source_id": outcome.id,
            })
    elif interpreted_ids_for_company:
        # An outcome can exist for an InterpretedSignal that never produced an Opportunity (e.g.
        # a reply that wasn't strong enough to open one) -- fall back to the InterpretedSignal
        # link so real outcome activity for this company is never silently dropped just because
        # no Opportunity happens to exist yet.
        for outcome in db.query(SalesOutcome).filter(SalesOutcome.tenant_id == tenant_id, SalesOutcome.interpreted_signal_id.in_(interpreted_ids_for_company)):
            events.append({
                "event_type": "sales_outcome_observed",
                "occurred_at": outcome.observed_at or outcome.created_at,
                "title": f"Sales outcome: {outcome.outcome_category}",
                "detail": outcome.reasoning_note,
                "source_type": "sales_outcome",
                "source_id": outcome.id,
            })

    contact_ids_for_company = [c.id for c in db.query(Contact.id).filter(Contact.company_id == company_id)]
    if contact_ids_for_company:
        for push in db.query(CampaignPush).filter(CampaignPush.contact_id.in_(contact_ids_for_company)):
            events.append({
                "event_type": "campaign_pushed",
                "occurred_at": push.pushed_at,
                "title": f"Pushed to campaign ({push.status})",
                "detail": push.campaign_label,
                "source_type": "campaign_push",
                "source_id": push.id,
            })
        for event in db.query(CampaignEvent).filter(CampaignEvent.contact_id.in_(contact_ids_for_company)):
            events.append({
                "event_type": "campaign_event",
                "occurred_at": event.received_at,
                "title": event.event_type or "Campaign event",
                "detail": None,
                "source_type": "campaign_event",
                "source_id": event.id,
            })

    for booking in db.query(CalendarBooking).filter(CalendarBooking.outcome_company_id == company_id):
        events.append({
            "event_type": "meeting_outcome_recorded",
            "occurred_at": booking.outcome_recorded_at,
            "title": f"Meeting outcome: {booking.outcome_status}" if booking.outcome_status else "Meeting outcome cleared",
            "detail": f"${booking.outcome_amount_usd:,.0f}" if booking.outcome_status == "won" and booking.outcome_amount_usd else booking.outcome_reason,
            "source_type": "calendar_booking",
            "source_id": booking.id,
        })

    events.sort(key=lambda e: e["occurred_at"] or datetime.min, reverse=True)
    return {"events": events, "untracked_event_types": UNTRACKED_EVENT_TYPES}
