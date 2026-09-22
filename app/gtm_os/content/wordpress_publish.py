"""Direct WordPress publishing (2026-09-22, explicit instruction) -- "no need to touch any
WordPress", a single Publish click in this dashboard sends the real post straight to a live
WordPress post. Reuses WordPress's own core REST API (`/wp-json/wp/v2/posts`) with an
Application Password (a built-in WP core feature since 5.6, no plugin required) -- works
regardless of which SEO plugin (AI Rank Lab or otherwise) is installed on the site, since it's
WordPress's own API, not a plugin-specific one.

Credentials are stored in the existing generic `credentials` table (Settings page), the same
mechanism every other integration key in this backend already uses -- no new table. Three named
credentials: wordpress_site_url, wordpress_username, wordpress_app_password.

status="publish" by default per the explicit instruction: click Publish here, it's live -- no
second manual step in WordPress. This is still one explicit human action (the user's own click
on an already-reviewed draft in this dashboard), consistent with the "never auto-publish without
an explicit action" discipline everywhere else in this codebase; it just doesn't require a
SECOND explicit action inside WordPress itself."""

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

WP_SITE_URL_CRED = "wordpress_site_url"
WP_USERNAME_CRED = "wordpress_username"
WP_APP_PASSWORD_CRED = "wordpress_app_password"


def _get_credential(db: Session, tenant_id: int, name: str) -> str | None:
    row = db.query(Credential).filter(Credential.tenant_id == tenant_id, Credential.name == name).first()
    return row.value if row else None


def get_wordpress_config(db: Session, tenant_id: int) -> dict | None:
    site_url = _get_credential(db, tenant_id, WP_SITE_URL_CRED)
    username = _get_credential(db, tenant_id, WP_USERNAME_CRED)
    app_password = _get_credential(db, tenant_id, WP_APP_PASSWORD_CRED)
    if not (site_url and username and app_password):
        return None
    return {"site_url": site_url.rstrip("/"), "username": username, "app_password": app_password}


def _plain_text_to_html(text: str) -> str:
    """Drafts are generated as plain text (blank line between paragraphs, no markdown -- see
    content_opportunity.py's DRAFT_PROMPT) so they're copy-paste safe everywhere else; WordPress's
    REST API expects HTML for `content`, so this is the one place that needs a real conversion."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "\n".join(f"<p>{p}</p>" for p in paragraphs)


def publish_blog_to_wordpress(db: Session, tenant_id: int, title: str, draft_text: str, status: str = "publish") -> dict:
    """Returns {"status": "not_configured" | "request_failed" | "wordpress_error" | "ok", ...}.
    Never raises -- a WordPress outage or a wrong credential must surface as a real, readable
    reason in the dashboard, not a 500."""
    config = get_wordpress_config(db, tenant_id)
    if config is None:
        return {"status": "not_configured", "reason": "WordPress site URL/username/application password not set yet -- add them in Settings"}

    try:
        response = httpx.post(
            f"{config['site_url']}/wp-json/wp/v2/posts",
            auth=(config["username"], config["app_password"]),
            json={"title": title, "content": _plain_text_to_html(draft_text), "status": status},
            timeout=20,
        )
    except httpx.HTTPError as e:
        return {"status": "request_failed", "reason": str(e)}

    if response.status_code not in (200, 201):
        return {"status": "wordpress_error", "reason": response.text[:500], "http_status": response.status_code}

    data = response.json()
    return {"status": "ok", "post_id": data.get("id"), "post_url": data.get("link")}
