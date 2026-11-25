"""Gmail integration via the official Gmail API and Google OAuth.

You sign in on Google's own page in your browser (SSO/2FA work as usual); the app only receives an
OAuth token, never your password. The refresh token lives in Windows Credential Manager.
Nothing is sent unless you click Send in the app.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import threading
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

import httpx

from . import config, db
from .env import APP_NAME
from .sources import strip_html

CLIENT_SECRET_PATH = db.DATA_DIR / "google_client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # read, label/archive, draft and send
TOKEN_SECRET = "gmail_refresh_token"

DEFAULT_QUERY = (
    '(subject:(application OR applied OR applying OR interview OR position OR role OR opportunity OR recruiter '
    'OR offer OR candidacy OR candidate OR "thank you for your interest" OR "next steps" OR hiring) '
    'OR from:(greenhouse.io OR lever.co OR myworkday.com OR workday.com OR ashbyhq.com OR smartrecruiters.com '
    'OR icims.com OR jobvite.com OR bamboohr.com OR indeed.com OR ziprecruiter.com OR linkedin.com OR dice.com '
    'OR hire.lever.co OR rippling.com OR workable.com)) -category:promotions -category:social'
)


class GmailError(Exception):
    pass


# ---- auth -----------------------------------------------------------------

def has_client_secret() -> bool:
    return CLIENT_SECRET_PATH.exists()


def save_client_secret(data: bytes) -> None:
    try:
        j = json.loads(data)
    except json.JSONDecodeError:
        raise GmailError("That file isn't valid JSON. Download the OAuth client JSON from Google Cloud Console.")
    if "installed" not in j:
        raise GmailError('Wrong client type. In Google Cloud, create an OAuth client ID of type "Desktop app".')
    CLIENT_SECRET_PATH.write_bytes(data)


def _client_info() -> dict:
    if not has_client_secret():
        raise GmailError("Upload your Google OAuth client file first (Settings > Gmail).")
    return json.loads(CLIENT_SECRET_PATH.read_text())["installed"]


def connect() -> str:
    """Opens Google's sign-in page in your default browser and waits (up to 5 min) for you to finish."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not has_client_secret():
        raise GmailError("Upload your Google OAuth client file first (Settings > Gmail).")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    try:
        creds = flow.run_local_server(
            host="localhost", port=0, open_browser=True, timeout_seconds=300,
            prompt="consent", access_type="offline",
            authorization_prompt_message="", success_message=f"{APP_NAME} is connected to Gmail. You can close this tab.",
        )
    except Exception as e:
        raise GmailError(f"Google sign-in didn't complete: {e}")
    if not creds.refresh_token:
        raise GmailError("Google didn't return a refresh token. Try Disconnect, then Connect again.")
    config.store_secret(TOKEN_SECRET, creds.refresh_token)
    email = _service(creds).users().getProfile(userId="me").execute().get("emailAddress")
    db.kv_set("gmail_account", email)
    db.log(f"Gmail connected: {email}")
    return email


def is_connected() -> bool:
    return bool(config.get_secret(TOKEN_SECRET)) and has_client_secret()


def account() -> str | None:
    return db.kv_get("gmail_account") if is_connected() else None


def disconnect() -> None:
    token = config.get_secret(TOKEN_SECRET)
    from .env import IS_STAGING
    # Staging may hold a copy of production's sign-in; revoking it would log production out too.
    if token and not IS_STAGING:
        try:
            httpx.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=15)
        except httpx.HTTPError:
            pass
    config.store_secret(TOKEN_SECRET, None)
    db.kv_set("gmail_account", None)
    db.log("Gmail disconnected")


def _creds():
    from google.oauth2.credentials import Credentials
    token = config.get_secret(TOKEN_SECRET)
    if not token:
        raise GmailError("Gmail isn't connected (Settings > Gmail).")
    info = _client_info()
    return Credentials(None, refresh_token=token, token_uri=info["token_uri"], client_id=info["client_id"],
                       client_secret=info["client_secret"], scopes=SCOPES)


_svc_cache: dict = {}


GMAIL_TIMEOUT = 60  # seconds per HTTP call; the library's default is to wait forever


def _service(creds=None):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    def _build(c):
        return build("gmail", "v1", http=AuthorizedHttp(c, http=httplib2.Http(timeout=GMAIL_TIMEOUT)),
                     cache_discovery=False)
    if creds is not None:
        return _build(creds)
    if "svc" not in _svc_cache:
        _svc_cache["svc"] = _build(_creds())
    return _svc_cache["svc"]


