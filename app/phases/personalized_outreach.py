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
from datetime import datetime, timedelta

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


DEFAULT_SENDER_NAME = "the founder"


def _get_sender_name(db: Session, tenant_id: int) -> str:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == "outreach_sender_name")
        .first()
    )
    if param and param.value and isinstance(param.value, dict) and param.value.get("name"):
        return param.value["name"]
    return DEFAULT_SENDER_NAME


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
1. A short opening that proves you understand their company and the contact's current priorities (reference a real anchor phrase or recent initiative). Avoid anchor phrases that are specific, sensitive comparison claims (e.g. "our AI outperforms human agents") -- referencing those too literally reads as an odd, oddly-specific thing for a stranger to bring up. Pick a neutral, substantive detail instead.
2. One paragraph positioning our value proposition specifically in relation to what they're dealing with right now -- not a generic pitch.
3. One line proposing a small, low-commitment next step (e.g. a short call, or piloting on a narrow slice of their pipeline first) -- not a hard sell. Frame it as confident and easy to say yes to, never as tentative or unproven -- avoid words like "experiment" or "test" that could imply we're not sure this works yet. The smallness is about THEIR commitment, not our confidence. Never name a specific day, date, or time frame (e.g. "next Tuesday," "this week") -- the message may not even be read until well after any day named in it, which reads as stale or presumptuous. Keep the ask open-ended (e.g. "open to a quick call?").
4. If there are non-fit areas, acknowledge what we are NOT trying to be -- but only if it adds real clarity, and only as a natural, conversational aside (the kind a person would actually say), never as a formal disclaimer or a "just to be clear, we don't do X" clause. If it can't be phrased that way, leave it out rather than force it in.

Keep it under 150 words. Return ONLY the message text, no subject line, no preamble, no explanation."""

# Real examples the lead has actually sent and gotten replies on -- given here as TONE/INTENT
# reference only, not templates. Both happen to open with "you're leading Sales at X" because
# both real recipients were Sales leaders; that line does not generalize to every contact
# (several of ours are CEOs/Co-Founders/Product roles) and must not be copied verbatim. The
# prompt below explicitly instructs the model to vary wording and structure per contact based
# on their real research, not to mechanically reuse either example's skeleton.
CURIOSITY_MESSAGE_EXAMPLES = """Example A:
Bryn, would you allow me to access your knowledge?
I'm a serial founder. Today I'm building a Fractional GTM business, and one thing I'm obsessed with is autonomous sales.
Instead of sitting in a room guessing what sales teams need, I'm talking to people who live it every day.
You're leading Sales at Slip Robotics. I'm sure you've seen things that don't show up in LinkedIn posts or playbooks.
Would you spare 20 minutes and let me borrow some of that knowledge? I'd love to understand how you run Sales team today, where the friction is, and what you'd automate if you could.
Just trying to build something that's actually useful.

Example B:
15-Minute Conversation on B2B Sales Challenges
I'm a serial founder currently building a Fractional Sales consulting business.
A key part of what I'm working on is an autonomous, AI-led sales motion. Rather than building it in isolation, I want to solve real GTM challenges that companies are facing today.
Slip Robotics is scaling rapidly, and I imagine you've encountered some interesting challenges around pipeline generation, sales execution, and GTM as you've grown.
Would you be open to a 20-minute conversation? My goal is simply to understand your current GTM and sales process, the bottlenecks you're seeing, and where you think AI could genuinely help. This isn't a sales call, I'm looking to learn from operators building great companies."""

SENDER_REAL_CONTEXT = "serial founder; currently building a Fractional GTM business; genuinely interested in autonomous/AI-led sales"

