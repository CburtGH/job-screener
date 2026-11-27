"""Monitoring pipeline: fetch -> filter -> dedupe -> keyword score -> (optional) Claude score.
Runs once when the app opens and again only when you click Refresh."""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timedelta, timezone

from . import ai, config, db, matcher, resume, sources

_run_lock = threading.Lock()
state = {"running": False, "last_run": None, "last_result": None}


def _insert(job: dict) -> int | None:
    with db.conn() as c:
        if c.execute("SELECT 1 FROM jobs WHERE source=? AND external_id=?", (job["source"], job["external_id"])).fetchone():
            return None
        if c.execute("SELECT 1 FROM jobs WHERE dedupe_key=?", (job["dedupe_key"],)).fetchone():
            return None  # same title+company already found via another source
        if c.execute("SELECT 1 FROM removed_jobs WHERE (source=? AND external_id=?) OR dedupe_key=?",
                     (job["source"], job["external_id"], job["dedupe_key"])).fetchone():
            return None  # the user removed this job; never bring it back
        cols = list(job.keys())
        cur = c.execute(
            f"INSERT INTO jobs({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
            [job[k] for k in cols],
        )
        return cur.lastrowid


def ai_score_job(job_id: int, context: str | None = None) -> dict:
    master = resume.get_master()
    if not master:
        raise ai.AIError("Add a resume first (Resume tab).")
    with db.conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise KeyError(job_id)
    job = db.row_to_job(row)
    res = ai.score_job(job, master["content"], config.get_preferences(), context=context)
    save_score(job_id, res)
    return res.model_dump()


def save_score(job_id: int, res: ai.MatchResult, scorer: str | None = None) -> None:
    """Store a Claude match score (from the API or from a Claude Code scoring session).
    scored_by records which engine produced it (ai.engine_label(), or an explicit override)."""
    score = max(0, min(100, int(res.score)))
    with db.conn() as c:
        c.execute(
            "UPDATE jobs SET ai_score=?, ai_summary=?, ai_strengths=?, ai_gaps=?, scored_by=? WHERE id=?",
            (score, f"[{res.recommendation.upper()}] {res.summary}", json.dumps(res.strengths), json.dumps(res.gaps),
             scorer or ai.engine_label(), job_id),
        )


ARCHIVE_AFTER_DAYS = 60
PURGE_AFTER_DAYS = 5
# Statuses the 60-day rule never touches: anything you acted on stays (saved = bookmarked, applying/applied/
# interview/offer = real progress). Everything else (new, low_match, rejected, dismissed) goes stale.
ARCHIVE_KEEP = ("saved", "applying", "applied", "interview", "offer")


def archive_stale() -> tuple[int, int]:
    """The 60-day rule, run at the start of every Refresh: a job found 60+ days ago that you never marked
    applied (see ARCHIVE_KEEP) is soft-deleted (archived_at set) -> it leaves the list but its scores, letters
    and linked emails survive while it shows under "Archived". Five days later it is deleted for good and
    recorded in removed_jobs, so the pipeline never fetches it back. Returns (archived, purged)."""
    now = db.now_iso()
    arch_cut = (datetime.now(timezone.utc) - timedelta(days=ARCHIVE_AFTER_DAYS)).isoformat(timespec="seconds")
    purge_cut = (datetime.now(timezone.utc) - timedelta(days=PURGE_AFTER_DAYS)).isoformat(timespec="seconds")
    with db.conn() as c:
        archived = c.execute(
            f"UPDATE jobs SET archived_at=? WHERE archived_at IS NULL AND found_at < ? "
            f"AND status NOT IN ({', '.join('?' for _ in ARCHIVE_KEEP)})",
            [now, arch_cut, *ARCHIVE_KEEP],
        ).rowcount
        stale = c.execute("SELECT id, source, external_id, dedupe_key, title, company FROM jobs "
                          "WHERE archived_at IS NOT NULL AND archived_at < ?", (purge_cut,)).fetchall()
        for r in stale:
            c.execute("INSERT OR IGNORE INTO removed_jobs(source, external_id, dedupe_key, title, company, removed_at) "
                      "VALUES(?,?,?,?,?,?)", (r["source"], r["external_id"], r["dedupe_key"], r["title"], r["company"], now))
            c.execute("DELETE FROM jobs WHERE id=?", (r["id"],))  # email_threads.job_id -> NULL (linked email stays)
        return archived, len(stale)


def hidden_sql() -> str:
    """SQL condition for jobs kept out of the list unless "Show hidden" is on:
      - the user hid it (hidden = 1), or it's dismissed;
      - a new job Claude scored below hide_below_score (default 70);
      - a new job Claude hasn't scored, with a keyword score below hide_below_keyword_score (default 30).
    A Claude score, when there is one, decides on its own (a strong Claude score beats a weak keyword match).
    Unhide sets hidden = -1, which exempts the job from both score rules. Either cutoff at 0 turns that rule off.
    Reads preferences (its own db connection): call it before taking the db lock."""
    prefs = config.get_preferences()
    ai_cut = float(prefs.get("hide_below_score") or 0)
    kw_cut = float(prefs.get("hide_below_keyword_score") or 0)
    rules = []
    if ai_cut > 0:
        rules.append(f"(ai_score IS NOT NULL AND ai_score < {ai_cut:g})")
    if kw_cut > 0:
        rules.append(f"(ai_score IS NULL AND COALESCE(keyword_score, 0) < {kw_cut:g})")
    low = f" OR (hidden = 0 AND status = 'new' AND ({' OR '.join(rules)}))" if rules else ""
    return f"(hidden = 1 OR status = 'dismissed'{low})"


