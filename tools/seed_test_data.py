"""Fill STAGING with fictional test data (a resume and jobs across every status). Refuses to run in production.

    .venv\\Scripts\\python tools\\seed_test_data.py            add test data (replaces earlier test data)
    .venv\\Scripts\\python tools\\seed_test_data.py --remove   remove it again

Everything is fake: made-up person and companies, links to example.com. Rows are tagged so --remove finds
them: jobs have source 'testdata', and the resume name starts with "[TEST]".
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, env  # noqa: E402
from app.sources import dedupe_key  # noqa: E402

RESUME = """# Jordan Rivera
jordan.rivera@example.com · (555) 010-0142 · Columbus, OH (remote) · github.com/jordan-rivera-example

## Summary
Application support analyst moving into software engineering. Three years keeping production systems healthy:
triaging incidents, writing Python and SQL to find root causes, and automating the fixes I kept doing by hand.

## Experience
### Application Support Analyst — Meridian Logistics (2022–present)
- Resolve 40+ production incidents a month across order and billing systems; on-call rotation one week in four
- Built Splunk dashboards and alerts that cut time-to-detect for failed batch jobs from hours to minutes
- Wrote Python scripts to reconcile order records against the billing database, replacing a daily manual check
- Partner with developers on root cause analysis; wrote 15 RCAs and runbooks used by the support team
- Manage tickets and changes in ServiceNow; follow ITIL incident and change processes

### IT Support Specialist — Clearbrook Community College (2020–2022)
- Supported 1,200 students and staff on Windows, Microsoft 365 and the student information system
- Automated new-account setup with PowerShell, saving about 5 hours a week

## Projects
- **Shiftboard** — Flask + PostgreSQL app for scheduling volunteer shifts; REST API, unit tests with pytest, deployed with Docker
- **Log Lens** — Python CLI that groups similar error lines in large log files to speed up triage

## Skills
Python, SQL (PostgreSQL, SQL Server), Java (coursework), JavaScript, REST APIs, Flask, Git, Linux, Bash,
PowerShell, Splunk, ServiceNow, Docker, AWS (EC2, S3, CloudWatch basics), ITIL, incident and problem management

