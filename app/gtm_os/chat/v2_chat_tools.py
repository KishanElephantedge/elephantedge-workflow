"""V2 GTM-OS chat assistant -- full read/write tool access, explicit instruction (2026-08-27):
"full access... whatever we can do that also should be able to do." Reuses the exact same
tool-calling mechanism V1's chat widget already runs (_run_chat_turn in app/routes/api.py,
Claude's native tool_use loop) -- this module only supplies a DIFFERENT tool list/dispatcher/
system prompt, scoped to GTM-OS V2's real functions instead of V1's legacy funnel.

Every function called here already exists and is already used by a real UI action elsewhere in
this codebase (Meetings' outcome form, the Contacts tab's escalation controls, Message Review,
Overrides & Evals, etc.) -- this module is a dispatch layer, not new business logic. The one
genuinely dangerous capability (send_message_draft, a REAL outbound send) is included because the
lead explicitly asked for it after being told the standard, safer default excludes it -- see
send_message_draft tool's own description, which is written to make an LLM treat it with real
weight, not as a routine action.

HubSpot CRM record deletion (delete_crm_company/delete_crm_contact) IS included, per explicit
instruction (2026-08-27) after this exclusion was raised and the lead confirmed they want it
anyway. Flagged here for the record: this is real, external SaaS data, and a misheard voice
command destructively deleting a real CRM record is a materially different risk than every other
write in this module (all of which touch this app's own, recoverable rows) -- the tool
descriptions below are written to make that weight explicit to the model, not soften it.

LEARNING (2026-08-27 explicit instruction: "if we tell that something it has to learn from that
as well") -- remember_knowledge auto-confirms via HumanKnowledge's own confirm_human_knowledge()
immediately after submission, rather than leaving it in the standalone Knowledge page's
pending_review queue. A human directly instructing the assistant in a live conversation ("remember
that...") is itself the explicit confirmation HumanKnowledge's own governance model requires --
see confirm_human_knowledge()'s docstring for the write-boundary this still respects (only ever
writes to human_knowledge, never to ICP/offering/motion config)."""

from sqlalchemy.orm import Session

ACTED_BY = "AI Assistant (chat)"


V2_CHAT_SYSTEM_PROMPT = """You are the AI assistant embedded in Elephant Edge's GTM-OS V2 \
dashboard (their internal go-to-market operating system) -- today is {today}.

You have REAL read and write access to the live system via tools. When asked to look something \
up, use a tool and answer from its real result -- never guess or invent a number, name, or \
status. When asked to DO something (fix a missing email, skip a stalled item, record a meeting \
outcome, approve a message, even send an approved message), actually call the tool that does it \
-- don't just describe what should be done. If a request is ambiguous about which real record it \
applies to (e.g. which company, which contact, which message), ask a brief clarifying question \
rather than guessing which real row to act on.

CRITICAL -- GROUNDING FOLLOW-UP REQUESTS: only the plain text of your own past replies is kept as \
conversation history, not the tool results that produced them. So when a follow-up refers back to \
something you just listed ("their emails," "those accounts," "opportunity 7"), you must resolve \
EXACTLY those same real records again -- by their name, id, or opportunity number as stated in \
your own prior reply (use get_opportunity for an opportunity number, get_account_brief/ \
search_accounts for a company) -- never by re-running a broader query (e.g. get_jobs_to_be_done \
again) and picking whichever result set looks topically related. Picking a different-but-similar \
set of real records instead of the exact ones already named is a serious grounding failure, not a \
minor imprecision.

You can also be explicitly told to remember something ("remember that...", "learn that...", "for \
future reference..."). When that happens, call remember_knowledge with the fact verbatim -- this \
persists it as real, confirmed knowledge the rest of the system can read.

send_message_draft triggers a REAL outbound message to a real prospect (LinkedIn connection \
request or email) -- it still passes through this system's own safety gates (rate limits, \
cooldowns, business hours, credentials), but it is not reversible once it succeeds. Only call it \
when the human has clearly asked for a message to actually be sent, not merely reviewed or \
approved.

delete_crm_company and delete_crm_contact PERMANENTLY delete a real record from HubSpot CRM, with \
no undo. Only call one of these when the human has explicitly and unambiguously named the exact \
record to delete -- never as a guess, never as a side effect of a different request, and never \
when more than one record could plausibly match what they said. If there's any doubt which real \
record they mean, ask before deleting.

Be concise and direct. Use real numbers and names from tool results, never estimates."""


