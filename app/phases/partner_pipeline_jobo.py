"""Partner-scoped company discovery sourced from Jobo, not Apify.

WHY A SECOND SOURCE. partner_pipeline.run_partner_discovery searches LinkedIn job postings via
Apify, which exposes no revenue field -- so every discovered company arrives unpriced and needs a
separate revenue-enrichment pass (Google AI Overview, then Deepline). Two consequences: the run
costs more than the search itself, and with Deepline switched off (see deepline_client's operator
kill switch) half that waterfall is unavailable.

Jobo returns the whole picture in one cycle. A metered job search gives the posting, and the
company profile lookup behind it is FREE and unmetered -- carrying company_size, revenue,
industries, headquarters, funding AND a leadership list. So one paid call yields the firmographics
that Apify needs a paid enrichment pass to approximate, plus decision makers at no cost.

WHAT JOBO DOES NOT GIVE. Its leadership entries carry Crunchbase person URLs, not LinkedIn ones,
and no email addresses. So this finds the company and names the people; it does not produce a
SalesRobot-ready prospect. That is a real limit, not an oversight -- outreach still needs a
LinkedIn URL from somewhere else.

ISOLATION is unchanged and deliberate: one tenant per partner, reusing
partner_pipeline.get_or_create_partner_tenant, so nothing a partner's search finds can ever leak
into Elephant Edge's own pipeline. Every company selector in this codebase filters on
Batch.tenant_id, so a partner's companies are invisible to our own runs by construction.
"""
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.db.models import Batch, Company, Contact
from app.jobo_client import _get_api_key, get_company_profile, search_jobs
from app.phases.decision_maker import is_board_only_title
from app.phases.partner_pipeline import PARTNER_BATCH_SOURCE, get_or_create_partner_tenant

# Jobo bills per DELIVERED job, measured live at 3 credits each (1,000,015 -> 1,000,000 for 5).
CREDITS_PER_JOB = 3
# Jobo's search rejects more than 10 query terms (400, "Too many search terms") -- confirmed live.
MAX_QUERY_TERMS = 10

REVENUE_PER_EMPLOYEE_USD = 79_545


def _fold_name(name: str) -> str:
    """Normalised company name for dedupe.

    Jobo issues a separate company id per ATS listing, so one company can appear several times
    under slightly different names -- a single real search returned "Eightfold", "Eightfold AI"
    and "Eightfold AI" as three ids, which filled 3 of a partner's 5 slots with one company.
    Deduping on the id alone cannot catch that; the domain and the folded name can.
    """
    import re
    n = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    for suffix in ("inc", "llc", "ltd", "limited", "corp", "corporation", "gmbh", "co", "ai"):
        if n.endswith(suffix) and len(n) > len(suffix) + 2:
            n = n[: -len(suffix)]
    return n


def _size_bounds(company_size: str | None) -> tuple[int | None, int | None]:
    """Jobo reports headcount as a bucket string ("51-100", "1001-5000", "10001+")."""
    if not company_size:
        return None, None
    s = str(company_size).replace(",", "").strip()
    if s.endswith("+"):
        lo = s[:-1]
        return (int(lo), None) if lo.isdigit() else (None, None)
    parts = s.split("-")
    if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
        return int(parts[0]), int(parts[1])
    return (int(s), int(s)) if s.isdigit() else (None, None)


# Jobo reports revenue as a BUCKET STRING with magnitude suffixes -- "200m-1b", "1m-10m",
# "under-1m", "10b+". Stripping non-digits (the first version of this) turned "200m-1b" into
# 2001 dollars, and Asana was then dropped for being "below $25,000,000" when it is a $200M-$1B
# company. A silent three-orders-of-magnitude error that reads as a legitimate rejection is far
# worse than a crash, so every bound is parsed explicitly and an unrecognised string returns
# None (unknown) rather than a number.
_MAGNITUDE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}


def _parse_money(token: str) -> int | None:
    t = token.strip().lower().lstrip("$").replace(",", "").rstrip("+")
    if not t:
        return None
    multiplier = 1
    if t[-1] in _MAGNITUDE:
        multiplier = _MAGNITUDE[t[-1]]
        t = t[:-1]
    try:
        return int(float(t) * multiplier)
    except ValueError:
        return None


def _parse_revenue_bucket(raw: str) -> tuple[int | None, int | None]:
    s = raw.strip().lower()
    if s.startswith("under-") or s.startswith("under "):
        hi = _parse_money(s.split("-", 1)[-1] if "-" in s else s.split(" ", 1)[-1])
        return (0, hi) if hi is not None else (None, None)
    if s.startswith("over-") or s.endswith("+"):
        lo = _parse_money(s.split("-", 1)[-1] if s.startswith("over-") else s)
        return (lo, None) if lo is not None else (None, None)
    if "-" in s:
        a, _, b = s.partition("-")
        lo, hi = _parse_money(a), _parse_money(b)
        if lo is not None or hi is not None:
            return lo, hi
        return None, None
    one = _parse_money(s)
    return (one, one) if one is not None else (None, None)


