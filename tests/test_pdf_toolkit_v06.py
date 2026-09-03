from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "pdf-toolkit"


class PdfToolkitV06ProductionContractTests(unittest.TestCase):
    def test_manifest_is_schema_preserving_v060_surface(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["id"], "pdf-toolkit")
        self.assertEqual(manifest["name"], "PDF Toolkit")
        self.assertEqual(manifest["version"], "0.6.0")
        self.assertEqual(manifest["entrypoint"], "plugin.py")
        self.assertEqual(manifest["permissions"], [
            "workspace.read",
            "workspace.write",
            "process.execute:powershell.exe",
        ])
        self.assertEqual(manifest["execution"], {"mode": "isolated-process", "timeout_seconds": 600})
        self.assertEqual(manifest["workspace_adapter"], {"mode": "none", "state": "none"})
        self.assertEqual(set(manifest["actions"]), {"status", "info", "outline", "read-pages", "search", "render-pages"})

        status = manifest["actions"]["status"]
        self.assertEqual((status["read_only"], status["requires_workspace"], status["authorization"]), (True, False, "global"))
        self.assertEqual(status["input_schema"], {"type": "object", "properties": {}, "additionalProperties": False})

        info = manifest["actions"]["info"]
        self.assertEqual(info["input_schema"]["properties"]["max_outline_items"], {"type": "integer", "minimum": 0, "maximum": 200, "default": 40})
        self.assertEqual(info["input_schema"]["properties"]["text_sample_pages"], {"type": "integer", "minimum": 0, "maximum": 20, "default": 8})

        outline = manifest["actions"]["outline"]
        self.assertEqual(outline["input_schema"]["properties"]["max_items"], {"type": "integer", "minimum": 1, "maximum": 500, "default": 500})

        read_pages = manifest["actions"]["read-pages"]
        self.assertEqual(read_pages["input_schema"]["properties"]["max_chars"], {"type": "integer", "minimum": 1024, "maximum": 500000, "default": 120000})

        search = manifest["actions"]["search"]
        self.assertEqual(search["input_schema"]["properties"]["query"], {"type": "string", "minLength": 1, "maxLength": 256})
        self.assertEqual(search["input_schema"]["properties"]["max_results"], {"type": "integer", "minimum": 1, "maximum": 200, "default": 50})
        self.assertEqual(search["input_schema"]["properties"]["snippet_chars"], {"type": "integer", "minimum": 80, "maximum": 2000, "default": 360})

        render = manifest["actions"]["render-pages"]
        self.assertFalse(render["read_only"])
        self.assertTrue(render["requires_workspace"])
        self.assertEqual(render["authorization"], "global")
        self.assertEqual(render["run_mode"], "job")
        self.assertEqual(render["timeout_seconds"], 7200)
        self.assertEqual(render["mutation_scope"], {"mode": "paths", "claims": [{"param": "output_dir", "kind": "tree"}]})
        self.assertEqual(render["input_schema"]["properties"]["dpi"], {"type": "integer", "minimum": 72, "maximum": 400, "default": 180})
        self.assertEqual(render["input_schema"]["properties"]["make_zip"], {"type": "boolean", "default": True})

        for action in manifest["actions"].values():
            self.assertEqual(action["authorization"], "global")
            self.assertFalse(action["input_schema"].get("additionalProperties", True))

    def test_python_runtime_has_no_third_party_pdf_parser_import(self) -> None:
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn('PINNED_PDFPIG_VERSION = "0.1.16"', source)
        self.assertIn('PDF_INSPECT_SCRIPT = PLUGIN_DIR / "pdf_inspect.ps1"', source)
        self.assertIn('VENDOR_DOTNET_DIR = PLUGIN_DIR / "_vendor-dotnet"', source)
        self.assertNotIn("PINNED_PYPDF_VERSION", source)
        self.assertNotIn('importlib.import_module("pypdf")', source)
        self.assertNotIn("from pypdf", source)
        self.assertNotIn("import pypdf", source)
        self.assertNotIn("pypdfium2", source)
        vendor_requirements = (PLUGIN_ROOT / "requirements-vendor.txt").read_text(encoding="utf-8")
        self.assertIn("has no Python vendor requirements", vendor_requirements)
        self.assertNotIn("pypdf==", vendor_requirements)

    def test_fixed_inspector_script_locks_loader_inventory_and_ascii_source(self) -> None:
        script_path = PLUGIN_ROOT / "pdf_inspect.ps1"
        self.assertTrue(script_path.is_file(), "v0.6 requires fixed pdf_inspect.ps1")
        raw = script_path.read_bytes()
        self.assertTrue(all(byte < 128 for byte in raw), "pdf_inspect.ps1 source must be ASCII-only")
        text = raw.decode("ascii")
        self.assertIn("Assembly.LoadFrom", text)
        for dll in (
            "System.Runtime.CompilerServices.Unsafe.dll",
            "System.Buffers.dll",
            "System.Numerics.Vectors.dll",
            "System.Memory.dll",
            "Microsoft.Bcl.HashCode.dll",
            "UglyToad.PdfPig.Core.dll",
            "UglyToad.PdfPig.DocumentLayoutAnalysis.dll",
            "UglyToad.PdfPig.Fonts.dll",
            "UglyToad.PdfPig.Package.dll",
            "UglyToad.PdfPig.Tokenization.dll",
            "UglyToad.PdfPig.Tokens.dll",
            "UglyToad.PdfPig.dll",
        ):
            self.assertIn(dll, text)
        self.assertIn("System.Memory, Version=4.0.2.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51", text)
        self.assertIn("System.Memory, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51", text)
        self.assertIn("System.Buffers, Version=4.0.4.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51", text)
        self.assertIn("System.Buffers, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51", text)

    def test_inspector_process_boundary_is_bounded_and_owned(self) -> None:
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("INSPECT_STDOUT_LIMIT = 8 * 1024 * 1024", source)
        self.assertIn("INSPECT_STDERR_LIMIT = 256 * 1024", source)
        self.assertIn("INSPECT_REQUEST_LIMIT = 64 * 1024", source)
        self.assertIn("INSPECT_TIMEOUT_SECONDS = 570", source)
        self.assertIn("PDF_INSPECT_CANCELLED", source)
        self.assertIn("PDF_INSPECT_TIMEOUT", source)
        self.assertIn("PDF_INSPECT_PROTOCOL_TOO_LARGE", source)
        self.assertIn("PDF_INSPECT_PROTOCOL_ERROR", source)
        self.assertIn("owned_process_group_kwargs", source)
        self.assertIn("terminate_owned_process_tree", source)
        self.assertIn("job_cancel_path", source)

    def test_status_contract_names_pdfpig_and_sandbox_limit(self) -> None:
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        for needle in (
            '"inspection_ready"',
            '"pinned_pdfpig_version"',
            '"loaded_pdfpig_version"',
            '"pdf_inspect_script_present"',
            '"casefold_unicode_version"',
            '"parser_memory_sandbox": False',
            '"page_render_png"',
        ):
            self.assertIn(needle, source)
        self.assertNotIn('"pinned_pypdf_version"', source)
        self.assertNotIn('"loaded_pypdf_version"', source)

    def test_public_inspection_actions_use_only_fixed_inspector_seam(self) -> None:
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for function_name in ("_info", "_outline", "_read_pages", "_search"):
            function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
            called_names: set[str] = set()
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        called_names.add(node.func.id)
                    elif isinstance(node.func, ast.Attribute):
                        called_names.add(node.func.attr)
            self.assertIn("_inspection_call", called_names, function_name)
            self.assertNotIn("_open_document", called_names, function_name)
            self.assertNotIn("_page_text_bounded", called_names, function_name)

    def test_inspector_owns_pdfpig_text_outline_geometry_and_casefold_semantics(self) -> None:
        text = (PLUGIN_ROOT / "pdf_inspect.ps1").read_text(encoding="ascii")
        for needle in (
            "'info'",
            "'outline'",
            "'read-pages'",
            "'search'",
            "ContentOrderTextExtractor",
            "TryGetBookmarks",
            "MediaBox",
            "casefold-map.json",
            "1000000",
            "500000",
            "500",
            "TOC_MAX_DEPTH",
            "QUERY_EMPTY",
            "PDF_TEXT_EXTRACT_FAILED",
            "PDF_PAGE_GEOMETRY_FAILED",
        ):
            self.assertIn(needle, text)

    def test_inspector_powershell_51_source_parses_without_execution(self) -> None:
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell is required for the v0.6 production inspector")
        script = str((PLUGIN_ROOT / "pdf_inspect.ps1").resolve())
        escaped_script = script.replace("'", "''")
        command = (
            "$tokens=$null;$errors=$null;"
            f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped_script}',[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count -gt 0){$errors|ForEach-Object{$_.Message};exit 1};exit 0"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_render_path_is_parser_independent(self) -> None:
        tree = ast.parse((PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8"))
        render_fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_render_pages")
        called_names: set[str] = set()
        for node in ast.walk(render_fn):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)
        self.assertNotIn("_open_document", called_names)
        self.assertNotIn("_run_inspector", called_names)
        self.assertNotIn("_inspect_pdf", called_names)
        source_segment = ast.get_source_segment((PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8"), render_fn) or ""
        self.assertIn('"inspection_backend_invoked": False', source_segment)
        self.assertIn('"source_units"', source_segment)
        self.assertIn('"selected_range"', source_segment)
        self.assertIn('"schema_version": 3', source_segment)

    def test_installer_is_locked_transaction_not_in_place_update(self) -> None:
        text = (PLUGIN_ROOT / "install.ps1").read_text(encoding="utf-8")
        for needle in (
            "0.6.0",
            "PdfPig",
            "0.1.16",
            "_vendor-dotnet",
            "VENDOR-PROVENANCE.json",
            "FileShare.None",
            "transaction.json",
            "INSTALL_RECOVERY_REQUIRED",
            "prepared",
            "old_backed_up",
            "new_published",
            "aborted",
            "committed",
            "quarantine",
            "256",
            "67108864",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("pypdf-6.16.2-py3-none-any.whl", text)
        self.assertNotIn("pypdf-reader-no-xmp-xml-v1", text)

    def test_installer_locks_supply_chain_and_live_namespace_rules(self) -> None:
        text = (PLUGIN_ROOT / "install.ps1").read_text(encoding="utf-8")
        for needle in (
            "d67171846ea8c28f50359137065fec4514266d7a32b23eae6c5f2ebed8ffcfc4",
            "f3b9b2bab0bf8cc717d5fdf6d7aee3ec54e36d9e85bd41347acae161319cbd6b",
            "26078aeb758c9ae985e8bf851f973026061da6a5eb4837204d0c2d2204c72955",
            "b00451e91d016fbec091ad1e361f3a7015e1d91d4047f7e48a74455b2a673d79",
            "2bc500a86dcb02f2032d6d877f9e2d6e9e4a79080e57239b4198679d4031f2c7",
            "5f6a7f53af3465f92beb6da873ebe0e496206c313313b98badee4355a6b25937",
            "a566cd48687b2cd897e02501118b2413c14ae86d318f9abbbba97feb84189f0f",
            "77db0452265524962de82ee70bb1d47d2e9539e10ec8ddab2571b252fe0f3504",
            "e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96",
            "c274f80372d90c012937370f0e1f15087d22e308ef98b27cea5dc0d2d088366c",
            "c3b1b78bc8bd3ea13aa4bc9778442d16560270afa235006d816e5e88cef24db4",
            "Move-Item -LiteralPath $destination -Destination $backup",
            "Move-Item -LiteralPath $destination -Destination $quarantine",
            "Move-Item -LiteralPath $backup -Destination $destination",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("Remove-Item -LiteralPath $destination -Recurse", text)
        lock_index = text.index("FileShare.None")
        had_previous_index = text.index("$hadPrevious = Test-Path -LiteralPath $destination")
        self.assertLess(lock_index, had_previous_index, "destination lock must be held before observing had_previous")

    def test_installer_powershell_51_source_parses_without_execution(self) -> None:
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell is required for the v0.6 production installer")
        script = str((PLUGIN_ROOT / "install.ps1").resolve())
        escaped_script = script.replace("'", "''")
        command = (
            "$tokens=$null;$errors=$null;"
            f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped_script}',[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count -gt 0){$errors|ForEach-Object{$_.Message};exit 1};exit 0"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def _offline_installer_inputs(self) -> tuple[str, Path]:
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell is required for the v0.6 production installer")
        cache = ROOT / "local-private" / "pdf-toolkit-v06-feasibility"
        required = (
            cache / "downloads" / "PdfPig.0.1.16.nupkg",
            cache / "downloads" / "Microsoft.Bcl.HashCode.6.0.0.nupkg",
            cache / "downloads" / "System.Memory.4.6.3.nupkg",
            cache / "downloads" / "System.Buffers.4.6.1.nupkg",
            cache / "downloads" / "System.Numerics.Vectors.4.6.1.nupkg",
            cache / "downloads" / "System.Runtime.CompilerServices.Unsafe.6.1.2.nupkg",
            cache / "unicode" / "CaseFolding.txt",
            cache / "unicode" / "LICENSE.txt",
            cache / "licenses" / "Apache-2.0.txt",
            cache / "licenses" / "MIT.txt",
        )
        if not all(path.is_file() for path in required):
            self.skipTest("fresh v0.6 feasibility cache is required for offline installer acceptance")
        return powershell, cache

    def _run_offline_installer(self, powershell: str, cache: Path, destination_root: Path, *, force: bool = False) -> subprocess.CompletedProcess[str]:
        argv = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str((PLUGIN_ROOT / "install.ps1").resolve()),
            "-DestinationRoot",
            str(destination_root),
            "-ReviewedCacheRoot",
            str(cache.resolve()),
        ]
        if force:
            argv.append("-Force")
        return subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    @staticmethod
    def _write_minimal_pdf(path: Path) -> None:
        objects = [
            "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
            "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
            "3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>\nendobj\n",
        ]
        stream = "BT\n/F1 12 Tf\n72 720 Td\n(Hello PDF Toolkit) Tj\nET\n"
        objects.append(f"4 0 obj\n<< /Length {len(stream.encode('ascii'))} >>\nstream\n{stream}endstream\nendobj\n")
        objects.append("5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
        content = "%PDF-1.4\n%FBPDF\n"
        offsets: list[int] = []
        for obj in objects:
            offsets.append(len(content.encode("ascii")))
            content += obj
        xref_offset = len(content.encode("ascii"))
        xref = "xref\n0 6\n0000000000 65535 f \n" + "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
        trailer = f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
        path.write_bytes((content + xref + trailer).encode("ascii"))

    def test_installed_inspector_file_backed_actions_work_on_minimal_pdf(self) -> None:
        powershell, cache = self._offline_installer_inputs()
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            destination_root = parent / "extensions"
            installed = self._run_offline_installer(powershell, cache, destination_root)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            live = destination_root / "pdf-toolkit"
            inspector = (live / "pdf_inspect.ps1").resolve()
            fixture = parent / "probe.pdf"
            self._write_minimal_pdf(fixture)
            fixture_path = str(fixture.resolve())

            def run_request(request: dict[str, object]) -> dict[str, object]:
                completed = subprocess.run(
                    [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(inspector)],
                    input=json.dumps(request, separators=(",", ":")),
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                diagnostic = f"request={request!r}\nstdout={completed.stdout!r}\nstderr={completed.stderr!r}"
                self.assertEqual(completed.returncode, 0, diagnostic)
                self.assertEqual(completed.stderr, "", diagnostic)
                envelope = json.loads(completed.stdout)
                self.assertEqual(envelope["protocol"], 1, envelope)
                self.assertTrue(envelope["ok"], envelope)
                result = envelope["result"]
                self.assertIsInstance(result, dict)
                return result

            info = run_request({
                "protocol": 1,
                "action": "info",
                "path": fixture_path,
                "max_outline_items": 10,
                "text_sample_pages": 1,
            })
            self.assertEqual(info["page_count"], 1)
            self.assertFalse(info["scan_candidate"])

            outline = run_request({
                "protocol": 1,
                "action": "outline",
                "path": fixture_path,
                "max_items": 10,
            })
            self.assertEqual(outline["page_count"], 1)
            self.assertEqual(outline["items"], [])

            read_pages = run_request({
                "protocol": 1,
                "action": "read-pages",
                "path": fixture_path,
                "page_start": 1,
                "page_end": 1,
                "max_chars": 12000,
            })
            self.assertEqual(read_pages["page_count"], 1)
            self.assertEqual(read_pages["returned_pages"], 1)
            self.assertIn("Hello PDF Toolkit", read_pages["pages"][0]["text"])

            search = run_request({
                "protocol": 1,
                "action": "search",
                "path": fixture_path,
                "query": "hello pdf toolkit",
                "case_sensitive": False,
                "max_results": 10,
                "snippet_chars": 120,
                "page_start": 1,
                "page_end": 1,
            })
            self.assertEqual(search["page_count"], 1)
            self.assertEqual(len(search["results"]), 1)
            self.assertEqual(search["results"][0]["page"], 1)

    def test_installer_offline_publish_refusal_force_replacement_and_cleanup(self) -> None:
        powershell, cache = self._offline_installer_inputs()
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            destination_root = parent / "extensions"
            first = self._run_offline_installer(powershell, cache, destination_root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            live = destination_root / "pdf-toolkit"
            manifest = json.loads((live / "folderbridge-extension.json").read_text(encoding="utf-8"))
            provenance = json.loads((live / "VENDOR-PROVENANCE.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], "0.6.0")
            self.assertEqual(provenance["schema_version"], 3)
            self.assertEqual(provenance["pdfpig_version"], "0.1.16")
            self.assertEqual(len(provenance["runtime_dlls"]), 12)
            self.assertFalse(list(parent.rglob("transaction.json")))

            sentinel = live / "UNAPPROVED-SENTINEL.txt"
            sentinel.write_text("must survive non-force refusal", encoding="utf-8")
            refused = self._run_offline_installer(powershell, cache, destination_root)
            self.assertNotEqual(refused.returncode, 0)
            self.assertTrue(sentinel.is_file(), refused.stdout + refused.stderr)
            self.assertFalse(list(parent.rglob("transaction.json")))

            replaced = self._run_offline_installer(powershell, cache, destination_root, force=True)
            self.assertEqual(replaced.returncode, 0, replaced.stdout + replaced.stderr)
            self.assertFalse(sentinel.exists())
            self.assertFalse(list(parent.rglob("transaction.json")))
            self.assertEqual(json.loads((live / "folderbridge-extension.json").read_text(encoding="utf-8"))["version"], "0.6.0")

    def test_installer_nonterminal_recovery_journal_fails_closed_before_live_mutation(self) -> None:
        powershell, cache = self._offline_installer_inputs()
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            destination_root = parent / "extensions"
            installed = self._run_offline_installer(powershell, cache, destination_root)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            live = (destination_root / "pdf-toolkit").resolve()
            manifest_path = live / "folderbridge-extension.json"
            before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

            destination_key = hashlib.sha256(str(live).rstrip("\\").lower().encode("utf-8")).hexdigest()
            transaction_root = parent / "extension-install-transactions"
            staging_root = parent / "extension-install-staging"
            backup_root = parent / "extension-backups"
            quarantine_root = parent / "extension-quarantine"
            state = transaction_root / f"pdf-toolkit-{destination_key}"
            state.mkdir(parents=True, exist_ok=True)
            journal = {
                "schema_version": 1,
                "destination_key": destination_key,
                "destination": str(live),
                "transaction_id": "fixture-nonterminal",
                "had_previous": True,
                "phase": "prepared",
                "staging": str(staging_root / "fixture"),
                "backup": str(backup_root / "fixture"),
                "quarantine": str(quarantine_root / "fixture"),
            }
            (state / "transaction.json").write_text(json.dumps(journal), encoding="utf-8")
            blocked = self._run_offline_installer(powershell, cache, destination_root, force=True)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("INSTALL_RECOVERY_REQUIRED", blocked.stdout + blocked.stderr)
            self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), before)
            self.assertTrue((state / "transaction.json").is_file())

    def test_installer_destination_lock_rejects_concurrent_owner_without_live_mutation(self) -> None:
        powershell, cache = self._offline_installer_inputs()
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            destination_root = parent / "extensions"
            installed = self._run_offline_installer(powershell, cache, destination_root)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            live = (destination_root / "pdf-toolkit").resolve()
            manifest_path = live / "folderbridge-extension.json"
            before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            destination_key = hashlib.sha256(str(live).rstrip("\\").lower().encode("utf-8")).hexdigest()
            lock_path = parent / "extension-install-locks" / f"pdf-toolkit-{destination_key}.lock"
            ready = parent / "lock-ready.txt"
            escaped_lock = str(lock_path).replace("'", "''")
            escaped_ready = str(ready).replace("'", "''")
            command = (
                f"$h=[IO.File]::Open('{escaped_lock}',[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);"
                f"[IO.File]::WriteAllText('{escaped_ready}','ready');"
                "try{Start-Sleep -Seconds 30}finally{$h.Dispose()}"
            )
            holder = subprocess.Popen(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    if holder.poll() is not None:
                        out, err = holder.communicate(timeout=2)
                        self.fail(f"lock holder exited before readiness: {out}{err}")
                    time.sleep(0.05)
                self.assertTrue(ready.is_file(), "lock holder did not acquire destination lock")
                blocked = self._run_offline_installer(powershell, cache, destination_root, force=True)
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("INSTALL_BUSY", blocked.stdout + blocked.stderr)
                self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), before)
            finally:
                if holder.poll() is None:
                    holder.terminate()
                try:
                    holder.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
