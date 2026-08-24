"""Semantic JD-content classifier for linkedin_job GtmSignals -- distinguishes a genuine
first-sales-hire / building-the-function-from-scratch signal from an ordinary scaling hire, using
the real job description body captured in extracted_info["description_text"] (2026-08-24 addition
-- previously unused, see sensing.py). This does NOT replace the existing deterministic
role-title interpretation in interpretation.py's _interpret_job_signal (ROLE_KEYWORDS/
_classify_role) -- it is a SEPARATE, additive enrichment step: after the base hiring_activity
event is created (title-based, unchanged, contextual tier), this classifier may additionally
promote it to "first_sales_hire_signal" (implied_gap tier, eligible to open a hypothesis) when the
REAL JD text itself supports it -- never based on the title or company size alone, and never
applied to theirstack_job (no JD body is captured for that source).

Real motivation: problem_detection.py's own docstring names the exact gap this closes -- "a title
match ... cannot be reliably distinguished from an ordinary scaling/organizational hire" without
JD content. That JD content exists in the raw provider response (a free byproduct of the same
billed Apify call) and is now captured; this module is what actually reads it.

Fails closed: any LLM error, missing description_text, or an unverifiable quote all result in no
promotion -- the signal stays plain hiring_activity, exactly as before this module existed."""

from app.llm_client import generate_json

FIRST_HIRE_EVENT_TYPE = "first_sales_hire_signal"

_DEFINITIONS = """- first_sales_hire_signal: the job description ITSELF clearly indicates this is the company's
  first dedicated sales hire, or that they are building a sales function/process from scratch
  (e.g. "you'll build our sales playbook from zero", "no sales team exists yet", "founding sales
  hire", "you'll define our entire GTM motion"). Must be supported by the JD text itself, not
  inferred from the title, seniority label, or company size alone.
- ordinary_hire: an additional/replacement hire into an existing sales team or process -- the
  normal case, and the correct default when the JD doesn't clearly say otherwise.
- unclear: the JD text doesn't give enough real signal either way."""


def classify_job_description(title: str, description_text: str, db, tenant_id: int) -> dict | None:
    """Returns {"event_type": "first_sales_hire_signal", "quote": "..."} only when the real JD
    text supports it and the quote is verified present in the text (same evidence-integrity
    discipline as linkedin_post_interpretation.py's semantic fallback) -- otherwise None."""
    if not description_text:
        return None

    prompt = f"""You are reading a real job posting for a B2B sales-intelligence system.

Category definitions (choose exactly one):
{_DEFINITIONS}

Job title: {title or "(not given)"}

Job description:
\"\"\"{description_text[:4000]}\"\"\"

Return JSON exactly:
{{"event_type": "first_sales_hire_signal" | "ordinary_hire" | "unclear", "quote": "<exact sentence/clause from the job description above that supports first_sales_hire_signal, verbatim, or empty string otherwise>"}}"""
    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=250)
    except Exception:
        return None  # LLM unavailable -- fail closed, no promotion

    event_type = response.get("event_type")
    if event_type != FIRST_HIRE_EVENT_TYPE:
        return None  # ordinary_hire / unclear / invalid -> stays plain hiring_activity

    quote = (response.get("quote") or "").strip()
    if not quote or quote.lower() not in description_text.lower():
        return None  # unverifiable/hallucinated evidence -- discarded, not trusted

    return {"event_type": FIRST_HIRE_EVENT_TYPE, "quote": quote}
