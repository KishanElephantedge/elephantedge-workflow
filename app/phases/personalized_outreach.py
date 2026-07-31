"""Phase 13 -- Personalized Outreach Message Synthesis, per the lead's 4-module spec:
1. Company research -- scrape the company website, have Claude extract a structured summary
   (product, value props, target industries, ICP hypothesis, buyer personas, anchor phrases).
2. Contact research -- fetch the decision-maker's recent LinkedIn posts (Aviato), have Claude
   extract their role focus, recent initiatives, stated needs, and implied gaps.
3. Fit analysis -- Claude compares our own value proposition against what was learned about
   the company/contact, producing fit areas, non-fit areas, and a positioning statement.
4. Message synthesis -- Claude writes the final outbound message using all of the above.

Every module's raw structured output is persisted (not just the final message), per the
spec's own requirement that a human can review or edit any stage. Each module can fail
independently (e.g. LinkedIn posts unavailable) without necessarily failing the whole
pipeline -- degraded input, not a hard stop, since a message with less context is still
better than no message.
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.claude_client import ClaudeError
from app.db.models import Company, Contact, Parameter, PersonalizedMessage
from app.deepline_client import DeeplineError, execute_tool
from app.llm_client import generate_json, generate_text
from app.website_scraper import ScrapeError, scrape_company_website

DEFAULT_VALUE_PROPOSITION = {
    "service": "Top-of-funnel pipeline generation for enterprise B2B accounts.",
    "strengths": [
        "ICP and persona research",
        "Targeted outbound (email + LinkedIn)",
        "Meeting-setting with qualified senior decision makers",
    ],
    "constraints": [
        "Remote, project-based engagements",
    ],
}


def _get_value_proposition(db: Session, tenant_id: int) -> dict:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == "core_value_proposition")
        .first()
    )
    return param.value if param and param.value else DEFAULT_VALUE_PROPOSITION


COMPANY_RESEARCH_PROMPT = """You are analyzing a company's website content to build a structured research summary for a B2B sales outreach tool.

Website content (scraped from their homepage and product/about pages):
---
{website_text}
---

Company name: {company_name}
Company domain: {domain}

Based ONLY on the content above, return a JSON object with exactly this shape (no prose, no markdown fences, just the JSON):
{{
  "company_summary": "one sentence describing what they do",
  "product": "one sentence describing their actual product",
  "value_props": ["3-5 bullet value propositions in their own words/phrasing"],
  "target_industries": ["industries or segments they explicitly target, if stated"],
  "icp_hypothesis": {{
    "company_type": "inferred ideal customer type",
    "size": "inferred ideal customer size",
    "notes": "any other ICP cues from the copy"
  }},
  "buyer_personas": ["likely buyer job titles based on the language/use-cases described"],
  "anchor_phrases": ["1-3 exact phrases from their own copy that are distinctive/memorable"]
}}

If the content is too thin to infer something confidently, use an empty string or empty array for that field rather than guessing."""

CONTACT_RESEARCH_PROMPT = """You are analyzing a person's recent LinkedIn posts to understand their current priorities for a B2B sales outreach tool.

Contact name: {contact_name}
Contact title: {contact_title}

Recent LinkedIn posts (raw commentary text, most recent first):
---
{posts_text}
---

Based ONLY on the content above, return a JSON object with exactly this shape (no prose, no markdown fences, just the JSON):
{{
  "role": "their current role/title",
  "recent_initiatives": ["things they're actively working on or hiring for, based on the posts"],
  "stated_needs": ["explicit needs or goals they've mentioned"],
  "implied_gaps": ["gaps or pain points reasonably implied by what they've posted"],
  "constraints": ["any explicit constraints or preferences mentioned (location, timing, etc.)"]
}}

If there isn't enough content to infer something confidently, use an empty array for that field rather than guessing."""

FIT_ANALYSIS_PROMPT = """You are comparing our own service offering against a prospective company/contact's real situation, for a B2B sales outreach tool.

Our value proposition:
{value_prop_json}

Company research:
{company_research_json}

Contact research:
{contact_research_json}

Based on this, return a JSON object with exactly this shape (no prose, no markdown fences, just the JSON):
{{
  "fit_areas": ["specific ways our service concretely helps this company/contact's real situation"],
  "non_fit_areas": ["things we should NOT claim to be or compete with, based on their stated needs/constraints"],
  "positioning": "one sentence describing how we should position ourselves to this specific contact"
}}"""

