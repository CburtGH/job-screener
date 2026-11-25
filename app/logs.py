"""Logging and the debug viewer's data.

- Python logging goes to rotating files in data/logs/ (jobscout.log, 2 MB x 5) and to an in-memory "console"
  buffer that the debug panel reads.
- Every request to the app (method, path, status, timing, bodies) and every outbound call (job sources, Gmail,
  Claude) goes to an in-memory "network" buffer.
- Everything is redacted first: API keys, tokens, passwords and the values of the user's saved secrets are
  replaced with ***. Request/response bodies are truncated.
Buffers live in memory only (cleared when the app closes); the log files persist.
"""
from __future__ import annotations

import itertools
import json
import logging
import logging.handlers
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db

LOG_DIR = db.DATA_DIR / "logs"
CONSOLE_MAX, NETWORK_MAX = 3000, 600
BODY_MAX = 16_000

log = logging.getLogger("jobscout")

_ids = itertools.count(1)
_console: deque = deque(maxlen=CONSOLE_MAX)
_network: deque = deque(maxlen=NETWORK_MAX)
_lock = threading.Lock()

SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|app[_-]?key|app[_-]?id|secret|token|password|passwd|authorization|cookie|refresh|"
    r"x-rapidapi-key|authorization-key|client_secret|code_verifier|^key$)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---- redaction ---------------------------------------------------------------

_secret_cache = {"at": 0.0, "values": []}


def _secret_values() -> list[str]:
    """Current saved secret values (cached briefly), so they're scrubbed even where no key name gives them away."""
    if time.time() - _secret_cache["at"] > 30:
        vals = []
        try:
            from . import config
            for name in list(config.SECRET_FIELDS) + ["gmail_refresh_token"]:
                v = config.get_secret(name)
                if v and len(v) >= 6:
                    vals.append(v)
        except Exception:
            pass
        _secret_cache.update(at=time.time(), values=sorted(vals, key=len, reverse=True))
    return _secret_cache["values"]


