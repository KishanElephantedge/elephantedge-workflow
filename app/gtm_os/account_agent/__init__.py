"""Account Agent Foundation -- Batch 12. NOT an autonomous agent (see account_agent.py's module
docstring for the hard boundary) -- a deterministic aggregation layer that answers "for this
account, what do we know, what supports it, what's missing, and what's next," built entirely by
reading the layers Batches 4-11 already built. Introduces zero new facts and zero new decision
logic beyond a single, explicitly-labeled minimum-gap ladder used only when no Opportunity/
GtmStrategy exists yet for the account (see `_derive_minimum_next_investigation`)."""