V2_CHAT_TOOLS = [
    # ---- Reads ----
    {
        "name": "get_jobs_to_be_done",
        "description": "The prioritized real queue of what needs human attention right now: deals needing action, hot leads to review, contacts/emails still missing.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_accounts",
        "description": "Search companies/accounts by name, domain, or industry substring.",
        "input_schema": {
            "type": "object",
            "properties": {"search": {"type": "string"}, "limit": {"type": "integer", "description": "default 20, max 100"}},
            "required": ["search"],
        },
    },
    {
        "name": "get_account_brief",
        "description": "Full 360 view of one account: status, ICP fit, opportunity/strategy, contacts, decision-maker status.",
        "input_schema": {"type": "object", "properties": {"company_id": {"type": "integer"}}, "required": ["company_id"]},
    },
    {
        "name": "get_account_timeline",
        "description": "Real chronological event history for one account (signals, hypotheses, strategy, messages, campaign activity, outcomes).",
        "input_schema": {"type": "object", "properties": {"company_id": {"type": "integer"}}, "required": ["company_id"]},
    },
    {
        "name": "list_account_messages",
        "description": "Message drafts (with their id, channel, status, text) for one account -- needed before approving/rejecting/sending a specific message.",
        "input_schema": {"type": "object", "properties": {"company_id": {"type": "integer"}}, "required": ["company_id"]},
    },
    {
        "name": "get_pipeline",
        "description": "Real opportunity pipeline: for each opportunity, its strategy, readiness, and next required action.",
        "input_schema": {"type": "object", "properties": {"page": {"type": "integer"}, "page_size": {"type": "integer", "description": "default 25, max 100"}}},
    },
    {
        "name": "get_opportunity",
        "description": "Full detail for ONE specific opportunity by its id (e.g. resolving 'Opportunity 7' from an earlier answer back to its exact real company/strategy/contacts) -- always use this to re-resolve a specific opportunity number mentioned earlier, rather than re-listing the whole pipeline.",
        "input_schema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}}, "required": ["opportunity_id"]},
    },
    {
        "name": "get_revenue_pace",
        "description": "Real revenue this month vs. target, by ICP/offering, plus meeting won/lost/pending counts and the gap to target.",
        "input_schema": {"type": "object", "properties": {"month": {"type": "string", "description": "YYYY-MM, defaults to current month"}}},
    },
    {
        "name": "get_revenue_pace_diagnosis",
        "description": "A template-composed sentence connecting Revenue Pace's gap, the best-performing ICP/offering, and how many opportunities are blocked on a human step.",
        "input_schema": {"type": "object", "properties": {"month": {"type": "string"}}},
    },
    {
        "name": "get_channel_intelligence",
        "description": "Real revenue/deal counts by source channel (personal network, LinkedIn content, inbound, webinar, outbound, other), plus a grounded comparison.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_campaign_intelligence",
        "description": "Real per-SalesRobot-campaign sent/accepted/replied/revenue numbers, plus a grounded comparison across campaigns.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_offering_performance",
        "description": "Real reply/deal-win rate per offering, with suggestions grounded only in real sample sizes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_overrides_evals",
        "description": "Real message-review override rate, confirmed/candidate recurring patterns behind rejections, and human-provided knowledge.",
        "input_schema": {"type": "object", "properties": {"month": {"type": "string"}}},
    },
    {
        "name": "list_meetings",
        "description": "Booked meetings with their recorded outcome (won/lost/pending), amount, channel, and reason.",
        "input_schema": {"type": "object", "properties": {"search": {"type": "string"}, "page": {"type": "integer"}}},
    },
    {
        "name": "list_human_knowledge",
        "description": "Facts previously taught to the assistant/system, with their status (confirmed/pending/dismissed).",
        "input_schema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["pending_review", "pending_interpretation", "confirmed", "dismissed"]}}},
    },
    {
        "name": "get_business_context",
        "description": "Real configured business context: revenue goal, positioning, and other company-level settings.",
        "input_schema": {"type": "object", "properties": {}},
    },

    # ---- Writes ----
    {
        "name": "update_contact_email",
        "description": "Add or correct a contact's email address -- e.g. resolving a 'missing email' escalation.",
        "input_schema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "email": {"type": "string"}}, "required": ["contact_id", "email"]},
    },
    {
        "name": "add_contact",
        "description": "Manually add a decision-maker contact to a company when none was found automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "title": {"type": "string"},
                "linkedin_url": {"type": "string"},
            },
            "required": ["company_id", "first_name"],
        },
    },
    {
        "name": "dismiss_contacts_to_find_item",
        "description": "Skip a 'contacts to find' job item (no contact found, or missing email) so it stops reappearing -- use when the human has manually researched and confirmed there's nothing more to find.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_type": {"type": "string", "enum": ["company", "contact"]},
                "source_id": {"type": "integer"},
                "subcategory": {"type": "string", "enum": ["no_contact_found", "missing_email"]},
                "reason": {"type": "string"},
            },
            "required": ["source_type", "source_id"],
        },
    },
    {
        "name": "undo_dismiss_contacts_to_find_item",
        "description": "Reverse a previous skip decision on a 'contacts to find' item.",
        "input_schema": {"type": "object", "properties": {"source_type": {"type": "string", "enum": ["company", "contact"]}, "source_id": {"type": "integer"}}, "required": ["source_type", "source_id"]},
    },
    {
        "name": "record_meeting_outcome",
        "description": "Record (or clear, by passing status null) a booked meeting's real outcome: won/lost, amount, offering, channel, reason, notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["won", "lost"]},
                "company_id": {"type": "integer"},
                "offering_name": {"type": "string"},
                "amount_usd": {"type": "number"},
                "channel": {"type": "string", "enum": ["personal_network", "linkedin_content", "inbound", "webinar", "outbound", "other"]},
                "reason": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["booking_id"],
        },
    },
    {
        "name": "approve_message_draft",
        "description": "Approve a drafted outreach message so it's eligible to send.",
        "input_schema": {"type": "object", "properties": {"message_draft_id": {"type": "integer"}}, "required": ["message_draft_id"]},
    },
    {
        "name": "reject_message_draft",
        "description": "Reject a drafted outreach message.",
        "input_schema": {"type": "object", "properties": {"message_draft_id": {"type": "integer"}, "note": {"type": "string"}}, "required": ["message_draft_id"]},
    },
    {
        "name": "request_message_draft_changes",
        "description": "Send a drafted message back for changes with a note on what to fix.",
        "input_schema": {"type": "object", "properties": {"message_draft_id": {"type": "integer"}, "note": {"type": "string"}}, "required": ["message_draft_id", "note"]},
    },
    {
        "name": "update_message_draft_content",
        "description": "Edit a message draft's subject/text directly.",
        "input_schema": {"type": "object", "properties": {"message_draft_id": {"type": "integer"}, "subject": {"type": "string"}, "message_text": {"type": "string"}}, "required": ["message_draft_id"]},
    },
    {
        "name": "send_message_draft",
        "description": "Actually send an already-approved message draft to the real prospect, through this system's normal safety gates (rate limits, cooldowns, business hours, credentials). This is a REAL, not-reversible outbound send once it succeeds -- only call this when explicitly asked to actually send, not just approve or prepare.",
        "input_schema": {"type": "object", "properties": {"message_draft_id": {"type": "integer"}}, "required": ["message_draft_id"]},
    },
    {
        "name": "remember_knowledge",
        "description": "Save a fact/rule the human is directly teaching you (e.g. 'remember that...', 'learn that...') as confirmed knowledge for future reference.",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "confirm_pattern",
        "description": "Confirm a candidate recurring pattern (from Overrides & Evals) as a real, durable fact.",
        "input_schema": {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]},
    },
    {
        "name": "dismiss_pattern",
        "description": "Dismiss a candidate recurring pattern as not real/not worth acting on.",
        "input_schema": {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]},
    },
    {
        "name": "update_crm_company",
        "description": "Update a real HubSpot CRM company record's fields.",
        "input_schema": {"type": "object", "properties": {"company_id": {"type": "string"}, "fields": {"type": "object", "description": "HubSpot property name -> new value"}}, "required": ["company_id", "fields"]},
    },
    {
        "name": "update_crm_contact",
        "description": "Update a real HubSpot CRM contact record's fields.",
        "input_schema": {"type": "object", "properties": {"contact_id": {"type": "string"}, "fields": {"type": "object", "description": "HubSpot property name -> new value"}}, "required": ["contact_id", "fields"]},
    },
    {
        "name": "sync_own_linkedin_content",
        "description": "Trigger a real, on-demand check for new posts on the tracked LinkedIn content profile.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_crm_company",
        "description": "PERMANENTLY DELETE a real company record from HubSpot CRM. Not reversible. Only call this when the human has explicitly and unambiguously asked for this specific record to be deleted -- never as a side effect of another request, and never if there is any doubt about which record they mean.",
        "input_schema": {"type": "object", "properties": {"company_id": {"type": "string"}}, "required": ["company_id"]},
    },
    {
        "name": "delete_crm_contact",
        "description": "PERMANENTLY DELETE a real contact record from HubSpot CRM. Not reversible. Only call this when the human has explicitly and unambiguously asked for this specific record to be deleted -- never as a side effect of another request, and never if there is any doubt about which record they mean.",
        "input_schema": {"type": "object", "properties": {"contact_id": {"type": "string"}}, "required": ["contact_id"]},
    },
]


