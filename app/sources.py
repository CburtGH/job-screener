"""Job sources. Each source returns a list of normalized job dicts.

Indeed and ZipRecruiter have no public search API, so their postings come in through JSearch
(OpenWeb Ninja, or its RapidAPI listing), which indexes Google for Jobs and reports the original publisher for each posting.
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db, logs

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
UA = "JobScout/1.0 (personal job search tool)"


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?i)<\s*br\s*/?>|</\s*(p|div|li|h\d)\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def dedupe_key(title: str, company: str | None) -> str:
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
    return hashlib.sha1(f"{norm(title)}|{norm(company)}".encode()).hexdigest()[:16]


def make_job(**kw) -> dict:
    job = {
        "source": kw["source"],
        "external_id": str(kw["external_id"]),
        "title": strip_html(kw.get("title")),
        "company": strip_html(kw.get("company")) or None,
        "location": kw.get("location"),
        "is_remote": bool(kw.get("is_remote")),
        "employment_type": kw.get("employment_type"),
        "salary_min": kw.get("salary_min"),
        "salary_max": kw.get("salary_max"),
        "salary_text": kw.get("salary_text"),
        "url": kw.get("url"),
        "apply_url": kw.get("apply_url") or kw.get("url"),
        "publisher": kw.get("publisher"),
        "description": kw.get("description") or "",
        "posted_at": kw.get("posted_at"),
    }
    job["dedupe_key"] = dedupe_key(job["title"], job["company"])
    return job


def _days_to_jsearch(days: int) -> str:
    if days <= 1:
        return "today"
    if days <= 3:
        return "3days"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "all"


def _throttled(source: str, hours: float) -> bool:
    """Free community boards ask clients not to poll aggressively. Skip if fetched recently."""
    last = db.kv_get(f"last_fetch:{source}")
    if last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(hours=hours):
        return True
    db.kv_set(f"last_fetch:{source}", db.now_iso())
    return False


class SourceError(Exception):
    pass


# ---- JSearch (Indeed / ZipRecruiter / LinkedIn / Glassdoor via Google for Jobs) -------------

# JSearch is sold two ways, and a key from one doesn't work on the other:
#   - OpenWeb Ninja directly (app.openwebninja.com): GET https://api.openwebninja.com/jsearch/search-v2,
#     header x-api-key, response {"data": {"jobs": [...], "cursor": ...}}
#   - RapidAPI (the original listing): GET https://jsearch.p.rapidapi.com/search, headers X-RapidAPI-Key/-Host,
#     response {"data": [...]}
# We try OpenWeb Ninja first, fall back to RapidAPI if it rejects the key, and remember what worked (kv
# "jsearch_channel") so later runs make one call per title.
JSEARCH_CHANNELS = {
    "openwebninja": {"url": "https://api.openwebninja.com/jsearch/search-v2", "headers": lambda k: {"x-api-key": k}},
    "rapidapi": {"url": "https://jsearch.p.rapidapi.com/search",
                 "headers": lambda k: {"X-RapidAPI-Key": k, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}},
}


def _jsearch_error(r: httpx.Response) -> str:
    try:
        msg = (r.json() or {}).get("message") or ""
    except ValueError:
        msg = ""
    return msg or f"HTTP {r.status_code}"


def fetch_jsearch(prefs: dict) -> list[dict]:
    key = config.get_secret("jsearch_api_key")
    if not key:
        raise SourceError("JSearch key not set (Settings > API keys)")
    remembered = db.kv_get("jsearch_channel")
    order = [remembered] + [c for c in JSEARCH_CHANNELS if c != remembered] if remembered in JSEARCH_CHANNELS \
        else list(JSEARCH_CHANNELS)
    jobs: list[dict] = []
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, event_hooks=logs.httpx_hooks('JSearch')) as client:
        for title in prefs["target_titles"]:
            query = title if prefs["remote_only"] else f"{title} in {', '.join(prefs['locations'])}"
            params = {
                "query": query,
                "num_pages": 1,
                "country": prefs.get("country", "us"),
                "date_posted": _days_to_jsearch(int(prefs["posted_within_days"])),
            }
            if prefs["remote_only"]:
                params["work_from_home"] = "true"
            if prefs.get("employment_types"):
                params["employment_types"] = ",".join(prefs["employment_types"])
            errors = []
            for channel in order:
                ch = JSEARCH_CHANNELS[channel]
                r = client.get(ch["url"], params=params, headers=ch["headers"](key))
                if r.status_code in (401, 403):
                    errors.append(f"{'OpenWeb Ninja' if channel == 'openwebninja' else 'RapidAPI'}: {_jsearch_error(r)}")
                    continue
                if channel != remembered:
                    db.kv_set("jsearch_channel", channel)
                    remembered = channel
                order = [channel]  # this key works here; skip the other channel for the rest of the run
                break
            else:
                raise SourceError(
                    "JSearch rejected the key (" + "; ".join(errors) + "). Use a key from app.openwebninja.com, or "
                    "subscribe to JSearch's free Basic plan on RapidAPI with the account that owns this key.")
            if r.status_code == 429:
                db.log("JSearch rate limit or monthly quota hit; stopping this run early", "warn")
                break
            r.raise_for_status()
            data = r.json().get("data") or []
            for j in (data.get("jobs") or []) if isinstance(data, dict) else data:
                loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if x)
                sal_text = None
                if j.get("job_min_salary") or j.get("job_max_salary"):
                    sal_text = f"{j.get('job_min_salary') or '?'}–{j.get('job_max_salary') or '?'} {j.get('job_salary_period') or ''}".strip()
                jobs.append(make_job(
                    source="jsearch",
                    external_id=j.get("job_id"),
                    title=j.get("job_title"),
                    company=j.get("employer_name"),
                    location=j.get("job_location") or loc or None,
                    is_remote=j.get("job_is_remote"),
                    employment_type=j.get("job_employment_type"),
                    salary_min=j.get("job_min_salary"),
                    salary_max=j.get("job_max_salary"),
                    salary_text=sal_text,
                    url=j.get("job_google_link") or j.get("job_apply_link"),
                    apply_url=j.get("job_apply_link"),
                    publisher=j.get("job_publisher"),
                    description=j.get("job_description"),
                    posted_at=j.get("job_posted_at_datetime_utc"),
                ))
    return jobs


# ---- Himalayas (free, no key, remote only) ---------------------------------------------------
# Terms (himalayas.app/terms#api): apply through the API's applicationLink, show visible Himalayas credit with a
# link back (we store it as publisher "Himalayas" and link the posting to himalayas.app), and don't pass the jobs
# on to other job boards. Keyless requests share a public allowance, and the data refreshes daily, so this is
# throttled like the other free boards.

def _epoch_or_iso(v) -> str | None:
    if v in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(v), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return str(v)


def fetch_himalayas(prefs: dict) -> list[dict]:
    if _throttled("himalayas", 6):
        return []
    jobs: list[dict] = []
    seen: set[str] = set()
    country = (prefs.get("country") or "us").upper()
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True,
                      event_hooks=logs.httpx_hooks('Himalayas')) as client:
        for title in prefs["target_titles"]:
            # Worldwide-eligible jobs are kept (exclude_worldwide is off): they're open to US applicants too.
            r = client.get("https://himalayas.app/jobs/api/search", params={"q": title, "country": country, "sort": "recent"})
            if r.status_code == 429:
                db.log("Himalayas rate limit hit; stopping this run early", "warn")
                break
            r.raise_for_status()
            for j in r.json().get("jobs") or []:
                link = j.get("applicationLink")
                ext_id = j.get("guid") or link
                if not ext_id or ext_id in seen:
                    continue
                seen.add(ext_id)
                places = j.get("locationRestrictions") or []
                salary_text = None
                if j.get("minSalary") or j.get("maxSalary"):
                    salary_text = (f"{j.get('minSalary') or '?'}–{j.get('maxSalary') or '?'} "
                                   f"{j.get('currency') or ''} {j.get('salaryPeriod') or ''}").replace("  ", " ").strip()
                jobs.append(make_job(
                    source="himalayas",
                    external_id=ext_id,
                    title=j.get("title"),
                    company=j.get("companyName"),
                    location="Remote" + (f" ({', '.join(places)})" if places else " (worldwide)"),
                    is_remote=True,
                    employment_type=j.get("employmentType"),
                    salary_min=j.get("minSalary") if j.get("salaryPeriod") in (None, "annual") else None,
                    salary_max=j.get("maxSalary") if j.get("salaryPeriod") in (None, "annual") else None,
                    salary_text=salary_text,
                    url=link,
                    apply_url=link,  # required by Himalayas' terms
                    publisher="Himalayas",
                    description=strip_html(j.get("description")) or j.get("excerpt") or "",
                    posted_at=_epoch_or_iso(j.get("pubDate")),
                ))
    return jobs


# ---- Remotive (free, remote-only) --------------------------------------------------------------

def fetch_remotive(prefs: dict) -> list[dict]:
    if _throttled("remotive", 6):
        return []
    jobs: list[dict] = []
    # The public feed returns a small set of recent jobs and ignores category filters, so one call is enough.
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, event_hooks=logs.httpx_hooks('Remotive')) as client:
        r = client.get("https://remotive.com/api/remote-jobs", params={"limit": 150})
        r.raise_for_status()
        data = r.json()
    for j in data.get("jobs") or []:
        jobs.append(make_job(
            source="remotive",
            external_id=j.get("id"),
            title=j.get("title"),
            company=j.get("company_name"),
            location=j.get("candidate_required_location"),
            is_remote=True,
            employment_type=j.get("job_type"),
            salary_text=j.get("salary") or None,
            url=j.get("url"),
            publisher="Remotive",
            description=strip_html(j.get("description")),
            posted_at=j.get("publication_date"),
        ))
    return jobs


# ---- Remote OK (free, remote-only; attribution: links back to remoteok.com) -------------------

def fetch_remoteok(prefs: dict) -> list[dict]:
    if _throttled("remoteok", 6):
        return []
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True, event_hooks=logs.httpx_hooks('Remote OK')) as client:
        r = client.get("https://remoteok.com/api")
        r.raise_for_status()
        data = r.json()
    jobs: list[dict] = []
    for j in data:
        if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
            continue  # first element is the legal notice
        jobs.append(make_job(
            source="remoteok",
            external_id=j.get("id"),
            title=j.get("position"),
            company=j.get("company"),
            location=j.get("location") or "Remote",
            is_remote=True,
            salary_min=j.get("salary_min") or None,
            salary_max=j.get("salary_max") or None,
            url=j.get("url"),
            apply_url=j.get("apply_url") or j.get("url"),
            publisher="Remote OK",
            description=strip_html(j.get("description")),
            posted_at=j.get("date"),
        ))
    return jobs


# ---- USAJOBS (optional, federal) -------------------------------------------------------------

def fetch_usajobs(prefs: dict) -> list[dict]:
    key, email = config.get_secret("usajobs_api_key"), config.get_secret("usajobs_email")
    if not (key and email):
        raise SourceError("USAJOBS key/email not set (Settings > API keys)")
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    jobs: list[dict] = []
    with httpx.Client(timeout=TIMEOUT, headers=headers, event_hooks=logs.httpx_hooks('USAJOBS')) as client:
        for title in prefs["target_titles"]:
            params = {"Keyword": title, "DatePosted": int(prefs["posted_within_days"]), "ResultsPerPage": 50}
            if prefs["remote_only"]:
                params["RemoteIndicator"] = "True"
            r = client.get("https://data.usajobs.gov/api/search", params=params)
            r.raise_for_status()
            items = ((r.json().get("SearchResult") or {}).get("SearchResultItems")) or []
            for it in items:
                m = it.get("MatchedObjectDescriptor") or {}
                pay = (m.get("PositionRemuneration") or [{}])[0]
                details = ((m.get("UserArea") or {}).get("Details")) or {}
                jobs.append(make_job(
                    source="usajobs",
                    external_id=m.get("PositionID") or it.get("MatchedObjectId"),
                    title=m.get("PositionTitle"),
                    company=m.get("OrganizationName"),
                    location=m.get("PositionLocationDisplay"),
                    is_remote=prefs["remote_only"] or details.get("RemoteIndicator"),
                    salary_min=_num(pay.get("MinimumRange")),
                    salary_max=_num(pay.get("MaximumRange")),
                    url=m.get("PositionURI"),
                    apply_url=(m.get("ApplyURI") or [None])[0],
                    publisher="USAJOBS",
                    description="\n\n".join(x for x in (details.get("JobSummary"), m.get("QualificationSummary")) if x),
                    posted_at=m.get("PublicationStartDate"),
                ))
    return jobs


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


SOURCES = {
    "jsearch": {"label": "JSearch (Indeed, ZipRecruiter, LinkedIn, Glassdoor…)", "fetch": fetch_jsearch, "needs": ["jsearch_api_key"]},
    "remotive": {"label": "Remotive (free, remote only)", "fetch": fetch_remotive, "needs": []},
    "remoteok": {"label": "Remote OK (free, remote only)", "fetch": fetch_remoteok, "needs": []},
    "himalayas": {"label": "Himalayas (free, remote only)", "fetch": fetch_himalayas, "needs": []},
    "usajobs": {"label": "USAJOBS (federal)", "fetch": fetch_usajobs, "needs": ["usajobs_api_key", "usajobs_email"]},
}
