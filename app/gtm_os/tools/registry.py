"""Catalog of existing functions that already do real, reusable work -- identified directly
from the codebase audit (2026-08-14). Each entry documents WHERE the function lives and WHAT
it does; it does not import or wrap the functions themselves, so this file has zero risk of
breaking anything and zero coupling to the existing pipeline's internals. A future step can
decide how these actually get invoked (direct import, a real tool-calling layer, etc.) --
that's an execution-layer decision, not a cataloging one.

`side_effect` matters for the founder's own stated approval-gate idea (research/scoring =
autonomous, sending/spending/committing = needs approval) -- marked here so that decision
doesn't have to be re-derived later by re-reading the pipeline again."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolEntry:
    name: str
    location: str  # "module.py::function_or_class.method"
    description: str
    side_effect: str  # "read_only" | "writes_internal_state" | "external_send" | "external_spend"


CATALOG: list[ToolEntry] = [
    ToolEntry(
        name="discover_companies",
        location="app/phases/apify_discovery.py::run_apify_discovery",
        description="Search for companies currently hiring for sales/GTM roles (job-posting-triggered discovery).",
        side_effect="external_spend",  # calls a paid Apify job-search actor; corrected during Step-1 verification
    ),
    ToolEntry(
        name="assess_team_composition",
        location="app/phases/hiring_signal.py::assess_team_composition",
        description="Real-time check of a company's existing sales/marketing headcount, to judge whether they already have a full team.",
        side_effect="external_spend",
    ),
    ToolEntry(
        name="find_decision_maker",
        location="app/phases/decision_maker.py::find_decision_maker",
        description="Resolve a company's founder/CEO or sales-leader contact, via a free-then-paid waterfall.",
        side_effect="external_spend",
    ),
    ToolEntry(
        name="research_company",
        location="app/phases/personalized_outreach.py::run_company_research",
        description="LLM research of a company's own website copy -- value props, ICP hypothesis, distinctive phrases.",
        side_effect="external_spend",
    ),
    ToolEntry(
        name="research_contact",
        location="app/phases/personalized_outreach.py::run_contact_research",
        description="LLM research of a decision-maker's recent LinkedIn posts -- stated needs, initiatives, implied gaps.",
        side_effect="external_spend",
    ),
    ToolEntry(
        name="generate_personalized_message",
        location="app/phases/personalized_outreach.py::generate_personalized_message",
        description="Runs the full 4-module Phase 13 research + message synthesis pipeline for one contact.",
        side_effect="external_spend",
    ),
    ToolEntry(
        name="apply_hot_lead_tags",
        location="app/phases/hot_leads.py::apply_hot_lead_tags",
        description="Tags companies as high-priority based on hiring velocity, JD language, and pain-language signals. Additive, never gates.",
        side_effect="writes_internal_state",
    ),
    ToolEntry(
        name="push_to_outreach_channel",
        location="app/outreach/salesrobot.py::SalesRobotChannel.push_lead / app/outreach/smartlead.py",
        description="Sends a real LinkedIn connection request or email to a contact. Irreversible external side effect.",
        side_effect="external_send",
    ),
    ToolEntry(
        name="run_score",
        location="app/phases/scoring.py::run_scoring",
        description="18-variable weighted fit score + tier for a company. Currently not called by the active pipeline (see audit).",
        side_effect="writes_internal_state",
    ),
    ToolEntry(
        name="check_buying_signal",
        location="app/phases/buying_signal.py::check_buying_signal",
        description="Funding-recency / headcount-growth / hiring-posting timing check. Currently not called by the active pipeline.",
        side_effect="external_spend",  # internally calls classify_hiring_signal, a paid TheirStack job search; corrected during Step-1 verification
    ),
    ToolEntry(
        name="check_tech_stack",
        location="app/phases/tech_stack.py::run_tech_stack_check",
        description="Detects a company's outbound/AI-SDR tooling. Currently not called by the active pipeline; needs a new paid call.",
        side_effect="external_spend",
    ),
    ToolEntry(
        name="sync_to_crm",
        location="app/phases/hubspot_sync.py::sync_to_hubspot",
        description="Pushes a company+contact to HubSpot. Currently not called by the active pipeline.",
        side_effect="external_send",
    ),
    ToolEntry(
        name="monitor_linkedin_profiles",
        location="app/phases/linkedin_monitor.py::run_linkedin_monitor_sweep",
        description="Polls a known list of LinkedIn profiles for new posts matching a keyword taxonomy, LLM-relevance-filtered.",
        side_effect="external_spend",
    ),
    ToolEntry(
        name="search_for_self_declared_interest",
        location="app/phases/reverse_discovery.py::run_reverse_discovery_sweep",
        description="Broad LinkedIn keyword search for people organically discussing AI SDR/autonomous sales, with ICP floor check.",
        side_effect="external_spend",
    ),
    ToolEntry(
        name="get_business_context",
        location="app/gtm_os/context/business_context.py::get_business_context",
        description="Reads the company's own goals/offerings/ICP/GTM-motions context.",
        side_effect="read_only",
    ),
]
