"""Jobs to Be Done -- the action-guidance layer: "what should the operator work on next," derived
live from real backend state, never persisted (see jobs_to_be_done.py's own docstring for why).

Deliberately reuses, never re-implements:
    - execution_readiness.py's get_next_execution_action() for "Deal needs you"
    - Company.hot_lead/hot_lead_reasoning (hot_leads.py) for "Hot leads to review"
    - Company.decision_maker_searched_at + Contact for "Contacts to find"
    - GtmSignal/InterpretedSignal for "Worth engaging"

"Calls to make" has no real qualifying condition in this tenant's data today (SalesOutcome,
CampaignEvent, and CalendarBooking outcomes are all empty, and CampaignEvent isn't even
tenant-scoped) -- shown as an honest unavailable category, never fabricated.

No LLM calls, no writes, no autonomous execution -- this module only reads and composes."""
