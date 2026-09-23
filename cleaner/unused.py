"""Finds folders you don't use anymore: leftover games and programs, old installers, launcher leftovers.

Every candidate must pass all of these:
  1. nothing points at it: no installed program, launcher, service, autostart entry or running program
     (hard references), and no shortcut unless Windows' launch records show it hasn't been opened for 6 months;
  2. nothing inside was launched recently, according to Windows' launch records (Prefetch), when available;
  3. nothing big inside changed recently (configs and logs are ignored, games touch those without being played);
  4. it's big enough to matter.
Folders that are mostly photos, videos or documents are reported separately, never as leftovers.
The topmost qualifying folder is reported, so one old games folder shows up as one item, not fifty.
"""
from __future__ import annotations

import os
import time

from cleaner.delete import protected_reason
from cleaner.scan import DriveScan, nc, under

DAY = 86400
MB = 1024**2
GB = 1024**3
# In Program Files / ProgramData, folders from these vendors are shared runtimes, SDKs and drivers.
SYSTEM_VENDORS = ("microsoft", "windows", "common files", "dotnet", "reference assemblies", "msbuild", "iis",
                  "internet explorer", "uninstall information", "modifiablewindowsapps", "package cache", "intel",
                  "nvidia", "amd", "realtek", "dell", "hp", "lenovo", "asus", "installshield", "java")



def steam_leftovers(drive: str, steam: dict) -> list[dict]:
    """Game folders, downloads, shader caches and workshop items Steam itself no longer uses."""
    out = []
    for lib, apps in steam["libraries"].items():
        apps_dir = os.path.join(lib, "steamapps")
        if nc(lib)[:2] != nc(drive)[:2] or not os.path.isdir(apps_dir):
            continue
        installed = {a["installdir"].lower() for a in apps.values()}
        for name in _listdir(os.path.join(apps_dir, "common")):
            if name.lower() not in installed:
                out.append((os.path.join(apps_dir, "common", name), f"Files left behind by a game you uninstalled in Steam."))
        for sub, what in (("shadercache", "Shader cache"), (r"workshop\content", "Workshop items"), ("compatdata", "Proton data")):
            for name in _listdir(os.path.join(apps_dir, sub)):
                if name.isdigit() and name not in apps:
                    out.append((os.path.join(apps_dir, sub, name), f"{what} for a game that's no longer installed."))
        for sub in ("downloading", "temp"):
            for name in _listdir(os.path.join(apps_dir, sub)):
                appid = name.split("_")[1] if name.startswith("state_") else name
                app = apps.get(appid)
                if not app or app["state"] == "4":  # 4 = fully installed, nothing pending
                    out.append((os.path.join(apps_dir, sub, name), "Unfinished Steam download or update that's no longer needed."))
    return [{"path": p, "detail": d} for p, d in out]


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def find(drive: str, scan: DriveScan, evidence: dict, known_names: list[str], skip: list[str], used_by) -> list[dict]:
    """Walks the drive from the top and returns candidates as dicts: group, path, size, title, detail, last."""
    now = time.time()
    hard, soft = evidence["hard"], evidence["soft"]
    launched = evidence["launched"]
    since = evidence["remembers_since"]
    # launch records only prove "unused" if Windows remembers far enough back
    records = bool(launched) and since is not None and now - since > 90 * DAY
    program_areas = [nc(p) for p in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""),
                                     os.environ.get("ProgramData", "")) if p]
    out: list[dict] = []

    def container(path: str) -> bool:
        """A protected folder whose contents may still be cleaned (e.g. C:\\Users, Program Files)."""
        return not protected_reason(os.path.join(path, "x"))

    def judge(path: str, k: str) -> dict | None | bool:
        """A candidate dict, False = don't look inside, None = look inside."""
        size = scan.size.get(k, 0)
        name = os.path.basename(path)
        changed_at = scan.newest_big.get(k) or scan.newest.get(k) or now
        touched_at = scan.newest.get(k) or now
        changed, touched = now - changed_at, now - touched_at
        ran = launched.get(k)
        pointed = any(under(s, k) or under(k, s) for s in soft)
        area = next((a for a in program_areas if under(k, a)), None)
        in_program_area = area is not None
        if area:  # e.g. Program Files\Microsoft SQL Server\150: every folder on the way counts
            parts = k[len(area):].strip("\\").split("\\")
            if any(p.startswith(SYSTEM_VENDORS) or (len(_key(p)) >= 4 and any(_key(p) in n for n in known_names)) for p in parts):
                return False
        if used_by(path):
            return None

        personal = scan.personal.get(k, 0) / max(size, 1)
        installer = scan.installer.get(k, 0) / max(size, 1)
        if installer >= 0.6 and changed > 30 * DAY:
            repack = scan.has_exe.get(k) and scan.installer.get(k, 0) > 0.6 * size and any(
                w in name.lower() for w in ("repack", "fitgirl", "dodi", "elamigos", "gog", "setup", "installer"))
            return {"group": "installers", "last": changed_at, "last_kind": "changed", "detail": (
                "Game installer (repack). Once the game is installed, these setup files aren't needed." if repack else
                f"Setup files, disk images and archives ({round(installer * 100)}% of the folder).")}
        if personal >= 0.3:  # even a partly-personal folder is never called a leftover
            if touched > 365 * DAY and size >= 500 * MB:
                return {"group": "personal", "last": touched_at, "last_kind": "changed",
                        "detail": "Mostly photos, videos or documents. Back up anything you want to keep."}
            return False  # never dig into personal folders for "leftovers"
        if scan.has_exe.get(k):
            if records:
                if ran and now - ran < 180 * DAY:
                    return None  # something in here is used: judge the subfolders one by one
                if changed < 60 * DAY:
                    return None
                if ran:
                    last, kind, why = ran, "opened", "No installed program, launcher or shortcut needs it."
                else:
                    last, kind, why = changed_at, "changed", "Windows has no record of it being opened."
            else:
                if pointed or changed < 365 * DAY:
                    return None
                last, kind, why = changed_at, "changed", "No shortcut or installed program uses it."
            if name.lower() in ("steam", "steamlibrary"):
                why = "Old Steam folder that Steam no longer uses. " + why
            return {"group": "games", "detail": why, "last": last, "last_kind": kind}
        if touched > 365 * DAY and size >= 500 * MB and not pointed and not in_program_area:
            return {"group": "old", "last": touched_at, "last_kind": "changed",
                    "detail": "No program, shortcut or launcher uses it."}
        return None

    stack = [(drive, 0)]
    while stack:
        parent, depth = stack.pop()
        for kid in scan.kids.get(nc(parent), []):
            k = nc(kid)
            size = scan.size.get(k, 0)
            if size < 100 * MB or any(under(k, s) for s in skip) or any(under(k, h) for h in hard):
                continue
            if protected_reason(kid):
                if container(kid) and depth < 6:
                    stack.append((kid, depth + 1))
                continue
            if any(under(h, k) for h in hard):  # something inside is in use: look at the rest separately
                if depth < 6:
                    stack.append((kid, depth + 1))
                continue
            verdict = judge(kid, k)
            if verdict:
                if verdict["group"] in ("games", "installers") and size < 200 * MB:
                    continue
                out.append(verdict | {"path": kid, "size": size, "title": os.path.basename(kid)})
            elif verdict is None and depth < 6:
                stack.append((kid, depth + 1))
    return out


def _key(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())