def _revenue_bounds(profile: dict) -> tuple[int | None, int | None, str]:
    """Real revenue when Jobo has it, else a headcount proxy that says so.

    Never silently presents a derived figure as a reported one -- the same discipline
    icp_matching.py's own revenue proxy follows.
    """
    raw = profile.get("revenue")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw), int(raw), "jobo_reported_revenue"
    if isinstance(raw, str) and raw.strip():
        lo, hi = _parse_revenue_bucket(raw)
        if lo is not None:
            return lo, hi, f"jobo_reported_revenue({raw})"
    lo, hi = _size_bounds(profile.get("company_size"))
    if lo:
        return (lo * REVENUE_PER_EMPLOYEE_USD,
                (hi or lo) * REVENUE_PER_EMPLOYEE_USD,
                f"DERIVED from company_size={profile.get('company_size')!r} x ${REVENUE_PER_EMPLOYEE_USD:,}/employee -- not a reported figure")
    return None, None, "no revenue and no headcount on file"


def _geo_matches(profile: dict, geographies: list[str]) -> bool:
    """Word-boundary match, never a substring one.

    "Fort Wayne, Indiana, United States" contains the substring "india", so a plain `in` test
    passed Franklin Electric -- an Indiana company -- as a match for an India ICP. Confirmed live.
    The same trap exists for other pairs (Niger/Nigeria, Oman/Romania), so this matches whole
    words rather than adding a special case for one country.
    """
    import re

    if not geographies:
        return True
    hay = " ".join(str(profile.get(k) or "") for k in
                   ("headquarters_location", "headquarters_region", "country_code", "continent")).lower()
    for g in geographies:
        g = g.strip().lower()
        if g and re.search(rf"\b{re.escape(g)}\b", hay):
            return True
    return False


def _industry_matches(profile: dict, industries: list[str]) -> bool:
    if not industries:
        return True
    hay = " ".join([str(profile.get("primary_industry") or "")] +
                   [str(i) for i in (profile.get("industries") or [])] +
                   [str(i) for i in (profile.get("categories") or [])]).lower()
    # Substring both ways: an ICP saying "Manufacturing" should match "Industrial Machinery
    # Manufacturing", and an ICP saying "Industrial Machinery Manufacturing" should match
    # "Manufacturing". Requiring exact equality against Jobo's own taxonomy would reject almost
    # everything on wording alone.
    for want in industries:
        w = want.strip().lower()
        if not w:
            continue
        if w in hay or any(tok in hay for tok in w.split() if len(tok) > 4):
            return True
    return False


def _operating_leadership(profile: dict) -> list[dict]:
    """Only people who actually run the company.

    Jobo's leadership list is Crunchbase-sourced and is mostly advisors and board members -- the
    PyTorch Lightning profile returned 8 entries of which 1 was an operator. Shipping those to a
    partner as 'decision makers' is the same error that put board members on Remy's list.
    """
    out = []
    for person in (profile.get("leadership") or []):
        # Jobo writes these as machine tokens -- "board_member", "advisor" -- not prose, so the
        # underscores are normalised before the shared title check. Without this,
        # is_board_only_title("board_member") is False (it matches "board member", with a space)
        # and board members ship to a partner as decision makers, which is exactly the error that
        # put five board members on Remy's list. "advisor"/"investor" are added here rather than
        # to the shared marker list because they are Crunchbase's vocabulary, not job-title prose.
        raw_title = (person.get("title") or "").strip()
        title = raw_title.replace("_", " ").strip()
        name = (person.get("name") or "").strip()
        if not name or not title:
            continue
        if is_board_only_title(title) or title.lower() in ("advisor", "adviser", "investor", "mentor"):
            continue
        out.append({"name": name, "title": raw_title, "url": person.get("linkedin_url")})
    return out


