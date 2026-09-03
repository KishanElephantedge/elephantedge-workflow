"""Real, verified email lookup -- the step this pipeline never had.

THE BUG THIS FIXES. Every email in this system was a `firstname@domain` guess. Confirmed
2026-09-03 against the live database: of 241 Contact rows across all of V1 and V2, the
email_source breakdown was pattern_guess 140, jobo_company 6, null 95, deepline 0. Not one real
address had ever been resolved, since July.

The cause was not a bug in our code. decision_maker._make_contact reads
person["professional_email"] from search_contact's response, and that IS the correct field name --
it simply comes back null on every call. search_contact returns identity (name, title, LinkedIn,
location, work history) and leaves both email fields empty. Nothing downstream noticed, because
resolve_fallback_email silently produced a plausible-looking guess instead.

And the guesses were WRONG, not merely unverified: Immuta's CEO is mcarroll@immuta.com, where we
had been guessing matthew@immuta.com. That address would have bounced.

WHY A WATERFALL, AND WHY THIS ORDER. Deepline bills these PER RESULT, so a provider that finds
nothing costs nothing -- confirmed live: icypeas returned status NONE and charged $0.0000. That
makes a cheapest-first chain nearly free to attempt: the only time a later provider costs money is
when an earlier, cheaper one missed. Prices are Deepline's own, read from `tools describe`:

    icypeas_email_search      0.14 credits / $0.014
    hunter_email_finder       0.30 credits / $0.030
    leadmagic_email_finder    0.34 credits / $0.034

Hunter and LeadMagic both independently returned mcarroll@immuta.com on the live test, with
verification metadata (Hunter score 97, source_type "found"; LeadMagic status "valid" plus a real
MX record). Icypeas is first purely because it is cheapest, not because it is best -- it missed
that lookup.

WHAT THIS DOES NOT DO. It does not verify an address we did not resolve, and it never upgrades a
pattern guess to "verified". A guess stays labelled a guess.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.deepline_client import DeeplineError, execute_tool

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# (tool_id, source_label, payload_builder). Cheapest first -- a miss costs nothing.
PROVIDERS: list[tuple[str, str, callable]] = [
    (
        "icypeas_email_search", "icypeas",
        lambda first, last, domain, company: {
            "firstname": first, "lastname": last, "domainOrCompany": domain,
            "wait_for_completion": True,
        },
    ),
    (
        "hunter_email_finder", "hunter",
        lambda first, last, domain, company: {
            "domain": domain, "first_name": first, "last_name": last,
        },
    ),
    (
        "leadmagic_email_finder", "leadmagic",
        lambda first, last, domain, company: {
            "first_name": first, "last_name": last, "domain": domain,
            **({"company_name": company} if company else {}),
        },
    ),
]

# A provider that says it is unsure is treated as a miss. An unverified address from a paid
# provider is no better than the guess it would replace, and labelling it "verified" would put a
# fabricated fact into outreach.
REJECT_STATUSES = {"invalid", "unknown", "none", "not_found", "risky", "catch_all", "catch-all"}


def _extract(response: dict, domain: str) -> tuple[str, str | None] | None:
    """Pulls a usable email + its reported status out of a provider response.

    Providers disagree on shape (Hunter nests under `data`, LeadMagic is flat, Icypeas returns an
    `items[].results.emails` list), so this reads the raw JSON rather than encoding three schemas
    that will drift. The domain check is the real guard: a provider returning an address at a
    DIFFERENT domain has resolved the wrong person, which is the failure mode that matters.
    """
    import json

    payload = response.get("toolResponse") or response
    raw = payload.get("rawV2") or payload.get("raw") or payload
    blob = json.dumps(raw)

    status = None
    for key in ("\"status\":", "\"result\":", "\"state\":"):
        m = re.search(re.escape(key) + r'\s*"([^"]+)"', blob)
        if m:
            status = m.group(1).lower()
            break

    for candidate in EMAIL_RE.findall(blob):
        if candidate.split("@")[1].lower() != domain.lower():
            continue  # someone at another company -- wrong person, not a weaker answer
        if status and status in REJECT_STATUSES:
            return None
        return candidate.lower(), status
    return None


def find_verified_email(db: Session, tenant_id: int, first_name: str, last_name: str,
                        domain: str, company_name: str | None = None) -> dict | None:
    """Returns {"email", "source", "status", "attempts"} or None.

    Never raises: a provider outage must not take down contact discovery, which was working
    (badly) before this existed and must keep working if this fails.
    """
    if not first_name or not last_name or not domain:
        return None

    attempts = []
    for tool_id, label, build in PROVIDERS:
        try:
            response = execute_tool(tool_id, build(first_name.strip(), last_name.strip(), domain.strip(), company_name))
        except DeeplineError as e:
            attempts.append({"source": label, "status": "provider_error", "detail": str(e)[:200]})
            continue
        except Exception as e:  # noqa: BLE001 -- never let this break the caller
            attempts.append({"source": label, "status": "error", "detail": f"{type(e).__name__}: {e}"[:200]})
            continue

        found = _extract(response, domain)
        if found:
            email, status = found
            attempts.append({"source": label, "status": status or "found", "email": email})
            logger.info("email_finder: %s %s @ %s -> %s via %s", first_name, last_name, domain, email, label)
            return {"email": email, "source": f"deepline:{label}", "status": status or "found", "attempts": attempts}
        attempts.append({"source": label, "status": "not_found"})

    return {"email": None, "source": None, "status": "not_found", "attempts": attempts} and None