def _call(req):
    import time
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError
    from . import logs
    t0 = time.perf_counter()
    method, uri = getattr(req, "method", "GET"), getattr(req, "uri", "gmail")
    body = getattr(req, "body", None)
    reqinfo = {"body": logs.body_text(body if len(str(body or "")) < 4000 else f"<{len(body):,} bytes>", "application/json")}
    try:
        res = req.execute()
        summary = {k: (f"<{len(v)} items>" if isinstance(v, list) else v) for k, v in (res or {}).items()
                   if k in ("id", "threadId", "historyId", "resultSizeEstimate", "nextPageToken", "threads", "messages", "labelIds", "emailAddress")}
        logs.record_call("outbound", "Gmail", method, uri, t0, 200, reqinfo, {"body": json.dumps(summary, indent=2)})
        return res
    except RefreshError as e:
        logs.record_call("outbound", "Gmail", method, uri, t0, 401, reqinfo, error=f"sign-in expired: {e}")
        _svc_cache.clear()
        raise GmailError("Gmail sign-in expired. Go to Settings > Gmail and click Connect again.")
    except HttpError as e:
        logs.record_call("outbound", "Gmail", method, uri, t0, e.status_code, reqinfo, error=str(e.reason))
        raise GmailError(f"Gmail API error {e.status_code}: {e.reason}")
    except (TimeoutError, OSError) as e:  # includes socket timeouts and connection failures
        logs.record_call("outbound", "Gmail", method, uri, t0, None, reqinfo, error=f"{type(e).__name__}: {e}")
        _svc_cache.clear()  # start a fresh connection next time
        raise GmailError("Gmail didn't respond. Check your internet connection and try again.")


def svc():
    """Gmail service; the client is not thread-safe, so callers hold _lock."""
    return _service()


_lock = threading.Lock()


# ---- parsing ----------------------------------------------------------------

def _headers(payload: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _body(payload: dict) -> str:
    plain, html_ = [], []

    def walk(p):
        mt = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and not (p.get("filename")):
            text = _b64(data).decode("utf-8", errors="replace")
            (plain if mt == "text/plain" else html_ if mt == "text/html" else plain).append(text)
        for part in p.get("parts") or []:
            walk(part)

    walk(payload)
    text = "\n".join(plain) if plain else strip_html("\n".join(html_))
    return text.strip()


def _strip_quoted(text: str) -> str:
    """Drop the quoted history under 'On <date>, <name> wrote:' so threads read cleanly."""
    m = re.search(r"\n\s*On .{5,200}wrote:\s*\n", text)
    return text[: m.start()].rstrip() if m else text


def _attachments(payload: dict) -> list[str]:
    out = []

    def walk(p):
        if p.get("filename"):
            out.append(p["filename"])
        for part in p.get("parts") or []:
            walk(part)
    walk(payload)
    return out


def _date_iso(s: str | None) -> str | None:
    try:
        return parsedate_to_datetime(s).isoformat() if s else None
    except (TypeError, ValueError):
        return None


# ---- sync -------------------------------------------------------------------

def _link_job(text: str) -> int | None:
    """Best-effort: match a job by company name appearing in the sender, subject or snippet."""
    t = text.lower()
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, company FROM jobs WHERE company IS NOT NULL AND length(company) >= 3 "
            "AND status NOT IN ('low_match','dismissed') ORDER BY found_at DESC"
        ).fetchall()
    for r in rows:
        name = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|group|technologies|technology)\b\.?", "", r["company"].lower()).strip(" ,.")
        if len(name) >= 3 and re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", t):
            return r["id"]
    return None


