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

CORRECTION, 2026-09-16 (real, repeated finding -- this claim is wrong for revenue and industry,
right only for cost and leads): Jobo's own bundled revenue/industry fields are NOT reliable enough
to qualify a company on. Already documented once (provider-cost-accuracy-tracker.md, 2026-09-08:
39% of a real 18-company batch had wildly wrong Jobo revenue, e.g. Fireworks AI -- Jobo said
$1-10M, real is $1B) and reconfirmed live again today on a real Amdrodd batch: Docker showed as
501 employees/$50-100M when it is really 1,000+/$500M-1B; Black Box showed as "Business Services"
when it is really IT Services and Consulting, 4,500+ employees; Scribe showed as $10-50M revenue
when it is really a $1.3B-valuation company at $100M+ ARR. All three would have been silently
qualified and shown to a partner as real, confirmed accounts.

Jobo is still the cheapest genuinely good tool for the JOB SEARCH itself ($0.003/job delivered,
$0 for non-matches, real leadership leads bundled free) -- keep using it for that. Never trust its
own revenue/industry/size fields as qualification fact. See run_tenant_discovery_jobo_verified()
below -- the real, permanent fix: run Jobo for discovery + leads, then the SAME free public-
LinkedIn-page check and the SAME Deepline-free/Google-cheap revenue waterfall the Apify path
already uses (company_profile_check.py, revenue_estimation.py) BEFORE trusting a single number
Jobo itself reported. One real verification path, reused, not a second one invented for this
source.

WHAT JOBO DOES NOT GIVE. Its leadership entries carry Crunchbase person URLs, not LinkedIn ones,
and no email addresses. Verified against the live API, not assumed: /api/people, /api/contacts,
/api/persons and /api/companies/{id}/people all 404, and a job record exposes only an apply_url.
Jobo has exactly two endpoints. So this finds the company and names the people; it does not
produce a SalesRobot-ready prospect.

AND THE PEOPLE DATA IS STALE. Checked against LinkedIn by a human on 2026-09-04, two of the five
leadership names this returned had left their company nine months earlier -- OpenLoop's Director
of Customer Success (departed Dec 2023) and Ontra's Senior Managing Director (departed Dec 2023) --
and both were still listed as current. Worse, the person who actually holds the buying role at
Ontra today (its Chief Revenue Officer) does not appear in Jobo's list at all.

Two further cases from an India batch the same day make it worse than staleness alone:

  - Log9 Materials' listed "Co-Founder and CEO" held that role Apr 2015 - Apr 2025. It ended
    seventeen months ago and is still returned as current.
  - Strides Pharma Science's listed "CEO - International Operations" is in fact a NON-EXECUTIVE
    DIRECTOR AND BOARD ADVISOR, and at Strides CONSUMER Pvt Ltd, a sibling entity. Both the title
    and the company were wrong -- a board role at one company presented as an operating CEO at
    another. Meanwhile the real Managing Director & Group CEO was absent from the list entirely.

So the failure mode is not only "this person left" but "this title is not what they do, at a
company they do not run". That is the wrong-company-attribution risk this codebase has already
been bitten by twice (Isabel -> Isabella Moore, and the Domaine revenue figure).

The cause is the source: this list is Crunchbase-derived, and Crunchbase people records are not
maintained on departure. So treat leadership here as a RESEARCH STARTING POINT that must be
confirmed before anyone is contacted -- never as a current org chart, and never as evidence of
what someone's job actually is. The company firmographics (size, revenue, industry, HQ) showed no
such problem across either batch and can be relied on.

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
# Real bug, 2026-09-16: this used to import a SEPARATE, duplicate _existing_domains() defined
# locally in jobo_discovery.py -- a near-identical copy that never got the rejected-domains merge
# (see discovery.py's own _existing_domains for why that merge exists). Cytek Biosciences and
# FormFactor, both already recorded as rejected, kept getting rediscovered through THIS import
# specifically because it pointed at the un-fixed copy. jobo_discovery.py's own copy is now
# deleted and re-exports this same one, so there is exactly one _existing_domains() in the
# codebase, not two silently drifting apart.
from app.phases.discovery import _existing_domains
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


