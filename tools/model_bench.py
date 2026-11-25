"""Model bench: how well does each scoring engine actually score? Run from the repo root:

    .venv\\Scripts\\python tools\\model_bench.py --pass-name baseline --targets light,medium,heavy,remote

Six fixture postings for a fictional junior candidate, each with two planted requirement gaps that are NOT on
the resume. Every strength bullet is checked against the resume text: a bullet is a hallucination if it names a
planted-gap technology or has no content word from the resume at all. Also measures latency, run-to-run score
consistency and agreement with the remote reference model. Results: printed table + JSON in data/bench/.
Uses the app's real ai.score_job path, so it benches exactly what the app prompts with.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import os
os.chdir(ROOT)

from app import ai, config, db, local_model  # noqa: E402

REMOTE_URL = os.environ.get("JOBSCOUT_BENCH_URL", "")    # your OpenAI-compatible server, e.g. http://192.168.1.10:8081/v1
REMOTE_MODEL = os.environ.get("JOBSCOUT_BENCH_MODEL", "")  # model id shown by GET <url>/models

# Fictional candidate. Everything below is invented on purpose: the resume is the only source of truth.
RESUME = """# Jordan Ellis
Junior NOC / application support engineer, Columbus OH, willing to relocate for the right role.

## Experience
### NOC Technician — Cardinal Health Network (2023–present)
- Monitor ~400 Windows Server and Linux hosts in Splunk and Nagios; open and triage ~30 tickets a week.
- First-line responder for P1/P2 incidents; write post-incident notes; run documented escalation paths.
- Manage user accounts and permissions in Active Directory and Entra ID; onboarding/offboarding.
- Wrote Python and Bash scripts to auto-restart hung services, cutting repeat pages by a third.
- Weekly patch windows for Windows Server 2019/2022 and RHEL 8.

### IT Support Student Worker — OSU College of Engineering (2021–2023)
- Ticket queue (Jira Service Management) for faculty/staff; hardware swaps; printer and VPN fixes.
- Built a small Flask dashboard (SQLite, HTML/CSS) showing open tickets by category.

## Projects
- Home lab: Raspberry Pi running Pi-hole and Grafana; proxmox VMs; documented runbooks in Markdown.
- Python Discord bot with basic rate limiting (REST API calls, error handling).

## Skills
Python (scripting), Bash, SQL (queries and simple joins), Windows Server, Active Directory, Entra ID,
Splunk, Nagios, Grafana, Jira, Git (basics), networking fundamentals (DNS, DHCP, TCP/IP), documentation.

