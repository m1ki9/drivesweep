import os
import stat
import subprocess
import tempfile
import unittest

from cleaner.delete import Progress, delete_path, protected_reason
from cleaner.scan import DriveScan


def write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_deletes_tree_but_never_follows_junctions(self):
        victim = os.path.join(self.root, "victim")
        outside = os.path.join(self.root, "outside")
        write(os.path.join(victim, "a", "one.bin"), 1000)
        write(os.path.join(victim, "two.bin"), 500)
        readonly = os.path.join(victim, "ro.bin")
        write(readonly, 100)
        os.chmod(readonly, stat.S_IREAD)
        write(os.path.join(outside, "keep.bin"), 10)
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(victim, "link"), outside], check=True, capture_output=True)

        progress = Progress()
        freed = delete_path(victim, progress)

        self.assertEqual(freed, 1600)
        self.assertFalse(os.path.exists(victim))
        self.assertTrue(os.path.exists(os.path.join(outside, "keep.bin")), "junction target must survive")

    def test_locked_file_is_skipped_rest_deleted(self):
        folder = os.path.join(self.root, "cache")
        write(os.path.join(folder, "free.bin"), 300)
        locked = os.path.join(folder, "locked.bin")
        write(locked, 200)
        # Python opens files without FILE_SHARE_DELETE, so an open handle blocks deletion like a running app would
        with open(locked, "rb"):
            progress = Progress()
            freed = delete_path(folder, progress, contents_only=True)
        self.assertEqual(freed, 300)
        self.assertTrue(os.path.isdir(folder), "contents_only keeps the folder")
        self.assertEqual([p for p, _ in progress.skipped], [locked])

    def test_locked_folder_is_hidden_afterwards(self):
        from cleaner.api import Api, _Drive
        folder = os.path.join(self.root, "stuff")
        write(os.path.join(folder, "free.bin"), 300)
        locked = os.path.join(folder, "locked.bin")
        write(locked, 100)
        api = Api()
        entry = _Drive(self.root)
        entry.scan.estimated = set()
        entry.scan.run()
        api._drives["T"], api._current = entry, "T"
        with open(locked, "rb"):
            api.execute([{"action": "delete", "path": folder}])
        row = api.list_folder(self.root)["rows"][0]
        self.assertTrue(row["protected"].startswith("In use"), "locked folder can't be selected again")
        self.assertEqual(row["size"], 100)

    def test_move_leaves_working_link(self):
        from cleaner.move import is_link, move_folder
        app = os.path.join(self.root, "src", "MyApp")
        write(os.path.join(app, "app.exe"), 2000)
        write(os.path.join(app, "data", "save.bin"), 300)
        elsewhere = os.path.join(self.root, "elsewhere")
        write(os.path.join(elsewhere, "big.bin"), 5000)
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(app, "link"), elsewhere], check=True, capture_output=True)
        dest = os.path.join(self.root, "otherdrive", "MyApp")

        progress = Progress()
        self.assertIsNone(move_folder(app, dest, progress))

        self.assertTrue(is_link(app), "old path is now a link")
        with open(os.path.join(app, "data", "save.bin"), "rb") as f:  # the app still finds its files
            self.assertEqual(len(f.read()), 300)
        self.assertEqual(os.path.getsize(os.path.join(dest, "app.exe")), 2000)
        self.assertEqual(progress.freed, 2300, "links inside are recreated, not copied")
        self.assertTrue(is_link(os.path.join(dest, "link")))
        self.assertFalse(os.path.exists(app + ".moving"))

    def test_move_with_open_file_changes_nothing(self):
        from cleaner.move import is_link, move_folder
        app = os.path.join(self.root, "MyApp")
        write(os.path.join(app, "app.exe"), 2000)
        dest = os.path.join(self.root, "otherdrive", "MyApp")
        with open(os.path.join(app, "app.exe"), "rb"):
            error = move_folder(app, dest, Progress())
        self.assertIn("in use", error)
        self.assertFalse(is_link(app))
        self.assertEqual(os.path.getsize(os.path.join(app, "app.exe")), 2000)
        self.assertFalse(os.path.exists(dest), "the half-made copy is cleaned up")

    def test_scan_sizes(self):
        write(os.path.join(self.root, "a", "b", "f1"), 100)
        write(os.path.join(self.root, "a", "f2"), 50)
        write(os.path.join(self.root, "c", "f3"), 7)
        scan = DriveScan(self.root)
        scan.estimated = set()
        scan.run()
        self.assertEqual(scan.folder_size(os.path.join(self.root, "a")), 150)
        self.assertEqual(scan.folder_size(self.root), 157)
        scan.forget(os.path.join(self.root, "a", "b"), 100)
        self.assertEqual(scan.folder_size(self.root), 57)


class ProtectionTests(unittest.TestCase):
    def test_protected(self):
        for p in [r"C:\\", r"C:\Windows", r"C:\Windows\System32\drivers", r"C:\Program Files", r"C:\Users",
                  r"C:\Users\bob", r"C:\Users\bob\Documents", r"C:\Users\bob\AppData\Local", r"D:\System Volume Information\x",
                  r"C:\Windows\Temp", r"C:\pagefile.sys", r"C:\ProgramData\Microsoft\Windows Defender",
                  r"C:\Users\bob\NTUSER.DAT", r"C:\Users\bob\AppData\Local\Microsoft\Windows\UsrClass.dat"]:
            self.assertIsNotNone(protected_reason(p), p)

    def test_allowed(self):
        for p in [r"C:\Windows\Temp\setup.log", r"C:\Windows\SoftwareDistribution\Download\abc", r"C:\Program Files\SomeApp",
                  r"C:\Users\bob\Documents\old", r"C:\Users\bob\AppData\Local\Temp\x", r"C:\Windows.old", r"D:\games\old"]:
            self.assertIsNone(protected_reason(p), p)


if __name__ == "__main__":
    unittest.main()