# Real bug, confirmed live 2026-09-16: the single-word fallback below matched Amdrodd's
# "Industrial Machinery Manufacturing" against FormFactor and Cytek Biosciences (semiconductor
# equipment / lab instruments) purely because their profile said bare "Manufacturing" -- and
# matched "Wellness and Fitness Services" against Sikich, Couchbase, and CrossCountry Consulting
# purely because they're labeled generic "Business Services". A category word this broad tells
# you almost nothing about fit; treating it as a specific-enough token to match on is exactly the
# false-positive machine. Every one of these words appears across dozens of unrelated real
# industries in Jobo's own taxonomy -- never safe as a lone match signal.
_GENERIC_INDUSTRY_WORDS = {
    "manufacturing", "services", "research", "health", "care", "technology", "solutions",
    "systems", "group", "industry", "products", "consulting", "development", "software",
    "business", "management", "science", "sciences", "equipment", "industries",
}


def _industry_matches(profile: dict, industries: list[str]) -> bool:
    if not industries:
        return True
    hay = " ".join([str(profile.get("primary_industry") or "")] +
                   [str(i) for i in (profile.get("industries") or [])] +
                   [str(i) for i in (profile.get("categories") or [])]).lower()
    # Substring both ways: an ICP saying "Manufacturing" should match "Industrial Machinery
    # Manufacturing", and an ICP saying "Industrial Machinery Manufacturing" should match
    # "Manufacturing". Requiring exact equality against Jobo's own taxonomy would reject almost
    # everything on wording alone -- but the fallback token must be a real, SPECIFIC word (not a
    # generic category buzzword every industry shares) or it stops meaning anything.
    for want in industries:
        w = want.strip().lower()
        if not w:
            continue
        if w in hay:
            return True
        if any(tok in hay for tok in w.split() if len(tok) > 4 and tok not in _GENERIC_INDUSTRY_WORDS):
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
                # Same normalize-once fix as run_tenant_discovery_jobo's own copy of this block --
                # Jobo's website field is inconsistently "www."-prefixed across calls for the same
                # real company, so the stored domain must be normalized here too, for consistency.
                domain = (domain or "").lower().replace("www.", "") or None
                identity = domain or _fold_name(name)
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


