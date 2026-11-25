# JobScout

A personal job-search assistant that runs entirely on your own Windows PC. It watches public
job-board APIs for postings that fit your resume and preferences, scores them with AI, and — for the
ones you pick — makes small, honest resume adjustments, writes cover letters and drafts replies to
recruiter email. You review everything and submit applications yourself. JobScout never sends email,
never submits applications, and never runs anything in the background: it does work when the window
is open and you click a button.

## How it works

1. **Find** — On demand (never on a timer), JobScout pulls postings from public APIs:
   JSearch (Indeed & ZipRecruiter via Google for Jobs), USAJOBS, Remotive, Remote OK and
   Himalayas, plus jobs you paste in manually.
2. **Filter** — Your title/keyword/location filters cut the list down; keyword ranking boosts
   postings that match your skills.
3. **Score** — The configured AI engine reads each posting against your master resume and returns a
   score with quoted evidence and honest gaps. It is explicitly forbidden from inventing anything:
   tailoring only re-arranges and re-words what is already on your resume, and lists every change it
   makes.
4. **Act** — For jobs you save: tailored resume versions (docx/PDF via your own layout), cover
   letters in your voice, application-page links that open in your normal browser, and Gmail
   integration that triages recruiter replies and drafts responses — which you send yourself.

The AI work runs wherever you choose: a **built-in local model** (a small GGUF model downloaded and
served on your PC by a bundled llama.cpp runtime — no key, no token cost, works offline), your own
**OpenAI-compatible llama.cpp server** (e.g. on a home workstation), the **Claude Code** panel on
your Claude plan, or the **Anthropic API**.

Everything stays on your machine: the server binds to `127.0.0.1` only, your database lives in
`data/`, and API keys live in Windows Credential Manager — never in files or the database. The only
outbound traffic is to the job-board APIs and AI/Gmail services you enable.

## Technologies & languages

- **Python 3.12** — the whole backend: **FastAPI** served by **uvicorn**, **Pydantic** for strict
  AI output schemas, **SQLite** (stdlib `sqlite3`) for storage, **httpx** for API calls.
- **Vanilla JavaScript + CSS** single-page front end (no framework, no build step) rendered in a
  **pywebview** desktop window.
- **llama.cpp** managed runtime with **GGUF** models (quantized Qwen3 tiers) exposed as an
  OpenAI-compatible endpoint with JSON-schema constrained decoding.
- **Anthropic SDK** / Claude API, **Claude Code**, and any **OpenAI-compatible** server as AI
  engines; **Google Gmail API** (OAuth desktop client you own) for email triage.
- Document handling: **python-docx**, **pypdf**, **fpdf2**; resume cleanup and cover letters.
- **Windows Credential Manager** (via `keyring`) for secrets; PowerShell scripts for the desktop
  shortcut and built-in model install; log scrubbing/redaction built in.

## Installation (from GitHub)

Requirements: Windows 10/11, [Python 3.12+](https://www.python.org/downloads/) (check *Add to PATH*),
and [Git](https://git-scm.com/).

```powershell
git clone https://github.com/CburtGH/job-screener.git
cd job-screener
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m app
```

That opens the JobScout window. To get a Desktop/Start-menu shortcut instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_shortcut.ps1
```

First run: open **Settings** (or the **Setup wizard** button) and

1. pick an **AI engine** — the built-in local model downloads its runtime and model on first use,
   or point the External engine at your own llama.cpp server, or add an Anthropic API key;
2. paste your job-source keys (JSearch needs a free key; USAJOBS optional; Remotive/Remote OK/
   Himalayas need nothing);
3. upload your resume (it is parsed to markdown so you can review exactly what the AI sees).

Gmail is optional: bring your own free OAuth desktop client (the Settings help walks through the
Google Cloud steps). The app opens locally, stores its database in `data/`, and needs no account.

## License

JobScout is licensed under
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/): you are free to fork, share and
adapt it — with attribution — for **non-commercial** use only. Commercial use requires prior
permission from the author. See [`LICENSE`](LICENSE).

## Disclaimer

This software is provided **"as is", without warranty of any kind**, express or implied, including
but not limited to the warranties of merchantability, fitness for a particular purpose and
noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages
or other liability arising from, out of or in connection with the software or the use of it.

AI systems make mistakes: scores, drafts and tailored documents are suggestions that you must review
before use. Respect the terms of service of any job board or API you connect to; JobScout only uses
official public APIs and stores no site credentials.
