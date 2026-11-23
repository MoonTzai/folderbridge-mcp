"""EXE Packager Toolkit: offline, no real toolchain/network/device calls."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from folderbridge_mcp.extensions import load_extension


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "Plugins" / "extensions" / "exe-packager-toolkit"
SPEC = importlib.util.spec_from_file_location("exe_packager_candidate_test", PLUGIN / "plugin.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("EXE packager plugin file missing")
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


def write_pe(path: Path):
    data = bytearray(4096)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", data, 0x84, 0x8664)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class EXEPackagerOfflineTests(unittest.TestCase):
    def test_folderbridge_schema_compatibility(self):
        from folderbridge_mcp.extensions import validate_json_schema
        raw = json.loads((PLUGIN / "folderbridge-extension.json").read_text(encoding="utf-8"))
        sample = {
            "package-onefile": {
                "source_path": "ZTC-1 Control/ZTC1-Local-Direct-Control-R03R1.pyw",
                "output_dir": "ZTC-1 Control/Release",
                "output_name": "ZTC1-Local-Control-R03R1",
                "backend": "auto",
                "gui": True,
                "expected_source_sha256": "a" * 64,
                "overwrite": False,
                "timeout_seconds": 3600,
            },
            "verify-exe": {
                "path": "ZTC-1 Control/Release/ZTC1-Local-Control-R03R1.exe",
                "expected_sha256": "b" * 64,
            },
        }
        for name, payload in sample.items():
            with self.subTest(name=name):
                validate_json_schema(payload, raw["actions"][name]["input_schema"], path="params")
        with self.assertRaises(Exception):
            validate_json_schema({**sample["package-onefile"],
                                  "expected_source_sha256": "too-short"},
                                 raw["actions"]["package-onefile"]["input_schema"],
                                 path="params")

    def test_manifest_and_one_time_workspace_scope(self):
        raw = json.loads((PLUGIN / "folderbridge-extension.json").read_text(encoding="utf-8"))
        record = load_extension(PLUGIN, bundled=False)
        self.assertEqual(record.manifest.extension_id, "exe-packager-toolkit")
        self.assertEqual(raw["workspace_adapter"]["mode"], "none")
        self.assertIn("process.execute:python.exe", raw["permissions"])
        self.assertIn("process.execute:go.exe", raw["permissions"])
        self.assertIn("process.execute:dotnet.exe", raw["permissions"])
        self.assertNotIn("network.outbound:https", raw["permissions"])
        self.assertNotIn("process.execute:cmd.exe", raw["permissions"])
        self.assertEqual(set(raw["actions"]), {"status", "preflight", "package-onefile", "verify-exe"})
        item = raw["actions"]["package-onefile"]
        self.assertEqual(item["run_mode"], "job")
        self.assertEqual(item["mutation_scope"], {"mode": "workspace"})
        self.assertEqual(item["effect_contract"]["effect_semantics"], "external_effect")
        self.assertNotIn("argv", item["input_schema"]["properties"])
        self.assertNotIn("command", item["input_schema"]["properties"])

    def test_relative_path_security_and_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.pyw").write_text("print('hello')", encoding="utf-8")
            (root / "hello.go").write_text("package main\nfunc main() {}", encoding="utf-8")
            (root / "hello.csproj").write_text("<Project/>", encoding="utf-8")
            src, rel, kind = p._source(root, "app.pyw")
            self.assertEqual(kind, "python")
            self.assertEqual(rel.as_posix(), "app.pyw")
            self.assertEqual(p._source(root, "hello.go")[2], "go")
            self.assertEqual(p._source(root, "hello.csproj")[2], "dotnet")
            for bad in (
                "/Windows/system32/secret.py", "../app.pyw", "dir\\app.pyw",
                "C:/Users/app.pyw", "node_modules/app.pyw", ".venv/app.pyw",
                ".git/hooks/app.pyw", "secrets/app.pyw", ".env",
            ):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    p._source(root, bad)
            for bad in (".", "../Release", "C:/Release", "Release\\", ".git/Release"):
                with self.subTest(out=bad), self.assertRaises(ValueError):
                    p._output(root, bad, create=False)
            self.assertEqual(p._output_name("ZTC1-Control-R03"), "ZTC1-Control-R03")
            for bad in ("CON", "NUL", "test.exe", "../test", "abc def", "A/evil", ""):
                with self.subTest(name=bad), self.assertRaises(ValueError):
                    p._output_name(bad)
            with self.assertRaises(ValueError):
                p._select_backend("python", "go")

    def test_no_overwrite_or_deletion_of_preexisting_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "source.exe"
            target = folder / "target.exe"
            write_pe(source)
            target.write_bytes(b"ORIGINAL OUTPUT MUST SURVIVE")
            with self.assertRaises(FileExistsError):
                p._copy_exclusive(source, target)
            self.assertEqual(target.read_bytes(), b"ORIGINAL OUTPUT MUST SURVIVE")
            new = folder / "new.exe"
            p._copy_exclusive(source, new)
            self.assertEqual(new.read_bytes(), source.read_bytes())

    def test_pe_validation_and_verify_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "out" / "valid.exe"
            write_pe(target)
            info = p._pe_info(target)
            self.assertEqual(info["pe_machine_hex"], "8664")
            self.assertEqual(info["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            context = {"workspace_root": str(root), "workspace_read_only": False}
            result = p.handle("verify-exe", {
                "path": "out/valid.exe", "expected_sha256": info["sha256"]
            }, context)
            self.assertTrue(result["ok"])
            with self.assertRaises(ValueError):
                p.handle("verify-exe", {
                    "path": "out/valid.exe", "expected_sha256": "0" * 64
                }, context)
            target.write_bytes(b"not PE" + b"x" * 5000)
            with self.assertRaises(ValueError):
                p._pe_info(target)

    def test_argv_is_fixed_and_no_network_download_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            src = folder / "main.pyw"
            src.write_text("import tkinter", encoding="utf-8")
            args, target = p._build_argv("python", ["python.exe"], src, folder, "TC1Control", True)
            self.assertIn("--onefile", args)
            self.assertIn("--windowed", args)
            self.assertNotIn("--add-data", args)
            self.assertNotIn("--additional-hooks-dir", args)
            self.assertEqual(target.name, "TC1Control.exe")
            args, _ = p._build_argv("go", ["go.exe"], folder / "main.go", folder, "Test", False)
            self.assertIn("-buildvcs=false", args)
            args, _ = p._build_argv("dotnet", ["dotnet.exe"], folder / "app.csproj",
                                    folder, "App", True)
            self.assertIn("--no-restore", args)
            env = p._environment()
            self.assertEqual(env["GOPROXY"], "off")
            self.assertEqual(env["PIP_NO_INDEX"], "1")
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_preflight_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "main.pyw"
            source.write_text("import tkinter\n", encoding="utf-8")
            context = {"workspace_root": str(root), "workspace_read_only": False}
            params = {"source_path": "main.pyw", "output_dir": "nested/output",
                      "output_name": "Test", "backend": "auto"}
            with mock.patch.object(p, "_find_toolchain",
                                   return_value=(["python.exe"], "fake-v1", [])):
                result = p.handle("preflight", params, context)
            self.assertTrue(result["ready"])
            self.assertFalse((root / "nested").exists())
            self.assertEqual(result["output_path"], "nested/output/Test.exe")

    @unittest.skipUnless(os.name == "nt", "Windows-only output integration")
    def test_pack_with_fake_compiler_and_real_sha_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "main.pyw"
            source.write_text("import tkinter\n", encoding="utf-8")
            expected = p._sha256(source)
            params = {"source_path": "main.pyw", "output_dir": "Release",
                      "output_name": "Demo", "expected_source_sha256": expected}
            context = {"workspace_root": str(root), "workspace_read_only": False}
            def fake_build(argv, source_arg, logfile, timeout):
                target = Path(argv[argv.index("--distpath") + 1]) / "Demo.exe"
                write_pe(target)
                return 0.02, "fake compiler success"
            with mock.patch.object(p, "_find_toolchain",
                                   return_value=(["python.exe"], "fake", [])):
                with mock.patch.object(p, "_build_once", side_effect=fake_build):
                    result = p.handle("package-onefile", params, context)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "BUILT_SINGLE_FILE_PE_NOT_RUNTIME_TESTED")
            self.assertTrue((root / "Release" / "Demo.exe").is_file())
            # A second attempt must fail without a current hash and overwrite=true.
            with mock.patch.object(p, "_find_toolchain",
                                   return_value=(["python.exe"], "fake", [])):
                with self.assertRaises(ValueError):
                    p.handle("package-onefile", params, context)
            # The reviewed source hash is an independent hard gate.
            with self.assertRaises(ValueError):
                p.handle("package-onefile",
                         {**params, "output_name": "Other",
                          "expected_source_sha256": "0" * 64}, context)


if __name__ == "__main__":
    unittest.main()
