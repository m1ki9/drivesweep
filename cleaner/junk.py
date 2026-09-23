"""Caches, temp files, recycle bin, hibernation, and 'you can delete this' suggestions."""
from __future__ import annotations

import ctypes
import glob
import hashlib
import os
import subprocess
import time
from collections import defaultdict

from cleaner import unused
from cleaner.apps import folder_bytes
from cleaner.delete import onedrive_roots, protected_reason, used_by
from cleaner.scan import DriveScan, nc, under

ENV = os.environ
L = ENV.get("LOCALAPPDATA", "")
R = ENV.get("APPDATA", "")
U = ENV.get("USERPROFILE", "")
W = ENV.get("SystemRoot", r"C:\Windows")
PD = ENV.get("ProgramData", r"C:\ProgramData")
DAY = 86400


def _chromium(base: str) -> list[str]:
    profiles = [base + r"\Default"] + glob.glob(base + r"\Profile *")
    return [os.path.join(p, c) for p in profiles for c in ("Cache", "Code Cache", "GPUCache", r"Service Worker\CacheStorage")]


# (id, name, what it is, folders whose contents get cleared)
CACHES = [
    ("user_temp", "Your temporary files", "Leftovers from installers and apps. Windows doesn't need them.", [L + r"\Temp"]),
    ("win_temp", "Windows temporary files", "Temporary files created by Windows and system services.", [W + r"\Temp"]),
    ("win_update", "Windows Update downloads", "Update installers that have already been applied.", [W + r"\SoftwareDistribution\Download"]),
    ("delivery_opt", "Delivery Optimization cache", "Update pieces kept to share with other PCs.",
     [W + r"\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache"]),
    ("crash", "Crash dumps & error reports", "Memory dumps and reports from crashed programs.",
     [L + r"\CrashDumps", W + r"\Minidump", W + r"\LiveKernelReports", L + r"\Microsoft\Windows\WER",
      PD + r"\Microsoft\Windows\WER\ReportArchive", PD + r"\Microsoft\Windows\WER\ReportQueue"]),
    ("win_logs", "Windows setup logs", "Component servicing logs; can grow to several GB.", [W + r"\Logs\CBS"]),
    ("chrome", "Google Chrome cache", "Saved copies of web pages. Logins and history are kept.", _chromium(L + r"\Google\Chrome\User Data")),
    ("edge", "Microsoft Edge cache", "Saved copies of web pages. Logins and history are kept.", _chromium(L + r"\Microsoft\Edge\User Data")),
    ("brave", "Brave cache", "Saved copies of web pages. Logins and history are kept.", _chromium(L + r"\BraveSoftware\Brave-Browser\User Data")),
    ("opera", "Opera cache", "Saved copies of web pages.", glob.glob(L + r"\Opera Software\*\Cache") + glob.glob(R + r"\Opera Software\*\Cache")),
    ("firefox", "Firefox cache", "Saved copies of web pages. Logins and history are kept.", glob.glob(L + r"\Mozilla\Firefox\Profiles\*\cache2")),
    ("discord", "Discord cache", "Cached images and videos from chats.", [R + r"\discord\Cache", R + r"\discord\Code Cache", R + r"\discord\GPUCache"]),
    ("spotify", "Spotify cache", "Cached songs. Downloaded playlists are not affected.", [L + r"\Spotify\Data"] + glob.glob(L + r"\Packages\SpotifyAB*\LocalCache\Spotify\Data")),
    ("teams", "Microsoft Teams cache", "Cached chat media.", [R + r"\Microsoft\Teams\Cache", R + r"\Microsoft\Teams\Service Worker\CacheStorage"]),
    ("gpu", "Graphics shader cache", "Rebuilt automatically by games and your GPU driver.",
     [L + r"\NVIDIA\DXCache", L + r"\NVIDIA\GLCache", L + r"\AMD\DxCache", L + r"\AMD\DxcCache", L + r"\D3DSCache", L + r"\Intel\ShaderCache"]),
    ("thumbs", "Thumbnail cache", "Small previews of pictures, rebuilt when you open folders.", [L + r"\Microsoft\Windows\Explorer"]),
    ("dev", "Developer caches", "pip, npm, yarn, NuGet, Gradle and Go download caches.",
     [L + r"\pip\cache", L + r"\npm-cache", R + r"\npm-cache", L + r"\Yarn\Cache", U + r"\.nuget\packages", U + r"\.gradle\caches", L + r"\go-build"]),
]
WINDOWS_OLD = ["Windows.old", "$Windows.~BT", "$Windows.~WS", "$WINDOWS.~Q", "$WINDOWS.~TMP"]
# temporary folders some installers and updaters leave at the top of a drive
ROOT_TEMP = {"temp", "tmp", "wudownloadcache", "msdownld.tmp", "$getcurrent", "$sysreset", "esd"}
JUNK_EXT = {".dmp", ".tmp", ".old", ".bak", ".etl", ".log"}
# Duplicates are only reported for your own kind of files; programs keep identical copies of their own
# files (jars, dlls, assets) on purpose and break if one is removed.
PERSONAL_EXT = {
    ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".m4v", ".mcpr", ".jpg", ".jpeg", ".png", ".heic", ".raw", ".cr2",
    ".nef", ".psd", ".mp3", ".wav", ".flac", ".m4a", ".pdf", ".docx", ".xlsx", ".pptx", ".zip", ".rar", ".7z", ".iso",
    ".img",
}
INSTALLER_EXT = {".exe", ".msi", ".iso", ".zip", ".rar", ".7z", ".img", ".dmg", ".cab"}
SYSTEM_DATA_NAMES = {
    "microsoft", "packages", "temp", "programs", "comms", "connecteddevicesplatform", "d3dscache", "crashdumps",
    "nvidia", "nvidia corporation", "intel", "amd", "windows", "package cache", "ssh", "usoshared", "usoprivate",
    "softwaredistribution", "regid.1991-06.com.microsoft", "microsoft_corporation", "ati", "google", "mozilla",
    "apple", "apple computer", "adobe", "pip", "npm-cache", "npm", "yarn", "nuget", "publishers", "virtualstore",
    "history", "iconcache", "desktop.ini", "microsoft help", "microsoft onedrive", "onedrive", "peernetworking",
    "placeholdertilelogofolder", "fontcache", "gamedvr", "deliveryoptimization",
}