## Education
- B.S. Information Technology — Ohio Valley University (2020)
- Full-Stack Software Development Certificate — Columbus Code Academy (2023)
"""

TAILORED = RESUME.replace(
    "## Summary\nApplication support analyst moving into software engineering.",
    "## Summary\nProduction support engineer with three years of on-call experience in payment-adjacent systems.",
).replace(
    "- Built Splunk dashboards and alerts",
    "- Built Splunk dashboards and alerts for transaction-processing jobs",
)


def ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def desc(company: str, role: str, about: str, must: list[str], nice: list[str], extra: str = "") -> str:
    return (f"About {company}\n{about}\n\nThe role\nAs a {role}, you'll join a small, friendly team. "
            f"This is a fully remote position open to candidates in the United States.\n\n"
            "What you'll need\n" + "\n".join(f"- {m}" for m in must) +
            "\n\nNice to have\n" + "\n".join(f"- {n}" for n in nice) +
            (f"\n\n{extra}" if extra else "") +
            "\n\n(Fictional posting created by JobScout's staging test data.)")


JOBS = [
    dict(title="Junior Software Engineer", company="Brightwave Analytics", status="new",
         employment_type="FULLTIME", salary_min=75000, salary_max=90000, posted=1, found=0.2,
         keyword_score=72, ai_score=84,
         ai_summary="[APPLY] Strong match: Python/SQL, a real Flask project, and support experience the team values.",
         ai_strengths=["Python and SQL used daily at Meridian", "Shiftboard shows Flask + PostgreSQL + tests",
                       "Production experience is a plus for their on-call rotation"],
         ai_gaps=["No professional React experience", "Limited AWS beyond basics"],
         description=desc("Brightwave Analytics", "Junior Software Engineer",
                          "We build dashboards that help regional hospitals track patient flow.",
                          ["1+ years writing Python or another backend language", "Comfort with SQL",
                           "Git and code review", "Clear written communication"],
                          ["Flask or Django", "React", "AWS"])),
    dict(title="Software Engineer I", company="Northpine Health", status="new",
         employment_type="FULLTIME", salary_min=82000, salary_max=98000, posted=2, found=0.5,
         keyword_score=64, ai_score=71,
         ai_summary="[APPLY] Good fit on fundamentals; Java is coursework only, so lead with Python projects.",
         ai_strengths=["REST APIs and Python", "Debugging production issues", "Healthcare-adjacent support work"],
         ai_gaps=["Role leans Java/Spring", "No HIPAA experience mentioned"],
         description=desc("Northpine Health", "Software Engineer I",
                          "Northpine builds scheduling software for outpatient clinics.",
                          ["CS degree, bootcamp, or equivalent experience", "Java or Python", "SQL", "Unit testing"],
                          ["Spring Boot", "HIPAA awareness", "Docker"])),
    dict(title="Junior Backend Developer (Python)", company="Kestrel Labs", status="new",
         employment_type="FULLTIME", salary_min=70000, salary_max=85000, posted=0.5, found=0.1,
         keyword_score=68, ai_score=None, ai_summary=None, ai_strengths=[], ai_gaps=[],
         description=desc("Kestrel Labs", "Junior Backend Developer",
                          "Kestrel makes inventory tools for independent bookstores.",
                          ["Python", "REST API design", "PostgreSQL", "Willingness to learn"],
                          ["FastAPI", "Docker", "pytest"])),
    dict(title="Senior Production Support Engineer", company="Ledgerly Payments", status="saved",
         employment_type="FULLTIME", salary_min=95000, salary_max=115000, posted=3, found=2,
         keyword_score=70, ai_score=77,
         ai_summary="[APPLY] Strong on incident management and Splunk; 'senior' is a stretch at 3 years but close.",
         ai_strengths=["On-call and incident response", "Splunk alerting", "RCAs and runbooks", "ITIL/ServiceNow"],
         ai_gaps=["Asks for 5+ years", "No payments-industry experience"],
         tailored=True,
         description=desc("Ledgerly Payments", "Senior Production Support Engineer",
                          "Ledgerly processes invoices and payouts for small businesses.",
                          ["5+ years in production or application support", "Splunk or similar", "SQL",
                           "Incident and problem management", "On-call rotation"],
                          ["Payments experience", "Python scripting", "AWS"])),
    dict(title="Application Support Engineer", company="Cobalt Freight Systems", status="applying",
         employment_type="FULLTIME", salary_min=72000, salary_max=88000, posted=4, found=3,
         keyword_score=78, ai_score=88,
         ai_summary="[APPLY] Near-exact match: logistics systems, batch jobs, Splunk, ServiceNow.",
         ai_strengths=["Logistics domain from Meridian", "Batch-job monitoring", "ServiceNow + ITIL"],
         ai_gaps=["Mentions Oracle; resume lists PostgreSQL/SQL Server"],
         cover=True,
         description=desc("Cobalt Freight Systems", "Application Support Engineer",
                          "Cobalt's platform routes freight for 3,000 carriers.",
                          ["Application support experience", "SQL", "Batch scheduling", "ServiceNow"],
                          ["Oracle", "Splunk", "Python"])),
    dict(title="Associate Software Engineer", company="Harborline Insurance", status="applied",
         employment_type="FULLTIME", salary_min=78000, salary_max=92000, posted=9, found=8, applied=5,
         keyword_score=61, ai_score=69,
         ai_summary="[MAYBE] Reasonable fit; mostly .NET, which you'd be learning on the job.",
         ai_strengths=["Strong fundamentals", "Support background"], ai_gaps=["C#/.NET stack"],
         notes="Applied through their careers site. Recruiter: Sam (fictional).",
         description=desc("Harborline Insurance", "Associate Software Engineer",
                          "Harborline offers renters and auto insurance online.",
                          ["Some programming experience (any language)", "SQL"], ["C#", ".NET", "Azure"])),
    dict(title="Production Support Analyst II", company="Quillmark Media", status="interview",
         employment_type="FULLTIME", salary_min=68000, salary_max=80000, posted=14, found=12, applied=10,
         keyword_score=74, ai_score=80,
         ai_summary="[APPLY] Good match for monitoring and incident work.",
         ai_strengths=["Monitoring and alerting", "Linux and log analysis"], ai_gaps=["Video streaming domain is new"],
         notes="Phone screen done. Technical interview Thursday 2pm ET with the support lead (fictional).",
         description=desc("Quillmark Media", "Production Support Analyst II",
                          "Quillmark streams local news video to 40 stations.",
                          ["2+ years production support", "Linux", "Log analysis"], ["Splunk", "Python"])),
    dict(title="Junior Developer", company="Tidewater Retail Group", status="rejected",
         employment_type="FULLTIME", salary_min=60000, salary_max=72000, posted=25, found=22, applied=20,
         keyword_score=58, ai_score=62,
         ai_summary="[MAYBE] Front-end heavy role.", ai_strengths=["JavaScript basics"],
         ai_gaps=["Mostly React/TypeScript front end"],
         notes="Rejection email received; they went with a front-end specialist.",
         description=desc("Tidewater Retail Group", "Junior Developer",
                          "Tidewater runs 60 home-goods stores and an online shop.",
                          ["JavaScript", "HTML/CSS"], ["React", "TypeScript"])),
    dict(title="Junior Data Engineer", company="Saltmarsh Energy", status="low_match",
         employment_type="CONTRACTOR", salary_min=None, salary_max=None, posted=2, found=1,
         keyword_score=12, ai_score=None, ai_summary=None, ai_strengths=[], ai_gaps=[],
         description=desc("Saltmarsh Energy", "Junior Data Engineer",
                          "Saltmarsh forecasts solar output for utilities.",
                          ["Spark", "Scala", "Airflow"], ["Databricks"])),
    dict(title="Support Engineer (Night Shift)", company="Orchid Cloud Hosting", status="dismissed",
         employment_type="FULLTIME", salary_min=55000, salary_max=62000, posted=6, found=5,
         keyword_score=40, ai_score=45,
         ai_summary="[SKIP] Overnight-only shift; below your salary range.", ai_strengths=["Linux"],
         ai_gaps=["Night shift", "Lower pay"],
         description=desc("Orchid Cloud Hosting", "Support Engineer",
                          "Orchid hosts WordPress sites.", ["Linux", "Customer support"], ["cPanel"])),
]

COVER = """Dear Cobalt Freight Systems hiring team,