def sync(days: int | None = None, max_threads: int = 200) -> dict:
    s = config.get_app_settings()
    days = days or int(s.get("gmail_days") or 60)
    query = f"newer_than:{days}d ({s.get('gmail_query') or DEFAULT_QUERY})"
    me = (account() or "").lower()
    new = updated = 0
    with _lock:
        ids, token = [], None
        while len(ids) < max_threads:
            res = _call(svc().users().threads().list(userId="me", q=query, maxResults=100, pageToken=token))
            ids += [t["id"] for t in res.get("threads", [])]
            token = res.get("nextPageToken")
            if not token:
                break
        # Also keep threads already in the app (e.g. outreach you sent) up to date.
        with db.conn() as c:
            known = {r["thread_id"]: (r["history_id"], r["message_count"]) for r in c.execute(
                "SELECT thread_id, history_id, message_count FROM email_threads WHERE last_date >= datetime('now', ?)",
                (f"-{days} days",))}
        for tid in dict.fromkeys(ids[:max_threads] + list(known)):
            t = _call(svc().users().threads().get(userId="me", id=tid, format="metadata",
                                                  metadataHeaders=["From", "To", "Cc", "Subject", "Date"]))
            msgs = t.get("messages", [])
            if not msgs or (tid in known and known[tid][0] == t.get("historyId")):
                continue
            if tid in known and known[tid][1] == len(msgs):
                # Only labels changed (read/archived elsewhere): refresh flags, keep Claude's triage.
                labels = {l for m in msgs for l in m.get("labelIds", [])}
                with db.conn() as c:
                    c.execute("UPDATE email_threads SET history_id=?, unread=?, in_inbox=?, labels=? WHERE thread_id=?",
                              (t.get("historyId"), int("UNREAD" in labels), int("INBOX" in labels),
                               json.dumps(sorted(labels)), tid))
                continue
            first, last = _headers(msgs[0]["payload"]), _headers(msgs[-1]["payload"])
            labels = sorted({l for m in msgs for l in m.get("labelIds", [])})
            people = {a.lower() for m in msgs for _, a in getaddresses(
                [_headers(m["payload"]).get(k, "") for k in ("from", "to", "cc")]) if a}
            counterpart = next((h.get("from") for h in (_headers(m["payload"]) for m in reversed(msgs))
                                if parseaddr(h.get("from", ""))[1].lower() != me), last.get("from"))
            row = {
                "thread_id": tid,
                "history_id": t.get("historyId"),
                "subject": first.get("subject") or "(no subject)",
                "counterpart": counterpart,
                "participants": json.dumps(sorted(people - {me})),
                "last_from_me": int(parseaddr(last.get("from", ""))[1].lower() == me),
                "last_date": _date_iso(last.get("date")) or db.now_iso(),
                "snippet": msgs[-1].get("snippet", ""),
                "message_count": len(msgs),
                "unread": int("UNREAD" in labels),
                "in_inbox": int("INBOX" in labels),
                "labels": json.dumps(labels),
                "synced_at": db.now_iso(),
            }
            # Match the thread to a job BEFORE taking the database lock: _link_job opens its own connection,
            # and calling it inside `with db.conn()` deadlocked the app (froze every request touching the DB).
            guess = _link_job(f"{row['counterpart']} {row['subject']} {row['snippet']}")
            with db.conn() as c:
                existing = c.execute("SELECT job_id, link_manual FROM email_threads WHERE thread_id=?", (tid,)).fetchone()
                if existing:
                    cols = [k for k in row if k != "thread_id"]
                    c.execute(f"UPDATE email_threads SET {', '.join(f'{k}=?' for k in cols)}, triaged_at=NULL WHERE thread_id=?",
                              [row[k] for k in cols] + [tid])
                    if not existing["link_manual"] and not existing["job_id"]:
                        c.execute("UPDATE email_threads SET job_id=? WHERE thread_id=?", (guess, tid))
                    updated += 1
                else:
                    row["job_id"] = guess
                    cols = list(row)
                    c.execute(f"INSERT INTO email_threads({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
                              [row[k] for k in cols])
                    new += 1
    db.kv_set("gmail_last_sync", db.now_iso())
    triaged = triage_pending()
    db.log(f"Gmail sync: {new} new thread(s), {updated} updated, {triaged} triaged by the AI")
    return {"new": new, "updated": updated, "triaged": triaged}


def triage_pending(limit: int = 25) -> int:
    """Have the AI label threads that changed since their last triage. Runs automatically for the local LLM and
    for the Anthropic API when a key is saved; skipped when Claude Code is the engine (then the user sorts them
    with the Mail tab's "Sort with Claude" button). See config.can_auto_ai()."""
    from . import ai
    if not config.can_auto_ai():
        return 0
    with db.conn() as c:
        tids = [r["thread_id"] for r in c.execute(
            "SELECT thread_id FROM email_threads WHERE triaged_at IS NULL ORDER BY last_date DESC LIMIT ?", (limit,))]
    done = 0
    for tid in tids:
        try:
            triage(tid)
            done += 1
        except (ai.AIError, GmailError) as e:
            db.log(f"Email triage stopped: {e}", "warn")
            break
    return done


def triage_jobs() -> list[dict]:
    """The job list Claude picks from when linking an email to a job."""
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, title, company, status FROM jobs WHERE status NOT IN ('low_match','dismissed') "
            "ORDER BY found_at DESC LIMIT 150")]


def untriaged_ids(limit: int = 25) -> list[str]:
    with db.conn() as c:
        return [r["thread_id"] for r in c.execute(
            "SELECT thread_id FROM email_threads WHERE triaged_at IS NULL ORDER BY last_date DESC LIMIT ?", (limit,))]


def triage(thread_id: str) -> dict:
    from . import ai
    thread = get_thread(thread_id, mark_read=False)
    jobs = triage_jobs()
    res = ai.triage_email(thread, jobs)
    save_triage(thread_id, res, {j["id"] for j in jobs})
    return res.model_dump()


