"""Evidence that a folder is still used.

- When programs were last launched from it: Windows Prefetch keeps a record per program with its full path and
  last run time (C:\\Windows\\Prefetch\\*.pf, compressed; reading needs admin).
- What points at it: installed programs, game launchers (Steam, Epic, Riot), Windows services, autostart entries,
  running programs (hard references) and Start menu / Desktop / taskbar shortcuts (soft references).
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import re
import struct
import winreg

from cleaner.apps import _exe_from, _powershell
from cleaner.scan import nc

ENV = os.environ
SYSTEM_ROOT = ENV.get("SystemRoot", r"C:\Windows")
PF_VOLUME = re.compile(r"\\VOLUME\{[0-9a-f]+-([0-9a-f]{8})\}(\\.*)", re.I)


# ---- Prefetch: when was a program last run from each folder ----------------------------------------------
def _decompress(data: bytes) -> bytes | None:
    """Windows 10/11 compress .pf files with XPRESS Huffman ("MAM" header); ntdll can undo it."""
    if data[:3] != b"MAM":
        return data
    fmt, flags = data[3] & 0x0F, data[3] & 0xF0
    size = struct.unpack_from("<I", data, 4)[0]
    payload = data[12:] if flags else data[8:]  # flagged headers carry a CRC first
    ntdll = ctypes.windll.ntdll
    ws, frag = ctypes.c_ulong(), ctypes.c_ulong()
    if ntdll.RtlGetCompressionWorkSpaceSize(ctypes.c_ushort(fmt), ctypes.byref(ws), ctypes.byref(frag)):
        return None
    workspace = ctypes.create_string_buffer(ws.value)
    out = ctypes.create_string_buffer(size)
    final = ctypes.c_ulong()
    status = ntdll.RtlDecompressBufferEx(ctypes.c_ushort(fmt), out, size, payload, len(payload), ctypes.byref(final), workspace)
    return out.raw[:final.value] if status == 0 else None


def _pf_exe_path(pf: bytes) -> str | None:
    """The program's own path ("\\VOLUME{...}\\GAMES\\X\\GAME.EXE") from a decompressed prefetch record."""
    if len(pf) < 0x6C or pf[4:8] != b"SCCA":
        return None
    exe_name = pf[0x10:0x4C].decode("utf-16-le", "ignore").split("\x00")[0].upper()
    offset, length = struct.unpack_from("<II", pf, 0x64)
    names = pf[offset:offset + length].decode("utf-16-le", "ignore").split("\x00")
    for name in names:
        if exe_name and name.upper().rsplit("\\", 1)[-1].startswith(exe_name):
            return name
    return None


def _volume_serials() -> dict[int, str]:
    out = {}
    for drive in os.listdrives():
        serial = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetVolumeInformationW(drive, None, 0, ctypes.byref(serial), None, None, None, 0):
            out[serial.value] = drive[:2]
    return out


def launch_index() -> tuple[dict[str, float], float | None]:
    """(normcased folder -> last time a program inside it was started, oldest record = how far back Windows remembers).
    Empty when Prefetch can't be read (not admin, or SysMain disabled)."""
    serials = _volume_serials()
    index: dict[str, float] = {}
    oldest = None
    try:
        entries = [e for e in os.scandir(os.path.join(SYSTEM_ROOT, "Prefetch")) if e.name.lower().endswith(".pf")]
    except OSError:
        return {}, None
    for e in entries:
        try:
            ran = e.stat().st_mtime
            with open(e.path, "rb") as f:
                pf = _decompress(f.read())
        except OSError:
            continue
        path = _pf_exe_path(pf) if pf else None
        match = PF_VOLUME.match(path or "")
        drive = serials.get(int(match.group(1), 16)) if match else None
        if not drive:
            continue
        oldest = ran if oldest is None else min(oldest, ran)
        folder = nc(drive + os.path.dirname(match.group(2)))
        while index.get(folder, 0) < ran:  # every parent folder was used at least this recently too
            index[folder] = ran
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
    # sanity check: every PC constantly runs programs from System32; if we didn't find those, the records
    # weren't read correctly and must not be used to call anything "unused"
    if nc(os.path.join(SYSTEM_ROOT, "System32")) not in index:
        return {}, None
    return index, oldest


# ---- game launchers ------------------------------------------------------------------------------------------
def _reg(hive, key: str, value: str, view: int = 0) -> str | None:
    try:
        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
            return str(winreg.QueryValueEx(k, value)[0])
    except OSError:
        return None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _vdf(text: str, key: str) -> list[str]:
    return [v.replace("\\\\", "\\") for v in re.findall(rf'"{key}"\s+"([^"]*)"', text, re.I)]


