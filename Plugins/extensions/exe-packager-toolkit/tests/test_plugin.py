"""Offline extension-local tests. No Windows toolchains or subprocesses invoked."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("exe_packager_test", ROOT / "plugin.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def fake_exe(path):
    body = bytearray(4096)
    body[:2] = b"MZ"
    struct.pack_into("<I", body, 0x3C, 0x80)
    body[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", body, 0x84, 0x8664)
    path.write_bytes(body)


class PackagerTests(unittest.TestCase):
    def test_manifest(self):
        m = json.loads((ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(m["id"], "exe-packager-toolkit")
        self.assertEqual(set(m["actions"]), {"status", "preflight", "package-onefile", "verify-exe"})
        self.assertNotIn("process.execute:cmd.exe", m["permissions"])
        self.assertEqual(m["actions"]["package-onefile"]["run_mode"], "job")

    def test_filename_path_and_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "app.pyw"
            path.write_text("import tkinter\n", encoding="utf-8")
            self.assertEqual(plugin._source(root, "app.pyw")[2], "python")
            self.assertEqual(plugin._sha256(path), hashlib.sha256(path.read_bytes()).hexdigest())
            for unsafe in ("../app.pyw", "C:/Windows/app.pyw", "node_modules/app.pyw", ".venv/app.pyw"):
                with self.assertRaises(ValueError):
                    plugin._source(root, unsafe)
            for name in ("CON", "a.exe", "x y", "../a"):
                with self.assertRaises(ValueError):
                    plugin._output_name(name)

    def test_exe_header_and_no_clobber(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "created.exe"
            destination = root / "old.exe"
            fake_exe(source)
            self.assertEqual(plugin._pe_info(source)["pe_machine_hex"], "8664")
            destination.write_bytes(b"ORIGINAL")
            with self.assertRaises(FileExistsError):
                plugin._copy_exclusive(source, destination)
            self.assertEqual(destination.read_bytes(), b"ORIGINAL")

    def test_backend_and_safe_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "app.pyw"
            source.write_text("import tkinter\n", encoding="utf-8")
            argv, output = plugin._build_argv("python", ["python.exe"], source, root, "Demo", True)
            self.assertIn("--onefile", argv)
            self.assertIn("--windowed", argv)
            self.assertNotIn("--add-data", argv)
            self.assertEqual(output.name, "Demo.exe")
            self.assertEqual(plugin._environment()["GOPROXY"], "off")


if __name__ == "__main__":
    unittest.main()
