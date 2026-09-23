"""Installed programs: list, last-used time, uninstall, leftover folders."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import winreg
from datetime import datetime

import psutil

from cleaner.delete import SYSTEM_ROOT, protected_reason, used_by
from cleaner.scan import nc, under

UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
HIVES = [
    (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    (winreg.HKEY_CURRENT_USER, 0),
]
ENV = os.environ
LEFTOVER_ROOTS = [
    ENV.get("LOCALAPPDATA", ""), ENV.get("APPDATA", ""), os.path.join(ENV.get("USERPROFILE", ""), r"AppData\LocalLow"),
    os.path.join(ENV.get("LOCALAPPDATA", ""), "Programs"), ENV.get("ProgramData", ""),
    ENV.get("ProgramFiles", ""), ENV.get("ProgramFiles(x86)", ""),
]


def _values(key) -> dict:
    out = {}
    i = 0
    while True:
        try:
            name, value, _ = winreg.EnumValue(key, i)
        except OSError:
            return out
        out[name] = value
        i += 1


def _exe_from(command: str) -> str | None:
    """First executable path in a command line like '"C:\\x\\unins.exe" /S' or 'C:\\x\\app.exe,0'."""
    if not command:
        return None
    command = command.strip()
    if command.startswith('"'):
        exe = command[1:].split('"', 1)[0]
    else:
        match = re.match(r"(.+?\.exe)", command, re.I)
        exe = match.group(1) if match else command.split(",")[0]
    return exe if os.path.isfile(exe) else None


def _guess_location(values: dict) -> str | None:
    loc = str(values.get("InstallLocation") or "").strip().strip('"')
    if loc and os.path.isdir(loc):
        return loc.rstrip("\\")
    for field in ("DisplayIcon", "UninstallString"):
        exe = _exe_from(str(values.get(field) or ""))
        if not exe:
            continue
        folder = os.path.dirname(exe)
        low = nc(folder)
        if under(low, SYSTEM_ROOT) or "package cache" in low or "installer" in low or protected_reason(folder):
            continue
        return folder
    return None


def _icon_file(values: dict, location: str | None) -> str | None:
    """The file Windows uses as the app's icon (DisplayIcon is 'path' or 'path,index')."""
    raw = os.path.expandvars(str(values.get("DisplayIcon") or "")).strip().strip('"')
    head, _, index = raw.rpartition(",")
    if head and index.strip().lstrip("-").isdigit():
        raw = head.strip().strip('"')
    if raw and os.path.isfile(raw):
        return raw
    if location:  # fall back to the biggest program file in the install folder
        try:
            exes = [e for e in os.scandir(location) if e.name.lower().endswith(".exe") and not e.name.lower().startswith("unins")]
            if exes:
                return max(exes, key=lambda e: e.stat().st_size).path
        except OSError:
            pass
    return None


def _prefetch_times() -> dict[str, float]:
    """exe name (lower) -> last run time. Windows keeps this in Prefetch (needs admin)."""
    times: dict[str, float] = {}
    try:
        with os.scandir(os.path.join(SYSTEM_ROOT, "Prefetch")) as it:
            for e in it:
                if e.name.lower().endswith(".pf"):
                    name = e.name.rsplit("-", 1)[0].lower()
                    times[name] = max(times.get(name, 0.0), e.stat().st_mtime)
    except OSError:
        pass
    return times


def _exe_names(folder: str, depth: int = 2) -> set[str]:
    names: set[str] = set()
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(".exe"):
                    names.add(e.name.lower())
                elif depth > 1 and e.is_dir(follow_symlinks=False):
                    names |= _exe_names(e.path, depth - 1)
    except OSError:
        pass
    return names


