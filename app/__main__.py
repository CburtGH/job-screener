"""Launch JobScout as a desktop window. The local server lives only as long as the window:
close the window and everything stops. Nothing runs in the background.

    pythonw -m app          desktop window (what the desktop shortcut runs)
    JOBSCOUT_HEADLESS=1     server only, for development

Production and staging are separate checkouts; see app/env.py.
"""
import os
import socket
import sys
import threading
import time

from .env import APP_NAME, BUNDLE, ICON, IS_STAGING, PORT as ENV_PORT, ROOT

PORT = int(os.environ.get("JOBSCOUT_PORT", ENV_PORT))
URL = f"http://127.0.0.1:{PORT}"

# pythonw has no console; send output to a log file so errors aren't lost.
if sys.stdout is None or sys.stderr is None:
    (ROOT / "data").mkdir(exist_ok=True)
    _log = open(ROOT / "data" / "jobscout.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log


def _port_in_use() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _message_box(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
    except Exception:
        print(text)


def _set_window_icon() -> None:
    """Window/taskbar icon (blue = production, orange = staging)."""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.LoadImageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        me, found = os.getpid(), []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _each(hwnd, _):
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == me and u.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                u.GetWindowTextW(hwnd, buf, 256)
                if buf.value == APP_NAME:
                    found.append(hwnd)
            return True

        u.EnumWindows(_each, 0)
        path = str(BUNDLE / "static" / f"{ICON}.ico")
        for hwnd in found:
            for size, which in ((256, 1), (32, 0)):  # ICON_BIG, ICON_SMALL
                hicon = u.LoadImageW(None, path, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
                if hicon:
                    u.SendMessageW(hwnd, 0x80, which, hicon)  # WM_SETICON
    except Exception as e:
        print(f"icon: {e}")


def _ensure_desktop_shortcut() -> None:
    """JobScout.exe only: put a JobScout icon on the Desktop on first launch (respects
    OneDrive-redirected Desktops; an existing or renamed shortcut is left alone)."""
    try:
        import subprocess
        exe, folder = sys.executable, str(ROOT)
        ps = (
            "$d=[Environment]::GetFolderPath('Desktop'); $l=Join-Path $d 'JobScout.lnk'; "
            "if (-not (Test-Path $l)) { $s=(New-Object -ComObject WScript.Shell).CreateShortcut($l); "
            f"$s.TargetPath='{exe}'; $s.WorkingDirectory='{folder}'; $s.IconLocation='{exe},0'; "
            "$s.Description='JobScout - job-search assistant'; $s.Save() }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       creationflags=0x08000000, timeout=20)  # CREATE_NO_WINDOW
    except Exception as e:
        print(f"shortcut: {e}")


def main() -> None:
    import uvicorn
    from .main import app as fastapi_app  # object, not "app.main:app": PyInstaller can't follow import strings

    if os.environ.get("JOBSCOUT_HEADLESS") == "1":
        uvicorn.run(fastapi_app, host="127.0.0.1", port=PORT, log_level="info")
        return

    if getattr(sys, "frozen", False) and not IS_STAGING:
        _ensure_desktop_shortcut()

    if _port_in_use():
        _message_box(f"{APP_NAME} is already open. Switch to its window (check the taskbar).")
        return

    # Give each environment its own taskbar group and icon so they're easy to tell apart.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"JobScout.{APP_NAME}")
    except Exception:
        pass

    # Bound to localhost only: the app holds your resume, API keys and Gmail access.
    server = uvicorn.Server(uvicorn.Config(fastapi_app, host="127.0.0.1", port=PORT, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True, name="jobscout-server")
    t.start()
    for _ in range(200):
        if server.started:
            break
        if not t.is_alive():
            _message_box(f"{APP_NAME} couldn't start. See {ROOT / 'data' / 'jobscout.log'}")
            return
        time.sleep(0.05)

    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    window = webview.create_window(APP_NAME, URL, width=1440, height=920, min_size=(900, 600), text_select=True)
    window.events.shown += _set_window_icon
    webview.start(private_mode=False, storage_path=str(ROOT / "data" / "webview"))

    # Window closed: stop the server (and any Claude Code sessions, via the app's shutdown hook).
    server.should_exit = True
    t.join(timeout=10)
    try:  # belt and braces: no Claude Code terminals may outlive the window
        from . import claude_code
        claude_code.stop_all()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
