"""Google Custom Search JSON API client (2026-10-12) -- direct, cheaper replacement for Apify's
Google-search actor (search_google_organic_results) specifically for sense_web_search_trends()/
sense_competitor_content() in app/gtm_os/intelligence/sensing.py. Same real provider (Google
Search), different path to it: a direct API call instead of paying Apify's markup to run an
actor that itself just calls Google.

ONE real Google Cloud project/API key behind every tenant's call -- same "one account, shared
credential, billed centrally" pattern as apify_client.py's own Elephant-Edge-fallback (see that
module's own ELEPHANT_EDGE_TENANT_ID/_get_api_key). Stored as Credential rows on Elephant Edge's
tenant (google_custom_search_api_key, google_custom_search_cx), set 2026-10-12.

QUOTA, FAIL CLOSED: Google's Custom Search API free tier is 100 queries/day, hard-capped unless
billing is explicitly enabled on the Cloud project -- not confirmed enabled here, so this never
assumes paid capacity exists. A shared daily counter (Parameter marker on Elephant Edge's tenant,
since it's the one real account/quota every tenant's calls draw from) refuses the 101st call of
the day rather than risk an unbudgeted charge -- same "real provider quota is the hard wall,
never silently exceeded" discipline as llm_client.py's own Gemini daily-quota tracking."""

from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential, Parameter

ELEPHANT_EDGE_TENANT_ID = 2
FREE_DAILY_QUOTA = 100
DAILY_COUNT_PARAMETER_KEY = "google_cse_daily_call_count"


class GoogleCseError(Exception):
    pass


def _get_credentials(db: Session, tenant_id: int) -> tuple[str, str]:
    api_key_cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id, Credential.name == "google_custom_search_api_key")
        .first()
    )
    cx_cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id, Credential.name == "google_custom_search_cx")
        .first()
    )
    if api_key_cred and api_key_cred.value and cx_cred and cx_cred.value:
        return api_key_cred.value, cx_cred.value
    if tenant_id != ELEPHANT_EDGE_TENANT_ID:
        return _get_credentials(db, ELEPHANT_EDGE_TENANT_ID)
    raise GoogleCseError("google_custom_search_api_key/google_custom_search_cx credentials are not set")


def _check_and_increment_daily_quota(db: Session) -> None:
    """Tracked on ELEPHANT_EDGE_TENANT_ID regardless of which tenant's call triggered it -- it is
    the one real account/quota, same reasoning apify_client.py's billing_tenant_id() documents
    for why a per-partner budget would be fiction when only one real account exists behind every
    call."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID, Parameter.key == DAILY_COUNT_PARAMETER_KEY)
        .first()
    )
    count = param.value.get("count", 0) if param and isinstance(param.value, dict) and param.value.get("date") == today else 0
    if count >= FREE_DAILY_QUOTA:
        raise GoogleCseError(
            f"Google Custom Search free daily quota ({FREE_DAILY_QUOTA}/day) reached for {today} -- "
            "refusing further calls rather than risk an unbudgeted paid charge"
        )

    new_value = {"date": today, "count": count + 1}
    if param:
        param.value = new_value
    else:
        param = Parameter(
            tenant_id=ELEPHANT_EDGE_TENANT_ID, key=DAILY_COUNT_PARAMETER_KEY, value=new_value,
            description="Shared daily call counter for the one real Google Custom Search API key/quota -- fails closed at the free tier limit.",
        )
        db.add(param)
    db.commit()


def search_google_custom(db: Session, tenant_id: int, query: str, num: int = 10) -> list[dict]:
    """Returns [{"title": ..., "url": ..., "description": ...}, ...] -- the same shape
    search_google_organic_results() (Apify) already returned, so sensing.py's callers need no
    change beyond which function they call and what they pass as the api key."""
    api_key, cx = _get_credentials(db, tenant_id)
    _check_and_increment_daily_quota(db)

    try:
        response = httpx.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": api_key, "cx": cx, "q": query, "num": min(num, 10)},
            timeout=15,
        )
    except httpx.HTTPError as e:
        raise GoogleCseError(f"Google Custom Search request failed: {e}") from e

    if response.status_code != 200:
        raise GoogleCseError(f"Google Custom Search returned {response.status_code}: {response.text[:300]}")

    data = response.json()
    return [
        {"title": item.get("title"), "url": item.get("link"), "description": item.get("snippet")}
        for item in data.get("items", [])
    ]
