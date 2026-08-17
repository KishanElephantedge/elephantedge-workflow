"""Sales Agent Foundation -- Batch 6. Consumes GtmStrategy (app/gtm_os/strategy/) as the fixed
decision layer; this package is execution-PREPARATION only, never execution.

    GtmStrategy (decision layer, unchanged)
        |
        +-- build_sales_handoff()      -- assembles what a future Sales Agent would consume
        +-- gather_account_research()  -- read-only, provenance-preserving
        +-- evaluate_decision_maker()  -- read-only, never invents a person
        +-- prepare_message()          -- structured preparation object, NEVER a sent message
        +-- evaluate_sales_readiness() -- deterministic, explainable, no opaque score
        +-- run_sales_agent_sweep()    -- reporting sweep, writes NOTHING (see sales_agent.py)

Hard execution boundary (Part H): nothing in this package imports
send_message_to_prospect/add_single_prospect (app/salesrobot_client.py's write endpoints),
app/smartlead_client.py, app/outreach/smartlead.py, or any calendar-booking/CRM-write code.
Verified by an explicit import-boundary test, not just by convention."""
