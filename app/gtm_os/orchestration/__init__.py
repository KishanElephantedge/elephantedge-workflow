"""Orchestration -- coordinates EXISTING sensing/interpretation/detection capabilities in the
right order. Contains no intelligence logic of its own: it never classifies a signal, decides
demand, or makes a business judgment -- every one of those decisions already lives in
app/gtm_os/intelligence/, unchanged and unmodified by anything in this package.

Answers "what should run and in what order" -- deliberately NOT "when should it run" (that's the
scheduler's job, app/main.py, not touched by this package) and NOT "what should we do" (that's a
future AI CEO/decision layer's job, not built here). See gtm-os-orchestrator-design.md (Step 13)
for the full design this package implements."""
