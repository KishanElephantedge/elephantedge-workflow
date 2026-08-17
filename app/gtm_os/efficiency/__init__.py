"""Efficiency ("time saved") readout -- the real chain is:

    automated run -> real activity performed -> activity ledger (EfficiencyActivityEvent) ->
    manual-time benchmark (Parameter-backed config) -> human-equivalent time -> net time saved

    benchmark_config.py    -- configurable manual-minutes-per-activity, same Parameter-backed
                               pattern as business_context/icp_config/offering_config/
                               gtm_motion_config. Seeded ONLY with the three benchmarks the
                               reference material actually gives a number for; every other real
                               activity_type is seeded manual_minutes=null until configured.
    activity_recorder.py   -- idempotent insert helper for EfficiencyActivityEvent, used at the
                               real call sites where each activity actually happens (never
                               inferred from current-state record counts).
    efficiency.py           -- read-only monthly aggregation: sums real activity volume, applies
                               benchmarks, reuses AutonomousRun's own started_at/completed_at for
                               actual automation runtime (no second execution-time tracker), and
                               computes human_equivalent_hours / net_hours_saved (never negative).

Explicitly NOT here: "equivalent SDRs", "ran outside 9-5 %", "admin time cut %" -- none of these
are defensible from real data yet (see efficiency-feature-audit.md). Never fabricated; shown as
"Not available yet" by the frontend instead."""