def scrub(text: str) -> str:
    if not text:
        return text
    for v in _secret_values():
        text = text.replace(v, "***")
    text = re.sub(r"\bsk-ant-[A-Za-z0-9_\-]{8,}", "sk-ant-***", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", text)
    return text


def redact(obj):
    """Redact sensitive fields in parsed JSON-like data."""
    if isinstance(obj, dict):
        return {k: ("***" if SENSITIVE_KEY.search(str(k)) and v not in (None, "", False) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, str):
        return scrub(obj)
    return obj


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(str(url))
        q = [(k, "***" if SENSITIVE_KEY.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        return scrub(urlunsplit(parts._replace(query=urlencode(q, safe="*"))))
    except Exception:
        return scrub(str(url))


def redact_headers(headers) -> dict:
    return {k: ("***" if SENSITIVE_KEY.search(k) else scrub(str(v))) for k, v in dict(headers).items()}


def body_text(raw: bytes | str | None, content_type: str = "") -> str | None:
    """Readable, redacted, truncated body for display."""
    if raw is None or raw == b"" or raw == "":
        return None
    if isinstance(raw, bytes):
        if "json" not in content_type and "text" not in content_type and "form" not in content_type and content_type:
            return f"<{len(raw):,} bytes {content_type.split(';')[0]}>"
        raw = raw.decode("utf-8", errors="replace")
    try:
        text = json.dumps(redact(json.loads(raw)), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        text = scrub(raw)
    return text if len(text) <= BODY_MAX else text[:BODY_MAX] + f"\n… ({len(text) - BODY_MAX:,} more characters)"


# ---- console buffer ---------------------------------------------------------------

class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = scrub(record.getMessage())
            if record.exc_info:
                msg += "\n" + scrub(logging.Formatter().formatException(record.exc_info))
            add_console(record.levelname.lower(), msg, record.name)
        except Exception:
            pass


def add_console(level: str, message: str, source: str = "server") -> dict:
    entry = {"id": next(_ids), "at": _now(), "level": level if level in ("debug", "info", "warning", "error") else
             ("error" if level == "critical" else "info"), "source": source, "message": message[:20000]}
    with _lock:
        _console.append(entry)
    return entry


def add_network(entry: dict) -> dict:
    entry = {"id": next(_ids), "at": _now(), **entry}
    with _lock:
        _network.append(entry)
    lvl = logging.WARNING if (entry.get("status") or 0) >= 400 or entry.get("error") else logging.DEBUG
    log.log(lvl, "%s %s %s -> %s (%s ms)%s", entry.get("kind"), entry.get("method"), entry.get("url"),
            entry.get("status"), entry.get("ms"), f" [{entry['error']}]" if entry.get("error") else "")
    return entry


def console_after(after: int, limit: int = 1000) -> list[dict]:
    with _lock:
        return [e for e in _console if e["id"] > after][-limit:]


def network_after(after: int, limit: int = 500) -> list[dict]:
    with _lock:
        rows = [e for e in _network if e["id"] > after][-limit:]
    keep = ("id", "at", "kind", "method", "url", "status", "ms", "size", "error", "label")
    return [{k: e.get(k) for k in keep} for e in rows]


def network_entry(eid: int) -> dict | None:
    with _lock:
        return next((e for e in _network if e["id"] == eid), None)


def counts() -> dict:
    with _lock:
        return {
            "errors": sum(1 for e in _console if e["level"] == "error"),
            "warnings": sum(1 for e in _console if e["level"] == "warning"),
            "requests": len(_network),
            "failed": sum(1 for e in _network if (e.get("status") or 0) >= 400 or e.get("error")),
            "last_id": max([e["id"] for e in _console] + [e["id"] for e in _network] + [0]),
        }


def clear() -> None:
    with _lock:
        _console.clear()
        _network.clear()


# ---- outbound HTTP (httpx) -----------------------------------------------------------

def httpx_hooks(label: str) -> dict:
    """event_hooks for httpx.Client: records each outbound request/response in the network buffer."""
    def on_request(request):
        request.extensions["jobscout_t0"] = time.perf_counter()

    def on_response(response):
        req = response.request
        t0 = req.extensions.get("jobscout_t0") or time.perf_counter()
        try:
            response.read()
            body = response.content
        except Exception:
            body = None
        add_network({
            "kind": "outbound", "label": label, "method": req.method, "url": redact_url(str(req.url)),
            "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000),
            "size": len(body or b""),
            "request": {"headers": redact_headers(req.headers), "body": body_text(req.content or None, req.headers.get("content-type", ""))},
            "response": {"headers": redact_headers(response.headers), "body": body_text(body, response.headers.get("content-type", ""))},
        })

    return {"request": [on_request], "response": [on_response]}


def record_call(kind: str, label: str, method: str, url: str, t0: float, status: int | None,
                request: dict | None = None, response: dict | None = None, error: str | None = None) -> None:
    """For SDK calls without an httpx hook (Gmail API client, Anthropic SDK)."""
    add_network({"kind": kind, "label": label, "method": method, "url": redact_url(url), "status": status,
                 "ms": round((time.perf_counter() - t0) * 1000), "size": None, "error": scrub(error) if error else None,
                 "request": redact(request or {}), "response": redact(response or {})})


class _ScrubbingFormatter(logging.Formatter):
    def format(self, record):
        return scrub(super().format(record))


# ---- setup ----------------------------------------------------------------------------

_configured = False


def setup(level: str = "INFO") -> None:
    global _configured
    if _configured:
        set_level(level)
        return
    _configured = True
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / "jobscout.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(_ScrubbingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    ring = _RingHandler()
    for name in ("jobscout", "uvicorn.error", "py.warnings"):
        lg = logging.getLogger(name)
        lg.addHandler(fh)
        lg.addHandler(ring)
    log.propagate = False
    logging.captureWarnings(True)
    set_level(level)

    def _excepthook(exc_type, exc, tb):
        log.error("Unhandled exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = _excepthook
    threading.excepthook = lambda a: log.error("Unhandled exception in thread %s", a.thread.name if a.thread else "?",
                                              exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    log.info("Logging started (level %s); files in %s", level, LOG_DIR)


def set_level(level: str) -> None:
    log.setLevel(getattr(logging, str(level).upper(), logging.INFO))
