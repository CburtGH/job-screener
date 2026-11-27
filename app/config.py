"""Preferences (stored in SQLite) and secrets (stored in Windows Credential Manager via keyring)."""
from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from . import db

from .env import IS_STAGING, KEYRING_SERVICE  # "JobScout" (production) or "JobScout-Staging"

# Every secret the app knows about. Values never go into SQLite or back to the browser in full.
SECRET_FIELDS = {
    "anthropic_api_key": "Anthropic API key (Claude) - only needed if Settings > App > \"AI Agent runs in\" is set to the API",
    "jsearch_api_key": "JSearch API key (OpenWeb Ninja; a RapidAPI JSearch key also works) - pulls Indeed, ZipRecruiter, LinkedIn, Glassdoor postings via Google for Jobs",
    "usajobs_api_key": "USAJOBS API key (optional)",
    "usajobs_email": "Email registered with USAJOBS (required with the USAJOBS key)",
}

DEFAULT_PREFERENCES = {
    "target_titles": [
        "Junior Software Engineer",
        "Software Engineer I",
        "Junior Engineer",
        "Associate Software Engineer",
        "Senior Production Support",
        "Production Support Engineer",
        "Application Support Engineer",
    ],
    # A job title must contain at least one of these (case-insensitive) to pass the title filter.
    # Leave empty to rely only on target_titles.
    "title_must_include_any": [
        "junior", "jr", "software engineer i", "engineer 1", "engineer i", "associate",
        "entry", "production support", "application support", "support engineer",
    ],
    # ...and also at least one of these role words, so "Junior Market Analyst" doesn't slip through.
    "title_role_any": [
        "engineer", "developer", "programmer", "software", "support", "sre", "reliability", "devops", "swe",
    ],
    "title_exclude": ["staff", "principal", "director", "manager", "lead", "architect", "senior software", "sr. software", "sr software"],
    "keywords_boost": [],          # skills you want to see (adds to the keyword score)
    "keywords_exclude": ["clearance required", "ts/sci", "security clearance"],
    "remote_only": True,
    "locations": ["United States"],  # used for non-remote searches and by APIs that require a location
    "country": "us",
    "employment_types": ["FULLTIME"],  # FULLTIME, PARTTIME, CONTRACTOR, INTERN
    "min_salary": 0,
    "posted_within_days": 7,
    "min_keyword_score": 15,        # jobs below this are stored with status 'low_match'
    "ai_score_enabled": True,
    "ai_score_max_per_run": 15,     # cap per run for the Anthropic API engine (the local LLM scores every job)
    "enabled_sources": ["jsearch", "remotive", "remoteok", "himalayas"],
}

DEFAULT_APP_SETTINGS = {
    # Who does the AI work: "local_builtin" = JobScout's built-in model on this PC (managed llama.cpp);
    # "local_external" = your own OpenAI-compatible llama.cpp server (e.g. the home workstation);
    # "claude_code" = the built-in Claude Code panel on your Claude plan (each task starts when you click);
    # "api" = the Anthropic API key. Both local engines: no key, no token cost; scoring and email
    # sorting run automatically.
    "ai_engine": "local_builtin",
    "model": "claude-opus-5-5",
    "effort": "medium",
    # "local_external" target: OpenAI-compatible base URL with /v1, and the model id shown by GET <url>/models.
    # These two fields are ONLY the external server - the built-in model never overwrites them, so both
    # setups stay configured and the engine dropdown flips between them for A/B testing.
    "local_llm_url": "",
    "local_llm_model": "",
    "builtin_model": "",            # model id of the built-in tier, saved when you pick one (managed server)
    "local_llm_wait": 0,            # seconds to keep retrying unreachable/5xx local LLM calls (VPN still coming
                                    # up, server reloading a model); 0 = fail fast
    # Startup auto-refresh burns search/Gmail quota, so staging never starts it itself (see AGENTS.md);
    # debug_panel is the other way round: handy while testing, hidden in production.
    "refresh_on_startup": not IS_STAGING,
    "applicant_name": "",
    "applicant_email": "",
    "applicant_phone": "",
    "applicant_location": "",
    "applicant_links": "",          # LinkedIn / GitHub / portfolio, one per line
    "cover_letter_tone": "warm, concise, confident; no clichés; under 300 words",
    "tailoring_rules": (
        "Make minor adjustments only. Reorder and reword existing bullets to mirror the posting's language. "
        "Surface relevant skills already on the resume. Never invent employers, titles, dates, degrees, "
        "certifications, metrics or skills that are not on the master resume."
    ),
    "gmail_days": 60,               # how far back Gmail sync looks
    "gmail_query": "",              # Gmail search used to find job mail; empty = built-in default
    "email_signature": "",
    "claude_code_enabled": True,    # integrated Claude Code panel (your Claude plan); off = writing needs the API key
    "debug_panel": IS_STAGING,      # show the Console/Network debug panel at the bottom of the window
    "log_level": "INFO",            # DEBUG also records every outbound call in the log file
    "claude_code_path": "",         # path to claude.exe for the embedded Claude Code panel; empty = auto-detect
    # Accessibility: these sit ON TOP of the OS (Windows text size, Narrator/NVDA, system high contrast and
    # reduced motion all work by themselves via forced-colors / prefers-* media queries). The app-level
    # switches here are for users who want more without touching the whole system.
    "a11y_text_size": "normal",     # normal | large | largest (zooms the whole UI)
    "a11y_high_contrast": False,    # stronger colours and borders regardless of the OS theme
    "a11y_reduce_motion": False,    # kill animations/transitions even when the OS allows them
}


