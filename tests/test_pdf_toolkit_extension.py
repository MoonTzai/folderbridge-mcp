from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "pdf-toolkit"
SPEC = importlib.util.spec_from_file_location("folderbridge_pdf_toolkit_plugin", PLUGIN_ROOT / "plugin.py")
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class PdfToolkitManifestTests(unittest.TestCase):
    def test_manifest_is_external_v1_and_has_bounded_actions(self):
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["id"], "pdf-toolkit")
        self.assertEqual(manifest["version"], "0.6.0")
        self.assertEqual(
            manifest["permissions"],
            ["workspace.read", "workspace.write", "process.execute:powershell.exe"],
        )
        self.assertEqual(
            set(manifest["actions"]),
            {"status", "info", "outline", "read-pages", "search", "render-pages"},
        )
        render = manifest["actions"]["render-pages"]
        self.assertEqual(render["run_mode"], "job")
        self.assertEqual(render["timeout_seconds"], 7200)
        self.assertEqual(render["mutation_scope"], {"mode": "paths", "claims": [{"param": "output_dir", "kind": "tree"}]})
        self.assertNotIn("overwrite", render["input_schema"]["properties"])
        self.assertNotIn("grayscale", render["input_schema"]["properties"])
        self.assertEqual(manifest["actions"]["outline"]["input_schema"]["properties"]["max_items"]["maximum"], 500)

    def test_runtime_has_no_network_or_generic_process_permission(self):
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        joined = " ".join(manifest["permissions"])
        self.assertNotIn("network.", joined)
        process_permissions = [item for item in manifest["permissions"] if item.startswith("process.execute:")]
        self.assertEqual(process_permissions, ["process.execute:powershell.exe"])

    def test_fixed_powershell_sources_parse_without_execution(self):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("powershell.exe unavailable")
        for name in ("fetch-upstreams.ps1", "install.ps1", "bootstrap.ps1", "pdf_inspect.ps1", "pdf_render.ps1"):
            with self.subTest(name=name):
                script = PLUGIN_ROOT / name
                escaped = str(script.resolve()).replace("'", "''")
                command = (
                    "$errors=$null;$tokens=$null;"
                    + "[void][System.Management.Automation.Language.Parser]::ParseFile('"
                    + escaped
                    + "',[ref]$tokens,[ref]$errors);"
                    + "if($errors.Count -gt 0){$errors|ForEach-Object{$_.Message};exit 1};exit 0"
                )
                completed = subprocess.run(
                    [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=20,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_installer_uses_whole_tree_transaction_and_no_legacy_python_vendor(self):
        text = (PLUGIN_ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("FileShare.None", text)
        self.assertIn("transaction.json", text)
        self.assertIn("Move-Item -LiteralPath $destination -Destination $backup", text)
        self.assertIn("Move-Item -LiteralPath $destination -Destination $quarantine", text)
        self.assertIn("Move-Item -LiteralPath $backup -Destination $destination", text)
        self.assertIn("_vendor-dotnet", text)
        self.assertIn("pdf_render.ps1", text)
        self.assertNotIn("pypdf-6.16.2-py3-none-any.whl", text)
        self.assertNotIn("Remove-Item -LiteralPath $destination -Recurse", text)

    def test_upstream_fetch_includes_research_only_agpl_reference_and_follows_default_branches(self):
        text = (PLUGIN_ROOT / "fetch-upstreams.ps1").read_text(encoding="utf-8")
        self.assertIn("https://github.com/nfsarch33/pdf-mcp-server.git", text)
        self.assertNotIn("branch = 'develop'", text)
        self.assertNotIn("--branch $repo.branch", text)
        self.assertIn("branch --show-current", text)
        self.assertIn("function Get-DefaultBranch", text)
        self.assertIn("$branch = Get-DefaultBranch -Target $target", text)
        self.assertIn("ls-remote --symref origin HEAD", text)


class PdfToolkitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "manual.pdf").write_bytes(b"%PDF-1.7\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_relative_rejects_escape_windows_and_sensitive_paths(self):
        for raw in (
            "../secret.pdf", "/tmp/a.pdf", r"C:\temp\a.pdf", ".git/config.pdf", "docs/secret.pem",
            "docs/manual.pdf:ads", "out/CON", "out/nul.txt", "out/name.", "out/name ",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(Exception):
                    plugin._clean_relative(raw)

    def test_existing_pdf_must_be_pdf(self):
        (self.root / "docs" / "note.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(Exception):
            plugin._resolve_existing_pdf(self.root, "docs/note.txt")
        resolved = plugin._resolve_existing_pdf(self.root, "docs/manual.pdf")
        self.assertEqual(resolved, self.root / "docs" / "manual.pdf")

    def test_page_range_limits_are_explicit(self):
        self.assertEqual(plugin._validated_range(200, 1, 50, 50, "read-pages"), (1, 50))
        with self.assertRaises(Exception):
            plugin._validated_range(200, 1, 51, 50, "read-pages")
        with self.assertRaises(Exception):
            plugin._validated_range(10, 0, 2, 50, "read-pages")
        with self.assertRaises(Exception):
            plugin._validated_range(10, 4, 11, 50, "read-pages")

    def test_fresh_render_output_requires_existing_parent_and_new_leaf(self):
        (self.root / "rendered").mkdir()
        created = plugin._create_fresh_output_dir(self.root, "rendered/pdf")
        self.assertTrue(created.is_dir())
        with self.assertRaises(Exception):
            plugin._create_fresh_output_dir(self.root, "rendered/pdf")
        with self.assertRaises(Exception):
            plugin._create_fresh_output_dir(self.root, "missing/pdf")


if __name__ == "__main__":
    unittest.main()
