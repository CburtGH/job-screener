"""Fast local filtering and keyword scoring. Runs on every fetched job before any Claude call."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

# Common skills/tools. A skill counts for scoring only if it appears on YOUR resume.
SKILL_VOCAB = [
    "python", "java", "javascript", "typescript", "c#", "c++", "go", "golang", "ruby", "php", "kotlin", "swift", "rust",
    "sql", "t-sql", "pl/sql", "postgresql", "postgres", "mysql", "sql server", "oracle", "mongodb", "redis", "elasticsearch",
    "html", "css", "react", "angular", "vue", "node", "node.js", "express", "django", "flask", "fastapi", "spring", ".net",
    "asp.net", "rest", "graphql", "api", "microservices", "linux", "unix", "bash", "shell", "powershell", "windows server",
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible", "jenkins", "git", "github", "gitlab", "ci/cd",
    "splunk", "datadog", "new relic", "grafana", "prometheus", "dynatrace", "elk", "kibana", "pagerduty", "servicenow",
    "jira", "confluence", "itil", "incident management", "problem management", "change management", "root cause",
    "rca", "on-call", "sla", "monitoring", "troubleshooting", "debugging", "production support", "application support",
    "batch", "autosys", "control-m", "cron", "scripting", "automation", "log analysis", "networking", "tcp/ip", "dns",
    "agile", "scrum", "kanban", "unit testing", "pytest", "junit", "selenium", "testing", "qa", "oop", "data structures",
    "algorithms", "excel", "tableau", "power bi", "etl", "kafka", "rabbitmq", "mq", "tomcat", "nginx", "apache", "iis",
    "salesforce", "sap", "mainframe", "cobol", "jcl", "vba", "c", "r", "pandas", "numpy", "machine learning",
]


def _word_re(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")


_SKILL_RES = {s: _word_re(s) for s in SKILL_VOCAB}


def resume_skills(resume_text: str) -> list[str]:
    t = (resume_text or "").lower()
    return [s for s, rx in _SKILL_RES.items() if rx.search(t)]


def _contains_any(text: str, terms: list[str]) -> str | None:
    t = text.lower()
    for term in terms:
        if term and _word_re(term).search(t):
            return term
    return None


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def looks_remote(job: dict) -> bool:
    if job.get("is_remote"):
        return True
    blob = f"{job.get('title','')} {job.get('location') or ''}".lower()
    return any(w in blob for w in ("remote", "work from home", "wfh", "anywhere", "telecommute"))


def filter_job(job: dict, prefs: dict) -> str | None:
    """Return a rejection reason, or None if the job passes the hard filters."""
    title = job.get("title") or ""
    if prefs.get("remote_only") and not looks_remote(job):
        return "not remote"
    hit = _contains_any(title, prefs.get("title_exclude") or [])
    if hit:
        return f"title excludes '{hit}'"
    must = prefs.get("title_must_include_any") or []
    if must and not _contains_any(title, must):
        best = max((SequenceMatcher(None, title.lower(), t.lower()).ratio() for t in prefs.get("target_titles") or [""]), default=0)
        if best < 0.75:
            return "title doesn't match targets"
    roles = prefs.get("title_role_any") or []
    if roles and not _contains_any(title, roles):
        return "title has no target role word"
    hit = _contains_any(f"{title}\n{job.get('description') or ''}", prefs.get("keywords_exclude") or [])
    if hit:
        return f"contains excluded keyword '{hit}'"
    min_sal = float(prefs.get("min_salary") or 0)
    if min_sal and job.get("salary_max") and float(job["salary_max"]) > 1000 and float(job["salary_max"]) < min_sal:
        return "salary below minimum"
    types = [t.lower().replace("-", "").replace("_", "") for t in prefs.get("employment_types") or []]
    et = (job.get("employment_type") or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    if types and et and not any(t in et or et in t for t in types):
        return f"employment type '{job.get('employment_type')}'"
    posted = _parse_date(job.get("posted_at"))
    days = int(prefs.get("posted_within_days") or 0)
    if posted and days and posted < datetime.now(timezone.utc) - timedelta(days=days + 1):
        return "too old"
    return None


def keyword_score(job: dict, prefs: dict, skills: list[str]) -> float:
    """0-100. Title closeness (40) + overlap with resume skills (45) + boost keywords (15)."""
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()
    targets = prefs.get("target_titles") or []
    title_sim = max((SequenceMatcher(None, title, t.lower()).ratio() for t in targets), default=0)
    if any(t.lower() in title for t in targets):
        title_sim = 1.0
    blob = f"{title}\n{desc}"
    job_skills = [s for s, rx in _SKILL_RES.items() if rx.search(blob)]
    if job_skills:
        overlap = len(set(job_skills) & set(skills)) / max(len(job_skills), 1)
    else:
        overlap = 0.3  # thin description; don't punish too hard
    boosts = prefs.get("keywords_boost") or []
    boost = (sum(1 for b in boosts if _word_re(b).search(blob)) / len(boosts)) if boosts else 0.5
    return round(40 * title_sim + 45 * overlap + 15 * boost, 1)