## Education
B.S. Computer Information Technology, Ohio State University, 2023. CompTIA Network+ (2023).
"""

# Two planted gaps per job: technologies a weak model loves to "find" because the posting keeps saying them.
JOBS = [
    {"title": "Junior Site Reliability Engineer", "company": "Brightline Logistics",
     "gaps": ["kubernetes", "terraform"],
     "description": "Keep our shipping platform running. Must work daily with Kubernetes (EKS) and manage "
                    "infrastructure with Terraform. You will own on-call for our Node.js services, tune "
                    "Prometheus/Grafana dashboards and write Go tooling. Junior role, but the platform is "
                    "100% cloud (AWS) so strong cloud chops matter. Linux troubleshooting and monitoring "
                    "experience welcome."},
    {"title": "Junior Support Engineer", "company": "Halyard Payments",
     "gaps": ["pci-dss", "java"],
     "description": "Tier-2 support for our card-processing API. You must be fluent in Java (we debug customer "
                    "stack traces) and comfortable working inside PCI-DSS audit rules. Log investigation in "
                    "Splunk, ticket ownership, and clear customer emails are the daily work. Payments or "
                    "fintech experience is a plus. On-call is one weekend in six."},
    {"title": "Application Support Analyst", "company": "Copperline Energy",
     "gaps": ["salesforce", "react"],
     "description": "Support our internal trading portal (React front end, Salesforce integrations). Triage "
                    "incoming tickets, reproduce issues, escalate with solid repro notes to the platform team. "
                    "You will write SQL against our Oracle warehouse, watch Grafana, and update runbooks. "
                    "Windows Server patching knowledge is useful. Junior-friendly with a mentor."},
    {"title": "NOC Engineer (Junior)", "company": "Meridian Fiber",
     "gaps": ["cisco ios", "ansible"],
     "description": "24x5 network operations center. You configure Cisco IOS routers and switches and automate "
                    "repetitive fixes with Ansible. Monitoring via SolarWinds, ticket triage, and clear "
                    "escalation write-ups. Linux CLI comfort helps; the rest we train. Must be able to lift "
                    "40 lbs of equipment."},
    {"title": "Junior DevOps Associate", "company": "Stackwell Analytics",
     "gaps": ["docker", "jenkins"],
     "description": "Help run our CI/CD: Jenkins pipelines, Docker images for our Python data services, and "
                    "post-deploy monitoring. You will fix broken builds, manage secrets in Vault, and help "
                    "migrate cron jobs into Airflow. Great fit for a scripter who wants to grow into DevOps. "
                    "Git fluency required; AWS experience a plus."},
    {"title": "IT Operations Junior Analyst", "company": "Fairview Clinics (USAJOBS)",
     "gaps": ["security clearance", "powershell"],
     "description": "Federal health IT support. This position requires an active Secret security clearance and "
                    "heavy PowerShell automation for Windows fleet management. You will administer Active "
                    "Directory, watch Nagios alerts, work tickets end to end and keep SOPs current. Entry "
                    "level, GS-7 equivalent. Must be a U.S. citizen."},
]
CONSISTENCY_JOBS = [0, 3]

NEGATORS = ("lack", "missing", "no ", "not ", "gap", "without", "despite", "needs", "strongly prefers",
            "rather than", "beyond")


def tokens(text: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "that", "this", "your", "their", "have", "been", "were",
            "will", "would", "about", "into", "over", "you", "our", "are", "can", "will", "well", "strong",
            "experience", "must", "junior", "role", "team", "work", "working", "candidate", "resume", "show",
            "shows", "shows", "candidate", "solid", "good", "great", "plus", "clear", "real", "direct",
            "close", "fit", "fits", "matches", "match", "role", "roles", "job", "posting", "skills",
            "while", "only", "very", "high", "low", "both", "his", "her", "they", "them", "than", "then",
            "but", "all", "any", "has", "was", "its", "who"}
    return {w for w in re.findall(r"[a-z0-9+#.]{4,}", text.lower()) if w not in stop and not w.isdigit()}


RESUME_TOKENS = tokens(RESUME)


def skill_hit(text: str, probe: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(probe)}(?![a-z0-9])", text.lower()) is not None


NEGATORS = ("lack", "missing", "no ", "not ", "gap", "without", "despite", "needs", "strongly prefers",
            "rather than", "beyond")
# extra cues that a gap-skill mention is an honest call-out rather than a claimed skill (either side of it)
GAP_CUES = NEGATORS + ("none", "never", "though", "may be", "possible", "compatible", "parallels", "unclear",
                       "required", "requirement", "training", "yet", "however")


def claimed(text: str, probe: str) -> bool:
    """Mentioned affirmatively: at least one occurrence with no gap cue within ~45 chars on either side,
    so honest call-outs like "no Jenkins experience yet" or "PowerShell is explicitly required" are not
    counted as hallucinations. "supports Ansible automation (key for the role)" still counts as a claim."""
    tl = text.lower()
    for m in re.finditer(rf"(?<![a-z0-9]){re.escape(probe)}(?![a-z0-9])", tl):
        window = tl[max(0, m.start() - 65):m.end() + 65]
        if not any(n in window for n in GAP_CUES):
            return True
    return False


def score_once(target: str, job: dict) -> dict:
    t0 = time.perf_counter()
    try:
        r = ai.score_job({"title": job["title"], "company": job["company"], "description": job["description"],
                          "location": "Columbus, OH (Remote eligible)"}, RESUME,
                         {"target_titles": ["support", "junior", "operations"], "remote_only": False,
                          "keywords_boost": [], "min_salary": 0, "employment_types": []})
        return {"ok": True, "seconds": round(time.perf_counter() - t0, 1), "score": r.score,
                "recommendation": r.recommendation, "summary": r.summary, "strengths": r.strengths,
                "gaps": r.gaps}
    except Exception as e:
        return {"ok": False, "seconds": round(time.perf_counter() - t0, 1), "error": str(e)[:200]}


def judge(job: dict, res: dict) -> dict:
    if not res.get("ok"):
        return {"planted": 0, "ungrounded": 0, "bullets": 0}
    planted = sum(1 for g in job["gaps"] if any(claimed(s, g) for s in res["strengths"])
                  or claimed(res["summary"], g))
    ungrounded = 0
    for b in res["strengths"]:
        bt = tokens(re.sub(rf"({'|'.join(re.escape(g) for g in job['gaps'])})", " ", b.lower()))
        if not (bt & RESUME_TOKENS):
            ungrounded += 1
    return {"planted": planted, "ungrounded": ungrounded, "bullets": len(res["strengths"])}


def switch(target: str) -> tuple[str, str]:
    """Point the app's local engine at a bench target; returns (label, model_id)."""
    if target == "remote":
        if not (REMOTE_URL and REMOTE_MODEL):
            raise SystemExit("set JOBSCOUT_BENCH_URL and JOBSCOUT_BENCH_MODEL first")
        local_model.stop_server()
        config.set_app_settings({"ai_engine": "local_external", "local_llm_url": REMOTE_URL, "local_llm_model": REMOTE_MODEL})
        return "external server", REMOTE_MODEL
    err = local_model.start_server(target, apply_engine=True)
    if err:
        raise SystemExit(f"can't start {target}: {err}")
    for _ in range(180):
        if local_model.running():
            break
        time.sleep(1)
    mid = local_model.model_id()
    config.set_app_settings({"ai_engine": "local", "local_llm_url": local_model.MANAGED_URL,
                             "local_llm_model": mid})
    return {"light": "built-in Light 1.7B", "medium": "built-in Medium 4B",
            "heavy": "built-in Heavy 8B"}[target], mid


