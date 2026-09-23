"""Fast whole-drive scan. Keeps folder sizes in memory (no database)."""
from __future__ import annotations

import os
import shutil
import threading

HIDDEN = 0x2
DIRECTORY = 0x10
# Cloud placeholders (OneDrive "online-only") report a size but take no disk space.
NOT_ON_DISK = 0x1000 | 0x40000 | 0x400000
BIG_FILE = 10 * 1024**2
REAL_CHANGE = 1024**2  # configs and logs are tiny; a game or program only "really" changes when big files do

# What a folder is made of, so an old folder of photos is never treated like a leftover game.
PERSONAL_EXT = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff", ".raw", ".cr2", ".cr3",
    ".nef", ".arw", ".dng", ".orf", ".rw2", ".psd", ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".m4v", ".webm", ".3gp",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".vob", ".flv", ".mod", ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".wma", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".txt", ".epub",
}
INSTALLER_EXT = {".iso", ".img", ".msi", ".zip", ".rar", ".7z", ".cab", ".dmg", ".tar", ".gz"}
SETUP_NAMES = ("setup", "install")


def nc(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def under(path: str, folder: str) -> bool:
    """True if normcased `path` is `folder` or inside it."""
    return path == folder or path.startswith(folder.rstrip("\\") + "\\")


class DriveScan:
    def __init__(self, root: str) -> None:
        self.root = root
        # all keyed by normcased dir, values cover the whole subtree
        self.size: dict[str, int] = {}
        self.newest: dict[str, float] = {}      # newest file mtime
        self.newest_big: dict[str, float] = {}  # newest mtime of a file >= 1 MB (ignores configs/logs)
        self.count: dict[str, int] = {}
        self.personal: dict[str, int] = {}      # bytes of photos/videos/music/documents
        self.installer: dict[str, int] = {}     # bytes of ISOs/archives/setup files
        self.has_exe: dict[str, bool] = {}
        self.kids: dict[str, list[str]] = {}    # child dir paths (original case)
        self.hidden: set[str] = set()
        self.big_files: list[tuple[str, int, float, float, bool]] = []  # path, size, mtime, atime, hidden
        self.files = 0
        self.bytes = 0
        self.current = root
        self.state = "idle"
        self.cancel = threading.Event()
        # ponytail: the Windows folder (100k+ dirs in WinSxS) is not walked; nothing in it is deletable
        # and its size is estimated as "used space minus everything else". The few cleanable spots
        # inside it (Temp, update cache) are measured on demand.
        self.estimated = {os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows"))}

    def run(self) -> None:
        self.state = "scanning"
        order: list[str] = []
        stack = [self.root]
        while stack:
            if self.cancel.is_set():
                self.state = "cancelled"
                return
            d = stack.pop()
            key = nc(d)
            order.append(key)
            self.current = d
            own = n = personal = installer = bins = 0
            newest = newest_big = 0.0
            exe = setup = False
            kids: list[str] = []
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_symlink() or e.is_junction():
                                continue  # never follow links: avoids loops and double counting
                            st = e.stat(follow_symlinks=False)
                            attrs = st.st_file_attributes
                            if attrs & DIRECTORY:
                                kids.append(e.path)
                                if nc(e.path) not in self.estimated:
                                    stack.append(e.path)
                                if attrs & HIDDEN:
                                    self.hidden.add(nc(e.path))
                                continue
                            size = 0 if attrs & NOT_ON_DISK else st.st_size
                            own += size
                            n += 1
                            newest = max(newest, st.st_mtime)
                            if size >= REAL_CHANGE:
                                newest_big = max(newest_big, st.st_mtime)
                            name = e.name.lower()
                            ext = os.path.splitext(name)[1]
                            if ext == ".exe":
                                exe = True
                                if name.startswith(SETUP_NAMES):
                                    setup = True
                                    installer += size
                            elif ext in PERSONAL_EXT:
                                personal += size
                            elif ext in INSTALLER_EXT:
                                installer += size
                            elif ext == ".bin":
                                bins += size
                            if size >= BIG_FILE:
                                self.big_files.append((e.path, size, st.st_mtime, st.st_atime, bool(attrs & HIDDEN)))
                        except OSError:
                            continue
            except OSError:
                pass
            if setup:  # game repacks: setup.exe next to big .bin archives
                installer += bins
            self.size[key] = own
            self.count[key] = n
            self.newest[key] = newest
            self.newest_big[key] = newest_big
            self.personal[key] = personal
            self.installer[key] = installer
            self.has_exe[key] = exe
            self.kids[key] = kids
            self.files += n
            self.bytes += own

        root = nc(self.root)
        for key in self.estimated:
            if os.path.dirname(key) == root.rstrip("\\") or os.path.dirname(key) == root:
                for stat in (self.size, self.count, self.personal, self.installer):
                    stat[key] = 0
                self.newest[key] = self.newest_big[key] = 0.0
                self.has_exe[key] = True
                self.kids[key] = []
        # Children are always popped after their parent, so reverse order sums bottom-up.
        for key in reversed(order):
            for kid in self.kids[key]:
                k = nc(kid)
                if k not in self.size:
                    continue
                for stat in (self.size, self.count, self.personal, self.installer):
                    stat[key] += stat[k]
                self.newest[key] = max(self.newest[key], self.newest[k])
                self.newest_big[key] = max(self.newest_big[key], self.newest_big[k])
                self.has_exe[key] = self.has_exe[key] or self.has_exe[k]
        for key in self.estimated:
            if key in self.size:
                rest = max(0, shutil.disk_usage(self.root).used - self.size[root])
                self.size[key] = rest
                self.size[root] += rest
        self.state = "done"

    def folder_size(self, path: str) -> int | None:
        return self.size.get(nc(path))

    def forget(self, path: str, freed: int) -> None:
        """Update sizes after `freed` bytes were removed at/under `path`."""
        p = nc(path)
        gone = not os.path.exists(path)
        if gone:
            parent = os.path.dirname(p)
            if parent in self.kids:
                self.kids[parent] = [k for k in self.kids[parent] if nc(k) != p]
            self.big_files = [f for f in self.big_files if not under(nc(f[0]), p)]
        while True:
            if p in self.size:
                self.size[p] = max(0, self.size[p] - freed)
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
