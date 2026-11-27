"""JobScout - local job screener. Run with:  .venv\\Scripts\\python -m app"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import ai, claude_code, config, db, env, gmail, local_model, logs, monitor, resume, sources

STATIC = env.BUNDLE / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    from . import env
    config.apply_env_defaults()  # one-time: flip refresh_on_startup / debug_panel to this environment's defaults
    logs.setup(config.get_app_settings().get("log_level") or "INFO")
    logs.log.info("%s starting on port %s", env.APP_NAME, env.PORT)
    # Staging never starts anything by itself (every launch would burn job-site API quota and Gmail calls,
    # and staging restarts constantly while testing): on staging, everything begins with a user click.
    if (config.get_app_settings().get("refresh_on_startup") and not env.IS_STAGING
            and os.environ.get("JOBSCOUT_SKIP_STARTUP_REFRESH") != "1"):
        monitor.refresh_on_startup()
    local_model.autostart_if_selected()  # built-in tier chosen in Settings: serve it (local, costs no quota)
    yield
    claude_code.stop_all()  # never leave Claude Code processes running after the window closes
    local_model.stop_server()


app = FastAPI(title="JobScout", lifespan=lifespan)

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def local_only(request, call_next):
    """Block DNS-rebinding (Host check) and cross-site requests from other web pages (custom header on writes)."""
    from fastapi.responses import JSONResponse
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-jobscout") != "1":
        return JSONResponse({"detail": "missing X-JobScout header"}, status_code=403)
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        # Always revalidate the UI files so an updated app never runs stale JS/CSS from the webview cache.
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def record_requests(request, call_next):
    """Debug viewer: record every app request (added after local_only, so it's outermost and sees 403s too)."""
    path = request.url.path
    if path.startswith("/api/debug/"):
        return await call_next(request)
    t0 = time.perf_counter()
    ctype = request.headers.get("content-type", "")
    req_body = None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if path.startswith("/api/settings/secrets/"):
            req_body = '{"value": "***"}'
        elif "multipart" in ctype:
            req_body = f"<file upload, {request.headers.get('content-length', '?')} bytes>"
        else:
            req_body = logs.body_text(await request.body(), ctype or "application/json")
    try:
        response = await call_next(request)
    except Exception as e:
        logs.log.exception("Unhandled error in %s %s", request.method, path)
        logs.add_network({"kind": "app", "method": request.method, "url": logs.redact_url(str(request.url.path) + ("?" + request.url.query if request.url.query else "")),
                          "status": 500, "ms": round((time.perf_counter() - t0) * 1000), "error": f"{type(e).__name__}: {e}",
                          "request": {"headers": logs.redact_headers(request.headers), "body": req_body}, "response": {}})
        raise
    resp_ctype = response.headers.get("content-type", "")
    resp_body = None
    if "json" in resp_ctype:
        raw = b"".join([chunk async for chunk in response.body_iterator])
        resp_body = logs.body_text(raw, resp_ctype)
        response = Response(content=raw, status_code=response.status_code, headers=dict(response.headers),
                            media_type=response.media_type)
    logs.add_network({
        "kind": "app", "method": request.method,
        "url": logs.redact_url(path + ("?" + request.url.query if request.url.query else "")),
        "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000),
        "size": int(response.headers.get("content-length") or 0) or None,
        "request": {"headers": logs.redact_headers(request.headers), "body": req_body},
        "response": {"headers": logs.redact_headers(response.headers), "body": resp_body},
    })
    return response


def _ai_guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))


# ---- settings -------------------------------------------------------------

@app.get("/api/env")
def get_env():
    from . import env
    return {"env": env.ENV, "app_name": env.APP_NAME, "port": env.PORT, "icon": env.ICON}


@app.post("/api/env/copy-production")
async def copy_production():
    from . import env_tools
    try:
        return await run_in_threadpool(env_tools.copy_production_data)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


def _first_run() -> bool:
    """True until the setup wizard has been shown once on a genuinely empty install
    (no saved keys on this machine, no resume, no jobs) — auto-open trigger for new users."""
    if db.kv_get("wizard_seen"):
        return False
    if any(s["is_set"] for s in config.secrets_status()):
        return False
    with db.conn() as c:
        return not (c.execute("SELECT 1 FROM resumes LIMIT 1").fetchone()
                    or c.execute("SELECT 1 FROM jobs LIMIT 1").fetchone())


@app.get("/api/settings")
def get_settings():
    return {
        "app": config.get_app_settings(),
        "preferences": config.get_preferences(),
        "secrets": config.secrets_status(),
        "sources": [{"id": k, "label": v["label"], "needs": v["needs"],
                     "ready": all(config.get_secret(n) for n in v["needs"])} for k, v in sources.SOURCES.items()],
        "first_run": _first_run(),
    }


@app.post("/api/settings/wizard-seen")
def post_wizard_seen():
    db.kv_set("wizard_seen", True)
    return {"ok": True}


@app.put("/api/settings/app")
def put_app_settings(values: dict):
    saved = config.set_app_settings(values)
    logs.set_level(saved.get("log_level") or "INFO")
    local_model.on_settings_change(saved)  # engine moved off the built-in model: free the RAM it held
    return saved


@app.put("/api/settings/preferences")
def put_preferences(values: dict):
    return config.set_preferences(values)


class SecretIn(BaseModel):
    value: str | None = None