def summarize(out: dict) -> None:
    print("\n=== summary ===")
    for t, d in out["targets"].items():
        m = d["metrics"]
        print(f"{d['label']:20} ok={m['calls_ok']}/{m['calls']} avg={m['avg_seconds']}s "
              f"planted_halluc={m['hallucinations_planted']} ungrounded={m['hallucinations_ungrounded']}"
              f"/{m['strengths_total']} dirty_jobs={m['jobs_with_hallucination']}/{m['calls']} "
              f"consistency={m['consistency_avg_delta']} (max {m['consistency_max_delta']})")


def rejudge(pass_name: str) -> None:
    """Re-run the judge over a saved pass (raw responses are kept) and rewrite its verdicts/metrics.
    Consistency numbers are kept as measured (the second-pass texts were not stored)."""
    path = ROOT / "data" / "bench" / f"{pass_name}.json"
    out = json.loads(path.read_text())
    for target, d in out["targets"].items():
        verdicts = []
        for row in d["rows"]:
            v = judge({"gaps": row["gaps"]}, row["result"])
            verdicts.append(v)
            row.update(v)
        m = d["metrics"]
        m["hallucinations_planted"] = sum(v["planted"] for v in verdicts)
        m["hallucinations_ungrounded"] = sum(v["ungrounded"] for v in verdicts)
        m["jobs_with_hallucination"] = sum(1 for v in verdicts if v["planted"] or v["ungrounded"])
    path.write_text(json.dumps(out, indent=1))
    print(f"rejudged {pass_name} (judge: {__import__('datetime').date.today()})")
    summarize(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="light,medium,heavy,remote")
    ap.add_argument("--pass-name", default="run")
    ap.add_argument("--rejudge", metavar="PASS", help="re-verdict a saved pass with the current judge, no model calls")
    args = ap.parse_args()
    if args.rejudge:
        for name in [n.strip() for n in args.rejudge.split(",") if n.strip()]:
            rejudge(name)
        return
    db.init()
    out: dict = {"pass": args.pass_name, "started": db.now_iso(), "targets": {}}
    out_dir = ROOT / "data" / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    for target in [t.strip() for t in args.targets.split(",") if t.strip()]:
        label, model_id = switch(target)
        rows, verdicts = [], []
        print(f"\n=== {label} ({model_id}) ===", flush=True)
        for i, job in enumerate(JOBS):
            r = score_once(target, job)
            v = judge(job, r)
            verdicts.append(v)
            flag = "HALLUCINATION" if (v["planted"] or v["ungrounded"]) else "clean"
            rows.append({"job": job["title"], "gaps": job["gaps"], "result": r, **v})
            sc = r.get("score", "ERR")
            print(f"  [{i}] {job['title'][:40]:40} score={sc!s:4} {r.get('seconds','-'):>6}s "
                  f"planted={v['planted']} ungrounded={v['ungrounded']}/{v['bullets']} {flag}", flush=True)
        cons = []
        for i in CONSISTENCY_JOBS:
            r2 = score_once(target, JOBS[i])
            first = rows[i]["result"]
            if r2.get("ok") and first.get("ok"):
                cons.append(abs(r2["score"] - first["score"]))
                print(f"  [consistency] {JOBS[i]['title'][:38]:38} delta_score={cons[-1]}", flush=True)
        ok = [r["result"] for r in rows if r["result"].get("ok")]
        out["targets"][target] = {
            "label": label, "model_id": model_id, "rows": rows,
            "metrics": {
                "calls_ok": len(ok), "calls": len(rows),
                "avg_seconds": round(statistics.mean(r["seconds"] for r in ok), 1) if ok else None,
                "max_seconds": round(max((r["seconds"] for r in ok), default=0), 1),
                "hallucinations_planted": sum(v["planted"] for v in verdicts),
                "hallucinations_ungrounded": sum(v["ungrounded"] for v in verdicts),
                "strengths_total": sum(v["bullets"] for v in verdicts),
                "jobs_with_hallucination": sum(1 for v in verdicts if v["planted"] or v["ungrounded"]),
                "consistency_avg_delta": round(statistics.mean(cons), 1) if cons else None,
                "consistency_max_delta": max(cons, default=None),
            },
        }
        (out_dir / f"{args.pass_name}.json").write_text(json.dumps(out, indent=1))
    summarize(out)


if __name__ == "__main__":
    main()
