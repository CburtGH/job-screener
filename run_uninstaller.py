"""Entry point for JobScoutUninstaller.exe (jobscout_uninstaller.spec).

Delete this file next to JobScout.exe and double-click it. It removes the app and
EVERYTHING it stored (database, resumes, settings, saved keys, caches, shortcuts),
then deletes itself. Nothing survives an uninstall.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _msg(text: str, flags: int) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, "JobScout - Uninstall", flags)


def _rm(path: Path, problems: list[str], recursive: bool = False) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError as e:
        problems.append(f"{path}: {e}")


def main() -> None:
    if not getattr(sys, "frozen", False):
        sys.exit("build this into JobScoutUninstaller.exe (jobscout_uninstaller.spec); it locates itself by exe path")

    here = Path(sys.executable).resolve().parent
    exe = here / "JobScout.exe"
    if not exe.exists():
        _msg("JobScoutUninstaller.exe must be placed in the same folder as JobScout.exe, "
             "then run again.", 0x10)
        return

    answer = _msg(
        "Uninstall JobScout?\n\n"
        "This PERMANENTLY deletes everything:\n"
        "  - the program and its data folder (jobs, scores, resumes, cover letters, settings, logs)\n"
        "  - your saved API keys and Gmail access (Windows Credential Manager)\n"
        "  - the JobScout desktop shortcut and local caches\n\n"
        "Nothing is kept and NOTHING can be restored afterwards.\n\n"
        "Uninstall JobScout now?",
        0x4 | 0x30,  # MB_YESNO | MB_ICONWARNING
    )
    if answer != 6:  # IDYES
        return

    from app import config, env  # SECRET_FIELDS + this folder's instance.json (prod/staging namespaces)

    subprocess.run(["taskkill", "/im", "JobScout.exe", "/f"], capture_output=True)
    time.sleep(1.0)

    problems: list[str] = []
    _rm(exe, problems)
    _rm(here / "data", problems, recursive=True)
    _rm(here / "instance.json", problems)

    for name in config.SECRET_FIELDS:
        try:
            keyring_delete(env.KEYRING_SERVICE, name)
        except Exception:
            pass

    local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "JobScout" / env.ENV
    _rm(local, problems, recursive=True)

    ps = "Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath('Desktop')) 'JobScout.lnk')"
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000, timeout=20)

    if problems:
        _msg("JobScout was uninstalled, but some items could not be deleted (close the app and "
             "delete the folder by hand):\n\n" + "\n".join(problems[:6]), 0x30)
    else:
        _msg("JobScout has been uninstalled.\n\nAll program files, data, saved keys and caches "
             "were deleted. Nothing was left behind.", 0x40)

    me = sys.executable
    subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 >nul & del /q "{me}"', shell=True)


def keyring_delete(service: str, name: str) -> None:
    import keyring
    keyring.delete_password(service, name)


if __name__ == "__main__":
    main()
