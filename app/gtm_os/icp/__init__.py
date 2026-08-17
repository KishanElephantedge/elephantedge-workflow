"""ICP + Trigger Intelligence -- Backend Batch 8 (per the lead's reference material, reconciled
via the gpt.txt planning discussion of 2026-08-17).

    Company signals (existing Score/hiring_signal pipeline, reused read-only)
        |
        v
    Trigger detection (icp_matching.py) -- deterministic, no LLM
        |
        v
    ICP identification (icp_matching.py) -- structured match result per ICP, never a score
        |
        v
    ICPMatch (persisted, one row per real match)

Deliberately STOPS here. Does NOT implement Offering Matcher changes, the ICP x Offering
matrix, GTM Motion engine, or Account Agents -- those are later, explicitly separate batches
(9-12), per the plan in gpt.txt. This package only answers "which ICP(s), if any, does this
company's existing data support," with full provenance and no fabrication."""
