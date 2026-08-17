"""Meeting Outcomes / Revenue Pace -- captures human-recorded deal outcomes (won/lost) against
booked meetings, and reads them back as a real month-to-date revenue-pace summary. Does NOT
automatically adjust ICP/offering/GTM-motion configuration, does NOT generate forecasts, does NOT
score or rank anything, and does NOT produce AI-generated narrative/reasoning text -- same
"optimization is future work" boundary app/gtm_os/learning/__init__.py already states. This
module's job is narrower and comes first: make sure the right facts (amount, offering, ICP
snapshot, win/loss reason) get captured now, so a future learning phase has real data instead of
having to retrofit it.

    revenue_pace.py -- record_meeting_outcome() (write, human-supplied), get_revenue_pace()
                        (read-only aggregation over recorded outcomes + business_context's goal)
"""