def _drive(path: str) -> str:
    return os.path.splitdrive(nc(path))[0]


def caches(drive: str, scan: DriveScan | None, stuck: dict[str, int] | None = None) -> list[dict]:
    """Cache folders on this drive. `stuck` = bytes per cache that couldn't be deleted last time (hidden)."""
    stuck = stuck or {}
    out = []
    for cid, name, desc, folders in CACHES:
        folders = [f for f in folders if _drive(f) == nc(drive)[:2] and os.path.isdir(f)]
        if not folders:
            continue
        size = 0
        for f in folders:
            known = scan.folder_size(f) if scan else None
            size += known if known is not None else folder_bytes(f)
        size -= stuck.get(cid, 0)
        if size < 1024**2:
            continue
        out.append({"id": cid, "name": name, "desc": desc, "paths": folders, "size": size})
    return sorted(out, key=lambda c: c["size"], reverse=True)


def clear_cache_paths(cid: str) -> list[str]:
    for c in CACHES:
        if c[0] == cid:
            if cid == "thumbs":  # only the thumbnail databases, not the whole Explorer folder
                return glob.glob(L + r"\Microsoft\Windows\Explorer\thumbcache_*.db") + glob.glob(L + r"\Microsoft\Windows\Explorer\iconcache_*.db")
            return [f for f in c[3] if os.path.isdir(f)]
    return []


class _RecycleInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def recycle_bin(drive: str) -> tuple[int, int]:
    info = _RecycleInfo()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.shell32.SHQueryRecycleBinW(drive, ctypes.byref(info)) != 0:
        return 0, 0
    return info.i64Size, info.i64NumItems


def empty_recycle_bin(drive: str) -> None:
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, drive, 0x1 | 0x2 | 0x4)  # no confirm/progress/sound