@app.put("/api/settings/secrets/{name}")
def put_secret(name: str, body: SecretIn):
    if name not in config.SECRET_FIELDS:
        raise HTTPException(404, "unknown secret")
    config.set_secret(name, body.value)
    return {"ok": True}


# ---- resumes --------------------------------------------------------------

@app.get("/api/resumes")
def list_resumes():
    return resume.list_resumes()


@app.get("/api/resumes/{rid}")
def get_resume(rid: int):
    r = resume.get_resume(rid)
    if not r:
        raise HTTPException(404)
    return r


@app.post("/api/resumes/upload")
async def upload_resume(file: UploadFile = File(...), name: str = Form(""), cleanup: bool = Form(False),
                        make_master: bool = Form(False)):
    data = await file.read()
    try:
        text = resume.extract_text(file.filename or "resume.txt", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not text:
        raise HTTPException(400, "No text found in that file. If it's a scanned PDF, paste the text instead.")
    (resume.UPLOAD_DIR / Path(file.filename or "resume").name).write_bytes(data)
    warning = None
    if cleanup and not config.use_claude_code():
        # Optional nicety: if Claude isn't available (no API key, offline), keep the extracted text and say so
        # instead of failing the whole upload.
        try:
            text = (await run_in_threadpool(ai.resume_to_markdown, text)).markdown
        except ai.AIError as e:
            warning = f"Uploaded, but skipped the AI cleanup: {e} Fix any formatting by hand, then Save."
            logs.log.warning("Resume cleanup skipped: %s", e)
    saved = resume.create_resume(name or Path(file.filename or "Resume").stem, text, file.filename, make_master)
    return {**saved, "warning": warning}


class ResumeIn(BaseModel):
    name: str | None = None
    content: str | None = None
    make_master: bool | None = None


@app.post("/api/resumes")
def create_resume(body: ResumeIn):
    return resume.create_resume(body.name or "Resume", body.content or "", None, bool(body.make_master))


@app.put("/api/resumes/{rid}")
def update_resume(rid: int, body: ResumeIn):
    r = resume.update_resume(rid, body.name, body.content, body.make_master)
    if not r:
        raise HTTPException(404)
    return r


@app.delete("/api/resumes/{rid}")
def delete_resume(rid: int):
    resume.delete_resume(rid)
    return {"ok": True}


@app.post("/api/resumes/{rid}/cleanup")
async def cleanup_resume(rid: int):
    r = resume.get_resume(rid)
    if not r:
        raise HTTPException(404)
    md = (await run_in_threadpool(_ai_guard, ai.resume_to_markdown, r["content"])).markdown
    return {"markdown": md}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60] or "document"


@app.get("/api/resumes/{rid}/docx")
def resume_docx(rid: int):
    r = resume.get_resume(rid)
    if not r:
        raise HTTPException(404)
    return _docx_response(r["content"], _slug(r["name"]))


def _docx_response(markdown: str, stem: str) -> Response:
    return Response(
        resume.markdown_to_docx(markdown),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{stem}.docx"'},
    )


# ---- jobs -----------------------------------------------------------------

SORTS = {
    "best": "COALESCE(ai_score, keyword_score) DESC, found_at DESC",
    "newest": "found_at DESC",
    "posted": "posted_at DESC",
}


hidden_sql = monitor.hidden_sql  # one definition, shared with the "Score new jobs" batch


@app.get("/api/jobs")
def list_jobs(status: str = "active", q: str = "", sort: str = "best", limit: int = 300, show_hidden: bool = False,
              score_min: int | None = None, score_max: int | None = None):
    where, args = [], []
    archived_only = status == "archived"
    # Archived (60-day rule) jobs live in their own view; nothing else ever shows them.
    where.append("archived_at IS NOT NULL" if archived_only else "archived_at IS NULL")
    # Score range applies to the number the list shows: AI score, or keyword score until it exists.
    if score_min is not None or score_max is not None:
        shown = "COALESCE(ai_score, keyword_score)"
        where.append(f"{shown} IS NOT NULL")
        if score_min is not None:
            where.append(f"{shown} >= ?")
            args.append(score_min)
        if score_max is not None:
            where.append(f"{shown} <= ?")
            args.append(score_max)
    # Hidden jobs stay out of the list unless "Show hidden" is on, or the status filter asks for dismissed jobs.
    hidden_sql_ = hidden_sql()
    if not show_hidden and status != "dismissed" and not archived_only:
        where.append(f"NOT {hidden_sql_}")
    if archived_only:
        pass
    elif status == "active":
        # Dismissed jobs are "hidden", so Show hidden brings them back into the Active view too.
        where.append("status IN ('new','saved','applying','applied','interview','offer'" + (",'dismissed')" if show_hidden else ")"))
    elif status != "all":
        where.append("status = ?")
        args.append(status)
    if q:
        where.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
        args += [f"%{q}%"] * 3
    sql = ("SELECT id, source, title, company, location, is_remote, employment_type, salary_text, salary_min, salary_max, "
           "publisher, posted_at, found_at, status, keyword_score, ai_score, ai_summary, scored_by, url, apply_url, "
           f"cover_letter IS NOT NULL AS has_cover, tailored_resume IS NOT NULL AS has_tailored, hidden, archived_at, "
           f"{hidden_sql_} AS is_hidden FROM jobs")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY archived_at DESC" if archived_only else f" ORDER BY {SORTS.get(sort, SORTS['best'])}"
    sql += " LIMIT ?"
    args.append(min(limit, 1000))
    with db.conn() as c:
        rows = c.execute(sql, args).fetchall()
        counts = dict(c.execute("SELECT status, COUNT(*) FROM jobs WHERE archived_at IS NULL GROUP BY status").fetchall())
        unscored = c.execute(f"SELECT COUNT(*) FROM jobs WHERE status='new' AND ai_score IS NULL AND archived_at IS NULL AND NOT {hidden_sql_}").fetchone()[0]
        hidden_count = c.execute(f"SELECT COUNT(*) FROM jobs WHERE archived_at IS NULL AND {hidden_sql_}").fetchone()[0]
        archived_count = c.execute("SELECT COUNT(*) FROM jobs WHERE archived_at IS NOT NULL").fetchone()[0]
    return {"jobs": [dict(r) | {"is_remote": bool(r["is_remote"]), "is_hidden": bool(r["is_hidden"])} for r in rows],
            "counts": counts, "unscored": unscored, "hidden_count": hidden_count, "archived_count": archived_count}