def steam() -> dict:
    """Steam's install folder, its libraries and, per library, the games Steam says are installed."""
    root = _reg(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath") or \
        _reg(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath", winreg.KEY_WOW64_32KEY)
    if not root or not os.path.isdir(root):
        return {"root": None, "libraries": {}}
    root = os.path.normpath(root)
    paths = [root] + _vdf(_read(os.path.join(root, "steamapps", "libraryfolders.vdf")), "path")
    libraries = {}
    for lib in {nc(p): os.path.normpath(p) for p in reversed(paths)}.values():
        apps = libraries.setdefault(lib, {})
        for acf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            text = _read(acf)
            appid = (_vdf(text, "appid") or [""])[0]
            if appid:
                apps[appid] = {"installdir": (_vdf(text, "installdir") or [""])[0], "state": (_vdf(text, "StateFlags") or [""])[0]}
    return {"root": root, "libraries": libraries}


def launcher_installs() -> list[str]:
    """Game folders Epic and Riot say are installed (Steam is handled per library)."""
    out = []
    for item in glob.glob(os.path.join(ENV.get("ProgramData", ""), r"Epic\EpicGamesLauncher\Data\Manifests\*.item")):
        try:
            out.append(json.loads(_read(item)).get("InstallLocation") or "")
        except ValueError:
            pass
    riot = os.path.join(ENV.get("ProgramData", ""), "Riot Games")
    for yaml in glob.glob(os.path.join(riot, "Metadata", "*", "*.yaml")):
        out += re.findall(r'product_install_full_path:\s*"([^"]+)"', _read(yaml))
    try:
        clients = json.loads(_read(os.path.join(riot, "RiotClientInstalls.json")) or "{}")
        out += [os.path.dirname(v) for v in clients.values() if isinstance(v, str)]
    except ValueError:
        pass
    return [os.path.normpath(p) for p in out if p]


# ---- everything that points at a folder ----------------------------------------------------------------------
def _service_dirs() -> list[str]:
    out = []
    base = r"SYSTEM\CurrentControlSet\Services"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return out
    i = 0
    while True:
        try:
            name = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        image = _reg(winreg.HKEY_LOCAL_MACHINE, base + "\\" + name, "ImagePath") or ""
        image = os.path.expandvars(image.replace("\\??\\", "").replace("\\SystemRoot", SYSTEM_ROOT))
        exe = _exe_from(image)
        if exe:
            out.append(os.path.dirname(exe))
    return out


def _autostart_dirs() -> list[str]:
    out = []
    run = r"Software\Microsoft\Windows\CurrentVersion\Run"
    for hive, view in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(hive, run, 0, winreg.KEY_READ | view) as k:
                i = 0
                while True:
                    try:
                        exe = _exe_from(os.path.expandvars(str(winreg.EnumValue(k, i)[1])))
                    except OSError:
                        break
                    i += 1
                    if exe:
                        out.append(os.path.dirname(exe))
        except OSError:
            pass
    return out


SHORTCUT_SCRIPT = r"""
$shell = New-Object -ComObject WScript.Shell
$dirs = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('CommonPrograms'),
          [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'),
          "$env:APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar")
$out = foreach ($d in $dirs) { if (Test-Path $d) { Get-ChildItem $d -Recurse -Filter *.lnk -ErrorAction SilentlyContinue |
  ForEach-Object { try { $shell.CreateShortcut($_.FullName).TargetPath } catch {} } } }
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json @($out | Where-Object { $_ }) -Compress)))
"""


def _shortcut_dirs() -> list[str]:
    import base64
    try:
        targets = json.loads(base64.b64decode(_powershell(SHORTCUT_SCRIPT)).decode("utf-8") or "[]")
    except ValueError:
        return []
    return [os.path.dirname(t) for t in targets if isinstance(t, str) and t.lower().endswith(".exe")]


def collect(apps: list[dict], programs) -> dict:
    """Gathered once after each scan."""
    st = steam()
    hard = [a["location"] for a in apps if a.get("location")] + [a["raw_location"] for a in apps if a.get("raw_location")]
    hard += launcher_installs() + _service_dirs() + _autostart_dirs() + [os.path.dirname(exe) for _, exe in programs]
    if st["root"]:
        hard.append(st["root"])
        hard += [os.path.join(lib, "steamapps") for lib in st["libraries"]]  # judged by the Steam rules instead
    index, oldest = launch_index()
    def resolved(paths):  # count both the path and where it really lives (folders moved behind a link)
        return sorted({nc(x) for p in paths if p for x in (p, os.path.realpath(p))})

    return {
        "hard": resolved(hard),
        "soft": resolved(_shortcut_dirs()),
        "launched": index,
        "remembers_since": oldest,
        "steam": st,
    }