def installed_apps(scan=None) -> list[dict]:
    prefetch = _prefetch_times()
    apps: dict[str, dict] = {}
    for hive, view in HIVES:
        try:
            root = winreg.OpenKey(hive, UNINSTALL, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub) as key:
                    v = _values(key)
            except OSError:
                continue
            name = str(v.get("DisplayName") or "").strip()
            uninstall = str(v.get("QuietUninstallString") or v.get("UninstallString") or "").strip()
            if not name or not uninstall or v.get("SystemComponent") == 1 or v.get("ParentKeyName"):
                continue
            if str(v.get("ReleaseType", "")).lower() in ("update", "hotfix", "security update"):
                continue
            location = _guess_location(v)
            exes = _exe_names(location) if location else set()
            icon = _exe_from(str(v.get("DisplayIcon") or ""))
            if icon:
                exes.add(os.path.basename(icon).lower())
            exes.discard("unins000.exe")
            exes.discard("uninstall.exe")
            last = max((prefetch.get(x, 0.0) for x in exes), default=0.0) or None
            installed = None
            date = str(v.get("InstallDate") or "")
            if re.fullmatch(r"\d{8}", date):
                try:
                    installed = datetime.strptime(date, "%Y%m%d").timestamp()
                except ValueError:
                    pass
            drive = os.path.splitdrive(location or _exe_from(uninstall) or ENV.get("SystemDrive", "C:"))[0].upper()
            app = {
                "id": f"{hive}:{view}:{sub}",
                "name": name,
                "publisher": str(v.get("Publisher") or ""),
                "version": str(v.get("DisplayVersion") or ""),
                "location": location,
                "raw_location": location,  # kept even when shared with other apps (location is then cleared)
                "drive": drive or "C:",
                "size": int(v.get("EstimatedSize") or 0) * 1024,
                "last_used": last,
                "installed": installed,
                "uninstall": uninstall,
                "prefetch": bool(prefetch),
                "icon": _icon_file(v, location),
                "kind": "desktop",
            }
            old = apps.get(name.lower())
            if not old or (app["location"] and not old["location"]):
                apps[name.lower()] = app
        winreg.CloseKey(root)
    result = list(apps.values())
    # A folder shared by several entries (Steam games pointing at Steam, Office suites) isn't any one app's size.
    locs = [nc(a["location"]) for a in result if a["location"]]
    for a in result:
        a["moved_to"] = None
        if not a["location"]:
            continue
        if os.path.islink(a["location"]) or os.path.isjunction(a["location"]):
            a["moved_to"] = os.readlink(a["location"]).removeprefix("\\\\?\\")  # moved to another drive by us
            continue
        loc = nc(a["location"])
        if sum(1 for o in locs if under(o, loc)) > 1:
            a["location"] = None
        elif scan and scan.folder_size(loc) is not None:
            a["size"] = scan.folder_size(loc)
    return sorted(result, key=lambda a: a["size"], reverse=True)


def is_installed(app: dict) -> bool:
    if app.get("kind") == "store":
        return any(a["full"] == app["full"] for a in store_apps())
    hive, view, sub = app["id"].split(":", 2)
    try:
        winreg.CloseKey(winreg.OpenKey(int(hive), UNINSTALL + "\\" + sub, 0, winreg.KEY_READ | int(view)))
        return True
    except OSError:
        return False


def uninstall(app: dict) -> None:
    """Run the app's own uninstaller and wait until it has really finished."""
    if app.get("kind") == "store":
        _powershell(f"Remove-AppxPackage -Package '{app['full']}'")
        return
    cmd = app["uninstall"]
    if re.search(r"msiexec", cmd, re.I):
        cmd = re.sub(r"/I(?=\{)", "/X", cmd, flags=re.I)
        if "/q" not in cmd.lower():
            cmd += " /qb"
    start = time.time()
    tracked = {subprocess.Popen(cmd, shell=True).pid}
    temp = nc(ENV.get("TEMP", ""))
    # Most uninstallers copy themselves to %TEMP%, start the copy and exit at once. Follow every process
    # they spawn, plus fresh orphans (parent already gone = handed off) and processes running from %TEMP%,
    # until all are gone or the app is unregistered.
    while time.time() - start < 30 * 60:
        for p in psutil.process_iter(["pid", "ppid", "create_time", "exe", "name"]):
            info = p.info
            if (info["create_time"] or 0) < start - 1 or info["pid"] in tracked:
                continue
            exe = nc(info["exe"]) if info["exe"] and os.path.isabs(info["exe"]) else ""
            name = (info["name"] or "").lower()
            orphan = not psutil.pid_exists(info["ppid"])
            if info["ppid"] in tracked or orphan or (temp and exe and under(exe, temp)) or name.startswith(("au_", "_iu", "un_a")):
                tracked.add(info["pid"])
        if not is_installed(app):
            return
        if not any(psutil.pid_exists(pid) for pid in tracked):
            time.sleep(1.5)  # some uninstallers clean the registry just after their window closes
            return
        time.sleep(0.25)


def _powershell(script: str, stdin: str = "") -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], input=stdin,
                       capture_output=True, text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    return r.stdout.strip()


STORE_SCRIPT = r"""
$start = @{}
Get-StartApps | ForEach-Object { $pfn = $_.AppID.Split('!')[0]; if (-not $start.ContainsKey($pfn)) { $start[$pfn] = $_.Name } }
$out = Get-AppxPackage | Where-Object { -not $_.IsFramework -and -not $_.NonRemovable -and $_.SignatureKind -ne 'System' -and $start.ContainsKey($_.PackageFamilyName) } | ForEach-Object {
  $logo = $null; $pub = ''
  try {
    $m = Get-AppxPackageManifest $_
    $pub = $m.Package.Properties.PublisherDisplayName
    $l = $m.Package.Properties.Logo
    if ($l) {
      $f = Join-Path $_.InstallLocation $l
      if (Test-Path $f) { $logo = $f } else {
        $c = Get-ChildItem (Split-Path $f) -Filter ([IO.Path]::GetFileNameWithoutExtension($f) + '*.png') -ErrorAction SilentlyContinue |
             Where-Object { $_.Name -notmatch 'contrast' } | Sort-Object Length | Select-Object -Last 1
        if ($c) { $logo = $c.FullName }
      }
    }
  } catch {}
  [pscustomobject]@{ name = $start[$_.PackageFamilyName]; full = $_.PackageFullName; pfn = $_.PackageFamilyName;
    version = $_.Version.ToString(); publisher = "$pub"; location = $_.InstallLocation; logo = $logo }
}
# base64 so names like "Intel(R)" survive the console code page
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json @($out) -Compress)))
"""