MESSAGE_SYNTHESIS_PROMPT = """Write a short, genuinely personalized LinkedIn/email outreach message using the research below. Do not use generic sales language or hollow flattery -- reference real, specific details from the research.

Contact first name: {first_name}

Company research:
{company_research_json}

Contact research:
{contact_research_json}

Fit analysis:
{fit_analysis_json}

Structure:
1. A short opening that proves you understand their company and the contact's current priorities (reference a real anchor phrase or recent initiative).
2. One paragraph positioning our value proposition specifically in relation to what they're dealing with right now -- not a generic pitch.
3. One line proposing a small, low-commitment next step (e.g. a short call, or piloting on a narrow slice of their pipeline first) -- not a hard sell. Frame it as confident and easy to say yes to, never as tentative or unproven -- avoid words like "experiment" or "test" that could imply we're not sure this works yet. The smallness is about THEIR commitment, not our confidence.
4. If there are non-fit areas, acknowledge what we are NOT trying to be -- but only if it adds real clarity, and only as a natural, conversational aside (the kind a person would actually say), never as a formal disclaimer or a "just to be clear, we don't do X" clause. If it can't be phrased that way, leave it out rather than force it in.

Keep it under 150 words. Return ONLY the message text, no subject line, no preamble, no explanation."""


def run_company_research(company: Company, db: Session, tenant_id: int) -> dict:
    if not company.domain:
        raise ScrapeError("Company has no domain to research")
    website_text = scrape_company_website(company.domain)
    prompt = COMPANY_RESEARCH_PROMPT.format(website_text=website_text, company_name=company.name, domain=company.domain)
    return generate_json(prompt, db, tenant_id, max_tokens=1000)


def run_contact_research(contact: Contact, db: Session, tenant_id: int) -> dict:
    if not contact.linkedin_url:
        raise DeeplineError("Contact has no LinkedIn URL to research")
    linkedin_id = contact.linkedin_url.rstrip("/").split("/")[-1]
    response = execute_tool("aviato_get_person_posts", {"linkedinID": linkedin_id})
    raw = response.get("toolResponse", {}).get("raw", {})
    posts = raw.get("results", []) if isinstance(raw, dict) else []
    posts_text = "\n\n".join(p.get("commentary", "") for p in posts if p.get("commentary"))
    if not posts_text:
        posts_text = "(no recent posts found)"
    contact_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
    prompt = CONTACT_RESEARCH_PROMPT.format(contact_name=contact_name, contact_title=contact.title or "", posts_text=posts_text[:6000])
    return generate_json(prompt, db, tenant_id, max_tokens=1000)


def run_fit_analysis(company_research: dict, contact_research: dict, db: Session, tenant_id: int) -> dict:
    value_prop = _get_value_proposition(db, tenant_id)
    prompt = FIT_ANALYSIS_PROMPT.format(
        value_prop_json=json.dumps(value_prop, indent=2),
        company_research_json=json.dumps(company_research, indent=2),
        contact_research_json=json.dumps(contact_research, indent=2),
    )
    return generate_json(prompt, db, tenant_id, max_tokens=800)


def run_message_synthesis(contact: Contact, company_research: dict, contact_research: dict, fit_analysis: dict, db: Session, tenant_id: int) -> str:
    prompt = MESSAGE_SYNTHESIS_PROMPT.format(
        first_name=contact.first_name or "there",
        company_research_json=json.dumps(company_research, indent=2),
        contact_research_json=json.dumps(contact_research, indent=2),
        fit_analysis_json=json.dumps(fit_analysis, indent=2),
    )
    return generate_text(prompt, db, tenant_id, max_tokens=500)


def generate_personalized_message(contact_id: int, db: Session, tenant_id: int) -> PersonalizedMessage:
    """Entrypoint. Runs all 4 modules in sequence, persisting whatever succeeds even if a
    later module fails -- a partial result (e.g. company research only) is still visible and
    useful, not silently discarded."""
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise ValueError(f"Contact {contact_id} not found")
    company = contact.company

    pm = contact.personalized_message
    if pm is None:
        pm = PersonalizedMessage(contact_id=contact_id, status="draft")
        db.add(pm)
        db.commit()
        db.refresh(pm)

    try:
        company_research = run_company_research(company, db, tenant_id)
        pm.company_research = company_research
        db.commit()

        try:
            contact_research = run_contact_research(contact, db, tenant_id)
        except (DeeplineError, ClaudeError) as e:
            contact_research = {"error": str(e)}
        pm.contact_research = contact_research
        db.commit()

        fit_analysis = run_fit_analysis(company_research, contact_research, db, tenant_id)
        pm.fit_analysis = fit_analysis
        db.commit()

        message = run_message_synthesis(contact, company_research, contact_research, fit_analysis, db, tenant_id)
        pm.generated_message = message
        pm.generated_at = datetime.utcnow()
        pm.error_message = None
        db.commit()
    except (ScrapeError, ClaudeError, DeeplineError) as e:
        pm.error_message = str(e)
        db.commit()

    db.refresh(pm)
    return pm