I'm applying for the Application Support Engineer role. For the past three years at Meridian Logistics I've kept
order and billing systems running for a freight-heavy business, so your platform's mix of batch jobs, carrier
integrations and on-call support feels familiar.

At Meridian I built Splunk alerts that caught failed batch jobs in minutes instead of hours, and wrote Python
scripts that reconcile orders against billing every day. I work in ServiceNow and follow ITIL for incidents and
changes, and I've written fifteen root cause analyses with our developers.

I'd welcome the chance to bring that experience to Cobalt's support team.

Best regards,
Jordan Rivera
"""


def remove() -> tuple[int, int]:
    with db.conn() as c:
        j = c.execute("DELETE FROM jobs WHERE source='testdata'").rowcount
        r = c.execute("DELETE FROM resumes WHERE name LIKE '[TEST]%'").rowcount
    return j, r


def seed() -> None:
    remove()
    with db.conn() as c:
        has_master = c.execute("SELECT COUNT(*) FROM resumes WHERE is_master=1").fetchone()[0]
    from app import resume
    resume.create_resume("[TEST] Jordan Rivera (fictional)", RESUME, None, make_master=not has_master)
    for i, j in enumerate(JOBS, 1):
        slug = j["company"].lower().replace(" ", "-")
        row = {
            "source": "testdata", "external_id": f"test-{i}", "dedupe_key": dedupe_key(j["title"], j["company"]),
            "title": j["title"], "company": j["company"], "location": "Remote (US)", "is_remote": 1,
            "employment_type": j["employment_type"], "salary_min": j["salary_min"], "salary_max": j["salary_max"],
            "salary_text": None, "url": f"https://example.com/jobs/{slug}", "apply_url": f"https://example.com/jobs/{slug}/apply",
            "publisher": "Test data", "description": j["description"], "posted_at": ago(j["posted"]),
            "found_at": ago(j["found"]), "status": j["status"], "keyword_score": j["keyword_score"],
            "ai_score": j["ai_score"], "ai_summary": j["ai_summary"],
            "ai_strengths": json.dumps(j["ai_strengths"]), "ai_gaps": json.dumps(j["ai_gaps"]),
            "tailored_resume": TAILORED if j.get("tailored") else None,
            "resume_changes": json.dumps(["Rewrote the summary toward production support in payments",
                                          "Named transaction-processing jobs in the Splunk bullet"]) if j.get("tailored") else None,
            "cover_letter": COVER if j.get("cover") else None,
            "notes": j.get("notes"), "applied_at": ago(j["applied"]) if j.get("applied") else None,
        }
        with db.conn() as c:
            cols = list(row)
            c.execute(f"INSERT INTO jobs({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", [row[k] for k in cols])
    db.log(f"Added staging test data: 1 fictional resume, {len(JOBS)} fictional jobs")


def main() -> int:
    if not env.IS_STAGING:
        print("Refusing to run: this checkout is production. Test data is for staging only.")
        return 1
    db.init()
    if "--remove" in sys.argv:
        j, r = remove()
        db.log(f"Removed staging test data: {j} jobs, {r} resume(s)")
        print(f"Removed {j} test jobs and {r} test resume(s) from staging.")
        return 0
    seed()
    print(f"Staging now has fictional test data: 1 resume and {len(JOBS)} jobs ({db.DB_PATH}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
