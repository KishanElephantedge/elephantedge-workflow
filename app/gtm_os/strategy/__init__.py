"""GTM Strategy + Action Planning -- Batch 5. Sits one layer above Opportunity/Offering Fit
(app/gtm_os/opportunity/), consuming both WITHOUT re-deriving their own logic.

    Opportunity          "There appears to be an actionable commercial situation."
    Offering Fit         "Which offering might address it?"
    GTM Strategy (here)  "Given the opportunity, context, offering fit, and constraints, what
                           approach should we take?"
    Action Plan (here)   "What concrete steps should the sales team perform?"
    Execution            "Actually perform those actions." -- NOT built in this batch, or ever
                          from this package -- see strategy.py's own module docstring.

Deterministic only, no LLM (Part M) -- see strategy.py for the reasoning."""
