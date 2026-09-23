import ctypes
import os
import shutil
import sys

import webview

from cleaner.api import Api
from cleaner.apps import _powershell
from cleaner.scan import nc

INSTALL_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "DriveSweep")


def _install() -> None:
    """First run of the downloaded .exe: copy it to its own folder and add Desktop + Start menu shortcuts.
    Later downloads just replace the installed copy (an update). Shortcuts are made only once, so if you
    delete one it stays deleted."""
    if not getattr(sys, "frozen", False):
        return
    target = os.path.join(INSTALL_DIR, "DriveSweep.exe")
    if nc(sys.executable) != nc(target):
        try:
            os.makedirs(INSTALL_DIR, exist_ok=True)
            shutil.copy2(sys.executable, target)
        except OSError:  # e.g. the installed copy is open right now
            if not os.path.exists(target):
                target = sys.executable
    marker = os.path.join(INSTALL_DIR, "shortcuts-created")
    if os.path.exists(marker):
        return
    quoted = target.replace("'", "''")
    _powershell(f"""
        $shell = New-Object -ComObject WScript.Shell
        foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {{
            $link = $shell.CreateShortcut((Join-Path $dir 'DriveSweep.lnk'))
            $link.TargetPath = '{quoted}'
            $link.WorkingDirectory = Split-Path '{quoted}'
            $link.Description = 'Find what fills your drives and free up space'
            $link.Save()
        }}""")
    try:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        open(marker, "w").close()
    except OSError:
        pass


def _relaunch_as_admin() -> bool:
    """Cleaning system folders and reading app usage needs admin. Returns True if a new elevated copy started."""
    if "--no-admin" in sys.argv or ctypes.windll.shell32.IsUserAnAdmin():
        return False
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, ""
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pythonw if os.path.exists(pythonw) else sys.executable
        params = f'"{os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")}"'
    # returns > 32 on success; the user may decline the UAC prompt, then we run without admin
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1) > 32


def main() -> None:
    if _relaunch_as_admin():
        return
    _install()
    ui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")
    webview.create_window("DriveSweep", ui, js_api=Api(), width=1240, height=860, min_size=(900, 600), maximized=True)
    webview.start()


if __name__ == "__main__":
    main()
