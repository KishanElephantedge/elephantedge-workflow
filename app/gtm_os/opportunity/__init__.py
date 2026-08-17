"""Opportunity Engine + Offering Matcher framework -- Batch 4.

Sits one layer above Problem/Demand Intelligence (app/gtm_os/intelligence/), consuming
ProblemHypothesis/DemandHypothesis WITHOUT re-deriving their own evidence/independence logic.
Deliberately NOT part of app/gtm_os/decisions/ (the future full "AI CEO" reasoning layer, still
explicitly unimplemented) -- Opportunity/Offering-fit is intelligence commercialization, one
concrete step past Demand, not the autonomous decision-making layer decisions/ is reserved for.

Boundary (see opportunity.py's own module docstring for the full reasoning):
    PROBLEM -> DEMAND -> OPPORTUNITY (account-scoped, this package) -> OFFERING FIT (this package)
    -> GTM STRATEGY (not built)

Market-level topic/trend intelligence (app/gtm_os/content/) deliberately does NOT feed this
package in this batch -- see opportunity.py's "Market vs Account" section."""