def run_tenant_discovery_jobo(batch_id: int, db: Session, tenant_id: int, icp: dict, title_search: list[str],
                              target: int = 8, pages: int = 2, page_size: int = 25) -> dict:
    """Same real Jobo pipeline as run_partner_discovery_jobo (bucket parsing, dedup, geo/industry
    matching, operating-leadership filtering -- all reused, none reimplemented), but writing into
    an EXISTING tenant's batch instead of creating a partner tenant. Built 2026-09-07 so Elephant
    Edge's own daily discovery can use Jobo across all ICP profiles, the same way discovery_profiles.py
    already lets Apify do -- see that module for why one search per ICP (not per offering) covers
    every offering an ICP feeds exactly once.

    Also dedupes against every company this tenant has EVER seen (_existing_domains), not just
    this run -- run_partner_discovery_jobo doesn't need that (a partner tenant's own history is
    always empty on day one), but our own tenant has months of prior batches.

    Decision-makers are NOT auto-drafted or auto-pushed from this function -- Jobo's leadership
    list is Crunchbase-derived and this module's own docstring documents real staleness/wrong-
    attribution problems (stale titles, departed people still listed, one case of a board role at
    a DIFFERENT company presented as an operating CEO). Treat every Contact this creates as a
    starting point for manual verification, never as ready-to-push -- per the explicit instruction
    that today's contacts get found and enriched by hand, not auto-drafted."""
    titles = (title_search or [])[:MAX_QUERY_TERMS]
    if not titles:
        return {"status": "failed", "error": "title_search is required -- it is the buying signal for this profile"}
    geographies = icp.get("geographies") or []
    industries = icp.get("industries") or []
    rev_min, rev_max = icp.get("revenue_min_usd"), icp.get("revenue_max_usd")

    api_key = _get_api_key(db, tenant_id)
    excluded_domains = _existing_domains(tenant_id, db)  # now includes rejected domains too, see discovery.py

    seen: set[str] = set()
    seen_identity: set[str] = set()
    kept, dropped = [], []
    jobs_seen = 0
    credits_end = None

    with httpx.Client() as client:
        for page in range(1, pages + 1):
            if len(kept) >= target:
                break
            data, balance = search_jobs(client, api_key, titles, page, page_size, locations=geographies or None)
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
                profile = get_company_profile(client, cid) or {}
                name = profile.get("name") or co.get("name")
                if not name:
                    continue
                if geographies:
                    has_hq = bool(profile.get("headquarters_location") or profile.get("country_code"))
                    if has_hq and not _geo_matches(profile, geographies):
                        dropped.append((name, f"HQ elsewhere ({profile.get('headquarters_location')})"))
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
                # REAL BUG, confirmed live 2026-09-16 via the diagnostic below (now removed):
                # Jobo's own `website` field is inconsistent across calls for the SAME real
                # company -- FormFactor came back as "https://formfactor.com" once and
                # "http://www.formfactor.com" another time. `identity` was already www-stripped
                # for the seen_identity/dedup-within-this-run check, but `domain` itself (used for
                # BOTH the excluded_domains check AND the value stored on Company.domain) never
                # was -- so a "www."-prefixed variant silently bypassed exclusion and created a
                # second, differently-shaped domain string for a company already proven bad.
                # Now normalized once, used everywhere domain identity matters.
                domain = (domain or "").lower().replace("www.", "") or None
                identity = domain or _fold_name(name)
                if identity in seen_identity or (domain and domain in excluded_domains):
                    dropped.append((name, "duplicate (already kept this run, or already known to this tenant)"))
                    continue
                seen_identity.add(identity)
                emp_lo, emp_hi = _size_bounds(profile.get("company_size"))
                company = Company(
                    batch_id=batch_id, name=name, domain=domain,
                    industry=profile.get("primary_industry"),
                    employee_count=emp_lo,
                    location=profile.get("headquarters_location"),
                    linkedin_url=profile.get("linkedin_url"),
                    source="jobo_discovery",
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
                                   matched_title_reasoning="Operating leadership from Jobo company profile (free) -- UNVERIFIED, confirm before contacting"))
                db.commit()
                kept.append({"company": name, "domain": domain, "size": profile.get("company_size"),
                             "revenue_basis": basis, "revenue": [lo, hi],
                             "location": profile.get("headquarters_location"),
                             "job_title_seen": job.get("title"), "people": people})

    return {
        "status": "completed", "batch_id": batch_id,
        "jobs_seen": jobs_seen, "companies_evaluated": len(seen),
        "kept": kept, "dropped": dropped,
        "credits_used": jobs_seen * CREDITS_PER_JOB,
        "credits_balance": credits_end,
    }


# MOVED to app/phases/discovery.py, 2026-09-16 -- the same recurrence (Geneoscopy, re-discovered
# via the APIFY path this time, not Jobo) proved this needs to protect every discovery path, not
# just this one. get_rejected_domains()/add_rejected_domain() now live there, merged directly into
# _existing_domains() so all 5 real call sites (this module, apify_discovery.py, jobo_discovery.py,
# jd_first_discovery.py, discovery.py itself) get the fix automatically. Re-imported here under
# their original names so this module's own callers below don't need to change.
from app.phases.discovery import get_rejected_domains, add_rejected_domain as _add_rejected_domain


