"""Checks that AGENTS.md still matches the code. Run before committing (the pre-commit hook and CI do this):

    .venv\\Scripts\\python tools\\check_agents_md.py

It reads source files without importing them, so it needs no dependencies. It checks:
  - every tracked file is listed in AGENTS.md, and every repo path AGENTS.md names exists
  - every HTTP route is documented, and no documented route is gone
  - every preference, app setting, secret, DB table and job source is mentioned
  - the ports in AGENTS.md match app/env.py
It can't check prose and rules; keep those accurate yourself.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "AGENTS.md"

# Tracked files that don't need a line in the code map.
NOT_REQUIRED = {".gitignore", "AGENTS.md"}
# Path prefixes in AGENTS.md that are runtime/user files, not repo files.
RUNTIME_PREFIXES = ("data/", ".venv")


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        files = out.split()
    except (OSError, subprocess.CalledProcessError):
        files = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
                 if p.is_file() and not any(part in (".git", ".venv", "data", "__pycache__") for part in p.parts)]
    # Include new, not-yet-tracked files that are staged, so the hook catches them.
    try:
        staged = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=A"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.split()
        files += staged
    except (OSError, subprocess.CalledProcessError):
        pass
    return sorted({f for f in files if (ROOT / f).exists()})


def dict_keys(path: Path, name: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if isinstance(node.value, ast.Dict):
                return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise SystemExit(f"check_agents_md: couldn't find {name} in {path}")


def main() -> int:
    if not DOC.exists():
        print("AGENTS.md is missing.")
        return 1
    doc = DOC.read_text(encoding="utf-8")
    backticked = set(re.findall(r"`([^`\n]+)`", doc))
    problems: list[str] = []

    def mentioned(term: str) -> bool:
        return term in backticked or f"`{term}`" in doc

    # 1. Files: every tracked file is in the doc; every repo path in the doc exists.
    for f in tracked_files():
        if f not in NOT_REQUIRED and not mentioned(f):
            problems.append(f"File `{f}` isn't listed in AGENTS.md (add it to the Code map).")
    for tok in backticked:
        for part in re.split(r",\s*", tok):
            if part.startswith(RUNTIME_PREFIXES) or " " in part:
                continue
            if re.match(r"^(app|static|tools|\.github|\.githooks|\.claude)/[\w./-]+$", part) or \
               part in ("README.md", "CLAUDE.md", "requirements.txt", "install_shortcut.ps1"):
                if not (ROOT / part).exists():
                    problems.append(f"AGENTS.md names `{part}`, which doesn't exist.")

    # 2. Routes, both directions.
    main_py = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    code_routes = {f"{m.upper()} {p}" for m, p in re.findall(r'^@app\.(get|post|put|patch|delete)\("([^"]+)"', main_py, re.M)}
    doc_routes = {f"{m} {p}" for m, p in re.findall(r"`(GET|POST|PUT|PATCH|DELETE) (/[^`\s]*)`", doc)}
    for r in sorted(code_routes - doc_routes):
        problems.append(f"Route `{r}` isn't documented in AGENTS.md (HTTP API table).")
    for r in sorted(doc_routes - code_routes):
        problems.append(f"AGENTS.md documents route `{r}`, which no longer exists in app/main.py.")

    # 3. Settings, secrets, sources.
    cfg = ROOT / "app" / "config.py"
    for name in ("DEFAULT_PREFERENCES", "DEFAULT_APP_SETTINGS", "SECRET_FIELDS"):
        for key in dict_keys(cfg, name):
            if not mentioned(key):
                problems.append(f"{name} key `{key}` isn't mentioned in AGENTS.md (Settings section).")
    for key in dict_keys(ROOT / "app" / "sources.py", "SOURCES"):
        if not mentioned(key):
            problems.append(f"Job source `{key}` isn't mentioned in AGENTS.md.")

    # 4. Database tables.
    db_py = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
    for table in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db_py):
        if not mentioned(table):
            problems.append(f"DB table `{table}` isn't documented in AGENTS.md (Database section).")

    # 5. Ports.
    env_py = (ROOT / "app" / "env.py").read_text(encoding="utf-8")
    m = re.search(r"(\d{4,5}) if IS_STAGING else (\d{4,5})", env_py)
    if m:
        staging, prod = m.groups()
        if not re.search(rf"\|\s*`main`\s*\|\s*{prod}\s*\|", doc):
            problems.append(f"AGENTS.md environment table should show production port {prod}.")
        if not re.search(rf"\|\s*`staging`\s*\|\s*{staging}\s*\|", doc):
            problems.append(f"AGENTS.md environment table should show staging port {staging}.")
    else:
        problems.append("Couldn't read ports from app/env.py; update tools/check_agents_md.py.")

    if problems:
        print("AGENTS.md is out of date with the code:\n")
        for p in problems:
            print(f"  - {p}")
        print("\nUpdate AGENTS.md in the same commit (and its prose, if behavior changed).")
        return 1
    print(f"AGENTS.md OK ({len(code_routes)} routes, files, settings, tables and ports match).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