def get_preferences() -> dict:
    saved = db.kv_get("preferences", {})
    prefs = {**DEFAULT_PREFERENCES, **saved}
    # Adzuna was removed (its "free" tier is a 25-call trial); Himalayas replaces it in saved source lists.
    src = list(prefs.get("enabled_sources") or [])
    if "adzuna" in src:
        prefs["enabled_sources"] = [s for s in src if s != "adzuna"] + ([] if "himalayas" in src else ["himalayas"])
    return prefs


def set_preferences(prefs: dict) -> dict:
    merged = {**get_preferences(), **{k: v for k, v in prefs.items() if k in DEFAULT_PREFERENCES}}
    db.kv_set("preferences", merged)
    return merged


def get_app_settings() -> dict:
    saved = db.kv_get("app_settings", {})
    s = {**DEFAULT_APP_SETTINGS, **saved}
    if s.get("ai_engine") == "local":  # pre-split value: route by the URL it pointed at
        from .local_model import MANAGED_URL
        s["ai_engine"] = ("local_builtin" if (s.get("local_llm_url") or "").rstrip("/") == MANAGED_URL
                          else "local_external")
    return s


def active_llm(s: dict | None = None) -> tuple[str, str]:
    """URL + model id of the local server the engine actually selects (never touches the other's config)."""
    from . import local_model
    s = s or get_app_settings()
    if s.get("ai_engine") == "local_builtin":
        sel = local_model._kv()
        model = (local_model.model_id() if local_model.running() else None) or s.get("builtin_model") \
            or (f"qwen3-{sel['tier']}" if sel.get("tier") else "")
        return local_model.MANAGED_URL, model
    return (s.get("local_llm_url") or "").rstrip("/"), (s.get("local_llm_model") or "").strip()


def apply_env_defaults(force: bool = False) -> None:
    """reset_env_defaults: the two environment-flipped settings (refresh_on_startup off / debug_panel on in
    staging, the reverse in production) can be stale in an already-saved settings dict (e.g. copied from the
    other environment), so each environment resets them once after install; later user edits stick."""
    flag = "env_defaults_v1"
    if db.kv_get(flag) and not force:
        return
    saved = db.kv_get("app_settings", {}) or {}
    changed = saved.get("refresh_on_startup") != DEFAULT_APP_SETTINGS["refresh_on_startup"] or \
        saved.get("debug_panel") != DEFAULT_APP_SETTINGS["debug_panel"]
    saved.update({"refresh_on_startup": DEFAULT_APP_SETTINGS["refresh_on_startup"],
                  "debug_panel": DEFAULT_APP_SETTINGS["debug_panel"]})
    db.kv_set("app_settings", saved)
    db.kv_set(flag, True)
    if changed:
        db.log(f"reset_env_defaults: refresh_on_startup={DEFAULT_APP_SETTINGS['refresh_on_startup']}, "
               f"debug_panel={DEFAULT_APP_SETTINGS['debug_panel']} ({'staging' if IS_STAGING else 'production'})")


def use_claude_code() -> bool:
    """True when Claude features run in the Claude Code panel instead of the API or the local LLM."""
    s = get_app_settings()
    return s.get("ai_engine") == "claude_code" and bool(s.get("claude_code_enabled", True))


def use_local_llm() -> bool:
    """True when AI features call an OpenAI-compatible server (built-in or external) instead of Claude."""
    return str(get_app_settings().get("ai_engine", "")).startswith("local")


def can_auto_ai() -> bool:
    """True when batch scoring (monitor) and email triage (gmail) may run unattended on refresh/sync:
    always for the local LLM (no cost), only with an API key for the Anthropic API, never for Claude Code."""
    engine = str(get_app_settings().get("ai_engine", ""))
    if engine.startswith("local"):
        return True
    return engine == "api" and bool(get_secret("anthropic_api_key"))


def set_app_settings(values: dict) -> dict:
    merged = {**get_app_settings(), **{k: v for k, v in values.items() if k in DEFAULT_APP_SETTINGS}}
    db.kv_set("app_settings", merged)
    return merged


# ---- secrets -------------------------------------------------------------

def get_secret(name: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, name) or None
    except KeyringError:
        return None


def set_secret(name: str, value: str | None) -> None:
    if name not in SECRET_FIELDS:
        raise KeyError(name)
    if value:
        keyring.set_password(KEYRING_SERVICE, name, value.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, name)
        except PasswordDeleteError:
            pass


def store_secret(name: str, value: str | None) -> None:
    """Internal secrets (e.g. the Gmail refresh token) that aren't shown in the API keys list."""
    if value:
        keyring.set_password(KEYRING_SERVICE, name, value)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, name)
        except PasswordDeleteError:
            pass


def secrets_status() -> list[dict]:
    out = []
    for name, label in SECRET_FIELDS.items():
        v = get_secret(name)
        out.append({
            "name": name,
            "label": label,
            "is_set": bool(v),
            "preview": (v[:4] + "…" + v[-4:]) if v and len(v) > 12 else ("set" if v else ""),
        })
    return out
