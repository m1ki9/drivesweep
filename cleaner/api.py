"""Bridge between the HTML interface (window.pywebview.api.*) and the cleaner."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import threading
import time

from cleaner import apps as apps_mod
from cleaner import junk, move, usage
from cleaner.delete import (Progress, delete_path, onedrive_roots, protected_reason, running_programs,
                            take_ownership, used_by)
from cleaner.scan import DriveScan, nc, under


def _volume_label(drive: str) -> str:
    buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(drive, buf, 261, None, None, None, None, 0)
    return buf.value if ok else ""


def _file_system(drive: str) -> str:
    buf = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(drive, None, 0, None, None, None, buf, 64)
    return buf.value if ok else ""


def _drive_type(drive: str) -> int:
    return ctypes.windll.kernel32.GetDriveTypeW(drive)  # 2 removable, 3 fixed, 4 network


class _AllScans:
    """Folder sizes from whichever drive scans have finished (programs can live on any drive)."""

    def __init__(self, drives: dict) -> None:
        self.drives = drives

    def folder_size(self, path: str) -> int | None:
        entry = self.drives.get(path[:2].upper() + "\\")
        return entry.scan.folder_size(path) if entry and entry.scan.state == "done" else None


def _usage(drive: str) -> dict:
    u = shutil.disk_usage(drive)
    return {"path": drive, "total": u.total, "used": u.used, "free": u.free}


class _Drive:
    """One drive's scan and what was worked out from it."""

    def __init__(self, root: str) -> None:
        self.scan = DriveScan(root)
        self.ready = False
        self.phase = ""
        self.dupes: list = []


def _key(drive: str) -> str:
    return drive[:2].upper() + "\\"