def store_apps() -> list[dict]:
    """Microsoft Store (MSIX) apps that have a Start menu entry."""
    try:
        data = json.loads(base64.b64decode(_powershell(STORE_SCRIPT)).decode("utf-8") or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def store_app_entries(scan=None) -> list[dict]:
    prefetch = _prefetch_times()
    out = []
    for s in store_apps():
        loc = s.get("location") or ""
        data_dir = os.path.join(ENV.get("LOCALAPPDATA", ""), "Packages", s["pfn"])
        data_size = (scan.folder_size(data_dir) if scan else None) or 0
        exes = _exe_names(loc) if loc else set()
        last = max((prefetch.get(x, 0.0) for x in exes), default=0.0) or None
        out.append({
            "id": "store:" + s["full"], "full": s["full"], "name": s["name"], "publisher": s.get("publisher") or "",
            "version": s.get("version") or "", "location": None, "drive": (os.path.splitdrive(loc)[0] or "C:").upper(),
            "size": (folder_bytes(loc) if loc else 0) + data_size, "last_used": last, "installed": None,
            "uninstall": None, "prefetch": bool(prefetch), "icon": s.get("logo"), "kind": "store", "moved_to": None,
        })
    return out


ICON_SCRIPT = r"""
Add-Type -AssemblyName System.Drawing
$out = @{}
foreach ($p in ([Console]::In.ReadToEnd() | ConvertFrom-Json)) {
  try {
    $bmp = [System.Drawing.Icon]::ExtractAssociatedIcon($p).ToBitmap()
    $ms = New-Object IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $out[$p] = [Convert]::ToBase64String($ms.ToArray())
  } catch {}
}
$out | ConvertTo-Json -Compress
"""


def icons(apps: list[dict]) -> dict[str, str]:
    """app id -> data: URI of its icon."""
    out: dict[str, str] = {}
    need = []
    for a in apps:
        path = a.get("icon")
        if not path:
            continue
        if path.lower().endswith(".png"):
            try:
                with open(path, "rb") as f:
                    out[a["id"]] = "data:image/png;base64," + base64.b64encode(f.read()).decode()
            except OSError:
                pass
        else:
            need.append(path)
    if need:
        try:
            pngs = json.loads(_powershell(ICON_SCRIPT, json.dumps(sorted(set(need)))) or "{}")
        except ValueError:
            pngs = {}
        for a in apps:
            if a.get("icon") in pngs:
                out[a["id"]] = "data:image/png;base64," + pngs[a["icon"]]
    return out


def _key(text: str) -> str:
    text = re.sub(r"\(.*?\)|\bv?\d+(\.\d+)+\b|\b(x64|x86|64-bit|32-bit)\b", "", text.lower())
    return re.sub(r"[^a-z0-9]", "", text)


GENERIC = {"application", "app", "apps", "bin", "program", "programs", "live", "current", "client", "games", "data"}


def leftovers(app: dict, all_apps: list[dict], programs) -> list[dict]:
    """Folders the app left behind in AppData / ProgramData / Program Files."""
    names = {_key(app["name"])}
    if app["location"]:
        names.add(_key(os.path.basename(app["location"])))
    names -= GENERIC
    publisher = _key(app["publisher"])
    names.discard("")
    found: dict[str, str] = {}
    if app["location"] and os.path.isdir(app["location"]):
        found[nc(app["location"])] = app["location"]
    for root in LEFTOVER_ROOTS:
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for e in entries:
            k = _key(e.name)
            if k in names:
                found[nc(e.path)] = e.path
            elif publisher and k == publisher:  # e.g. AppData\Roaming\<Publisher>\<App>
                try:
                    for sub in os.scandir(e.path):
                        if sub.is_dir(follow_symlinks=False) and _key(sub.name) in names:
                            found[nc(sub.path)] = sub.path
                except OSError:
                    pass
    others = [nc(a["location"]) for a in all_apps if a["location"] and a["id"] != app["id"]]
    out = []
    for path in found.values():
        p = nc(path)
        # never offer a folder that holds (or sits inside) another installed program
        if protected_reason(path) or any(under(o, p) or under(p, o) for o in others):
            continue
        out.append({"path": path, "size": folder_bytes(path), "in_use": used_by(path, programs)})
    return out


def folder_bytes(path: str) -> int:
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not e.is_junction():
                                stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total