def run_partner_discovery_jobo(db: Session, partner_name: str, icp: dict, target: int = 5,
                               title_search: list[str] | None = None, dry_run: bool = True,
                               ee_tenant_id: int = 2, pages: int = 2, page_size: int = 25,
                               trust_search_location: bool = False) -> dict:
    """Find companies for one partner, in their own tenant, sourced from Jobo.

    trust_search_location: Jobo's `locations` is a real SERVER-SIDE filter, so every posting
    returned is already in the requested country. Re-checking the company PROFILE's headquarters
    afterwards then rejects companies whose profile simply has no HQ field -- measured on a real
    India run, that discarded a large share of the results for missing data rather than for being
    a bad fit. Set this when a posting in the country is sufficient evidence (a company hiring in
    India operates in India), which is what a market-entry partner actually cares about.
    """
    titles = (title_search or [])[:MAX_QUERY_TERMS]
    if not titles:
        return {"status": "failed", "error": "title_search is required -- it is the buying signal for this partner"}
    geographies = icp.get("geographies") or []
    industries = icp.get("industries") or []
    rev_min, rev_max = icp.get("revenue_min_usd"), icp.get("revenue_max_usd")

    plan = {
        "partner": partner_name, "queries": titles, "locations": geographies,
        "pages": pages, "page_size": page_size,
        "max_jobs": pages * page_size,
        "estimated_max_credits": pages * page_size * CREDITS_PER_JOB,
        "icp_filters_applied_after": {"industries": industries, "revenue_usd": [rev_min, rev_max]},
        "note": "company profiles (firmographics + leadership) are free; only the job search is metered",
    }
    if dry_run:
        return {"status": "dry_run", "plan": plan}

    api_key = _get_api_key(db, ee_tenant_id)
    tenant = get_or_create_partner_tenant(db, partner_name)
    batch = Batch(tenant_id=tenant.id, name=f"{partner_name} ICP (jobo) — {datetime.utcnow():%Y-%m-%d %H:%M}",
                  source=PARTNER_BATCH_SOURCE, current_phase="signal_discovery")
    db.add(batch)
    db.commit()

    seen: set[str] = set()
    seen_identity: set[str] = set()   # folded name / domain -- see _fold_name
    kept, dropped = [], []
    jobs_seen = 0
    credits_start = credits_end = None

    with httpx.Client() as client:
        for page in range(1, pages + 1):
            if len(kept) >= target:
                break
            data, balance = search_jobs(client, api_key, titles, page, page_size, locations=geographies or None)
            credits_start = balance if credits_start is None else credits_start
            credits_end = balance
            jobs = data.get("jobs") or []
            jobs_seen += len(jobs)
            if not jobs:
                break
            for job in jobs:
                if len(kept) >= target:
                    break
                co = job.get("company") or {}
                cid = co.get("id")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                profile = get_company_profile(client, cid) or {}   # free
                name = profile.get("name") or co.get("name")
                if not name:
                    continue
                # Geography, three-way rather than two. A strict profile-HQ check discards every
                # company whose profile simply lacks an HQ field (a large share of Indian results),
                # rejecting them for missing data rather than for being a bad fit. But trusting the
                # search location alone is worse: Jobo's `locations` filter matches the POSTING, so
                # "India" returned Arista, Lam Research, Nike, Amazon and Johnson Matthey -- US and
                # UK multinationals hiring in India, the opposite of a partner's India-market ICP.
                # So: an HQ that is present and elsewhere is a real rejection; only a MISSING HQ
                # falls back to the posting's own location.
                if geographies:
                    has_hq = bool(profile.get("headquarters_location") or profile.get("country_code"))
                    if has_hq:
                        if not _geo_matches(profile, geographies):
                            dropped.append((name, f"HQ elsewhere ({profile.get('headquarters_location')})"))
                            continue
                    elif not trust_search_location:
                        dropped.append((name, "no HQ on file and posting location not trusted"))
                        continue
                if not _industry_matches(profile, industries):
                    dropped.append((name, f"industry ({profile.get('primary_industry')})"))
                    continue
                lo, hi, basis = _revenue_bounds(profile)
                if isinstance(rev_min, int) and hi is not None and hi < rev_min:
                    dropped.append((name, f"revenue below ${rev_min:,} ({basis})"))
                    continue
                if isinstance(rev_max, int) and lo is not None and lo > rev_max:
                    dropped.append((name, f"revenue above ${rev_max:,} ({basis})"))
                    continue

                website = profile.get("website") or ""
                domain = website.replace("https://", "").replace("http://", "").strip("/").split("/")[0] or None
                identity = (domain or "").lower().replace("www.", "") or _fold_name(name)
                if identity in seen_identity:
                    dropped.append((name, "duplicate of a company already kept (different Jobo id)"))
                    continue
                seen_identity.add(identity)
                emp_lo, emp_hi = _size_bounds(profile.get("company_size"))
                company = Company(
                    batch_id=batch.id, name=name, domain=domain,
                    industry=profile.get("primary_industry"),
                    employee_count=emp_lo,
                    location=profile.get("headquarters_location"),
                    linkedin_url=profile.get("linkedin_url"),
                    source="jobo_partner_discovery",
                    estimated_revenue_lower_usd=lo, estimated_revenue_higher_usd=hi,
                )
                db.add(company)
                db.flush()

                people = _operating_leadership(profile)
                for p in people:
                    first, _, last = p["name"].partition(" ")
                    db.add(Contact(company_id=company.id, first_name=first, last_name=last or None,
                                   title=p["title"], linkedin_url=p.get("url"),
                                   email=None, email_source=None, thread_role="primary",
                                   matched_title_reasoning="Operating leadership from Jobo company profile (free)"))
                db.commit()
                kept.append({"company": name, "domain": domain, "size": profile.get("company_size"),
                             "revenue_basis": basis, "revenue": [lo, hi],
                             "location": profile.get("headquarters_location"),
                             "job_title_seen": job.get("title"), "people": people})

    return {
        "status": "completed", "partner": partner_name, "tenant_id": tenant.id, "batch_id": batch.id,
        "jobs_seen": jobs_seen, "companies_evaluated": len(seen),
        "kept": kept, "dropped": dropped,
        # Jobo returns the balance only AFTER a call, so the true starting balance is never seen.
        # Spend is therefore derived from jobs actually delivered at the measured rate, not from a
        # first-to-last difference (which reads 0 on a single-page run and understates every run).
        "credits_used": jobs_seen * CREDITS_PER_JOB,
        "credits_balance": credits_end,
    }
