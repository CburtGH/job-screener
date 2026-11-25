"""Staging helpers: copy production's data into staging so you can test with real jobs, resumes and keys.
Production is only read, never modified."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import keyring

from . import config, db, env

SECRET_NAMES = list(config.SECRET_FIELDS) + ["gmail_refresh_token"]


def production_root() -> Path:
    cfg = json.loads((env.ROOT / "instance.json").read_text()) if (env.ROOT / "instance.json").exists() else {}
    return Path(cfg.get("production_path") or (env.ROOT.parent / "job-screener"))


def copy_production_data() -> dict:
    if not env.IS_STAGING:
        raise RuntimeError("Only staging can copy production data.")
    prod = production_root()
    prod_data = prod / "data"
    if not (prod_data / "jobscout.db").exists():
        raise RuntimeError(f"No production database found at {prod_data}.")
    copied = []

    # Database: SQLite's backup API takes a consistent snapshot even if production is open.
    src = sqlite3.connect(f"file:{prod_data / 'jobscout.db'}?mode=ro", uri=True)
    try:
        with db._lock:
            dst = sqlite3.connect(db.DB_PATH)
            try:
                src.backup(dst)
            finally:
                dst.close()
    finally:
        src.close()
    copied.append("database (jobs, resumes, preferences, settings, email threads)")
    config.apply_env_defaults(force=True)  # the copy brings production's startup/debug values; flip them for staging

    for name in ("uploads",):
        if (prod_data / name).exists():
            shutil.copytree(prod_data / name, db.DATA_DIR / name, dirs_exist_ok=True)
            copied.append(name)
    if (prod_data / "google_client_secret.json").exists():
        shutil.copy2(prod_data / "google_client_secret.json", db.DATA_DIR / "google_client_secret.json")
        copied.append("Google OAuth client file")

    n = 0
    for name in SECRET_NAMES:
        v = keyring.get_password("JobScout", name)
        if v:
            keyring.set_password(env.KEYRING_SERVICE, name, v)
            n += 1
    if n:
        copied.append(f"{n} saved key(s)/sign-in(s)")
    from . import gmail
    gmail._svc_cache.clear()
    db.log("Copied production data into staging: " + ", ".join(copied))
    return {"copied": copied, "from": str(prod)}


if __name__ == "__main__":
    import sys
    print(copy_production_data())