def verify_jobo_companies(db: Session, tenant_id: int, company_ids: list[int],
                          revenue_min_usd: int | None, revenue_max_usd: int | None,
                          employee_min: int | None, employee_max: int | None,
                          enrichment_tenant_id: int = 2) -> dict:
    """THE REAL FIX for Jobo's own unreliable revenue/industry fields (see this module's
    docstring correction above) -- runs every Jobo-discovered company through the SAME two free/
    cheap verification steps the Apify path already trusts, instead of qualifying on Jobo's own
    word. Reused, not reinvented:

    1. fetch_public_company_profile() + profile_rejection_reason() (company_profile_check.py) --
       free (one public HTTP fetch), catches a wrong industry/country/declared-size-band
       mismatch. This alone caught Marmon Foodservice Technologies (real 1,001-5,000 employees
       and Food and Beverage Services, not the 251/'Manufacturing' Jobo reported) at zero cost.
    2. estimate_company_revenue() (revenue_estimation.py) -- Deepline's free identify call first,
       then Google AI Overview (~$0.0085/company) only if that misses. Jobo's own revenue is
       cleared first so this never no-ops on a number we now know cannot be trusted.

    A company that fails the free profile check is deleted immediately, before any paid call --
    same "free filters before paid ones" discipline as apify_discovery.py's own keep loop. A
    company whose real revenue cannot be independently confirmed (both sources miss, or Google's
    actor is unavailable -- e.g. a real monthly platform quota, not a budget block) keeps Jobo's
    own number but the caller must treat it as UNVERIFIED, never as confirmed.

    Returns {verified, corrected, dropped, unverified} -- every company ends up in exactly one
    bucket, never silently skipped."""
    from app.phases.company_profile_check import fetch_public_company_profile, profile_rejection_reason
    from app.gtm_os.icp.revenue_estimation import estimate_company_revenue

    result = {"verified": [], "corrected": [], "dropped": [], "unverified": []}

    for company_id in company_ids:
        company = db.get(Company, company_id)
        if company is None:
            continue
        batch = db.get(Batch, company.batch_id)
        if batch is None or batch.tenant_id != tenant_id:
            continue  # safety: never verify/delete a company outside the caller's own tenant

        if company.linkedin_url:
            profile = fetch_public_company_profile(company.linkedin_url)
            if profile is not None:
                reason = profile_rejection_reason(profile, employee_min, employee_max)
                if reason:
                    result["dropped"].append({"name": company.name, "reason": f"free_profile_check:{reason}"})
                    _add_rejected_domain(db, tenant_id, company.domain)
                    db.delete(company)
                    db.commit()
                    continue

        jobo_lo, jobo_hi = company.estimated_revenue_lower_usd, company.estimated_revenue_higher_usd
        company.estimated_revenue_lower_usd = None
        company.estimated_revenue_higher_usd = None
        db.commit()
        revenue_result = estimate_company_revenue(db, enrichment_tenant_id, company)

        if revenue_result["status"] == "resolved":
            lo, hi = revenue_result["lower_usd"], revenue_result["higher_usd"]
            below = isinstance(revenue_min_usd, int) and hi is not None and hi < revenue_min_usd
            above = isinstance(revenue_max_usd, int) and lo is not None and lo > revenue_max_usd
            if below or above:
                result["dropped"].append({"name": company.name, "reason": f"real_revenue_out_of_band [{lo}, {hi}] (Jobo had said [{jobo_lo}, {jobo_hi}])"})
                _add_rejected_domain(db, tenant_id, company.domain)
                db.delete(company)
            elif (lo, hi) != (jobo_lo, jobo_hi):
                result["corrected"].append({"name": company.name, "jobo_said": [jobo_lo, jobo_hi], "real": [lo, hi]})
            else:
                result["verified"].append({"name": company.name, "revenue": [lo, hi]})
        else:
            # Could not independently confirm -- restore Jobo's own number rather than leave the
            # company unpriced, but this bucket is the caller's signal to never present it as
            # confirmed. See estimate_company_revenue's own "attempts" list for exactly why it
            # missed (e.g. a real Apify monthly quota, not a bug).
            company.estimated_revenue_lower_usd = jobo_lo
            company.estimated_revenue_higher_usd = jobo_hi
            result["unverified"].append({"name": company.name, "jobo_said": [jobo_lo, jobo_hi], "why": revenue_result["attempts"]})
        db.commit()

    return result