def save_triage(thread_id: str, res, job_ids: set[int]) -> None:
    """Store Claude's sorting of a thread (from the API or a Claude Code session). A job link the user set by
    hand always wins; Claude's job_id is used only if it's one of the jobs it was shown."""
    with db.conn() as c:
        current = c.execute("SELECT job_id, link_manual FROM email_threads WHERE thread_id=?", (thread_id,)).fetchone()
    if not current:
        return
    job_id = current["job_id"] if current["link_manual"] else (res.job_id if res.job_id in job_ids else current["job_id"])
    with db.conn() as c:
        c.execute(
            "UPDATE email_threads SET category=?, ai_summary=?, needs_reply=?, suggested_status=?, job_id=?, triaged_at=? WHERE thread_id=?",
            (res.category, res.summary, int(res.needs_reply), res.suggested_job_status or None, job_id, db.now_iso(), thread_id),
        )


# ---- read / act ---------------------------------------------------------------

def get_thread(thread_id: str, mark_read: bool = True) -> dict:
    with _lock:
        t = _call(svc().users().threads().get(userId="me", id=thread_id, format="full"))
        if mark_read and any("UNREAD" in m.get("labelIds", []) for m in t.get("messages", [])):
            _call(svc().users().threads().modify(userId="me", id=thread_id, body={"removeLabelIds": ["UNREAD"]}))
            with db.conn() as c:
                c.execute("UPDATE email_threads SET unread=0 WHERE thread_id=?", (thread_id,))
    me = (account() or "").lower()
    messages = []
    for m in t.get("messages", []):
        h = _headers(m["payload"])
        body = _body(m["payload"])
        messages.append({
            "id": m["id"],
            "from": h.get("from"), "to": h.get("to"), "cc": h.get("cc"),
            "date": _date_iso(h.get("date")), "subject": h.get("subject"),
            "message_id": h.get("message-id"), "references": h.get("references"),
            "from_me": parseaddr(h.get("from", ""))[1].lower() == me,
            "body": _strip_quoted(body)[:20000],
            "attachments": _attachments(m["payload"]),
        })
    return {"thread_id": thread_id, "messages": messages, "subject": messages[0]["subject"] if messages else ""}


def _build(to: str, subject: str, body: str, cc: str | None, in_reply_to: str | None, references: str | None,
           attachments: list[tuple[str, bytes]]) -> str:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if account():
        name = config.get_app_settings().get("applicant_name")
        msg["From"] = f"{name} <{account()}>" if name else account()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(body)
    for filename, data in attachments:
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send(to: str, subject: str, body: str, *, cc: str | None = None, thread_id: str | None = None,
         in_reply_to: str | None = None, references: str | None = None, attachments=(), job_id: int | None = None,
         draft_only: bool = False) -> dict:
    if not to.strip():
        raise GmailError("Add a recipient.")
    raw = _build(to, subject, body, cc, in_reply_to, references, list(attachments))
    payload = {"raw": raw, **({"threadId": thread_id} if thread_id else {})}
    with _lock:
        if draft_only:
            res = _call(svc().users().drafts().create(userId="me", body={"message": payload}))
            tid = res["message"]["threadId"]
        else:
            res = _call(svc().users().messages().send(userId="me", body=payload))
            tid = res["threadId"]
    # Track the thread in the app (so outreach shows up in Mail and on the job) and link it.
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM email_threads WHERE thread_id=?", (tid,)).fetchone():
            c.execute(
                "INSERT INTO email_threads(thread_id, subject, counterpart, participants, last_from_me, last_date, snippet, "
                "message_count, unread, in_inbox, labels, synced_at, job_id, link_manual, category) "
                "VALUES(?,?,?,?,1,?,?,1,0,0,'[]',?,?,?,?)",
                (tid, subject, to, json.dumps([to]), db.now_iso(), body[:200], db.now_iso(), job_id, int(bool(job_id)),
                 "outreach" if not thread_id else None),
            )
        elif job_id:
            c.execute("UPDATE email_threads SET job_id=?, link_manual=1 WHERE thread_id=?", (job_id, tid))
        if not draft_only:
            c.execute("UPDATE email_threads SET last_from_me=1, needs_reply=0, last_date=? WHERE thread_id=?", (db.now_iso(), tid))
    db.log(f"Gmail: {'saved draft' if draft_only else 'sent'} '{subject}' to {to}")
    return {"thread_id": tid, "draft": draft_only}


def archive(thread_id: str) -> None:
    with _lock:
        _call(svc().users().threads().modify(userId="me", id=thread_id, body={"removeLabelIds": ["INBOX"]}))
    with db.conn() as c:
        c.execute("UPDATE email_threads SET in_inbox=0 WHERE thread_id=?", (thread_id,))