CURIOSITY_MESSAGE_SYNTHESIS_PROMPT = """Write a short LinkedIn outreach message in the TONE and SPIRIT of the two real examples below -- genuinely curious, not selling anything, asking to learn from this specific person's real experience. These examples are style/intent reference ONLY, not templates: do not copy their structure, opening line, or wording, and do not force a line like "you're leading Sales at X" onto someone whose real role is different. Notice how LIGHT the company references are in both examples -- one general observation, not a detailed rundown. Match that same simplicity and brevity, not the depth of the research data below.

{examples}

Sender's name (sign off with this): {sender_name}
Real facts about the sender (rephrase these in your own words -- never copy this phrasing directly): {sender_context}
Contact first name: {first_name}
Contact's real title: {contact_title}
Company name: {company_name}

Company research (background only -- do not dump multiple details from this into the message):
{company_research_json}

Contact research (background only -- do not dump multiple details from this into the message):
{contact_research_json}

{avoid_openers_block}

Rules:
- No product/service pitch of any kind. Nothing about what we offer, sell, or could help with. Do not describe anything the sender would do, offer, handle, or provide for them -- not even hedged or conditional ("we could help with...", "I could run..."). This is purely an ask to learn, nothing else.
- Keep the company/contact reference to ONE light, general observation. Do not list out multiple specific initiatives, features, or details from the research -- that reads as a deep-dive, not a casual message.
- Always work in the real facts about the sender given above, somewhere in the message -- do not drop them, and do not adapt or reword them to sound like they match the recipient's own specific domain (e.g. never claim to be building loan-servicing AI, security infrastructure, farming tech, etc. just because that happens to be the recipient's business -- that would be fabricating what the sender does). Say "GTM" in all caps, never "Gtm" or "Gtn."
- Genuinely vary your wording message to message -- do not default to the exact phrases in the two reference examples ("I'm a serial founder," "would you be open to sharing a bit of your experience?," etc.). Say the same underlying things in fresh, different words each time, the way a real person writing many separate messages naturally would never phrase the intro or the ask identically twice. If an "avoid" list is given above, do not open with anything similar to those lines.
- Do not use the phrase "don't show up in/find in [playbooks/textbooks/LinkedIn posts]" or any close variant of it, even though one of the reference examples uses it -- it's become a repeated crutch across separate messages and needs to stop appearing. Find a different, message-specific way to say the same thing, or leave the thought out.
- Avoid the phrase "a big focus for me right now" or similar filler -- say what the sender is actually doing or interested in plainly and specifically instead of using a vague intensifier like "big focus."
- Keep the language plain and casual, the way the two real examples are written -- short, simple sentences, like a real person typing a message, not a formal business tone. Avoid corporate-sounding phrases (e.g. "operator experience," "architectural challenges," "hands-on experience," "sales motions" -- say "sales" or "outreach" instead).
- Never open a sentence with a distancing, third-person description of the recipient like "As someone who..." or "As a person who..." -- it reads like the message is describing them from the outside rather than actually talking to them. Address them directly instead.
- Never use an em dash (the "—" character, distinct from a normal hyphen "-"). It's a well-known tell that text was AI-generated. Use a period, comma, or a plain word like "and"/"but" instead.
- Ask for a short (15-20 minute), no-pressure conversation to learn from their real experience -- frame it as genuine curiosity, not a disguised sales call.
- Never name a specific day, date, or time frame.
- Sign off with the sender's name given above.
- Keep it under 130 words. Return ONLY the message text, no subject line unless it reads naturally as one, no preamble, no explanation."""


CURIOSITY_EMAIL_SYNTHESIS_PROMPT = """Write a short, genuinely curious cold email in the TONE and SPIRIT of the two real examples below (those are LinkedIn messages, not emails -- use them for tone/intent only, not structure). Same rules as the LinkedIn version: not selling anything, asking to learn from this specific person's real experience, one light company/contact reference, never a product pitch.

{examples}

Sender's name (sign off with this): {sender_name}
Real facts about the sender (rephrase in your own words): {sender_context}
Contact first name: {first_name}
Contact's real title: {contact_title}
Company name: {company_name}

Company research (background only -- do not dump multiple details into the email):
{company_research_json}

Contact research (background only -- do not dump multiple details into the email):
{contact_research_json}

{avoid_openers_block}

Rules:
- No product/service pitch of any kind, same as the LinkedIn version -- purely an ask to learn.
- Subject line: short (under 8 words), plain, specific enough to not read as spam -- never generic like "Quick question" or "Following up," never clickbait, never in all caps or title case (write it the way a real person types a subject line).
- Body: keep the company/contact reference to ONE light, general observation, not a detailed rundown.
- Always work in the real facts about the sender given above, without adapting them to sound like they match the recipient's own domain.
- Genuinely vary wording from the reference examples and from the avoid-list below.
- Plain, casual language -- short sentences, no corporate phrasing, no em dash.
- Never open with "As someone who..." or similar third-person distancing.
- Ask for a short (15-20 minute), no-pressure conversation -- frame as genuine curiosity.
- Never name a specific day, date, or time frame.
- Sign off the body with the sender's name given above.
- Keep the body under 130 words.

Return ONLY a JSON object with exactly this shape (no markdown fences, no prose): {{"subject": "...", "body": "..."}}"""


