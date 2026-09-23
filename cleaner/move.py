"""Move a program's folders to another drive, leaving a junction behind so everything keeps working."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from cleaner.delete import LINK_TAGS, Progress, delete_path, protected_reason

MOVED_ROOT = "Moved apps"


def _junction(link: str, target: str) -> bool:
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode == 0


def is_link(path: str) -> bool:
    try:
        return os.lstat(path).st_reparse_tag in LINK_TAGS
    except OSError:
        return False


def _copy(src: str, dest: str, progress: Progress) -> None:
    os.makedirs(dest)
    shutil.copystat(src, dest)
    with os.scandir(src) as it:
        entries = list(it)
    for e in entries:
        d = os.path.join(dest, e.name)
        st = os.lstat(e.path)
        is_dir = bool(st.st_file_attributes & 0x10)
        if st.st_reparse_tag in LINK_TAGS:  # recreate links as links, never copy what they point to
            target = os.readlink(e.path)
            if is_dir:
                if not _junction(d, target):
                    raise OSError(f"Couldn't recreate link {e.path}")
            else:
                os.symlink(target, d)
        elif is_dir:
            _copy(e.path, d, progress)
        else:
            progress.current = e.path
            shutil.copy2(e.path, d)
            progress.freed += st.st_size
            progress.files += 1


def _tally(path: str) -> tuple[int, int]:
    """(files, bytes), not following links."""
    files = total = 0
    stack = [path]
    while stack:
        with os.scandir(stack.pop()) as it:
            for e in it:
                st = os.lstat(e.path)
                if st.st_reparse_tag in LINK_TAGS:
                    continue
                if st.st_file_attributes & 0x10:
                    stack.append(e.path)
                else:
                    files += 1
                    total += st.st_size
    return files, total


def _unique(path: str) -> str:
    n, candidate = 2, path
    while os.path.exists(candidate):
        candidate, n = f"{path} ({n})", n + 1
    return candidate


def destination(drive: str, app_name: str, src: str) -> str:
    """e.g. D:\\Moved apps\\Discord\\Local\\Discord"""
    safe = re.sub(r'[<>:"/\\|?*]', "", app_name).strip() or "App"
    parent = os.path.basename(os.path.dirname(src.rstrip("\\"))) or "Folder"
    return os.path.join(drive, MOVED_ROOT, safe, parent, os.path.basename(src.rstrip("\\")))


def move_folder(src: str, dest: str, progress: Progress) -> str | None:
    """Copy, verify, swap in a junction, delete the original. Returns an error message, or None on success.
    Any failure before the swap leaves the original untouched."""
    reason = protected_reason(src)
    if reason:
        return reason
    if is_link(src):
        return "Already moved"
    dest = _unique(dest)
    progress.stage = "copying"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _copy(src, dest, progress)
        if _tally(src) != _tally(dest):
            raise OSError("the copy doesn't match the original")
    except OSError as error:
        shutil.rmtree(dest, ignore_errors=True)
        return f"Copy failed: {getattr(error, 'strerror', None) or error}"
    old = src.rstrip("\\") + ".moving"
    try:
        os.rename(src, old)  # fails if any file is open, so nothing is half-moved
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)
        return "Some files are in use. Close the program (and its tray icon) and try again."
    if not _junction(src, dest):
        os.rename(old, src)
        shutil.rmtree(dest, ignore_errors=True)
        return "Couldn't create the link to the new location"
    leftover = progress.cleanup = Progress()
    progress.stage = "removing"
    delete_path(old, leftover)
    progress.removed += leftover.freed
    progress.cleanup = None
    if leftover.skipped_count:
        return f"Moved, but {leftover.skipped_count} old files couldn't be removed from {old}"
    return None