def execute_v2_chat_tool(name: str, tool_input: dict, db: Session, tenant_id: int) -> dict:
    """Dispatches one Claude tool_use call to a real V2 GTM-OS function. Every branch is wrapped
    by the caller in a try/except -- a failed tool call becomes an error string fed back to
    Claude, not a crashed chat turn (same discipline as V1's _execute_chat_tool)."""

    if name == "get_jobs_to_be_done":
        from app.gtm_os.jobs.jobs_to_be_done import get_jobs_to_be_done
        return get_jobs_to_be_done(db, tenant_id)

    if name == "search_accounts":
        from app.db.models import Batch, Company
        limit = min(tool_input.get("limit", 20), 100)
        like = f"%{tool_input['search'].strip()}%"
        companies = (
            db.query(Company)
            .join(Batch, Company.batch_id == Batch.id)
            .filter(Batch.tenant_id == tenant_id)
            .filter((Company.name.ilike(like)) | (Company.domain.ilike(like)) | (Company.industry.ilike(like)))
            .order_by(Company.created_at.desc())
            .limit(limit)
            .all()
        )
        return {"companies": [{"id": c.id, "name": c.name, "domain": c.domain, "industry": c.industry} for c in companies]}

    if name == "get_account_brief":
        from app.gtm_os.account_agent.account_agent import build_account_brief
        return build_account_brief(db, tenant_id, tool_input["company_id"])

    if name == "get_account_timeline":
        from app.gtm_os.execution.account_timeline import get_account_event_timeline
        return get_account_event_timeline(db, tenant_id, tool_input["company_id"])

    if name == "list_account_messages":
        from app.gtm_os.learning.message_draft import list_messages_for_company
        return {"messages": list_messages_for_company(db, tenant_id, tool_input["company_id"])}

    if name == "get_pipeline":
        from app.gtm_os.execution.execution_readiness import list_pipeline_items
        return list_pipeline_items(db, tenant_id, page=tool_input.get("page", 1), page_size=min(tool_input.get("page_size", 25), 100))

    if name == "get_opportunity":
        from app.gtm_os.execution.execution_readiness import get_pipeline_item
        item = get_pipeline_item(db, tenant_id, tool_input["opportunity_id"])
        return item if item is not None else {"error": "opportunity not found"}

    if name == "get_revenue_pace":
        from app.gtm_os.revenue.revenue_pace import get_revenue_pace
        return get_revenue_pace(db, tenant_id, month=tool_input.get("month"))

    if name == "get_revenue_pace_diagnosis":
        from app.gtm_os.revenue.revenue_pace_diagnosis import get_revenue_pace_diagnosis
        return get_revenue_pace_diagnosis(db, tenant_id, month=tool_input.get("month"))

    if name == "get_channel_intelligence":
        from app.gtm_os.learning.channel_intelligence import generate_channel_intelligence, get_channel_performance
        performance = get_channel_performance(db, tenant_id)
        return {**performance, "intelligence": generate_channel_intelligence(db, tenant_id, performance)}

    if name == "get_campaign_intelligence":
        from app.gtm_os.learning.campaign_intelligence import generate_campaign_intelligence, get_campaign_tracking
        tracking = get_campaign_tracking(db, tenant_id)
        return {**tracking, "intelligence": generate_campaign_intelligence(db, tenant_id, tracking)}

    if name == "get_offering_performance":
        from app.gtm_os.learning.offering_performance import generate_offering_suggestions, get_offering_performance
        performance = get_offering_performance(db, tenant_id)
        return {**performance, "suggestions": generate_offering_suggestions(performance)}

    if name == "get_overrides_evals":
        from app.gtm_os.learning.overrides_evals import get_overrides_evals
        return get_overrides_evals(db, tenant_id, month=tool_input.get("month"))

    if name == "list_meetings":
        from sqlalchemy import or_
        from app.db.models import CalendarBooking
        query = db.query(CalendarBooking)
        search = tool_input.get("search", "")
        if search.strip():
            like = f"%{search.strip()}%"
            query = query.filter(or_(CalendarBooking.booker_name.ilike(like), CalendarBooking.booker_email.ilike(like)))
        page = max(tool_input.get("page", 1), 1)
        page_size = 25
        bookings = query.order_by(CalendarBooking.start_time.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return {
            "bookings": [
                {
                    "id": b.id, "booker_name": b.booker_name, "booker_email": b.booker_email,
                    "start_time": b.start_time, "status": b.status,
                    "outcome_status": b.outcome_status, "outcome_company_name": b.outcome_company.name if b.outcome_company else None,
                    "outcome_amount_usd": b.outcome_amount_usd, "outcome_channel": b.outcome_channel,
                    "outcome_reason": b.outcome_reason, "outcome_notes": b.outcome_notes,
                }
                for b in bookings
            ]
        }

    if name == "list_human_knowledge":
        from app.gtm_os.learning.human_knowledge import list_human_knowledge
        return {"knowledge": list_human_knowledge(db, tenant_id, status=tool_input.get("status"))}

    if name == "get_business_context":
        from app.gtm_os.context.business_context import get_business_context
        return get_business_context(db, tenant_id)

    if name == "update_contact_email":
        from app.gtm_os.sales.contact_discovery import update_contact_email
        contact = update_contact_email(db, tenant_id, tool_input["contact_id"], tool_input["email"])
        return {"id": contact.id, "email": contact.email, "email_source": contact.email_source}

    if name == "add_contact":
        from app.db.models import Contact
        contact = Contact(
            company_id=tool_input["company_id"], first_name=tool_input.get("first_name"),
            last_name=tool_input.get("last_name"), title=tool_input.get("title"),
            linkedin_url=tool_input.get("linkedin_url"), matched_title_reasoning="Manually added via AI assistant",
        )
        db.add(contact)
        db.commit()
        db.refresh(contact)
        return {"id": contact.id, "first_name": contact.first_name, "last_name": contact.last_name}

    if name == "dismiss_contacts_to_find_item":
        from app.gtm_os.jobs.escalation import dismiss_job_item
        dismissal = dismiss_job_item(
            db, tenant_id, category="contacts_to_find", source_type=tool_input["source_type"],
            source_id=tool_input["source_id"], subcategory=tool_input.get("subcategory"),
            reason=tool_input.get("reason"), dismissed_by=ACTED_BY,
        )
        return {"id": dismissal.id, "dismissed_at": dismissal.dismissed_at}

    if name == "undo_dismiss_contacts_to_find_item":
        from app.gtm_os.jobs.escalation import undo_job_dismissal
        undone = undo_job_dismissal(db, tenant_id, category="contacts_to_find", source_type=tool_input["source_type"], source_id=tool_input["source_id"])
        return {"undone": undone}

    if name == "record_meeting_outcome":
        from app.gtm_os.revenue.revenue_pace import record_meeting_outcome
        booking = record_meeting_outcome(
            db, tenant_id, tool_input["booking_id"], status=tool_input.get("status"),
            company_id=tool_input.get("company_id"), offering_name=tool_input.get("offering_name"),
            amount_usd=tool_input.get("amount_usd"), reason=tool_input.get("reason"),
            notes=tool_input.get("notes"), recorded_by=ACTED_BY, channel=tool_input.get("channel"),
        )
        return {"id": booking.id, "outcome_status": booking.outcome_status, "outcome_amount_usd": booking.outcome_amount_usd, "outcome_channel": booking.outcome_channel}

    if name == "approve_message_draft":
        from app.gtm_os.learning.message_draft import approve_message_draft
        draft = approve_message_draft(db, tenant_id, tool_input["message_draft_id"], approved_by=ACTED_BY)
        return {"id": draft.id, "status": draft.status}

    if name == "reject_message_draft":
        from app.gtm_os.learning.message_draft import reject_message_draft
        draft = reject_message_draft(db, tenant_id, tool_input["message_draft_id"], reviewed_by=ACTED_BY, note=tool_input.get("note"))
        return {"id": draft.id, "status": draft.status}

    if name == "request_message_draft_changes":
        from app.gtm_os.learning.message_draft import request_changes_message_draft
        draft = request_changes_message_draft(db, tenant_id, tool_input["message_draft_id"], reviewed_by=ACTED_BY, note=tool_input["note"])
        return {"id": draft.id, "status": draft.status}

    if name == "update_message_draft_content":
        from app.gtm_os.learning.message_draft import update_message_draft_content
        draft = update_message_draft_content(db, tenant_id, tool_input["message_draft_id"], subject=tool_input.get("subject"), message_text=tool_input.get("message_text"))
        return {"id": draft.id, "subject": draft.subject, "message_text": draft.message_text}

    if name == "send_message_draft":
        from app.gtm_os.learning.message_draft import MessageDraft
        from app.gtm_os.send.send import send_message_draft
        draft = db.get(MessageDraft, tool_input["message_draft_id"])
        if draft is None or draft.tenant_id != tenant_id:
            return {"error": "message draft not found"}
        return send_message_draft(db, tenant_id, draft)

    if name == "remember_knowledge":
        from app.gtm_os.learning.human_knowledge import confirm_human_knowledge, submit_human_knowledge
        row = submit_human_knowledge(db, tenant_id, tool_input["text"], created_by=ACTED_BY)
        return confirm_human_knowledge(db, tenant_id, row["id"], confirmed_by=ACTED_BY)

    if name == "confirm_pattern":
        from app.gtm_os.learning.overrides_evals import confirm_pattern
        pattern = confirm_pattern(db, tenant_id, tool_input["category"], confirmed_by=ACTED_BY)
        return {"id": pattern.id, "status": pattern.status}

    if name == "dismiss_pattern":
        from app.gtm_os.learning.overrides_evals import dismiss_pattern
        pattern = dismiss_pattern(db, tenant_id, tool_input["category"], confirmed_by=ACTED_BY)
        return {"id": pattern.id, "status": pattern.status}

    if name == "update_crm_company":
        from app.hubspot_client import update_company
        return update_company(tool_input["company_id"], tool_input["fields"], db, tenant_id)

    if name == "update_crm_contact":
        from app.hubspot_client import update_contact
        return update_contact(tool_input["contact_id"], tool_input["fields"], db, tenant_id)

    if name == "sync_own_linkedin_content":
        from app.gtm_os.learning.own_linkedin_content import sync_own_linkedin_posts
        return sync_own_linkedin_posts(db, tenant_id)

    if name == "delete_crm_company":
        from app.hubspot_client import delete_company
        delete_company(tool_input["company_id"], db, tenant_id)
        return {"deleted": True, "company_id": tool_input["company_id"]}

    if name == "delete_crm_contact":
        from app.hubspot_client import delete_contact
        delete_contact(tool_input["contact_id"], db, tenant_id)
        return {"deleted": True, "contact_id": tool_input["contact_id"]}

    return {"error": f"unknown tool {name!r}"}
