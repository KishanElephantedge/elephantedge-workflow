"""Meeting intelligence -- what actually happened in a conversation, and what follows from it.

Scope is deliberately narrow. This layer INGESTS and INTERPRETS meeting records (Granola notes,
AI summaries, transcripts) and links them to the CalendarBooking they belong to. It drafts a
meeting outcome for a human to confirm; it never records one on its own, because an outcome sets
a revenue figure and the system has no business asserting revenue nobody agreed to.

What it explicitly does NOT do:
  - No prospecting. Meeting transcripts are client-confidential conversations, not a lead source.
    The same rule already applied to the Slack community's #ask-a-question channel.
  - No automated outreach off the back of a transcript.
  - No revenue forecast. get_revenue_pace reports what humans confirmed; nothing here changes that.
"""
