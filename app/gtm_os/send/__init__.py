"""V2 Send -- Phase 7. The one layer in this codebase allowed to make a real outbound
provider call on behalf of an approved MessageDraft. Everything upstream (Opportunity,
GtmStrategy, MessageDraft) only ever recommends/prepares; this package is where "approved"
finally becomes a real email or LinkedIn connection request, gated by the Phase 0 control
plane and a full idempotency/safety check chain (see send.py's own docstring)."""
