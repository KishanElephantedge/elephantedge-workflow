"""Market/demand/opportunity intelligence.

Step 3 (2026-08-15) added the raw sensing layer: signal.py (the GtmSignal model, observation
-only) and sensing.py (adapters that call existing CLIENT-level functions and write GtmSignal
rows). Neither classifies demand/opportunity or scores offering-fit -- they only record what
was observed and where it came from.

Classification -- consuming app/gtm_os/context/ (what we sell, who we sell to) plus these raw
signals to decide where real demand exists, and why -- is NOT YET IMPLEMENTED. That is later
work, deliberately not started until explicitly asked for. Do not add placeholder classes for
it here."""