def _get_job(jid: int) -> dict:
    hidden = hidden_sql()  # reads preferences (its own connection), so build it before taking the db lock
    with db.conn() as c:
        row = c.execute(f"SELECT *, {hidden} AS is_hidden FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        raise HTTPException(404)
    job = db.row_to_job(row)
    job["is_hidden"] = bool(job["is_hidden"])
    return job


@app.get("/api/jobs/{jid}")
def get_job(jid: int):
    return _get_job(jid)


EDITABLE_JOB_FIELDS = {"status", "notes", "tailored_resume", "resume_changes", "cover_letter", "title", "company",
                       "description", "apply_url", "hidden"}


@app.patch("/api/jobs/{jid}")
def patch_job(jid: int, values: dict):
    values = {k: v for k, v in values.items() if k in EDITABLE_JOB_FIELDS}
    if "resume_changes" in values:
        values["resume_changes"] = json.dumps([str(x) for x in (values["resume_changes"] or [])])
    if "hidden" in values:  # 1 = user hid it, 0 = visible (legacy -1 rows read as visible)
        values["hidden"] = 1 if values["hidden"] else 0
    if not values:
        return _get_job(jid)
    if values.get("status") == "applied":
        values["applied_at"] = db.now_iso()
    with db.conn() as c:
        c.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in values)} WHERE id=?", [*values.values(), jid])
    return _get_job(jid)


@app.delete("/api/jobs/{jid}")
def delete_job(jid: int):
    """Remove a job for good: delete it and remember it in removed_jobs so Refresh never pulls it back.
    Emails linked to it stay in Mail, unlinked. (Pasting it into Add a job yourself still works.)"""
    job = _get_job(jid)
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO removed_jobs(source, external_id, dedupe_key, title, company, removed_at) "
                  "VALUES(?,?,?,?,?,?)", (job["source"], job["external_id"], job["dedupe_key"], job["title"],
                                         job.get("company"), db.now_iso()))
        c.execute("UPDATE email_threads SET job_id=NULL, link_manual=0 WHERE job_id=?", (jid,))
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))
    db.log(f"Removed job: {job['title']} at {job.get('company') or '?'} (won't be fetched again)")
    return {"ok": True}


def _master_or_400() -> dict:
    m = resume.get_master()
    if not m or not m["content"].strip():
        raise HTTPException(400, "Add a resume first (Resume tab).")
    return m


@app.post("/api/jobs/{jid}/score")
async def score_job(jid: int, body: dict | None = None):
    context = (body or {}).get("context") or None
    try:
        return await run_in_threadpool(monitor.ai_score_job, jid, context)
    except ai.AIError as e:
        raise HTTPException(400, str(e))


@app.post("/api/jobs/{jid}/tailor")
async def tailor(jid: int):
    job, master = _get_job(jid), _master_or_400()
    res = await run_in_threadpool(_ai_guard, ai.tailor_resume, job, master["content"])
    with db.conn() as c:
        c.execute("UPDATE jobs SET tailored_resume=?, resume_changes=? WHERE id=?",
                  (res.tailored_resume, json.dumps(res.changes), jid))
    return _get_job(jid)


@app.post("/api/jobs/{jid}/cover-letter")
async def cover_letter(jid: int):
    job, master = _get_job(jid), _master_or_400()
    resume_md = job.get("tailored_resume") or master["content"]
    res = await run_in_threadpool(_ai_guard, ai.write_cover_letter, job, resume_md)
    with db.conn() as c:
        c.execute("UPDATE jobs SET cover_letter=? WHERE id=?", (res.cover_letter, jid))
    return _get_job(jid)


