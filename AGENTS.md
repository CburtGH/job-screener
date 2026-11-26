# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Copilot, Cursor, etc.) working in this repository.
Read this first. Humans: see `README.md` for the user guide.

> **Keep this file true.** If your change makes anything below wrong or incomplete, update this file in the
> same commit. `python tools/check_agents_md.py` checks the mechanical parts (files, routes, settings,
> secrets, tables, ports). A git pre-commit hook and a GitHub Action run it too. Wording and rules are on you.

## What this is

**JobScout**: a personal, single-user job-search desktop app for Windows. It:

- pulls job postings from APIs,
- filters and scores them against the user's resume and preferences,
- uses Claude to tailor the resume, write cover letters, and triage and draft Gmail replies.

The user stays in control: they submit applications and click Send themselves.

Stack:
- **Python 3.12 + FastAPI**, served by uvicorn on `127.0.0.1` only.
- A **pywebview** desktop window (Edge WebView2).
- **SQLite** for data, **Windows Credential Manager** (via `keyring`) for secrets.
- A **vanilla HTML/CSS/JS** front end with no build step.

## Hard rules (do not break these)

1. **Staging first, production on approval.** Two checkouts of this repo exist on the user's machine:

   | Env | Folder | Branch | Port | Desktop shortcut |
   |---|---|---|---|---|
   | production | clone of `main`, e.g. `C:\Projects\job-screener` | `main` | 8765 | JobScout (blue icon) |
   | staging | git worktree, e.g. `C:\Projects\job-screener-staging` | `staging` | 8766 | JobScout (Staging) (orange) |

   - Remotes: `origin` = the public GitHub repository (**`main` only** — never push other branches or
     tags that aren't releases). Daily work lives on the local `staging` branch, backed by the
     maintainer's private remote; the branch's name/URL is not referenced here.
   - Make every change in the **staging** folder, then commit and push to the maintainer's private
     remote. Never push a `staging` branch to `origin` except as the short-lived head of the PR below.
   - Never edit, commit or push in the production folder, and never push to `main`.
   - The user tests in the staging app. Only after they explicitly approve: open a PR `staging` → `main`
     (`git push origin staging`, then `gh pr create --base main --head staging`), merge it, delete the
     `staging` branch on `origin` again (`git push origin --delete staging`), fast-forward `staging` to `main`,
     then in the production folder run `git pull` and `.venv\Scripts\python -m pip install -r requirements.txt`.
     Re-run `install_shortcut.ps1` if launch or shortcut details changed.
   - One approval covers one promotion.
 2. **Runs only while the window is open.** No background services, schedulers, polling loops or auto-start.
    Data is fetched once on startup (setting `refresh_on_startup`) and when the user clicks Refresh
    (`/api/refresh`). **Staging never refreshes on startup** (`env.IS_STAGING` skips it) — restarts during
    testing must not burn job-site API quota or Gmail calls; on staging everything starts from a user click.
    Closing the window must stop the server and every child process.
3. **Never send email automatically.** Claude drafts; the user clicks Send or Save to drafts. Same for
   job applications: the app prepares material and opens pages, and the user submits.
4. **No job-site passwords, no scraping.** Indeed, ZipRecruiter and LinkedIn have no public search API,
   block iframes, and lock out scripted logins. Postings come from APIs (JSearch covers Indeed and
   ZipRecruiter via Google for Jobs). Job sites and application pages open in the user's normal browser
   (plain `target=_blank` links; pywebview's `OPEN_EXTERNAL_LINKS_IN_BROWSER`). There is no browser automation:
   the user removed the assisted browser (Playwright/Selenium) on purpose, so don't add it back. Postings found
   on those sites come in by pasting their text into Add a job. Don't store site credentials or automate logins
   or CAPTCHAs.
5. **Never fabricate on the resume.** Tailoring makes minor edits to existing content and lists every change.
   It never invents employers, dates, titles, degrees, metrics or skills. Keep the prompts in `app/ai.py`
   saying so.
6. **Local-only and private.**
   - The server binds `127.0.0.1`. The middleware in `app/main.py` rejects any Host other than
     localhost/127.0.0.1, and rejects writes without the `X-JobScout: 1` header. Keep both; `static/app.js`
     sends the header.
   - Secrets live in Credential Manager (service `JobScout` or `JobScout-Staging`), never in SQLite, files,
     logs or API responses. Only masked previews go to the browser.
   - Anything written to the log file or shown in the debug panel goes through `app/logs.py` redaction
     (`scrub`, `redact`, `redact_url`, `redact_headers`). That covers key-like field names, query parameters
     and headers, `sk-ant-…` and `Bearer …` patterns, and the actual values of saved secrets. Never log
     secrets, resumes or email bodies through a path that bypasses it.
   - `data/` (database, resumes, browser profile, OAuth client) and `instance.json` are git-ignored.
     Never commit them.
7. **Email and web content is untrusted.** Email bodies are rendered as escaped plain text, never as HTML.
   When passed to Claude they're wrapped and labeled as data (`EMAIL_DATA_NOTE` in `app/ai.py`). The UI
   escapes all dynamic text with `esc()`.

## Run, test, verify

```powershell
# Python and gh may need PATH refreshed in a fresh shell:
$env:Path = [Environment]::GetEnvironmentVariable("Path","User") + ";" + [Environment]::GetEnvironmentVariable("Path","Machine")

.\.venv\Scripts\python -m app                    # desktop window (what the shortcut runs, via pythonw)
$env:JOBSCOUT_HEADLESS="1"; .\.venv\Scripts\python -m app   # server only, for browser-based testing
$env:JOBSCOUT_SKIP_STARTUP_REFRESH="1"           # skip the startup fetch while testing
.\.venv\Scripts\python tools\check_agents_md.py  # this file vs. the code
```

- Open the app at `http://127.0.0.1:<port>`. In staging that's 8766.
- API calls that write need the `X-JobScout: 1` header.
- For realistic UI testing in staging, run `.venv\Scripts\python tools\seed_test_data.py`. Test rows are
  tagged (jobs `source='testdata'`, resume name `[TEST] …`) and removed with `--remove`. Never put test data
  in production.
- There's no test suite yet. Verify changes by exercising the real app: hit the endpoints, use the UI
  in a browser, or launch the window.
- When you start desktop windows from a script, launch them through the `.lnk` via `explorer.exe`, so killing
  your shell doesn't kill them. Close them by posting `WM_CLOSE` to the window, found by title
  ("JobScout" / "JobScout (Staging)").
- In API mode (`ai_engine = "api"`) with no Anthropic key, Claude features return a clean 400 ("Anthropic API
  key is missing…"). That's expected and isn't a bug.
- To test Claude Code result handling without starting a real session (which uses the user's Claude plan), build
  a task with `main._cc_task(...)`, make a `claude_code.Session` by hand, write its `result.json`/`result.md`, and
  call `main._apply_result(session)`. Restore any rows it changed.
- The `jobscout-test` config in `.claude/launch.json` runs the server on port 8799 with no startup refresh. Some
  tools resolve `launch.json` from the production folder: check which folder and port actually started, and never
  run anything against production.
- Free sources (Remotive, Remote OK) are throttled to one fetch per 6 hours per environment (`last_fetch:*`
  keys in the `kv` table).

## Environments

`app/env.py` reads the git-ignored `instance.json` in the checkout root. `{"env": "staging", "production_path": ...}`
means staging; a missing file means production. It sets the port, window title, keyring service name and icon.
`install_shortcut.ps1 -Environment staging|production` writes `instance.json` and creates the shortcut.

`app/env_tools.py` copies production data into staging (read-only on production). It powers
`/api/env/copy-production` and the staging-only Settings card. Staging's Gmail Disconnect does not revoke
the Google token, because the token may be a copy of production's.

## Code map

| Path | Purpose |
|---|---|
| `app/__init__.py` | Package marker |
| `app/__main__.py` | Entry point: starts uvicorn in a thread, opens the pywebview window, sets the window icon, exits on close; logs to `data/jobscout.log` under pythonw |
| `app/env.py` | Environment (production/staging): port, app name, keyring service, icon |
| `app/env_tools.py` | Copy production data, keys and sign-ins into staging |
| `app/main.py` | FastAPI app: all HTTP routes, local-only middleware, startup hook |
| `app/db.py` | SQLite schema and helpers (`conn()`, `kv_get/kv_set`, `log`, `row_to_job`) |
| `app/config.py` | Preferences, app settings (stored in `kv`), secrets (keyring) |
| `app/sources.py` | Job source fetchers + `make_job()` normalizer + `SOURCES` registry |
| `app/matcher.py` | Hard filters (`filter_job`) and keyword score (`keyword_score`) |
| `app/monitor.py` | Pipeline `run_once()`: archive_stale (60-day rule) → fetch → filter → dedupe → keyword score → AI score; `refresh_on_startup()` |
| `app/ai.py` | All AI calls (Anthropic API + local llama.cpp server) and their output schemas |
| `app/local_model.py` | Built-in local model tiers: hardware scan + recommender, llama.cpp/GGUF installer, managed `llama-server` on 127.0.0.1:8899 |
| `app/gmail.py` | Gmail OAuth, sync, thread parsing, triage, send/draft, archive |
| `app/resume.py` | Resume CRUD, PDF/DOCX/TXT text extraction, markdown → DOCX / PDF (fpdf2, Windows Arial) / plain-text export |
| `app/logs.py` | Logging: rotating files in `data/logs/`, in-memory Console/Network buffers for the debug panel, redaction, httpx hooks |
| `app/claude_code.py` | Embedded Claude Code sessions: task folders, locked-down `claude` process in a PTY (pywinpty), output fan-out |
| `static/vendor/xterm/xterm.js`, `static/vendor/xterm/xterm.css`, `static/vendor/xterm/addon-fit.js` | xterm.js 5.5.0 + fit addon 0.10.0, vendored from npm (`@xterm/xterm`, `@xterm/addon-fit`) |
| `static/vendor/xterm/LICENSE` | xterm.js MIT license |
| `static/index.html` | Single-page UI markup (tabs: Jobs, Mail, Resume, Preferences, Settings, Activity) |
| `static/app.js` | All UI logic; `api()` wrapper adds the `X-JobScout` header |
| `static/styles.css` | Styles; light and dark via `prefers-color-scheme`; staging marker styles |
| `static/icon.png`, `static/icon.ico` | Production icon (blue); generated |
| `static/icon-staging.png`, `static/icon-staging.ico` | Staging icon (orange); generated |
| `tools/make_icon.py` | Generates both icons (pure Python, no dependencies) |
| `tools/check_agents_md.py` | Checks this file against the code |
| `tools/seed_test_data.py` | Staging only: adds fictional test data (1 resume, 10 jobs, one per status); `--remove` deletes it. Refuses to run in production |
| `tools/model_bench.py` | Scoring-engine bench: fixture jobs with planted skill gaps measure hallucinations, latency and score consistency across the built-in tiers and the remote model (writes `data/bench/*.json`); `--rejudge PASS` re-verdicts saved responses with the current judge, no model calls |
| `install_shortcut.ps1` | Creates venv if needed, installs requirements, writes `instance.json`, makes shortcuts |
| `requirements.txt` | Dependencies (unpinned) |
| `README.md` | User guide (public-facing: overview, install, license, disclaimer) |
| `LICENSE` | CC BY-NC 4.0 (non-commercial forks allowed) |
| `CLAUDE.md` | Points Claude Code at this file |
| `.githooks/pre-commit` | Runs the AGENTS.md check before each commit |
| `.github/workflows/agents-md.yml` | Runs the AGENTS.md check on pushes and PRs |
| `.github/workflows/release-exe.yml` | On `v*` tags: builds `JobScout.exe` + `JobScoutUninstaller.exe` (PyInstaller, windows-latest) and attaches both to the GitHub Release |
| `run_jobscout.py` | PyInstaller entry point (calls `app.__main__.main()`) |
| `jobscout.spec` | PyInstaller one-file build config (bundles `static/`, collect_all for webview/googleapiclient/keyring/winpty) |
| `run_uninstaller.py` | Entry for `JobScoutUninstaller.exe`: confirms, kills the app, deletes exe + `data/` + `instance.json` + this env's keyring secrets + `%LOCALAPPDATA%\JobScout\<env>` + desktop shortcut, then self-deletes (nothing survives) |
| `jobscout_uninstaller.spec` | PyInstaller config for the uninstaller (excludes the heavy app deps) |
| `.claude/launch.json` | Dev-server configs for Claude Code's preview browser (headless; `jobscout-test` = port 8799, no startup refresh) |

## Data flow

```
sources.SOURCES[...]  ->  matcher.filter_job  ->  dedupe (source+external_id, then title+company hash)
   -> matcher.keyword_score  ->  jobs table (status new | low_match)  ->  ai.score_job for every new visible unscored job
user: Tailor / Cover letter  ->  ai.tailor_resume / ai.write_cover_letter  ->  saved on the job row (master resume untouched)
gmail.sync  ->  email_threads table  ->  gmail._link_job (company name)  ->  ai.triage_email (category, summary, needs_reply)

Claude Code mode (ai_engine = "claude_code"): no AI calls at all. run_once and gmail.sync skip
scoring and triage; the user clicks "Score N new jobs" / "Sort N emails" (or any AI button) and a Claude Code
session does it -> result file -> main._apply_result -> monitor.save_score / gmail.save_triage / job row / editor

Local LLM mode (ai_engine = "local_builtin" or "local_external", the default): same flow as the API engine, but every
ai.py call goes to `config.active_llm()`: the managed built-in server, or the external llama.cpp server in
Settings (`local_llm_url`, `local_llm_model` - never touched by the built-in model, so both stay configured
and the engine dropdown A/B switches between them). No API key, no token cost.
```

Job statuses: `new`, `saved`, `applying`, `applied`, `interview`, `offer`, `rejected`, `dismissed`, `low_match`.

**Hidden jobs** stay in the database but leave the list until the user ticks "Show hidden".
`monitor.hidden_sql()` is the single definition (the list, the counts and the "Score new jobs" batch all use it):
`jobs.hidden = 1` (user clicked Hide), status `dismissed`, or a `new` job whose `ai_score` is below
`hide_below_score` (default 70), or, when the AI hasn't scored it, whose `keyword_score` is below
`hide_below_keyword_score` (default 30). Unhide sets `hidden = -1`, which exempts the job from both score rules. **Removed jobs** are deleted and recorded in `removed_jobs`;
`monitor._insert` skips any posting matching one (same source+external_id, or same dedupe_key from any source).
Pasting a job into Add a job deliberately bypasses that.

**Archived jobs** (60-day rule, `monitor.archive_stale()` at the start of every run): a job found 60+ days ago
whose status is not one of `saved/applying/applied/interview/offer` gets `archived_at` set — it leaves every
normal list and count but keeps its scores, letters and linked emails, and shows only when the user ticks
"Archived" (the list endpoint's `archived` status view, ordered by `archived_at DESC`, badge counts it down). Five days
after archiving, the next run deletes it for good and records it in `removed_jobs`, so it never comes back.

Current sources (`SOURCES` keys): `jsearch` (Indeed, ZipRecruiter, LinkedIn, Glassdoor via Google for Jobs;
key from OpenWeb Ninja, `api.openwebninja.com/jsearch/search-v2` with `x-api-key`; a RapidAPI key also works via
the old `jsearch.p.rapidapi.com/search`, and the channel that accepted the key is remembered in kv
`jsearch_channel`), `remotive` (free, remote only), `remoteok` (free, remote only), `himalayas` (free, no key,
remote only; its terms require applying via `applicationLink`, visible "via Himalayas" credit with a link back, and
not passing jobs on to other boards), `usajobs`
(key + email).

To **add a job source**: write `fetch_x(prefs) -> list[dict]` in `app/sources.py` using `make_job(...)`,
raise `SourceError` for config problems, register it in `SOURCES`, and mention it in README.

## Database (`data/jobscout.db`)

| Table | Holds |
|---|---|
| `kv` | JSON key/values: `preferences`, `app_settings`, `gmail_account`, `gmail_last_sync`, `last_fetch:<source>`, `jsearch_channel` |
| `resumes` | Markdown resumes; exactly one `is_master` |
| `jobs` | Postings plus scores, Claude summary/strengths/gaps, tailored resume, cover letter, notes, status, 60-day archive timestamp (`archived_at`) |
| `email_threads` | Gmail thread metadata, job link (`link_manual` = user-set), Claude triage fields |
| `removed_jobs` | Jobs the user removed (source, external_id, dedupe_key, title, company); never re-fetched |
| `activity` | Activity log (last 500 rows) |

**Locking:** `db.conn()` takes one process-wide lock (`db._lock`, an `RLock`). Never call anything that opens its
own connection (`_link_job`, `kv_get`/`kv_set`, `db.log`, `get_*` helpers, `config.get_*`) inside a
`with db.conn()` block. Do lookups first, then take the lock. A nested call once deadlocked the app (Gmail sync
froze every request that touched the database). The lock is re-entrant now as a backstop, but nesting still
holds a write transaction open across a second connection.

Schema changes: `db.SCHEMA` uses `CREATE TABLE IF NOT EXISTS`, and there's no migration tool. To add a column
to an existing table, add an idempotent `ALTER TABLE` in `db.init()` that checks `PRAGMA table_info` first.
Users have real data in production.

## HTTP API (`app/main.py`)

All under `/api`. Writes require the `X-JobScout: 1` header.

| Route | Purpose |
|---|---|
| `GET /api/env` | Environment name, app name, port, icon |
| `POST /api/env/copy-production` | Staging only: copy production data in |
| `GET /api/settings` | App settings, preferences, masked secrets, source readiness, `first_run` |
| `POST /api/settings/wizard-seen` | Suppress the first-run setup-wizard auto-open |
| `PUT /api/settings/app` | Save app settings |
| `PUT /api/settings/preferences` | Save search preferences |
| `PUT /api/settings/secrets/{name}` | Save or clear one secret |
| `GET /api/resumes` | List resumes |
| `GET /api/resumes/{rid}` | One resume |
| `POST /api/resumes/upload` | Upload PDF/DOCX/TXT/MD (optional Claude cleanup) |
| `POST /api/resumes` | Create from text |
| `PUT /api/resumes/{rid}` | Edit, rename, set master |
| `DELETE /api/resumes/{rid}` | Delete |
| `POST /api/resumes/{rid}/cleanup` | Claude markdown cleanup (returns text; doesn't save) |
| `GET /api/resumes/{rid}/docx` | Export DOCX |
| `GET /api/jobs` | List jobs (status filter incl. `archived`, search, sort, `show_hidden`) + counts, `hidden_count`, `archived_count`, `unscored` |
| `GET /api/jobs/{jid}` | One job |
| `PATCH /api/jobs/{jid}` | Edit status, notes, tailored resume, cover letter, `hidden` (true = Hide, false = sticky Unhide), … |
| `DELETE /api/jobs/{jid}` | Remove: delete the job, unlink its emails, add it to `removed_jobs` so it's never fetched again |
| `POST /api/jobs/{jid}/score` | Claude match score |
| `POST /api/ai/score-unscored` | Score-now catch-up: every new visible unscored job, off-thread (local uncapped, API capped; 409 if a run holds the pipeline; rejected for Claude Code) |
| `POST /api/jobs/{jid}/tailor` | Claude resume tailoring |
| `POST /api/jobs/{jid}/cover-letter` | Claude cover letter |
| `GET /api/jobs/{jid}/resume.{ext}` | Tailored (or master) resume download; `ext` = `pdf`, `txt` or `docx` |
| `GET /api/jobs/{jid}/cover-letter.{ext}` | Cover letter download; `ext` = `pdf`, `txt` or `docx` |
| `POST /api/jobs/import` | Paste a posting; Claude extracts fields |
| `GET /api/local-llm/status` | Local llama.cpp server: reachability, loaded model ids, configured URL/model (Settings card and setup banner) |
| `GET /api/local-model/status` | Built-in model wizard state: hardware scan, recommended tier, per-tier install/use state, install progress, managed server state |
| `POST /api/local-model/install` | Download runtime + GGUF tier (`{"tier": "light|medium|heavy|max"}`) in a background thread; becomes the default engine when ready |
| `POST /api/local-model/use` | Start the managed `llama-server` for an installed tier and point Settings' local-LLM engine at it |
| `POST /api/local-model/stop` | Stop the managed `llama-server` we launched (frees RAM; a server owned by the other JobScout window is left alone) |
| `GET /api/claude-code/status` | Whether Claude Code is installed, and its path |
| `POST /api/claude-code/sessions` | Start (or re-attach to) the Claude Code session for a task (`kind`: tailor, cover, reply, outreach, score, triage, import, cleanup); returns id + token |
| `GET /api/claude-code/sessions/{sid}` | Session alive? Result file modified time (needs `token`) |
| `POST /api/claude-code/sessions/{sid}/apply` | Parse the result file and save it (job fields, scores, email sorting, imported job) or return it (email draft, cleaned resume) |
| `POST /api/claude-code/sessions/{sid}/stop` | End the session (kills the process) |
| `GET /api/debug/summary` | Error/warning/request/failure counts for the debug panel's badges |
| `GET /api/debug/console` | Console entries after id `after` (server logs, activity, UI errors) |
| `GET /api/debug/network` | Network entries after id `after` (app requests + outbound calls), list fields only |
| `GET /api/debug/network/{eid}` | One request with redacted headers and bodies |
| `POST /api/debug/client-log` | UI errors/warnings, forwarded to the log file |
| `POST /api/debug/clear` | Clear the in-memory Console/Network buffers |
| `GET /api/debug/log-file` | Download `data/logs/jobscout.log` |

WebSocket `/api/claude-code/ws/{sid}?token=…` streams the terminal. Client sends JSON `{"t":"in","d":…}` /
`{"t":"resize","r":rows,"c":cols}`; server sends raw terminal text. WebSockets skip the HTTP middleware and CORS,
so the handler itself checks Host, Origin (must be localhost/127.0.0.1) and the per-session token. Keep all three.
| `POST /api/monitor/run` | Run the job pipeline once |
| `POST /api/refresh` | Refresh button: job pipeline + Gmail sync |
| `GET /api/monitor/status` | Last run result + activity log |
| `GET /api/gmail/status` | Connection state |
| `POST /api/gmail/client-secret` | Upload the user's Google OAuth client JSON (Desktop app type) |
| `POST /api/gmail/connect` | OAuth sign-in in the user's default browser (loopback flow) |
| `POST /api/gmail/disconnect` | Forget the token (and revoke it, in production) |
| `POST /api/gmail/sync` | Sync job-related threads, then Claude triage |
| `GET /api/gmail/threads` | List threads (views: needs_reply, unread, by category, by job…) |
| `GET /api/gmail/threads/{tid}` | Full thread (marks read) |
| `PUT /api/gmail/threads/{tid}/job` | Link or unlink a job |
| `POST /api/gmail/threads/{tid}/triage` | Re-run Claude triage |
| `POST /api/gmail/threads/{tid}/archive` | Archive in Gmail |
| `POST /api/gmail/threads/{tid}/done` | Clear "needs reply" |
| `POST /api/gmail/draft-ai` | Claude drafts a reply or outreach email (never sends) |
| `POST /api/gmail/send` | Send or save a draft (only on the user's click) |
| `GET /` | The UI |

## Settings

The Settings tab's subtabs are **Integrations / AI Settings / Profile** (`data-stab` values stay
`account`/`setup`/`app`); its header has a **Setup wizard** button (`#open-wizard`) that opens the 4-step
`#wizard-dialog` (`openWizard()` in app.js: About you / Job search / Resume upload / AI engine, pre-filled
from the current settings; "Finish" PUTs preferences + app settings + any typed keys). `boot()` also
auto-opens it once on a genuine first run: `GET /api/settings` returns `first_run` (no `wizard_seen` kv flag,
no saved secrets on the machine, no resumes, no jobs) and `openWizard()` POSTs `/api/settings/wizard-seen`
so it never auto-opens again.

**Preferences** (`config.DEFAULT_PREFERENCES`, the Preferences tab): `target_titles`, `title_must_include_any`,
`title_role_any`, `title_exclude`, `keywords_boost`, `keywords_exclude`, `remote_only`, `locations`, `country`,
`employment_types`, `min_salary`, `posted_within_days`, `min_keyword_score`, `ai_score_enabled`,
`ai_score_max_per_run`, `hide_below_score`, `hide_below_keyword_score`, `enabled_sources`.

**App settings** (`config.DEFAULT_APP_SETTINGS`, the Settings tab): `model`, `effort`, `refresh_on_startup`,
`applicant_name`, `applicant_email`, `applicant_phone`, `applicant_location`, `applicant_links`,
`cover_letter_tone`, `tailoring_rules`, `gmail_days`, `gmail_query`, `email_signature`,
`claude_code_enabled`, `claude_code_path`, `debug_panel`, `log_level`, `ai_engine`, `local_llm_url`,
`local_llm_model`, `builtin_model`, `local_llm_wait`. Two of them default per environment (`config.apply_env_defaults()`):
`refresh_on_startup` on in production / off in staging, `debug_panel` the other way; each instance resets them
once at startup and after "copy production data into staging" (`env_tools`), later user edits stick.

`ai_engine` ("AI Agent runs in"): `local_builtin` (default) = JobScout's managed model on this PC, `local_external`
calls your own llama.cpp (or any OpenAI-compatible) server — both score/sort
automatically — every run (refresh AND startup refresh) scores the whole backlog of new, visible, unscored jobs with
no cap (`monitor.unscored_jobs(None)`), no key, no token cost; `claude_code` runs every Claude feature in the Claude
Code panel; `api` uses the Anthropic API key (and also scores/sorts automatically, capped per run by
`ai_score_max_per_run`, 0 = never; the Claude Code batch button uses the same cap). `config.use_local_llm()`,
`config.use_claude_code()`, `config.can_auto_ai()` and `config.active_llm()` are the backend checks; `ccMode()` is
the UI's. A legacy saved `local` migrates in `get_app_settings()` (by whether the URL is `MANAGED_URL`).

`local_llm_url` + `local_llm_model`: the **External** engine's OpenAI-compatible base URL (ending in `/v1`) and model
id (`GET <url>/models`), empty by default — the built-in model never writes these, so both local setups persist for
A/B switching. A llama.cpp-style server answers one request at a time and may go away while it reloads; JobScout
waits (Settings "Network wait", up to 10 minutes per call), then reports a clean
400 — no data is lost, refresh/Retry picks the work back up.

`claude_code_enabled` (default on) allows sessions. Engine choice is global — there are no per-task AI buttons
anymore (the Claude.ai copy/paste dialog and the extra Claude Code buttons were removed): every AI button
follows Settings > `ai_engine`. The "Finish setup" banner asks for whatever the selected engine needs
(Claude Code installed, an API key, or a reachable local server).

New settings need: a default in `config.py`, a form field in `static/index.html` (the `name` attribute must
match the key; list fields go in `PREF_LISTS` in `static/app.js`), and a line here.

**Secrets** (`config.SECRET_FIELDS`, keyring): `anthropic_api_key`, `jsearch_api_key`, `usajobs_api_key`, `usajobs_email`. Internal: `gmail_refresh_token` (stored with
`config.store_secret`).

## Claude usage (`app/ai.py`, `app/claude_code.py`)

Three engines, picked by the `ai_engine` setting. Every feature works with all three (the local LLM uses the
same "API function" column as the API engine — `ai._call()` dispatches on the engine):

| Feature | API function | Claude Code task kind | Result file | Saved by |
|---|---|---|---|---|
| Match scoring | `score_job` | `score` (batch of `monitor.unscored_jobs()`, or one `job_id`) | `result.json` | `monitor.save_score` |
| Tailor resume | `tailor_resume` | `tailor` | `result.md` | `patch_job` |
| Cover letter | `write_cover_letter` | `cover` | `result.md` | `patch_job` |
| Email sorting | `triage_email` | `triage` (batch of `gmail.untriaged_ids()`, or one `thread_id`) | `result.json` | `gmail.save_triage` |
| Reply / outreach draft | `draft_reply` / `draft_outreach` | `reply` / `outreach` | `result.md` | returned to the composer |
| Job import (paste) | `parse_job_text` | `import` | `result.json` | `main._save_import` |
| Resume cleanup | `resume_to_markdown` | `cleanup` | `result.md` | returned to the editor (user saves) |

Both paths share one system prompt per feature (`SCORING_SYSTEM`, `TRIAGE_SYSTEM`, `IMPORT_SYSTEM`,
`CLEANUP_SYSTEM`, and the `*_prompt` builders). Change rules there, never in only one path. Claude Code tasks
are built in `ai.score_task` / `triage_task` / `import_task` / `cleanup_task` (TASK.md plus data files) and
`main._cc_task`; results are read by `main._apply_result`, which validates each item against the same Pydantic
schemas, ignores extra keys, skips items outside the task (wrong job/thread id), and reports per-item problems.

### Local LLM engine

- `ai._call_local()` posts to `<local_llm_url>/chat/completions` (OpenAI chat-completions dialect, httpx):
  the Pydantic schema goes in `response_format.json_schema` (llama.cpp builds a grammar, so replies are valid
  JSON by construction), `effort` maps to `reasoning_effort`, and `max_tokens` is 16000.
- Same prompts and schemas as the API engine; a lenient `parse_json_result()` retry covers stray fences.
- The workstation server is shared: one request at a time (the house chat included) and stopped entirely while
  an image/video job renders. `LOCAL_TIMEOUT` (10 min) waits instead of failing fast; connect timeout 10 s.
  Connection errors and non-200s become `AIError` (HTTP 400), so nothing is lost mid-run — the next refresh
  retries whatever stayed unscored/untriaged.
- `ai.local_status()` (`GET /api/local-llm/status`) is the health check shown in Settings and the setup banner.
- The endpoint may live behind a VPN (e.g. a Tailscale address, which CGNAT black-holes when the VPN is down):
  `local_llm_wait` seconds of retry (3 s apart) on transport errors and 5xx/408/429 before the clean 400; 0 (the
  default) fails fast. 4xx is a config error (wrong model/port) and never waits. `ai.local_status()` stays a
  quick 5 s check so Settings never blocks; it says "check your VPN" on connect errors or 4xx/3xx, and
  "busy / spinning up" on 5xx/408/429 (a fresh handover from the house chat returns 503 until the model is
   loaded). The Settings card has a "Check now" button (`checkLocalLlm()` in app.js).

### Built-in local model (`app/local_model.py`, Settings card)

- Four optional GGUF tiers served by a jobscout-managed llama.cpp server on `127.0.0.1:8899`
   (`MANAGED_URL`): light Qwen3-1.7B-Q8 (~1.7 GB), medium Qwen3-4B-Q4_K_M (~2.5 GB), heavy Qwen3-8B-Q4_K_M
   (~4.7 GB), max Inkling-2-Mini-UD-Q2_K_XL (26B MoE / 3.4B active, ~8.5 GB, alias `inkling-2-mini`, `-c 8192`,
   `--cpu-moe` so the experts stay in RAM and the GPU only carries dense layers).
  Nothing downloads until the user clicks Install. Downloads live in `%LOCALAPPDATA%\JobScout\models\`
  (shared by production and staging), never in the checkout.
- Hardware scan (`hardware()`, cached): RAM via kernel32, GPUs via `Win32_VideoController` + `nvidia-smi`
  (name/vRAM/vendor, NVIDIA VRAM corrected to nvidia-smi's exact value), free disk. `recommend()` walks
  max→light and returns the first tier whose `min_vram` OR `min_ram` fits with 2 GB disk headroom; the UI
  always lets the user override (confirm dialog).
- Vendor-neutral runtime: NVIDIA → CUDA build (+ separate cudart zip, extracted into the same folder),
  AMD/Intel/Arc → Vulkan build, no GPU → CPU build; asset chosen from the newest llama.cpp GitHub release.
  The cached runtime is refreshed when a tier requires a newer build (tier `min_build` vs `runtime.json` tag;
  Inkling needs llama.cpp >= b12600).
- `start_install()` runs in a daemon thread and streams progress into the `_install` dict (UI polls
  `/api/local-model/status`). When done, `start_server(..., apply_engine=True)` launches `llama-server`
  (`-ngl 99 -c 8192 --jinja`), waits for `/v1/models`, then writes `ai_engine=local_builtin`,
  `builtin_model=<served id>` via `config.set_app_settings()` (the external `local_llm_url`/`local_llm_model`
  fields are left alone). `config.active_llm()` resolves URL+model per engine.
- Selection persists in `kv['local_model']` (`{tier, model_id, installed_at}`). Lifespan autostarts the
  server when the engine is `local_builtin`; `on_settings_change()` stops it when the engine moves
  off it; lifespan shutdown always stops it. A server owned by the other JobScout window is adopted
  read-only (`adopted`) and never killed.
- UI: "Built-in local model" card in Settings (`lm-*` ids in index.html, `renderLocalModel()` +
  `lmPollUntil()` in app.js): hardware line incl. GPU vendor/backend, three tier tiles with Recommended /
  installed / running badges, per-tier Install/Use buttons with override confirmation, progress bar, Stop.

### API engine

- Official `anthropic` Python SDK. Default model `claude-opus-5-5`; the user can choose Sonnet or Haiku 5.5 in Settings.
- One helper, `_call()`, handles every request:
  - `client.beta.messages.create` with `output_config.format` (JSON schema from strict Pydantic models), `effort`,
    and server-side refusal fallbacks (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`).
  - Checks `stop_reason` (`refusal`, `max_tokens`) before parsing.
  - Maps SDK errors to `AIError`, which routes turn into HTTP 400.
- The master resume is the last system block with `cache_control`, so batch scoring reuses the cache.
- Each writing task has one prompt builder (`tailor_prompt`, `cover_prompt`, `reply_prompt`,
  `outreach_prompt`) that returns `(system_parts, user)`. The API call and Claude Code's `chat_prompt()`
  (via `_task_prompt`) both use it. Change the rules in the builder, never in only one path.
- `parse_chat_reply()` turns a plain-text answer (a Claude Code `result.md`) into fields. It must be lenient:
  preambles, code fences, bold headings and a missing code block all happen. The app must never try to use
  consumer Claude credentials.
### Claude Code engine

- **Embedded Claude Code panel** (`app/claude_code.py`) is a real, interactive Claude Code session the user drives
  on their own Claude plan.
  - Rules:
    - Only start it on a user click. Never run Claude Code unattended (no `-p` jobs, nothing on startup or
      refresh). In Claude Code mode, scoring and email sorting wait for the user's "Score" / "Sort" click and run
      as one visible batch session; refresh only fetches.
    - Keep it locked down: `--restricted --tools Read,Edit,Write,Glob,Grep --strict-mcp-config --no-chrome
      --permission-mode acceptEdits`. Postings and emails are untrusted, so no command, code or web tools.
      If you change flags, re-check them against `claude --help`.
    - Task folders live outside the repo, in `%LOCALAPPDATA%\JobScout\<env>\claude-tasks\`, so this repo's
      AGENTS.md/CLAUDE.md never load into the user's session.
    - Every session process must die when the window closes (`claude_code.stop_all()` runs in the lifespan
      shutdown and again in `app/__main__.py`).
  - How it works:
    - Writing tasks: `TASK.md` is `chat_prompt()` + `task_markdown()` (save to `result.md`).
    - Structured tasks: `TASK.md` from `ai._json_task()`, data in files next to it (`jobs/<id>.md`,
      `emails/<thread_id>.md`, `resume.md`, `page.txt`, `extracted.txt`). Batch folders are wiped when a new
      session starts (`fresh=True`) so old data and results never mix in.
    - The UI polls the session, and when the result file changes it calls `/apply` (`_apply_result`). Claude may
      rewrite the file; each rewrite is applied again, so saving must be idempotent.
    - Claude Code is installed at `%USERPROFILE%\.local\bin\claude.exe` (not on PATH); `find_claude()`
      checks the `claude_code_path` setting, then PATH, then that location.
- Thinking can't be disabled on Opus 5.5. Control cost with `effort` (scoring and triage use `low`).
- Before changing model IDs or API parameters, check the current Claude API docs. Model IDs carry no date suffix.

## Gmail (`app/gmail.py`)

- Gmail API with scope `gmail.modify`. Uses the user's own OAuth client (`data/google_client_secret.json`,
  Desktop app type) and the `InstalledAppFlow.run_local_server` loopback flow in the system browser. Google
  blocks OAuth inside embedded webviews.
- Only the refresh token is stored (keyring).
- The client isn't thread-safe: hold `gmail._lock` around service calls.
- Every Gmail HTTP call has a timeout (`GMAIL_TIMEOUT`, 60 s, via `httplib2.Http(timeout=…)`); the library's
  default waits forever. Timeouts and connection errors become `GmailError`.
- Sync re-triages a thread only when its message count changes, not on label-only changes.

## Logging and the debug panel

- `app/logs.py` is set up in the lifespan (`logs.setup(log_level)`). Loggers named `jobscout` and
  `jobscout.*`, plus `uvicorn.error`, write to `data/logs/jobscout.log` (rotating, 2 MB × 5) and to an
  in-memory Console buffer (3,000 entries). `db.log()` (the user-facing Activity log) is mirrored there.
- The Network buffer (600 entries) records:
  - every app request, via the `record_requests` middleware in `app/main.py`. It's registered after
    `local_only`, so it's outermost and sees rejected requests too. It skips `/api/debug/*`, captures JSON
    bodies, and blanks the secrets endpoint's body.
  - every outbound call: job sources pass `event_hooks=logs.httpx_hooks(label)` to their `httpx.Client`;
    `gmail._call` and `ai._call` call `logs.record_call(...)`.
  - New outbound HTTP code must do the same so it shows up.
- Buffers are memory-only and cleared when the app closes; the log file persists.
- The panel (`#debug-dock`) polls `/api/debug/*` every 1.5 s while open and every 8 s while collapsed (badges
  only). Its open state, height and tab are kept in `localStorage`; Ctrl+` toggles it; `debug_panel` hides it.
- UI errors are captured by wrapping `console.error`/`console.warn` and listening for `error` and
  `unhandledrejection`, then forwarded to `/api/debug/client-log`.

## Front end

- No framework or build step. `static/app.js` uses `$`/`$$` helpers, a global state object `S`, `api()` for
  fetch, `busy()` for button spinners, `toast()` for messages, and `md()` for markdown preview.
- Escape every dynamic value with `esc()`.
- Downloads use `location.href` (pywebview has `ALLOW_DOWNLOADS` on).
- The Claude Code panel (`#cc-dialog`) blocks the dialog's `cancel` event: Esc must reach Claude Code (it
  interrupts). xterm.js loads from `static/vendor/` (no CDN), so it works offline.
- Copy to clipboard with `copyText()`. It falls back to `execCommand("copy")` because the async Clipboard
  API is often blocked in embedded browsers. Always give a manual fallback if both fail.
- `/` and `/static/*` are served with `Cache-Control: no-cache` (middleware in `app/main.py`). The webview
  keeps a persistent cache, so without this an updated app would run stale JS/CSS.
- Don't leave a button disabled with no explanation. Disabled buttons use `cursor: not-allowed`; only
  `busy()` (which sets `aria-busy`) shows the progress cursor. If an action has a prerequisite, keep the button
  clickable and tell the user what to do first.
- External links open in the system browser (`OPEN_EXTERNAL_LINKS_IN_BROWSER`).

## Platform notes

- Windows 11 with Windows PowerShell 5.1: no `&&`, so chain with `;` and check `$?`.
- `.venv\Scripts\pythonw.exe` is a launcher that spawns the real interpreter as a child process. Window and PID
  lookups must account for that.
- `gh` is authenticated; `gh auth setup-git` provides git credentials. The commit identity is set in the repo
  config. Don't change it.
- Release exes: the frozen `JobScout.exe` creates a `JobScout.lnk` desktop shortcut on first launch (skipped in
  staging/headless). `JobScoutUninstaller.exe` sits next to the exe and removes everything after one confirmation:
  the exe, `data/`, `instance.json`, this environment's keyring secrets, `%LOCALAPPDATA%\JobScout\<env>`, the
  desktop shortcut, and itself. No data may survive an uninstall.