class Api:
    # Underscored attributes are hidden from JavaScript by pywebview.
    def __init__(self) -> None:
        self._drives: dict[str, _Drive] = {}  # "C:\\" -> scan; drives are scanned in the background at startup
        self._current: str | None = None
        self._lock = threading.Lock()  # programs/evidence are loaded once, by whichever drive finishes first
        self._apps: list[dict] = []
        self._apps_loaded = False
        self._evidence: dict | None = None
        self._icons: dict[str, str] = {}
        self._icons_ready = threading.Event()
        self._job: Progress | None = None
        self._job_label = ""
        self._job_total = 0
        self._job_step = (0, 0)
        self._prescanning = False
        # things a delete couldn't remove (in use): hidden from then on, until the next scan
        self._stuck: dict = {"bytes": {}, "paths": set()}

    @property
    def _entry(self) -> _Drive:
        return self._drives[self._current]

    @property
    def _scan(self) -> DriveScan:
        return self._entry.scan

    # ---- drives & scanning -------------------------------------------------
    def drives(self) -> list[dict]:
        out = []
        for d in os.listdrives():
            if _drive_type(d) not in (2, 3):
                continue
            try:
                info = _usage(d)
            except OSError:
                continue
            info["label"] = _volume_label(d) or ("Local Disk" if _drive_type(d) == 3 else "USB Drive")
            info["system"] = nc(d)[:2] == nc(os.environ.get("SystemDrive", "C:"))[:2]
            out.append(info)
        return out

    def is_admin(self) -> bool:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    def prescan(self) -> None:
        """Scan every internal drive in the background (Windows drive first), so opening one is instant."""
        if self._prescanning:
            return
        self._prescanning = True
        system = _key(os.environ.get("SystemDrive", "C:"))
        fixed = sorted((_key(d) for d in os.listdrives() if _drive_type(d) == 3), key=lambda d: d != system)

        def work() -> None:
            for d in fixed:
                entry = self._claim(d)
                if entry:
                    self._process(entry)

        threading.Thread(target=work, daemon=True).start()

    def _claim(self, drive: str, fresh: bool = False) -> _Drive | None:
        """A new scan for the drive, or None if one is already running/finished (and fresh wasn't asked)."""
        with self._lock:
            old = self._drives.get(drive)
            if old and not fresh and old.scan.state in ("idle", "scanning", "done"):
                return None
            if old:
                old.scan.cancel.set()
            entry = self._drives[drive] = _Drive(drive)
            return entry

    def _process(self, entry: _Drive) -> None:
        entry.scan.run()
        if entry.scan.state != "done":
            return
        with self._lock:
            if not self._apps_loaded:
                entry.phase = "Checking installed programs..."
                self._apps = self._load_apps()
                threading.Thread(target=self._load_icons, daemon=True).start()
                entry.phase = "Checking which folders are still used..."
                self._evidence = usage.collect(self._apps, running_programs())
                for a in self._apps:  # Windows' launch records know about programs the exe-name check missed
                    ran = self._evidence["launched"].get(nc(a["location"])) if a["location"] else None
                    if ran and (not a["last_used"] or ran > a["last_used"]):
                        a["last_used"] = ran
                self._apps_loaded = True
            else:  # sizes of programs on this drive are now known exactly
                for a in self._apps:
                    size = entry.scan.folder_size(a["location"]) if a["location"] else None
                    if size:
                        a["size"] = size
        entry.phase = "Looking for duplicate files..."
        entry.dupes = junk.find_duplicates(entry.scan, self._apps, self._evidence["hard"])
        entry.ready = True

    def start_scan(self, drive: str, fresh: bool = False) -> None:
        """Open a drive: reuses the background scan if there is one; `fresh` scans again."""
        drive = _key(drive)
        self._current = drive
        if fresh:
            self._stuck = {"bytes": {}, "paths": set()}
        entry = self._claim(drive, fresh)
        if entry:
            threading.Thread(target=self._process, args=(entry,), daemon=True).start()

    def _load_apps(self) -> list[dict]:
        sizes = _AllScans(self._drives)
        return sorted(apps_mod.installed_apps(sizes) + apps_mod.store_app_entries(sizes),
                      key=lambda a: a["size"], reverse=True)

    def _load_icons(self) -> None:
        self._icons = apps_mod.icons(self._apps)
        self._icons_ready.set()

    def icons(self) -> dict[str, str]:
        """app id -> icon image. Waits until the icons are extracted."""
        self._icons_ready.wait(60)
        return self._icons

    def cancel_scan(self) -> None:
        if self._current in self._drives:
            self._drives.pop(self._current).scan.cancel.set()

    def scan_status(self) -> dict:
        entry = self._drives.get(self._current)
        if not entry:
            return {"state": "cancelled"}
        return self._status(entry) | {"drive": _usage(entry.scan.root)}

    @staticmethod
    def _status(entry: _Drive) -> dict:
        s = entry.scan
        state = s.state
        if state == "done" and not entry.ready:
            state = "apps"  # files done, still reading programs / looking for duplicates
        return {"state": state, "phase": entry.phase, "files": s.files, "bytes": s.bytes, "current": s.current}

    def drive_states(self) -> dict:
        """Background scan progress per drive, for the drive cards."""
        return {d: {"state": self._status(e)["state"], "bytes": e.scan.bytes} for d, e in list(self._drives.items())}

    # ---- results -------------------------------------------------------------
    def overview(self) -> dict:
        s = self._scan
        programs = running_programs()
        drive = s.root
        return {
            "drive": _usage(drive) | {"label": _volume_label(drive)},
            "admin": self.is_admin(),
            "suggestions": junk.suggestions(drive, s, self._apps, programs, self._stuck, self._entry.dupes, self._evidence),
            "remembers_since": (self._evidence or {}).get("remembers_since"),
            "caches": junk.caches(drive, s, self._stuck["bytes"]) + self._recycle(drive),
            "apps": [a for a in self._apps if a["drive"] == drive[:2].upper()],
            "other_apps": sum(1 for a in self._apps if a["drive"] != drive[:2].upper()),
        }

    @staticmethod
    def _recycle(drive: str) -> list[dict]:
        size, items = junk.recycle_bin(drive)
        return [{"id": "recycle", "name": "Recycle Bin", "desc": f"{items:,} deleted items.", "paths": [], "size": size}] if size else []

    def list_folder(self, path: str) -> dict:
        s = self._scan
        programs = running_programs()
        onedrive = onedrive_roots()
        rows = []
        for kid in s.kids.get(nc(path), []):
            k = nc(kid)
            rows.append({"path": kid, "name": os.path.basename(kid), "dir": True, "size": s.size.get(k, 0),
                         "count": s.count.get(k, 0), "modified": s.newest.get(k) or None, "hidden": k in s.hidden})
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False) or e.is_symlink():
                            continue
                        st = e.stat(follow_symlinks=False)
                        rows.append({"path": e.path, "name": e.name, "dir": False, "size": st.st_size, "count": 1,
                                     "modified": st.st_mtime, "hidden": bool(st.st_file_attributes & 2)})
                    except OSError:
                        continue
        except OSError:
            pass
        rows.sort(key=lambda r: r["size"], reverse=True)
        rows = rows[:500]
        for r in rows:
            r["protected"] = protected_reason(r["path"])
            r["in_use"] = used_by(r["path"], programs) if r["dir"] else []
            r["onedrive"] = any(under(nc(r["path"]), o) for o in onedrive)
            if nc(r["path"]) in self._stuck["paths"]:
                r["protected"] = "In use by another program, can't be deleted right now"
        total = s.size.get(nc(path)) or sum(r["size"] for r in rows)
        return {"path": path, "size": total, "rows": rows}

    def check(self, paths: list[str]) -> list[dict]:
        """Warnings to show before deleting."""
        programs = running_programs()
        onedrive = onedrive_roots()
        app_dirs = [(nc(a["location"]), a["name"]) for a in self._apps if a["location"]]
        out = []
        for p in paths:
            n = nc(p)
            warn = []
            users = used_by(p, programs)
            if users:
                warn.append("Open right now in: " + ", ".join(users[:4]) + ". Files in use will be skipped.")
            if any(under(n, o) for o in onedrive):
                warn.append("Synced with OneDrive: it will also be removed from your OneDrive.")
            owners = [name for d, name in app_dirs if under(d, n) or under(n, d)]
            if owners:
                warn.append("Part of installed program " + owners[0] + ". Uninstalling it is cleaner.")
            if warn:
                out.append({"path": p, "warnings": warn})
        return out

    # ---- moving apps to another drive ------------------------------------------
    def move_plan(self, app_id: str) -> dict:
        """The app's folders (program files + data), drives it can go to, and programs that must be closed first."""
        app = next(a for a in self._apps if a["id"] == app_id)
        programs = running_programs()
        folders = []
        for f in apps_mod.leftovers(app, self._apps, programs):
            if move.is_link(f["path"]) or nc(f["path"])[:2] != nc(self._scan.root)[:2]:
                continue
            main = bool(app["location"]) and nc(f["path"]) == nc(app["location"])
            folders.append(f | {"kind": "Program files" if main else "App data"})
        folders.sort(key=lambda f: (f["kind"] != "Program files", -f["size"]))
        drives = [d for d in self.drives()
                  if nc(d["path"])[:2] != nc(self._scan.root)[:2] and _drive_type(d["path"]) == 3 and _file_system(d["path"]) == "NTFS"]
        return {"app": app["name"], "folders": folders, "drives": drives,
                "running": sorted({n for f in folders for n in f["in_use"]})}

    def move_app(self, app_id: str, drive: str, paths: list[str]) -> dict:
        app = next(a for a in self._apps if a["id"] == app_id)
        sizes = {p: apps_mod.folder_bytes(p) for p in paths}
        if sum(sizes.values()) > shutil.disk_usage(drive).free - 512 * 1024**2:
            return {"error": f"Not enough free space on {drive[:2]} for {app['name']}."}
        source = self._scan.root
        free_before = shutil.disk_usage(source).free
        job = Progress()
        self._job = job
        self._job_total = sum(sizes.values())
        errors = []
        for i, p in enumerate(paths):
            self._job_step = (i, len(paths))
            self._job_label = f"{i + 1} of {len(paths)}: {os.path.basename(p)}"
            error = move.move_folder(p, move.destination(drive, app["name"], p), job)
            if error:
                errors.append({"path": p, "reason": error})
            else:
                self._scan.forget(p, sizes[p])
        self._job_label = ""
        if app["location"] and move.is_link(app["location"]):
            app["moved_to"] = os.readlink(app["location"]).removeprefix("\\\\?\\")
        self._drives.pop(_key(drive), None)  # the target drive changed: scan it again when it's opened
        return {"freed": max(0, shutil.disk_usage(source).free - free_before), "copied": job.freed, "files": job.files,
                "errors": errors, "drive": _usage(source), "target": drive[:2]}

    def open_store_settings(self) -> None:
        """Store apps are moved by Windows itself (Settings > Apps > Installed apps > ... > Move)."""
        os.startfile("ms-settings:appsfeatures")

    # ---- actions -------------------------------------------------------------
    def execute(self, items: list[dict]) -> dict:
        """items: {action: delete|clear|recycle|hibernate|winold|uninstall, path?, id?}. Deletes permanently."""
        drive = self._scan.root
        job = Progress()
        self._job = job
        self._job_total = sum(int(i.get("size") or 0) for i in items)
        free_before = shutil.disk_usage(drive).free
        uninstalled = []
        for i, item in enumerate(items):
            action = item["action"]
            self._job_step = (i, len(items))
            self._job_label = f"{i + 1} of {len(items)}: {item.get('title') or item.get('path') or action}"
            if action in ("delete", "winold"):
                if action == "winold":
                    take_ownership(item["path"])
                self._scan.forget(item["path"], delete_path(item["path"], job))
                if os.path.exists(item["path"]):
                    self._stuck["paths"].add(nc(item["path"]))
            elif action == "clear":
                paths = junk.clear_cache_paths(item["id"])
                for p in paths:
                    freed = delete_path(p, job, contents_only=os.path.isdir(p))
                    self._scan.forget(p if os.path.isdir(p) else os.path.dirname(p), freed)
                # whatever is left is locked by a running program
                self._stuck["bytes"][item["id"]] = sum(
                    apps_mod.folder_bytes(p) if os.path.isdir(p) else (os.path.getsize(p) if os.path.exists(p) else 0)
                    for p in paths)
            elif action == "recycle":
                junk.empty_recycle_bin(drive)
            elif action == "hibernate":
                if not junk.disable_hibernation():
                    job.skip("hiberfil.sys", "Needs administrator rights")
            elif action == "uninstall":
                app = next((a for a in self._apps if a["id"] == item["id"]), None)
                if app:
                    self._job_label = f"{i + 1} of {len(items)}: deleting {app['name']} (finish its uninstaller window if one opens)"
                    apps_mod.uninstall(app)
                    if apps_mod.is_installed(app):
                        job.skip(app["name"], "Not deleted: its uninstaller was cancelled or didn't finish")
                    else:
                        uninstalled.append(app)
                        self._apps = [a for a in self._apps if a["id"] != app["id"]]
        self._job_step = (len(items), len(items))
        time.sleep(0.5)  # let the file system report the new free space
        freed = max(0, shutil.disk_usage(drive).free - free_before)
        self._job_label = ""
        programs = running_programs()
        left = []
        for a in uninstalled:
            if a["kind"] == "desktop":  # Windows removes Store app data itself
                for f in apps_mod.leftovers(a, self._apps + uninstalled, programs):
                    left.append(f | {"app": a["name"]})
        return {
            "freed": freed,
            "counted": job.freed,
            "files": job.files,
            "skipped": [{"path": p, "reason": r} for p, r in job.skipped],
            "skipped_count": job.skipped_count,
            "leftovers": left,
            "drive": _usage(drive),
        }

    def job_status(self) -> dict:
        j = self._job
        if not j:
            return {}
        cleanup = j.cleanup
        return {"freed": j.freed, "files": j.files, "current": (cleanup or j).current, "label": self._job_label,
                "stage": j.stage, "removed": j.removed + (cleanup.freed if cleanup else 0),
                "total": self._job_total, "step": self._job_step[0], "steps": self._job_step[1]}

    def reveal(self, path: str) -> None:
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