CURIOSITY_RECENT_OPENERS_MAX = 5


def _get_recent_curiosity_openers(db: Session, tenant_id: int) -> list[str]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == "curiosity_recent_openers")
        .first()
    )
    if param and param.value and isinstance(param.value, dict):
        return param.value.get("openers", [])
    return []


def _record_curiosity_opener(db: Session, tenant_id: int, opener: str) -> None:
    """Keeps a short rolling history of recent opening lines so the next generation can be
    told explicitly what to avoid -- found live that a single "vary your wording" instruction
    isn't enough on its own, since each contact's message is generated independently with no
    memory of what was written for anyone else. A concrete "don't open like these" list is
    far more reliable than an abstract instruction."""
    openers = _get_recent_curiosity_openers(db, tenant_id)
    openers.append(opener)
    openers = openers[-CURIOSITY_RECENT_OPENERS_MAX:]
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == "curiosity_recent_openers")
        .first()
    )
    if param:
        param.value = {"openers": openers}
    else:
        param = Parameter(tenant_id=tenant_id, key="curiosity_recent_openers", value={"openers": openers},
                           description="Rolling history of recent curiosity-style message openers, used to keep new messages from repeating the same opening line")
        db.add(param)
    db.commit()


def run_curiosity_message_synthesis(contact: Contact, company_research: dict, contact_research: dict, db: Session, tenant_id: int) -> str:
    recent_openers = _get_recent_curiosity_openers(db, tenant_id)
    if recent_openers:
        avoid_block = (
            "Full text of recent messages already sent to OTHER contacts -- found live that "
            "tracking only the opening line wasn't enough, since messages kept reusing the "
            "same sentence structure to introduce the sender ('I'm a serial founder currently "
            "[X]-ing a Fractional GTM business') with just a synonym swapped in place of [X], "
            "which reads as obvious copy-paste, not real variation. Do not reuse a similar "
            "SENTENCE STRUCTURE to any of these anywhere in your message, not just the opening "
            "line -- restructure entirely (different sentence order, different framing, "
            "sometimes mention the sender's background later in the message instead of up "
            "front, sometimes phrase it as running/leading rather than building, etc.), not "
            "just different word choices for the same sentence shape:\n\n"
            + "\n\n---\n\n".join(recent_openers)
        )
    else:
        avoid_block = ""

    prompt = CURIOSITY_MESSAGE_SYNTHESIS_PROMPT.format(
        examples=CURIOSITY_MESSAGE_EXAMPLES,
        sender_name=_get_sender_name(db, tenant_id),
        sender_context=SENDER_REAL_CONTEXT,
        first_name=contact.first_name or "there",
        contact_title=contact.title or "unknown",
        company_name=contact.company.name if contact.company else "unknown",
        company_research_json=json.dumps(company_research, indent=2),
        contact_research_json=json.dumps(contact_research, indent=2),
        avoid_openers_block=avoid_block,
    )
    message = generate_text(prompt, db, tenant_id, max_tokens=400)
    # Guaranteed, not just instructed -- found live the model doesn't reliably follow a
    # "never use an em dash" instruction on its own. Replacing it here means it truly never
    # reaches a real message, regardless of what the model does.
    message = message.replace("—", ", ").replace("--", ", ")

    _record_curiosity_opener(db, tenant_id, message.strip())

    return message


def run_curiosity_email_synthesis(contact: Contact, company_research: dict, contact_research: dict, db: Session, tenant_id: int) -> dict:
    """Email counterpart to run_curiosity_message_synthesis -- same tone/rules, same rolling
    avoid-openers list (one shared voice across both channels for the same sender), but
    returns {"subject", "body"} since email needs a distinct subject line LinkedIn doesn't."""
    recent_openers = _get_recent_curiosity_openers(db, tenant_id)
    if recent_openers:
        avoid_block = (
            "Full text of recent messages already sent to OTHER contacts -- do not reuse a "
            "similar sentence structure anywhere in this email, not just the opening line:\n\n"
            + "\n\n---\n\n".join(recent_openers)
        )
    else:
        avoid_block = ""

    prompt = CURIOSITY_EMAIL_SYNTHESIS_PROMPT.format(
        examples=CURIOSITY_MESSAGE_EXAMPLES,
        sender_name=_get_sender_name(db, tenant_id),
        sender_context=SENDER_REAL_CONTEXT,
        first_name=contact.first_name or "there",
        contact_title=contact.title or "unknown",
        company_name=contact.company.name if contact.company else "unknown",
        company_research_json=json.dumps(company_research, indent=2),
        contact_research_json=json.dumps(contact_research, indent=2),
        avoid_openers_block=avoid_block,
    )
    result = generate_json(prompt, db, tenant_id, max_tokens=500)
    subject = (result.get("subject") or "").replace("—", ", ").replace("--", ", ").strip()
    body = (result.get("body") or "").replace("—", ", ").replace("--", ", ").strip()

    _record_curiosity_opener(db, tenant_id, body)

    return {"subject": subject, "body": body}


