"""Learning / Evaluation Foundation -- Batch 7. Observes and records outcomes; does NOT
automatically modify offering configuration, strategy rules, messaging rules, qualification
rules, or outreach sequences (Part B's explicit boundary -- optimization is future work).

    SalesOutcome (outcome.py)     -- observed reply events, reusing existing linkedin_reply
                                      InterpretedSignal classification, never re-interpreting
    MessageDraft (message_draft.py) -- controlled, LLM-assisted message generation +
                                      explicit human-approval lifecycle. NEVER auto-approved,
                                      NEVER sent -- see message_draft.py's own module docstring
                                      for the hard execution boundary.
    evaluation.py                  -- pure read-only readout, no ML/optimization.

Hard execution boundary, same discipline as app/gtm_os/sales/: nothing in this package imports
send_message_to_prospect/add_single_prospect, app/smartlead_client.py, app/outreach/smartlead.py,
or any calendar-booking/CRM-write code. Verified by an explicit import-boundary test."""