def disable_hibernation() -> bool:
    r = subprocess.run(["powercfg", "/hibernate", "off"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode == 0


def _hiberfil(drive: str) -> int:
    try:
        return os.stat(os.path.join(drive, "hiberfil.sys")).st_size
    except OSError:
        # Windows hides and locks it, but the directory listing still reports the size
        try:
            for e in os.scandir(drive):
                if e.name.lower() == "hiberfil.sys":
                    return e.stat().st_size
        except OSError:
            pass
    return 0


def _known_app_names(apps: list[dict]) -> list[str]:
    """Everything that hints a program is installed: registry, Store packages, Start Menu, per-user installs."""
    names = [_k(a["name"]) + " " + _k(a["publisher"]) + " " + _k(a["location"] or "") for a in apps]
    for folder in (L + r"\Packages", L + r"\Programs"):
        try:
            names += [_k(e.name.split("_")[0]) for e in os.scandir(folder)]
        except OSError:
            pass
    for menu in (R + r"\Microsoft\Windows\Start Menu\Programs", PD + r"\Microsoft\Windows\Start Menu\Programs"):
        for _, dirs, files in os.walk(menu):
            names += [_k(os.path.splitext(n)[0]) for n in dirs + files]
    return [n for n in names if n]


def _program_areas(apps: list[dict]) -> list[str]:
    """Program & app-data areas: only specific rules (caches, leftovers, uninstall) apply inside them."""
    fixed = (ENV.get("ProgramFiles", ""), ENV.get("ProgramFiles(x86)", ""), PD, os.path.join(U, "AppData"), W)
    return [nc(p) for p in fixed if p] + [nc(a["location"]) for a in apps if a["location"]]


def _hash(path: str, quick: bool) -> str | None:
    h = hashlib.blake2b(digest_size=20)
    try:
        with open(path, "rb") as f:
            if quick:  # first and last MB are enough to rule out almost every non-duplicate
                h.update(f.read(1024**2))
                f.seek(-min(1024**2, os.fstat(f.fileno()).st_size), os.SEEK_END)
                h.update(f.read())
            else:
                while chunk := f.read(4 * 1024**2):
                    h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(scan: DriveScan, apps: list[dict], in_use: list[str] = ()) -> list[list[tuple[str, int, float]]]:
    """Groups of identical big files (10 MB+) in your own folders. Compares size, then a quick hash, then the full content.
    `in_use` = folders something still uses (programs keep identical copies of their files on purpose)."""
    skip = _program_areas(apps) + list(in_use)
    by_size: dict[int, list[tuple[str, int, float]]] = defaultdict(list)
    for path, size, mtime, _, _ in scan.big_files:
        p = nc(path)
        if os.path.splitext(p)[1] not in PERSONAL_EXT or any(part.startswith(".") for part in p.split("\\")[1:-1]):
            continue
        if not any(under(p, a) for a in skip) and not protected_reason(p):
            by_size[size].append((path, size, mtime))
    groups = []
    for files in by_size.values():
        if len(files) < 2:
            continue
        for quick in _group(files, lambda f: _hash(f[0], True)):
            if scan.cancel.is_set():
                return groups
            groups += _group(quick, lambda f: _hash(f[0], False))
    return groups


def _group(files, key) -> list[list]:
    out = defaultdict(list)
    for f in files:
        k = key(f)
        if k:
            out[k].append(f)
    return [g for g in out.values() if len(g) > 1]


def suggestions(drive: str, scan: DriveScan, apps: list[dict], programs, stuck: dict | None = None,
                dupes: list | None = None, evidence: dict | None = None) -> list[dict]:
    """`stuck` = {"bytes": {cache id: bytes}, "paths": {normcased path}} that couldn't be deleted earlier; never suggested again.
    `evidence` = what still uses which folders (see usage.collect)."""
    stuck = stuck or {"bytes": {}, "paths": set()}
    evidence = evidence or {"hard": [], "soft": [], "launched": {}, "remembers_since": None, "steam": {"libraries": {}}}
    launched = evidence["launched"]
    now = time.time()
    root = nc(drive)
    onedrive = onedrive_roots()
    app_dirs = [nc(a["location"]) for a in apps if a["location"]]
    program_areas = _program_areas(apps)
    taken: list[str] = []  # paths already suggested, so we never suggest the same bytes twice
    out: list[dict] = []

    def add(group, path, size, title, detail, risk="review", action="delete", last=None, hidden=False, extra=None,
            kind="changed"):
        """`last` is the date the detail text talks about; `kind` says what it is: opened / changed / used."""
        if path and nc(path) in stuck["paths"]:
            taken.append(nc(path))
            return
        if size < 1024**2:
            return
        item = {
            "group": group, "path": path, "size": size, "title": title, "detail": detail, "risk": risk,
            "action": action, "last_used": last, "last_kind": kind, "hidden": hidden,
            "in_use": used_by(path, programs) if path and action == "delete" else [],
        }
        item.update(extra or {})
        out.append(item)
        if path:
            taken.append(nc(path))

    def free(p: str) -> bool:
        p = nc(p)
        return (
            not protected_reason(p)
            and not any(under(p, t) or under(t, p) for t in taken)
            and not any(under(p, o) for o in onedrive)
        )

    # 1. caches / temp
    for c in caches(drive, scan, stuck["bytes"]):
        if c["size"] >= 1024**2:
            add("cache", None, c["size"], c["name"], c["desc"], risk="safe", action="clear", extra={"id": c["id"]})
            taken.extend(nc(p) for p in c["paths"])
    size, items = recycle_bin(drive)
    if size:
        add("cache", None, size, "Recycle Bin", f"{items:,} deleted items still taking up space.", risk="safe", action="recycle")

    # 2. old Windows installs & hibernation
    for name in WINDOWS_OLD:
        p = os.path.join(drive, name)
        if os.path.isdir(p):
            s = scan.folder_size(p) or folder_bytes(p)
            add("system", p, s, "Previous Windows installation" if name == "Windows.old" else f"Windows upgrade files ({name})",
                "Left over from a Windows upgrade. Deleting it removes the option to roll back.", risk="safe", action="winold")
    for name in _listdir(drive):
        p = os.path.join(drive, name)
        k = nc(p)
        if (name.lower() in ROOT_TEMP or name.lower().endswith(".tmp")) and os.path.isdir(p) and not protected_reason(p) \
                and now - (scan.newest.get(k) or now) > 30 * DAY:
            add("system", p, scan.size.get(k) or folder_bytes(p), f"Temporary folder ({name})",
                "Left at the top of the drive by an installer or updater.", risk="safe",
                last=scan.newest[k])
    if os.path.isdir(os.path.join(drive, "MSOCache")):
        p = os.path.join(drive, "MSOCache")
        add("system", p, scan.folder_size(p) or folder_bytes(p), "Old Office installation files (MSOCache)",
            "Copy of an Office installer. Office runs without it; only its repair option needs the original disc/download then.")
    hib = _hiberfil(drive)
    if hib:
        add("system", None, hib, "Hibernation file (hiberfil.sys)",
            "Hidden file the size of your RAM. Turning hibernation off deletes it; your PC will still sleep normally, "
            "but Fast Startup is disabled.", action="hibernate", hidden=True)

    # 2b. what Steam itself no longer uses (uninstalled games, stale downloads, caches)
    for item in unused.steam_leftovers(drive, evidence["steam"]):
        p = item["path"]
        size = scan.folder_size(p) or (folder_bytes(p) if os.path.isdir(p) else os.path.getsize(p))
        add("launcher", p, size, os.path.basename(p), item["detail"], risk="safe", last=scan.newest.get(nc(p)))

    # 3. apps not used for 90+ days
    for a in apps:
        if a["drive"] != root[:2].upper() or a["size"] < 50 * 1024**2:
            continue
        if a["last_used"] and now - a["last_used"] > 90 * DAY:
            add("apps", None, a["size"], a["name"], "Deleting runs the app's own uninstaller.", action="uninstall",
                last=a["last_used"], kind="opened", extra={"id": a["id"], "publisher": a["publisher"]})

    # 4. leftover data from programs that are no longer installed
    app_keys = _known_app_names(apps)
    for base in (L, R, U + r"\AppData\LocalLow", PD, L + r"\Programs"):
        if _drive(base) != root[:2]:
            continue
        for kid in scan.kids.get(nc(base), []):
            k = nc(kid)
            name = os.path.basename(kid).lower()
            size = scan.size.get(k, 0)
            age = now - scan.newest.get(k, now)
            if name in SYSTEM_DATA_NAMES or "package cache" in name or "installer" in name or size < 20 * 1024**2 or age < 90 * DAY or not free(kid):
                continue
            if now - launched.get(k, 0) < 180 * DAY or any(under(h, k) or under(k, h) for h in evidence["hard"]):
                continue  # a program in here was started recently, or something registered lives here
            key = _k(name)
            if len(key) < 3 or any(key in ak or ak in key for ak in app_keys if len(ak) >= 4) \
                    or any(under(d, k) or under(k, d) for d in app_dirs):
                continue
            if used_by(kid, programs):
                continue
            add("leftover", kid, size, os.path.basename(kid), "No installed program uses this folder.",
                last=scan.newest.get(k), hidden=k in scan.hidden)

    # 5. games, programs and folders nothing uses anymore, old installers, old personal folders (see unused.py)
    skip = [nc(os.path.join(U, "AppData")), nc(W)] + onedrive + taken
    for c in unused.find(drive, scan, evidence, [a for a in app_keys if len(a) >= 4], skip, lambda p: used_by(p, programs)):
        if free(c["path"]):
            add(c["group"], c["path"], c["size"], c["title"], c["detail"], last=c["last"], kind=c["last_kind"],
                hidden=nc(c["path"]) in scan.hidden)

    # 6. old installers / archives in Downloads
    downloads = nc(os.path.join(U, "Downloads"))
    for path, size, mtime, atime, hidden in scan.big_files:
        p = nc(path)
        if under(p, downloads) and os.path.splitext(p)[1] in INSTALLER_EXT and now - mtime > 30 * DAY and free(p):
            add("downloads", path, size, os.path.basename(path), "Installer or archive you already downloaded.",
                last=mtime)

    # 7. crash dumps / temp / backup files anywhere (includes hidden ones)
    for path, size, mtime, atime, hidden in sorted(scan.big_files, key=lambda f: -f[1]):
        p = nc(path)
        if os.path.splitext(p)[1] in JUNK_EXT and now - mtime > 7 * DAY and free(p) and not used_by(os.path.dirname(p), programs):
            add("junkfiles", path, size, os.path.basename(path), f"{_JUNK_NAMES[os.path.splitext(p)[1]]}.",
                risk="safe", last=mtime, hidden=hidden)

    # 8. identical copies of the same file: keep the oldest, suggest the rest
    for group in dupes or []:
        group = [f for f in group if os.path.exists(f[0])]
        if len(group) < 2:
            continue
        keep = min(group, key=lambda f: (f[2], len(f[0])))
        for path, size, mtime in group:
            if path != keep[0] and free(path):
                add("dupes", path, size, os.path.basename(path), f"Identical copy of {keep[0]}", last=mtime)

    # 9. your own big files not touched in 6 months (includes hidden ones)
    for path, size, mtime, atime, hidden in sorted(scan.big_files, key=lambda f: -f[1]):
        last = max(mtime, atime)
        if size < 100 * 1024**2 or now - last < 180 * DAY:
            continue
        p = nc(path)
        if not free(p) or any(under(p, a) for a in program_areas) or p.endswith((".sys", ".dll", ".vhdx", ".vmdk")):
            continue
        add("large", path, size, os.path.basename(path), "Your own file. Check it before deleting.", last=last, hidden=hidden,
            kind="used")

    return out


_JUNK_NAMES = {".dmp": "Crash dump", ".tmp": "Temporary file", ".old": "Old backup copy", ".bak": "Backup copy",
               ".etl": "Diagnostic trace", ".log": "Log file"}


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _k(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())