def run_company_research(company: Company, db: Session, tenant_id: int) -> dict:
    if not company.domain:
        raise ScrapeError("Company has no domain to research")
    website_text = scrape_company_website(company.domain)
    prompt = COMPANY_RESEARCH_PROMPT.format(website_text=website_text, company_name=company.name, domain=company.domain)
    return generate_json(prompt, db, tenant_id, max_tokens=1000)


RECENT_POST_MAX_AGE_DAYS = 90


def _is_recent_post(post: dict) -> bool:
    """Found live: a real message called a 9-month-old post 'recent' and congratulated the
    contact on it as if it just happened -- the LLM was only ever given the post's text, with
    no age information at all, so it had no way to know otherwise. Aviato's response does
    include a real timestamp (postRange.latest) that was simply never being read. Filtering
    here, before anything reaches the prompt, means a stale post can never again be mistaken
    for current news -- more reliable than hoping the model correctly weighs an age hint in
    the text."""
    latest = (post.get("postRange") or {}).get("latest")
    if not latest:
        return False
    try:
        post_date = datetime.fromisoformat(latest.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    return post_date >= datetime.utcnow() - timedelta(days=RECENT_POST_MAX_AGE_DAYS)


def run_contact_research(contact: Contact, db: Session, tenant_id: int) -> dict:
    if not contact.linkedin_url:
        raise DeeplineError("Contact has no LinkedIn URL to research")
    linkedin_id = contact.linkedin_url.rstrip("/").split("/")[-1]
    response = execute_tool("aviato_get_person_posts", {"linkedinID": linkedin_id})
    raw = response.get("toolResponse", {}).get("raw", {})
    posts = raw.get("results", []) if isinstance(raw, dict) else []
    recent_posts = [p for p in posts if _is_recent_post(p)]
    posts_text = "\n\n".join(p.get("commentary", "") for p in recent_posts if p.get("commentary"))
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


def generate_personalized_message(contact_id: int, db: Session, tenant_id: int, style: str = "pitch") -> PersonalizedMessage:
    """Entrypoint. Runs company/contact research either way, then branches on style:
    "pitch" (default, the original 4-module spec) runs fit_analysis and pitches our value
    proposition; "curiosity" (the lead's real, tested alternative -- genuine no-pitch outreach
    asking to learn from the contact's real experience) skips fit_analysis entirely since
    there's no value-prop positioning to compare against, and no reason to spend the extra
    tokens on it. Persists whatever succeeds even if a later step fails -- a partial result is
    still visible and useful, not silently discarded."""
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

        if style == "curiosity":
            message = run_curiosity_message_synthesis(contact, company_research, contact_research, db, tenant_id)
        else:
            fit_analysis = run_fit_analysis(company_research, contact_research, db, tenant_id)
            pm.fit_analysis = fit_analysis
            db.commit()
            message = run_message_synthesis(contact, company_research, contact_research, fit_analysis, db, tenant_id)

        pm.generated_message = message
        pm.generated_at = datetime.utcnow()
        pm.error_message = None
        db.commit()

        # Email channel -- only if this contact actually has a captured email (see
        # Contact.email, only ever populated via the paid Deepline decision-maker path).
        # Best-effort: a failure here must never wipe out the LinkedIn message that already
        # succeeded above.
        if style == "curiosity" and contact.email:
            try:
                email = run_curiosity_email_synthesis(contact, company_research, contact_research, db, tenant_id)
                pm.email_subject = email["subject"]
                pm.email_body = email["body"]
                db.commit()
            except ClaudeError as e:
                pm.error_message = (pm.error_message + " | " if pm.error_message else "") + f"email generation failed: {e}"
                db.commit()
    except (ScrapeError, ClaudeError, DeeplineError) as e:
        pm.error_message = str(e)
        db.commit()

    db.refresh(pm)
    return pm
