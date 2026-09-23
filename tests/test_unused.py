import ctypes
import os
import struct
import tempfile
import time
import unittest

from cleaner import unused, usage
from cleaner.scan import DriveScan, nc

YEAR = 365 * 86400


def write(path, size, age=0.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    t = time.time() - age
    os.utime(path, (t, t))


def fake_prefetch(exe_path: str) -> bytes:
    """A Windows 10/11-style .pf: SCCA record, compressed with XPRESS Huffman behind a MAM header."""
    names = ("\\VOLUME{01d0000000000000-1234abcd}\\WINDOWS\\SYSTEM32\\NTDLL.DLL\x00" + exe_path + "\x00").encode("utf-16-le")
    body = bytearray(0x100)
    struct.pack_into("<I4s", body, 0, 30, b"SCCA")
    exe = exe_path.rsplit("\\", 1)[1].encode("utf-16-le")
    body[0x10:0x10 + len(exe)] = exe
    struct.pack_into("<II", body, 0x64, 0x100, len(names))
    raw = bytes(body) + names
    ntdll = ctypes.windll.ntdll
    ws, frag = ctypes.c_ulong(), ctypes.c_ulong()
    ntdll.RtlGetCompressionWorkSpaceSize(ctypes.c_ushort(4), ctypes.byref(ws), ctypes.byref(frag))
    out = ctypes.create_string_buffer(len(raw) * 2 + 1024)
    size = ctypes.c_ulong()
    status = ntdll.RtlCompressBuffer(ctypes.c_ushort(4), raw, len(raw), out, len(out), 4096, ctypes.byref(size),
                                     ctypes.create_string_buffer(ws.value))
    assert status == 0
    return b"MAM\x04" + struct.pack("<I", len(raw)) + out.raw[:size.value]


class PrefetchTests(unittest.TestCase):
    def test_reads_program_path(self):
        exe = "\\VOLUME{01d0000000000000-1234abcd}\\GAMES\\OLDGAME\\GAME.EXE"
        pf = usage._decompress(fake_prefetch(exe))
        self.assertEqual(usage._pf_exe_path(pf), exe)
        m = usage.PF_VOLUME.match(exe)
        self.assertEqual((int(m.group(1), 16), m.group(2)), (0x1234ABCD, "\\GAMES\\OLDGAME\\GAME.EXE"))


class UnusedFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.old_mb, unused.MB = unused.MB, 1024  # sizes in these tests are KB, not MB
        r = self.root
        write(os.path.join(r, "OldGame", "game.exe"), 150_000, 2 * YEAR)
        write(os.path.join(r, "OldGame", "data.pak"), 150_000, 2 * YEAR)
        write(os.path.join(r, "UsedGame", "game.exe"), 300_000, 2 * YEAR)
        write(os.path.join(r, "ShortcutGame", "game.exe"), 300_000, 2 * YEAR)
        write(os.path.join(r, "PlayedGame", "game.exe"), 300_000, 2 * YEAR)
        for i in range(3):
            write(os.path.join(r, "Photos", f"img{i}.jpg"), 200_000, 3 * YEAR)
        write(os.path.join(r, "Cool Game Repack", "setup.exe"), 10_000, 0.5 * YEAR)
        write(os.path.join(r, "Cool Game Repack", "fg-01.bin"), 300_000, 0.5 * YEAR)
        self.scan = DriveScan(r)
        self.scan.estimated = set()
        self.scan.run()

    def tearDown(self):
        unused.MB = self.old_mb
        self.tmp.cleanup()

    def find(self, launched=None, since=None):
        r = self.root
        evidence = {"hard": [nc(os.path.join(r, "UsedGame"))], "soft": [nc(os.path.join(r, "ShortcutGame"))],
                    "launched": launched or {}, "remembers_since": since, "steam": {"libraries": {}}}
        found = unused.find(r, self.scan, evidence, [], [], lambda p: [])
        return {os.path.basename(c["path"]): c["group"] for c in found}

    def test_without_launch_records(self):
        self.assertEqual(self.find(), {"OldGame": "games", "PlayedGame": "games", "Photos": "personal",
                                       "Cool Game Repack": "installers"})

    def test_launch_records_decide(self):
        now = time.time()
        launched = {nc(os.path.join(self.root, "PlayedGame")): now - 86400, nc(self.root): now - 86400}
        found = self.find(launched, since=now - YEAR)
        self.assertNotIn("PlayedGame", found, "opened yesterday")
        self.assertEqual(found.get("ShortcutGame"), "games", "has a shortcut but Windows says it wasn't opened in a year")
        self.assertEqual(found.get("OldGame"), "games")


if __name__ == "__main__":
    unittest.main()
