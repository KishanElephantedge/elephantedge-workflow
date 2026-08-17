"""GTM Motion Engine -- Batch 10. Answers "given this company's ICP + offering match, which
GTM motion (outbound/inbound/partnerships/events) should we recommend?" -- a strategy/allocation
question, not merely historical channel-performance reporting (see gtm_motion.py's own module
docstring for the A/B distinction this batch deliberately stays on the "A" side of).

    Company -> ICPMatch (Batch 8) -> ICP x Offering candidate_match (Batch 9) -> GTM Motion

Deterministic, configuration-driven, no LLM. Reuses match_offerings_for_company() (Batch 9)
unmodified as its own input -- does not re-derive ICP/offering matching itself."""
