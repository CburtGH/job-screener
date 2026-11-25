"""Which environment this checkout is: production or staging.

Set by `instance.json` in the checkout root (git-ignored, written by install_shortcut.ps1):
    {"env": "staging"}
No file = production. Each environment has its own port, data folder (the checkout's data/),
stored keys (separate Credential Manager entries), window title and shortcut.
"""
import json
import sys
from pathlib import Path

if getattr(sys, "frozen", False):  # PyInstaller exe: data next to the exe, resources in the bundle
    ROOT = Path(sys.executable).resolve().parent
    BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent.parent
    BUNDLE = ROOT
_INSTANCE = ROOT / "instance.json"

try:
    _cfg = json.loads(_INSTANCE.read_text()) if _INSTANCE.exists() else {}
except (OSError, json.JSONDecodeError):
    _cfg = {}

ENV = _cfg.get("env", "production")
if ENV not in ("production", "staging"):
    ENV = "production"

IS_STAGING = ENV == "staging"
PORT = int(_cfg.get("port") or (8766 if IS_STAGING else 8765))
APP_NAME = "JobScout (Staging)" if IS_STAGING else "JobScout"
KEYRING_SERVICE = "JobScout-Staging" if IS_STAGING else "JobScout"
ICON = "icon-staging" if IS_STAGING else "icon"