DOWNLOAD_TYPES = {
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _document_response(markdown: str, stem: str, ext: str, title: str) -> Response:
    """A resume or cover letter as PDF, plain text or Word, for uploading to an application site."""
    if ext not in DOWNLOAD_TYPES:
        raise HTTPException(404, "Download as .pdf, .txt or .docx")
    data = {"pdf": lambda: resume.markdown_to_pdf(markdown, title),
            "txt": lambda: resume.markdown_to_text(markdown).encode("utf-8"),
            "docx": lambda: resume.markdown_to_docx(markdown)}[ext]()
    return Response(data, media_type=DOWNLOAD_TYPES[ext],
                    headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'})


@app.get("/api/jobs/{jid}/resume.{ext}")
def job_resume_download(jid: int, ext: str):
    """The tailored resume (or the master, if this job has none yet)."""
    job = _get_job(jid)
    md = job.get("tailored_resume") or _master_or_400()["content"]
    who = config.get_app_settings().get("applicant_name") or ""
    return _document_response(md, _slug(f"{who} Resume {job.get('company') or ''}"), ext, f"{who} Resume".strip())


@app.get("/api/jobs/{jid}/cover-letter.{ext}")
def job_cover_download(jid: int, ext: str):
    job = _get_job(jid)
    if not job.get("cover_letter"):
        raise HTTPException(404, "No cover letter yet")
    who = config.get_app_settings().get("applicant_name") or ""
    return _document_response(job["cover_letter"], _slug(f"{who} Cover Letter {job.get('company') or ''}"), ext,
                              f"{who} Cover Letter".strip())


class ImportIn(BaseModel):
    url: str | None = None
    text: str


def _import(url: str | None, text: str) -> dict:
    return _save_import(url, text, ai.parse_job_text(text, url))


def _save_import(url: str | None, text: str, parsed: ai.ParsedJob) -> dict:
    """Add a posting Claude read (API or Claude Code). Pasting the same text twice finds the same job."""
    ext_id = url or f"manual-{hashlib.sha1(text.strip().encode('utf-8')).hexdigest()[:16]}"
    job = sources.make_job(
        source="manual", external_id=ext_id, title=parsed.title or "Untitled role",
        company=parsed.company, location=parsed.location, is_remote=parsed.is_remote,
        employment_type=parsed.employment_type or None, salary_text=parsed.salary_text or None,
        url=url, description=parsed.description,
        publisher=_publisher_from_url(url),
    )
    prefs = config.get_preferences()
    master = resume.get_master()
    from . import matcher
    job["keyword_score"] = matcher.keyword_score(job, prefs, matcher.resume_skills(master["content"]) if master else [])
    job["found_at"] = db.now_iso()
    job["status"] = "saved"
    with db.conn() as c:
        existing = c.execute("SELECT id FROM jobs WHERE source='manual' AND external_id=?", (job["external_id"],)).fetchone()
        if existing:
            return {"id": existing["id"], "existing": True}
        cols = list(job.keys())
        cur = c.execute(f"INSERT INTO jobs({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", [job[k] for k in cols])
        jid = cur.lastrowid
    db.log(f"Imported job: {job['title']} at {job['company'] or '?'}")
    return {"id": jid, "existing": False}


def _publisher_from_url(url: str | None) -> str | None:
    if not url:
        return None
    for host, name in (("indeed.", "Indeed"), ("ziprecruiter.", "ZipRecruiter"), ("linkedin.", "LinkedIn"),
                       ("glassdoor.", "Glassdoor"), ("dice.", "Dice"), ("wellfound.", "Wellfound"),
                       ("greenhouse.io", "Greenhouse"), ("lever.co", "Lever"), ("myworkdayjobs", "Workday")):
        if host in url:
            return name
    return None


@app.post("/api/jobs/import")
async def import_job(body: ImportIn):
    if len(body.text.strip()) < 80:
        raise HTTPException(400, "Paste the full job description text.")
    return await run_in_threadpool(_ai_guard, _import, body.url, body.text)


# ---- Claude.ai (copy prompt / paste reply) --------------------------------
# Lets the user run tailoring, cover letters and email drafts in the Claude app (e.g. on a Pro plan)
# with the exact prompt the API path uses. Works without an Anthropic API key.

class PromptIn(BaseModel):
    kind: str                       # tailor | cover | reply | outreach
    job_id: int | None = None
    thread_id: str | None = None
    recipient: str = ""
    instructions: str = ""


async def _task_prompt(body: PromptIn) -> tuple[str, str, dict | None]:
    """(chat prompt, reply kind, job) for a writing task. Shared by Claude.ai copy/paste and Claude Code."""
    master = _master_or_400()
    job = _get_job(body.job_id) if body.job_id else None
    if body.kind == "tailor":
        if not job:
            raise HTTPException(400, "Pick a job first.")
        parts, user = ai.tailor_prompt(job, master["content"]); fmt = "tailor"
    elif body.kind == "cover":
        if not job:
            raise HTTPException(400, "Pick a job first.")
        parts, user = ai.cover_prompt(job, job.get("tailored_resume") or master["content"]); fmt = "cover"
    elif body.kind == "reply":
        if not body.thread_id:
            raise HTTPException(400, "No email thread given.")
        thread = await run_in_threadpool(_gmail_guard, gmail.get_thread, body.thread_id, False)
        resume_md = (job or {}).get("tailored_resume") or master["content"]
        parts, user = ai.reply_prompt(thread, job, resume_md, body.instructions); fmt = "email"
    elif body.kind == "outreach":
        resume_md = (job or {}).get("tailored_resume") or master["content"]
        parts, user = ai.outreach_prompt(job, resume_md, body.recipient, body.instructions); fmt = "email"
    else:
        raise HTTPException(400, f"Unknown prompt type {body.kind!r}.")
    return ai.chat_prompt(parts, user, fmt), fmt, job


# ---- local LLM (home workstation) ------------------------------------------

@app.get("/api/local-llm/status")
def local_llm_status():
    return ai.local_status()


# ---- built-in local model (managed llama.cpp + Qwen3 GGUF tiers) ------------

@app.get("/api/local-model/status")
def local_model_status():
    return local_model.status()


@app.post("/api/local-model/install")
def local_model_install(body: dict):
    if err := local_model.start_install(body.get("tier", "")):
        raise HTTPException(400, err)
    return {"started": True}


@app.post("/api/local-model/use")
def local_model_use(body: dict):
    if err := local_model.start_server(body.get("tier", ""), apply_engine=True):
        raise HTTPException(400, err)
    return {"started": True}


@app.post("/api/local-model/stop")
def local_model_stop():
    local_model.stop_server()
    return {"stopped": True}


@app.post("/api/ai/score-unscored")
def score_unscored():
    """Manual catch-up: score every new, visible job left unscored (local LLM: uncapped; API engine: capped)."""
    if config.use_claude_code():
        raise HTTPException(400, "With Claude Code, scores come from the Claude Code panel button.")
    if not config.can_auto_ai():
        raise HTTPException(400, "Pick an AI engine first (Settings > App).")
    _master_or_400()
    if err := monitor.start_score_backlog():
        raise HTTPException(409, err)
    return {"started": True}


# ---- Claude Code (embedded interactive terminal) ---------------------------

@app.get("/api/claude-code/status")
def claude_code_status():
    exe = claude_code.find_claude()
    return {"installed": bool(exe), "path": exe,
            "enabled": bool(config.get_app_settings().get("claude_code_enabled", True))}


class ClaudeCodeIn(PromptIn):
    restart: bool = False
    rows: int = 30
    cols: int = 110
    resume_id: int | None = None    # cleanup
    text: str = ""                  # import: the posting's text
    url: str | None = None          # import: where it came from


WRITING_KINDS = ("tailor", "cover", "reply", "outreach")


async def _cc_task(body: ClaudeCodeIn) -> dict:
    """Everything claude_code.start() needs for one task: TASK.md, data files, how to read the result."""
    if body.kind in WRITING_KINDS:
        prompt, fmt, job = await _task_prompt(body)
        return {"task": claude_code.task_markdown(prompt, fmt), "files": None, "reply_kind": fmt,
                "result_name": "result.md", "key": (body.kind, body.job_id, body.thread_id), "job_id": body.job_id,
                "label": (job or {}).get("company") or (job or {}).get("title") or body.kind, "meta": {}, "fresh": False}
    if body.kind == "score":
        master = _master_or_400()
        cap = int(config.get_preferences().get("ai_score_max_per_run") or 15)  # cc batch stays capped (plan usage)
        jobs = [_get_job(body.job_id)] if body.job_id else await run_in_threadpool(monitor.unscored_jobs, cap)
        if not jobs:
            raise HTTPException(400, "No new jobs are waiting for an AI score.")
        task, files = ai.score_task(jobs, master["content"], config.get_preferences())
        label = (jobs[0].get("company") or "job") if body.job_id else "new-jobs"
        return {"task": task, "files": files, "reply_kind": "score", "result_name": "result.json",
                "key": ("score", body.job_id or "batch", None), "job_id": body.job_id, "label": label,
                "meta": {"job_ids": [j["id"] for j in jobs]}, "fresh": True}
    if body.kind == "triage":
        if not gmail.is_connected():
            raise HTTPException(400, "Connect Gmail first (Settings > Gmail).")
        tids = [body.thread_id] if body.thread_id else await run_in_threadpool(gmail.untriaged_ids)
        if not tids:
            raise HTTPException(400, "No emails are waiting to be sorted.")
        threads = [await run_in_threadpool(_gmail_guard, gmail.get_thread, t, False) for t in tids]
        jobs = await run_in_threadpool(gmail.triage_jobs)
        task, files = ai.triage_task(threads, jobs)
        return {"task": task, "files": files, "reply_kind": "triage", "result_name": "result.json",
                "key": ("triage", None, body.thread_id or "batch"), "job_id": None, "label": "email",
                "meta": {"thread_ids": tids, "job_ids": [j["id"] for j in jobs]}, "fresh": True}
    if body.kind == "import":
        if len(body.text.strip()) < 80:
            raise HTTPException(400, "Paste the full job description text.")
        task, files = ai.import_task(body.text, body.url)
        digest = hashlib.sha1(body.text.strip().encode("utf-8")).hexdigest()[:10]
        return {"task": task, "files": files, "reply_kind": "import", "result_name": "result.json",
                "key": ("import", digest, None), "job_id": None, "label": "posting",
                "meta": {"url": body.url, "text": body.text}, "fresh": True}
    if body.kind == "cleanup":
        r = resume.get_resume(body.resume_id) if body.resume_id else None
        if not r or not r["content"].strip():
            raise HTTPException(400, "Pick a resume with some text first.")
        task, files = ai.cleanup_task(r["content"])
        return {"task": task, "files": files, "reply_kind": "cleanup", "result_name": "result.md",
                "key": ("cleanup", r["id"], None), "job_id": None, "label": "resume",
                "meta": {"resume_id": r["id"]}, "fresh": True}
    raise HTTPException(400, f"Unknown task type {body.kind!r}.")


@app.post("/api/claude-code/sessions")
async def claude_code_start(body: ClaudeCodeIn):
    if not config.get_app_settings().get("claude_code_enabled", True):
        raise HTTPException(400, "Claude Code is turned off in Settings.")
    t = await _cc_task(body)
    try:
        s, reused = await run_in_threadpool(
            claude_code.start, body.kind, t["reply_kind"], t["key"], t["task"], t["job_id"], t["label"],
            body.restart, body.rows, body.cols, t["files"], t["result_name"], t["meta"], t["fresh"])
    except claude_code.ClaudeCodeError as e:
        raise HTTPException(400, str(e))
    return {"id": s.id, "token": s.token, "reply_kind": s.reply_kind, "reused": reused, "folder": str(s.folder),
            "result_mtime": s.result_mtime()}


def _cc_session(sid: str, token: str | None):
    try:
        return claude_code.get(sid, token)
    except claude_code.ClaudeCodeError as e:
        raise HTTPException(404, str(e))


@app.get("/api/claude-code/sessions/{sid}")
def claude_code_session(sid: str, token: str):
    s = _cc_session(sid, token)
    return {"alive": s.alive, "result_mtime": s.result_mtime()}


def _fields(model: type[BaseModel], item: dict) -> BaseModel:
    """Validate one item from result.json against an ai.py schema, ignoring any extra keys Claude added."""
    return model.model_validate({k: item[k] for k in model.model_fields if k in item})


def _results(data) -> list[dict]:
    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ai.AIError('result.json should have a "results" list.')
    return [x for x in items if isinstance(x, dict)]


def _first_line(e: Exception) -> str:
    return str(e).splitlines()[0] if str(e) else type(e).__name__


def _apply_result(s) -> dict:
    """Parse a session's result file and save it where it belongs. Returns the fields for the UI."""
    text = s.result_path.read_text(encoding="utf-8")
    kind = s.reply_kind
    if kind in ("tailor", "cover", "email"):
        fields = ai.parse_chat_reply(kind, text)
        if kind in ("tailor", "cover") and s.job_id:
            patch_job(s.job_id, fields)
        return fields
    if kind == "cleanup":
        md = ai.parse_chat_reply("cover", text)["cover_letter"]  # same unwrapping: strips fences and preambles
        if len(md) < 50:
            raise ai.AIError("result.md doesn't look like a full resume yet.")
        return {"markdown": md, "resume_id": s.meta.get("resume_id")}
    if kind == "score":
        wanted, saved, problems = set(s.meta["job_ids"]), 0, []
        for item in _results(ai.parse_json_result(text)):
            try:
                jid = int(item.get("job_id"))
                if jid not in wanted:
                    continue
                item.setdefault("seniority_fit", "unclear")
                item.setdefault("is_remote", False)
                item.setdefault("strengths", [])
                item.setdefault("gaps", [])
                monitor.save_score(jid, _fields(ai.MatchResult, item), scorer="Claude Code")
                saved += 1
            except (TypeError, ValueError) as e:
                problems.append(f"job {item.get('job_id')}: {_first_line(e)}")
        if not saved:
            raise ai.AIError("No scores in result.json yet" + (f" ({problems[0]})" if problems else "."))
        db.log(f"Claude Code scored {saved} job(s)")
        return {"saved": saved, "expected": len(wanted), "problems": problems}
    if kind == "triage":
        wanted, job_ids, saved, problems = set(s.meta["thread_ids"]), set(s.meta["job_ids"]), 0, []
        for item in _results(ai.parse_json_result(text)):
            tid = str(item.get("thread_id") or "").removeprefix("emails/").removesuffix(".md")
            if tid not in wanted:
                continue
            try:
                item.setdefault("suggested_job_status", "")
                item["suggested_job_status"] = item["suggested_job_status"] or ""
                item["job_id"] = int(item.get("job_id") or 0)
                gmail.save_triage(tid, _fields(ai.EmailTriage, item), job_ids)
                saved += 1
            except (TypeError, ValueError) as e:
                problems.append(f"{tid}: {_first_line(e)}")
        if not saved:
            raise ai.AIError("No sorted emails in result.json yet" + (f" ({problems[0]})" if problems else "."))
        db.log(f"Claude Code sorted {saved} email thread(s)")
        return {"saved": saved, "expected": len(wanted), "problems": problems}
    if kind == "import":
        data = ai.parse_json_result(text)
        if not isinstance(data, dict):
            raise ai.AIError("result.json should be one JSON object.")
        defaults = {"title": "", "company": "", "location": "", "is_remote": False, "employment_type": "",
                    "salary_text": "", "description": s.meta["text"]}
        item = {**defaults, **{k: v for k, v in data.items() if v is not None}}
        try:
            parsed = _fields(ai.ParsedJob, item)
        except ValueError as e:
            raise ai.AIError(f"result.json: {_first_line(e)}")
        return _save_import(s.meta.get("url"), s.meta["text"], parsed)
    raise ai.AIError(f"Unknown result type {kind!r}.")


@app.post("/api/claude-code/sessions/{sid}/apply")
def claude_code_apply(sid: str, token: str):
    """Read the session's result file, parse it, and save it (job fields, scores, email sorting, imported job)."""
    s = _cc_session(sid, token)
    if not s.result_path.exists():
        raise HTTPException(400, f"Claude hasn't saved {s.result_name} yet.")
    try:
        fields = _apply_result(s)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    return {"fields": fields, "saved_to_job": s.reply_kind in ("tailor", "cover") and bool(s.job_id),
            "result_mtime": s.result_mtime()}


@app.post("/api/claude-code/sessions/{sid}/stop")
def claude_code_stop(sid: str, token: str):
    _cc_session(sid, token).stop()
    return {"ok": True}


@app.websocket("/api/claude-code/ws/{sid}")
async def claude_code_ws(ws: WebSocket, sid: str, token: str = ""):
    # WebSockets skip the HTTP middleware and aren't covered by CORS, so check Host, Origin and the
    # per-session token here; otherwise any web page could drive the terminal.
    host = (ws.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    origin = ws.headers.get("origin") or ""
    origin_host = re.sub(r"^https?://", "", origin).split("/")[0].rsplit(":", 1)[0]
    try:
        s = claude_code.get(sid, token)
    except claude_code.ClaudeCodeError:
        s = None
    if host not in ALLOWED_HOSTS or origin_host not in ALLOWED_HOSTS or s is None:
        await ws.close(code=4403)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    q, backlog = s.subscribe(loop)

    async def pump_out():
        if backlog:
            await ws.send_text(backlog)
        while True:
            data = await q.get()
            if data is None:
                break
            await ws.send_text(data)

    out = asyncio.create_task(pump_out())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("t") == "in":
                await run_in_threadpool(s.write, str(msg.get("d", "")))
            elif msg.get("t") == "resize":
                await run_in_threadpool(s.resize, int(msg.get("r", 0)), int(msg.get("c", 0)))
    except (WebSocketDisconnect, json.JSONDecodeError, ValueError, RuntimeError):
        pass
    finally:
        out.cancel()
        s.unsubscribe(q)


# ---- debug viewer -----------------------------------------------------------
# Data for the Console/Network panel. These routes are excluded from the Network log (it would log itself).

@app.get("/api/debug/summary")
def debug_summary():
    return logs.counts()


@app.get("/api/debug/console")
def debug_console(after: int = 0):
    return {"entries": logs.console_after(after)}


@app.get("/api/debug/network")
def debug_network(after: int = 0):
    return {"entries": logs.network_after(after)}


@app.get("/api/debug/network/{eid}")
def debug_network_entry(eid: int):
    e = logs.network_entry(eid)
    if not e:
        raise HTTPException(404, "That request is no longer in the buffer.")
    return e


class ClientLogIn(BaseModel):
    level: str = "error"
    message: str


@app.post("/api/debug/client-log")
def debug_client_log(body: ClientLogIn):
    """UI errors/warnings, so they reach the log file too."""
    lvl = {"error": 40, "warning": 30, "warn": 30}.get(body.level, 20)
    import logging
    logging.getLogger("jobscout.ui").log(lvl, logs.scrub(body.message[:5000]))
    return {"ok": True}


@app.post("/api/debug/clear")
def debug_clear():
    logs.clear()
    return {"ok": True}


@app.get("/api/debug/log-file")
def debug_log_file():
    path = logs.LOG_DIR / "jobscout.log"
    if not path.exists():
        raise HTTPException(404, "No log file yet.")
    from . import env
    return FileResponse(path, media_type="text/plain", filename=f"jobscout-{env.ENV}-log.txt")


# ---- monitor --------------------------------------------------------------

@app.post("/api/monitor/run")
async def run_now():
    return await run_in_threadpool(monitor.run_once)


@app.post("/api/refresh")
async def refresh_all():
    """The dashboard's Refresh button: one job check plus one Gmail sync (if connected)."""
    jobs = await run_in_threadpool(monitor.run_once)
    mail = None
    if gmail.is_connected():
        try:
            mail = await run_in_threadpool(gmail.sync)
        except (gmail.GmailError, ai.AIError) as e:
            mail = {"error": str(e)}
    return {"jobs": jobs, "mail": mail}


@app.get("/api/monitor/status")
def monitor_status():
    with db.conn() as c:
        log = [dict(r) for r in c.execute("SELECT at, level, message FROM activity ORDER BY id DESC LIMIT 100").fetchall()]
    return {**monitor.state, "activity": log}


# ---- Gmail ----------------------------------------------------------------

def _gmail_guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except gmail.GmailError as e:
        raise HTTPException(400, str(e))
    except ai.AIError as e:
        raise HTTPException(400, str(e))


@app.get("/api/gmail/status")
def gmail_status():
    return {
        "has_client_secret": gmail.has_client_secret(),
        "connected": gmail.is_connected(),
        "account": gmail.account(),
        "last_sync": db.kv_get("gmail_last_sync"),
        "default_query": gmail.DEFAULT_QUERY,
    }


@app.post("/api/gmail/client-secret")
async def gmail_client_secret(file: UploadFile = File(...)):
    _gmail_guard(gmail.save_client_secret, await file.read())
    return {"ok": True}


@app.post("/api/gmail/connect")
async def gmail_connect():
    email = await run_in_threadpool(_gmail_guard, gmail.connect)
    return {"account": email}


@app.post("/api/gmail/disconnect")
async def gmail_disconnect():
    await run_in_threadpool(gmail.disconnect)
    return {"ok": True}


@app.post("/api/gmail/sync")
async def gmail_sync():
    return await run_in_threadpool(_gmail_guard, gmail.sync)


@app.get("/api/gmail/threads")
def gmail_threads(view: str = "all", q: str = "", job_id: int | None = None):
    where, args = [], []
    if job_id is not None:
        where.append("t.job_id = ?"); args.append(job_id)
    elif view == "needs_reply":
        where.append("t.needs_reply = 1 AND t.last_from_me = 0")
    elif view == "unread":
        where.append("t.unread = 1")
    elif view == "inbox":
        where.append("t.in_inbox = 1")
    elif view == "linked":
        where.append("t.job_id IS NOT NULL")
    elif view in ("interview", "assessment", "offer", "rejection", "application_received", "recruiter_outreach", "outreach"):
        where.append("t.category = ?"); args.append(view)
    if q:
        where.append("(t.subject LIKE ? OR t.counterpart LIKE ? OR t.snippet LIKE ?)"); args += [f"%{q}%"] * 3
    sql = ("SELECT t.*, j.title AS job_title, j.company AS job_company, j.status AS job_status "
           "FROM email_threads t LEFT JOIN jobs j ON j.id = t.job_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.last_date DESC LIMIT 500"
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        counts = dict(c.execute(
            "SELECT 'needs_reply', COUNT(*) FROM email_threads WHERE needs_reply=1 AND last_from_me=0 "
            "UNION ALL SELECT 'unread', COUNT(*) FROM email_threads WHERE unread=1 "
            "UNION ALL SELECT 'untriaged', COUNT(*) FROM email_threads WHERE triaged_at IS NULL").fetchall())
    return {"threads": rows, "counts": counts}


@app.get("/api/gmail/threads/{tid}")
async def gmail_thread(tid: str):
    thread = await run_in_threadpool(_gmail_guard, gmail.get_thread, tid)
    with db.conn() as c:
        meta = c.execute("SELECT t.*, j.title AS job_title, j.company AS job_company, j.status AS job_status "
                         "FROM email_threads t LEFT JOIN jobs j ON j.id=t.job_id WHERE t.thread_id=?", (tid,)).fetchone()
    return {**thread, "meta": dict(meta) if meta else None}


class LinkIn(BaseModel):
    job_id: int | None = None


@app.put("/api/gmail/threads/{tid}/job")
def gmail_link(tid: str, body: LinkIn):
    with db.conn() as c:
        c.execute("UPDATE email_threads SET job_id=?, link_manual=1 WHERE thread_id=?", (body.job_id, tid))
    return {"ok": True}


@app.post("/api/gmail/threads/{tid}/triage")
async def gmail_triage(tid: str):
    return await run_in_threadpool(_gmail_guard, gmail.triage, tid)


@app.post("/api/gmail/threads/{tid}/archive")
async def gmail_archive(tid: str):
    await run_in_threadpool(_gmail_guard, gmail.archive, tid)
    return {"ok": True}


@app.post("/api/gmail/threads/{tid}/done")
def gmail_mark_done(tid: str):
    """Clear the 'needs reply' flag without replying."""
    with db.conn() as c:
        c.execute("UPDATE email_threads SET needs_reply=0 WHERE thread_id=?", (tid,))
    return {"ok": True}


class DraftIn(BaseModel):
    thread_id: str | None = None
    job_id: int | None = None
    recipient: str = ""
    instructions: str = ""


@app.post("/api/gmail/draft-ai")
async def gmail_draft_ai(body: DraftIn):
    """Claude writes a reply (thread_id given) or a new outreach email. Nothing is sent."""
    master = resume.get_master()
    if not master:
        raise HTTPException(400, "Add a resume first (Resume tab).")
    job = _get_job(body.job_id) if body.job_id else None
    resume_md = (job or {}).get("tailored_resume") or master["content"]
    if body.thread_id:
        thread = await run_in_threadpool(_gmail_guard, gmail.get_thread, body.thread_id, False)
        res = await run_in_threadpool(_gmail_guard, ai.draft_reply, thread, job, resume_md, body.instructions)
    else:
        res = await run_in_threadpool(_gmail_guard, ai.draft_outreach, job, resume_md, body.recipient, body.instructions)
    return res.model_dump()


class SendIn(BaseModel):
    to: str
    subject: str
    body: str
    cc: str | None = None
    thread_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    job_id: int | None = None
    attach_resume: bool = False
    attach_cover_letter: bool = False
    draft_only: bool = False


@app.post("/api/gmail/send")
async def gmail_send(body: SendIn):
    attachments = []
    s = config.get_app_settings()
    who = s.get("applicant_name") or "Resume"
    job = _get_job(body.job_id) if body.job_id else None
    if body.attach_resume:
        md = (job or {}).get("tailored_resume") or _master_or_400()["content"]
        attachments.append((f"{_slug(who)}_Resume.docx", resume.markdown_to_docx(md)))
    if body.attach_cover_letter:
        if not job or not job.get("cover_letter"):
            raise HTTPException(400, "This job has no cover letter yet.")
        attachments.append((f"{_slug(who)}_Cover_Letter.docx", resume.markdown_to_docx(job["cover_letter"])))
    return await run_in_threadpool(
        _gmail_guard, gmail.send, body.to, body.subject, body.body, cc=body.cc, thread_id=body.thread_id,
        in_reply_to=body.in_reply_to, references=body.references, attachments=attachments, job_id=body.job_id,
        draft_only=body.draft_only)


# ---- UI -------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
