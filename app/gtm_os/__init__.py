"""Elephant Edge GTM Operating System -- the new, separate system being built alongside the
existing outbound pipeline (app/phases/), NOT a replacement of it yet.

Boundary rule, deliberate and load-bearing: nothing in this package is imported by app/main.py,
app/phases/*, or app/routes/api.py's existing endpoints. The existing autonomous daily cycle,
its scheduler jobs, and its production behavior are completely unaffected by anything added
here. This package can be developed, tested, and even deployed with zero risk to the running
outbound pipeline, precisely because nothing wires into it yet.

Sub-packages, one concept each (per the foundation-step design -- see the step's own report for
the reasoning):

- context/       Business knowledge -- goals, offerings, ICP, TAM, GTM motions, sales
                 methodology, messaging/objections. Static, human-maintained, machine-readable.
- intelligence/  (not yet implemented) Market/demand/opportunity signal detection and
                 reasoning. Will consume context/ and external data, produce evidence.
- decisions/     (not yet implemented) The layer that turns evidence + context into a
                 prioritized decision ("what should we do next, and why").
- tools/         Real, existing pipeline functions cataloged as reusable capabilities, so a
                 future decision-making layer has a documented, stable surface to call --
                 without any agent framework, tool-calling protocol, or execution wrapper
                 imposed yet. Pure documentation-as-code at this stage.
- outcomes/      (not yet implemented) Evaluation and learning -- did a decision/action work,
                 and what should change because of it.

Populate one sub-package at a time, in the order the actual work is prioritized -- not all at
once, and never by inventing placeholder classes just to look complete."""
