"""Built-in local model tiers: light / medium / heavy Qwen3 + MAX Inkling-2-Mini GGUFs served by a managed llama.cpp server.

The app downloads the llama.cpp Windows runtime + one GGUF into %LOCALAPPDATA%\\JobScout\\models (shared by
production and staging), picks a tier from a hardware scan (RAM / GPU VRAM / free disk) with a user override,
then runs `llama-server --port 8899` and points Settings' local-LLM URL at it. GPU vendors are handled
automatically: NVIDIA gets the CUDA build, AMD / Intel / Arc the Vulkan build, no-GPU machines plain CPU.
Settings > App keeps the custom-URL option for your own server (e.g. the home workstation).
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import httpx

from . import db

LOCAL = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "JobScout" / "models"
RUNTIME = LOCAL / "runtime"
MODEL_DIR = LOCAL / "gguf"
PORT = 8899
MANAGED_URL = f"http://127.0.0.1:{PORT}/v1"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

_LLAMA_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=1"

TIERS: dict[str, dict] = {
    "light": {
        "label": "Light", "blurb": "Snappy on any laptop; the basics of scoring",
        "gguf": "Qwen/Qwen3-1.7B-GGUF/Qwen3-1.7B-Q8_0.gguf", "size": 1.7 * 2**30, "min_ram": 6, "min_vram": 0,
    },
    "medium": {
        "label": "Medium", "blurb": "Strong scoring, still quick; most modern laptops",
        "gguf": "Qwen/Qwen3-4B-GGUF/Qwen3-4B-Q4_K_M.gguf", "size": 2.5 * 2**30, "min_ram": 12, "min_vram": 5,
    },
    "heavy": {
        "label": "Heavy", "blurb": "Most accurate; an 8 GB GPU fits it entirely",
        "gguf": "Qwen/Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf", "size": 4.7 * 2**30, "min_ram": 16, "min_vram": 6,
    },
    "max": {
        "label": "MAX", "blurb": "Inkling 2 Mini: 26B MoE with only 3.4B active - top quality, built for 8 GB GPUs",
        "gguf": "unsloth/Inkling-2-Mini-GGUF/Inkling-2-Mini-UD-Q2_K_XL.gguf", "size": 8.5 * 2**30,
        "min_ram": 16, "min_vram": 8, "alias": "inkling-2-mini", "min_build": 12600, "ctx": 8192,
        "flags": ["--cpu-moe"],  # keep the MoE experts in RAM: ~2-3 GB VRAM and a far gentler power draw
    },
}

_lock = threading.Lock()
_install = {"active": False, "tier": None, "phase": None, "done": 0, "total": 0, "error": None}
_server = {"proc": None, "tier": None, "model_id": None, "adopted": False}
_hw_cache: dict | None = None


def _kv() -> dict:
    with db.conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key='local_model'").fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def _kv_set(values: dict) -> None:
    with db.conn() as c:
        c.execute("INSERT INTO kv(key, value) VALUES('local_model', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(values),))


# ---------- hardware ---------------------------------------------------------

def _vendors(name: str) -> str:
    n = name.lower()
    for vendor, label in (("nvidia", "NVIDIA"), ("geforce", "NVIDIA"), ("rtx", "NVIDIA"), ("gtx", "NVIDIA"),
                          ("radeon", "AMD"), ("amd", "AMD"), ("intel", "Intel"), ("arc", "Intel")):
        if vendor in n:
            return label
    return "Other"


def _backend(gpus: list[dict]) -> str:
    """Which llama.cpp runtime to fetch: CUDA for NVIDIA, Vulkan for AMD/Intel GPUs, plain CPU with none."""
    if not gpus:
        return "cpu"
    if any(g["vendor"] == "NVIDIA" for g in gpus):
        return "cuda"
    if any(g["vendor"] in ("AMD", "Intel") for g in gpus):
        return "vulkan"
    return "cpu"


def hardware() -> dict:
    global _hw_cache
    if _hw_cache:
        return _hw_cache

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                    ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64)]

    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    hw = {"ram_gb": round(m.ullTotalPhys / 2**30, 1), "cores": os.cpu_count() or 4, "gpus": []}
    try:  # GPU names (any vendor: NVIDIA / AMD / Intel / Arc...); AdapterRAM is capped at 4 GB on some drivers
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_VideoController | ForEach-Object "
                              "{ \"$($_.Name)|$(($_.AdapterRAM / 1GB))\" }"],
                             capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
        for line in out.stdout.strip().splitlines():
            if "|" in line:
                name, _, gb = line.partition("|")
                if name.strip():
                    hw["gpus"].append({"name": name.strip(), "vram_gb": float(gb or 0),
                                       "vendor": _vendors(name)})
    except (OSError, ValueError):
        pass
    try:  # NVIDIA report exact VRAM per GPU
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,name", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        for line in out.stdout.strip().splitlines():
            mb, _, name = line.partition(",")
            if name.strip():
                exact = round(float(mb) / 1024, 1)
                match = next((g for g in hw["gpus"] if g["vendor"] == "NVIDIA" and g["vram_gb"] < exact), None)
                if match:
                    match["vram_gb"] = exact
                    match["name"] = name.strip()
                elif not any(g["name"] == name.strip() for g in hw["gpus"]):
                    hw["gpus"].append({"name": name.strip(), "vram_gb": exact, "vendor": "NVIDIA"})
    except (OSError, ValueError):
        pass
    hw["gpus"].sort(key=lambda g: -g["vram_gb"])
    hw["vram_gb"] = max((g["vram_gb"] for g in hw["gpus"]), default=0.0)
    hw["backend"] = _backend(hw["gpus"])
    hw["gpu"] = hw["gpus"][0]["name"] if hw["gpus"] else None
    LOCAL.mkdir(parents=True, exist_ok=True)
    hw["free_gb"] = round(shutil.disk_usage(LOCAL).free / 2**30,)
    _hw_cache = hw
    return hw


def recommend(hw: dict | None = None) -> str:
    hw = hw or hardware()
    for tier, spec in reversed(list(TIERS.items())):
        if (hw["vram_gb"] >= spec["min_vram"] or hw["ram_gb"] >= spec["min_ram"]) and (
                hw["free_gb"] >= round(spec["size"] / 2**30) + 2):
            return tier
    return "light"


# ---------- install ----------------------------------------------------------

def tier_state(tid: str) -> dict:
    model = MODEL_DIR / Path(TIERS[tid]["gguf"]).name
    sel = _kv()
    return {"installed": model.exists(), "selected": sel.get("tier") == tid,
            "running": _server["tier"] == tid and running()}


def runtime_zip(backend: str) -> tuple[str, str, bool]:
    """(zip_url, tag, needs_cudart) for the managed llama.cpp runtime: cuda/vulkan/cpu Windows x64 build."""
    with httpx.Client(timeout=30) as c:
        rel = c.get(_LLAMA_RELEASES).raise_for_status().json()[0]
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    # e.g. llama-b12669-bin-win-cpu-x64.zip / -vulkan-x64.zip / -cuda-12.4-x64.zip (never arm64, never the cudart zip)
    name = next(n for n in assets if n.startswith("llama-") and f"bin-win-{backend}" in n and n.endswith("-x64.zip"))
    return assets[name], rel["tag_name"], backend == "cuda"


def _download(url: str, dest: Path, expected: int | None = None) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=httpx.Timeout(60, read=120), follow_redirects=True) as c, c.stream("GET", url) as r:
        r.raise_for_status()
        _install["total"] = int(r.headers.get("content-length") or expected or 0)
        _install["done"] = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                _install["done"] += len(chunk)
    tmp.replace(dest)


def _install_worker(tid: str) -> None:
    global _install
    try:
        hw = hardware()
        spec = TIERS[tid]
        # Inkling needs a recent llama.cpp: force a refresh when the cached build predates the tier's min_build.
        need_runtime = not (RUNTIME / "llama-server.exe").exists()
        if not need_runtime and spec.get("min_build"):
            try:
                tag = json.loads((RUNTIME / "runtime.json").read_text()).get("tag", "b0")
                need_runtime = int(tag.lstrip("b") or 0) < spec["min_build"]
            except (OSError, ValueError, json.JSONDecodeError):
                need_runtime = True
        if need_runtime:
            shutil.rmtree(RUNTIME, ignore_errors=True)
            _install["phase"] = "runtime"
            url, tag, needs_cudart = runtime_zip(hw["backend"])
            zpath = LOCAL / "llama-runtime.zip"
            _download(url, zpath)
            _install["phase"] = "runtime-extract"
            with zipfile.ZipFile(zpath) as z:
                z.extractall(RUNTIME)
            if needs_cudart:  # the CUDA build needs the matching cudart dlls next to llama-server.exe
                with httpx.Client(timeout=30) as c:
                    rel = c.get(_LLAMA_RELEASES).raise_for_status().json()[0]
                cudart = next(a["browser_download_url"] for a in rel["assets"]
                              if a["name"].startswith("cudart-llama-bin-win-cuda-12.4"))
                _install["phase"] = "cudart"
                _download(cudart, zpath)
                with zipfile.ZipFile(zpath) as z:
                    z.extractall(RUNTIME)
            zpath.unlink(missing_ok=True)
            (RUNTIME / "runtime.json").write_text(json.dumps({"tag": tag}))
        _install["phase"] = "model"
        model = MODEL_DIR / Path(spec["gguf"]).name
        if not model.exists():
            repo_path, _, fname = spec["gguf"].rpartition("/")
            _download(f"https://huggingface.co/{repo_path}/resolve/main/{fname}", model, int(spec["size"]))
        sel = _kv()
        sel.update({"tier": tid, "installed_at": db.now_iso()})
        _kv_set(sel)
        _install["phase"] = "ready"
        db.log(f"Built-in model {spec['label']} installed")
        start_server(tid, apply_engine=True)  # an installed tier becomes the default
    except Exception as e:
        _install["error"] = str(e)
        _install["phase"] = "error"
        db.log(f"Built-in model install failed: {e}", "warn")
    finally:
        _install["active"] = False


def start_install(tid: str) -> str | None:
    if tid not in TIERS:
        return "Unknown model tier."
    if _install["active"]:
        return "An install is already running."
    spec = TIERS[tid]
    free = round(shutil.disk_usage(LOCAL).free / 2**30)
    if free < round(spec["size"] / 2**30) + 2:
        return f"Only {free} GB free - {spec['label']} needs {round(spec['size'] / 2**30) + 2} GB."
    with _lock:
        if _install["active"]:
            return "An install is already running."
        _install.update({"active": True, "tier": tid, "phase": "starting", "done": 0, "total": 0, "error": None})
    threading.Thread(target=_install_worker, args=(tid,), daemon=True, name="jobscout-model-install").start()
    return None


# ---------- managed server ---------------------------------------------------

def running() -> bool:
    try:
        with httpx.Client(timeout=3) as c:
            return c.get(f"{MANAGED_URL}/models").status_code == 200
    except httpx.HTTPError:
        return _server["proc"] is not None and _server["proc"].poll() is None


def model_id() -> str | None:
    try:
        with httpx.Client(timeout=3) as c:
            return c.get(f"{MANAGED_URL}/models").json()["data"][0]["id"]
    except (httpx.HTTPError, KeyError, IndexError):
        return None


def start_server(tid: str, apply_engine: bool = False) -> str | None:
    """Bring up the managed llama-server for an installed tier. When apply_engine, the tier also becomes
    the Settings choice (local LLM pointed at the managed URL). Returns an error string, or None once started."""
    if tid not in TIERS or not tier_state(tid)["installed"]:
        return f"The {TIERS[tid]['label']} model isn't installed."
    if running():
        if _server["proc"] is None:  # another JobScout window already serves it
            _server.update({"tier": tid, "adopted": True})
        if apply_engine:
            _apply_engine(tid)
        return None
    exe = RUNTIME / "llama-server.exe"
    model = MODEL_DIR / Path(TIERS[tid]["gguf"]).name
    spec = TIERS[tid]
    proc = subprocess.Popen([str(exe), "-m", str(model), "--alias", spec.get("alias", f"qwen3-{tid}"),
                             "--host", "127.0.0.1", "--port", str(PORT),
                             "-ngl", "99", "-c", str(spec.get("ctx", 8192)), "--jinja", *spec.get("flags", [])],
                            creationflags=NO_WINDOW)
    _server.update({"proc": proc, "tier": tid, "adopted": False})
    db.log(f"Built-in model: started llama-server ({TIERS[tid]['label']}) on port {PORT}")
    if apply_engine:
        def _wait():
            for _ in range(120):
                if running():
                    _apply_engine(tid)
                    return
                time.sleep(1)
            db.log("Built-in model: server didn't answer in time", "warn")
        threading.Thread(target=_wait, daemon=True, name="jobscout-model-wait").start()
    return None


def _apply_engine(tid: str) -> None:
    from . import config
    sel = _kv()
    sel["model_id"] = model_id() or Path(TIERS[tid]["gguf"]).name
    _kv_set(sel)
    config.set_app_settings({"ai_engine": "local_builtin", "builtin_model": sel["model_id"]})
    db.log(f"AI engine now the built-in {TIERS[tid]['label']} model")


def stop_server() -> None:
    proc = _server["proc"]
    if proc is not None and proc.poll() is None:
        proc.terminate()
        db.log("Built-in model: llama-server stopped")
    _server.update({"proc": None, "tier": None, "model_id": None, "adopted": False})


def autostart_if_selected() -> None:
    """App start: if the engine is the built-in model, serve the tier that was chosen last."""
    from . import config
    s = config.get_app_settings()
    if s.get("ai_engine") == "local_builtin":
        tid = _kv().get("tier")
        if tid:
            start_server(tid, apply_engine=True)  # also refreshes the served model id in Settings


def on_settings_change(saved: dict) -> None:
    """Settings saved: releasing the built-in model frees its RAM (only touches a server we launched)."""
    using = saved.get("ai_engine") == "local_builtin"
    if not using and _server["proc"] is not None and _server["proc"].poll() is None:
        stop_server()


def status() -> dict:
    rec = recommend()
    return {
        "hardware": hardware(),
        "recommended": rec,
        "install": dict(_install),
        "server": {"running": running(), "tier": _server["tier"] if running() else None,
                   "model_id": _server["model_id"], "url": MANAGED_URL},
        "selection": _kv().get("tier"),
        "tiers": [{**{k: v for k, v in spec.items() if k != "gguf"}, "id": tid,
                   "gb": round(spec["size"] / 2**30, 1), **tier_state(tid)} for tid, spec in TIERS.items()],
    }