def unscored_jobs(limit: int | None = None) -> list[dict]:
    """New, visible jobs the AI hasn't scored yet, best keyword score first (what the "Score with the AI Agent"
    batch covers). limit=None means all of them. Jobs hidden by the keyword cutoff are skipped, so no AI calls
    are spent on them."""
    hidden = hidden_sql()
    sql = f"SELECT * FROM jobs WHERE status='new' AND ai_score IS NULL AND archived_at IS NULL AND NOT {hidden} " \
          "ORDER BY keyword_score DESC, found_at DESC"
    args: tuple = () if limit is None else (limit,)
    if limit is not None:
        sql += " LIMIT ?"
    with db.conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [db.row_to_job(r) for r in rows]


def score_backlog() -> tuple[int, list[str]]:
    """Score every new, visible, unscored job right now: the whole backlog for the local LLM (no cap), up to
    ai_score_max_per_run for the Anthropic API engine (0 = never). Called by run_once and by the manual
    "Score unscored jobs" button; Claude Code never runs it (the user clicks its batch button).
    Stops at the first AI error so an unreachable server ends the pass. Returns (scored, errors)."""
    prefs = config.get_preferences()
    cap = None if config.use_local_llm() else int(prefs.get("ai_score_max_per_run") or 0)
    scored, errors = 0, []
    if cap == 0:
        return scored, errors
    for job in unscored_jobs(cap):
        try:
            ai_score_job(job["id"])
            scored += 1
        except ai.AIError as e:
            errors.append(f"AI: {e}")
            db.log(f"AI scoring stopped: {e}", "warn")
            break
    return scored, errors


def start_score_backlog() -> str | None:
    """Kick off a score-unscored-now pass off the request thread (the manual button).
    Returns an error string when a run already holds the pipeline; None once it's started."""
    if not _run_lock.acquire(blocking=False):
        return "A refresh or scoring pass is already in progress."
    def _go():
        state["running"] = True
        try:
            scored, errors = score_backlog()
            db.log(f"Score now: {scored} job(s) scored" + (f", {len(errors)} error(s)" if errors else ""))
        except Exception as e:
            db.log(f"Score now crashed: {e}\n{traceback.format_exc()}", "error")
        finally:
            state["running"] = False
            _run_lock.release()
    threading.Thread(target=_go, daemon=True, name="jobscout-score-unscored").start()
    return None


def run_once() -> dict:
    if not _run_lock.acquire(blocking=False):
        return {"skipped": "a run is already in progress"}
    state["running"] = True
    result = {"fetched": 0, "passed_filters": 0, "new": 0, "low_match": 0, "ai_scored": 0, "archived": 0, "purged": 0, "errors": [], "by_source": {}}
    try:
        result["archived"], result["purged"] = archive_stale()
        if result["archived"] or result["purged"]:
            db.log(f"60-day rule: {result['archived']} job(s) archived, {result['purged']} purged for good")
        prefs = config.get_preferences()
        master = resume.get_master()
        skills = matcher.resume_skills(master["content"]) if master else []

        for name in prefs.get("enabled_sources") or []:
            src = sources.SOURCES.get(name)
            if not src:
                continue
            try:
                jobs = src["fetch"](prefs)
            except sources.SourceError as e:
                result["errors"].append(f"{name}: {e}")
                db.log(f"{name}: {e}", "warn")
                continue
            except Exception as e:  # network errors, API shape changes
                result["errors"].append(f"{name}: {type(e).__name__}: {e}")
                db.log(f"{name} failed: {type(e).__name__}: {e}", "error")
                continue
            stats = {"fetched": len(jobs), "new": 0}
            result["fetched"] += len(jobs)
            for job in jobs:
                if not job["title"] or matcher.filter_job(job, prefs):
                    continue
                result["passed_filters"] += 1
                score = matcher.keyword_score(job, prefs, skills)
                job["keyword_score"] = score
                job["found_at"] = db.now_iso()
                low = score < float(prefs.get("min_keyword_score") or 0)
                job["status"] = "low_match" if low else "new"
                jid = _insert(job)
                if jid:
                    stats["new"] += 1
                    result["new"] += 1
                    if low:
                        result["low_match"] += 1
            result["by_source"][name] = stats

        # The local LLM and the Anthropic API (with a key) score automatically (config.can_auto_ai()): every run
        # scores the whole backlog of new, visible, unscored jobs, not just this run's. The local LLM has no usage
        # cap, so ai_score_max_per_run doesn't limit it; the Anthropic API engine still caps per run (0 = never).
        # With Claude Code as the engine, scoring waits for the user to click "Score" (never unattended).
        if prefs.get("ai_score_enabled") and master and config.can_auto_ai():
            scored, errors = score_backlog()
            result["ai_scored"] += scored
            result["errors"].extend(errors)

        db.log(
            f"Run finished: {result['fetched']} fetched, {result['passed_filters']} passed filters, "
            f"{result['new']} new ({result['low_match']} low match), {result['ai_scored']} scored by the AI"
            + (f", {len(result['errors'])} error(s)" if result["errors"] else "")
        )
        return result
    except Exception as e:
        db.log(f"Run crashed: {e}\n{traceback.format_exc()}", "error")
        result["errors"].append(str(e))
        return result
    finally:
        state["running"] = False
        state["last_run"] = db.now_iso()
        state["last_result"] = result
        _run_lock.release()


def refresh_on_startup() -> None:
    """One job check (and one Gmail sync, if connected) when the app opens. No recurring polling:
    after this, nothing is fetched until you click Refresh."""
    def _go():
        run_once()
        from . import gmail
        if gmail.is_connected():
            try:
                gmail.sync()
            except Exception as e:
                db.log(f"Gmail sync on startup failed: {e}", "warn")
    threading.Thread(target=_go, daemon=True, name="jobscout-startup-refresh").start()
