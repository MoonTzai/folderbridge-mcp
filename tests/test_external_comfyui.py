from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.extensions import ExtensionRegistry, ExtensionTrustStore, load_extension
from folderbridge_mcp.security import Workspace


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "comfyui"
PLUGIN_TEST_PATH = PLUGIN_ROOT / "tests" / "test_plugin.py"

# The repository suite explicitly executes the plugin-local self-tests as well.
# This keeps the external Extension independently testable while ensuring the
# host full suite does not silently lose runtime behavior coverage after the
# legacy tests/test_comfyui.py shim is removed.
_PLUGIN_TEST_SPEC = importlib.util.spec_from_file_location("folderbridge_external_comfyui_selftests", PLUGIN_TEST_PATH)
if _PLUGIN_TEST_SPEC is None or _PLUGIN_TEST_SPEC.loader is None:
    raise RuntimeError("Could not load external ComfyUI plugin self-tests")
_PLUGIN_TEST_MODULE = importlib.util.module_from_spec(_PLUGIN_TEST_SPEC)
_PLUGIN_TEST_SPEC.loader.exec_module(_PLUGIN_TEST_MODULE)
for _test_name in dir(_PLUGIN_TEST_MODULE):
    _test_type = getattr(_PLUGIN_TEST_MODULE, _test_name)
    if isinstance(_test_type, type) and issubclass(_test_type, unittest.TestCase) and _test_type is not unittest.TestCase:
        globals()[f"PluginSelfTest_{_test_name}"] = _test_type


class ExternalComfyUiTests(unittest.TestCase):
    def test_external_manifest_uses_optional_tree_scope_and_global_authorization(self) -> None:
        record = load_extension(PLUGIN_ROOT, bundled=False)
        self.assertEqual(record.manifest.extension_id, "comfyui")
        self.assertFalse(record.bundled)
        self.assertIn("network.loopback:127.0.0.1:8188", record.manifest.permissions)
        status = record.manifest.actions["status"]
        run = record.manifest.actions["run"]
        self.assertEqual(status.authorization, "global")
        self.assertEqual(status.mutation_scope.mode, "none")
        self.assertTrue(status.mutation_scope.explicit)
        self.assertTrue(run.read_only)
        self.assertEqual(run.run_mode, "job")
        self.assertEqual(run.timeout_seconds, 0)
        self.assertEqual(run.mutation_scope.mode, "paths")
        self.assertTrue(run.mutation_scope.explicit)
        self.assertEqual(len(run.mutation_scope.claims), 1)
        claim = run.mutation_scope.claims[0]
        self.assertEqual((claim.param, claim.kind, claim.optional), ("save_directory", "tree", True))

    def test_external_comfyui_scope_is_none_without_save_directory_and_tree_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            user_root = base / "extensions"
            bundled_root = base / "bundled"
            workspace_root = base / "workspace"
            user_root.mkdir()
            bundled_root.mkdir()
            workspace_root.mkdir()
            shutil.copytree(PLUGIN_ROOT, user_root / "comfyui")
            trust = ExtensionTrustStore(base / "trust.json")
            registry = ExtensionRegistry(user_root=user_root, bundled_root=bundled_root, trust_store=trust)
            record = registry.get("comfyui")
            trust.approve(record, enabled=True)
            contract = registry.prepare_action("comfyui", "run")
            workspace = Workspace(workspace_root)

            no_save = registry.prepare_run(
                contract,
                {"workflow_path": "workflow.json"},
                workspace=workspace,
                read_only=False,
            )
            self.assertEqual(no_save.mutation_scope.kind, "none")
            self.assertEqual(no_save.mutation_scope.claims, ())

            with_save = registry.prepare_run(
                contract,
                {"workflow_path": "workflow.json", "save_directory": "Output/renders"},
                workspace=workspace,
                read_only=False,
            )
            self.assertEqual(with_save.mutation_scope.kind, "paths")
            self.assertEqual(len(with_save.mutation_scope.claims), 1)
            self.assertEqual(with_save.mutation_scope.claims[0].kind, "tree")
            self.assertEqual(
                with_save.mutation_scope.claims[0].path,
                os.path.normcase(str((workspace_root / "Output" / "renders").resolve())),
            )

    def test_external_comfyui_hot_reload_marks_hash_stale_and_reapproval_recovers_without_registry_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            user_root = base / "extensions"
            bundled_root = base / "bundled"
            user_root.mkdir()
            bundled_root.mkdir()
            installed = user_root / "comfyui"
            shutil.copytree(PLUGIN_ROOT, installed)
            trust = ExtensionTrustStore(base / "trust.json")
            registry = ExtensionRegistry(user_root=user_root, bundled_root=bundled_root, trust_store=trust)

            first = registry.get("comfyui")
            trust.approve(first, enabled=True)
            self.assertTrue(trust.status(first)["enabled"])
            with (installed / "README.md").open("a", encoding="utf-8") as handle:
                handle.write("\n<!-- hot-reload-test -->\n")

            changed = registry.get("comfyui")
            stale = trust.status(changed)
            self.assertNotEqual(first.sha256, changed.sha256)
            self.assertFalse(stale["trusted"])
            self.assertFalse(stale["enabled"])
            self.assertTrue(stale["approval_stale"])

            trust.approve(changed, enabled=True)
            refreshed = registry.get("comfyui")
            recovered = trust.status(refreshed)
            self.assertEqual(refreshed.sha256, changed.sha256)
            self.assertTrue(recovered["trusted"])
            self.assertTrue(recovered["enabled"])
            described = next(item for item in registry.describe()["extensions"] if item["id"] == "comfyui")
            self.assertTrue(described["loaded"])
            self.assertFalse(described["bundled"])

    def test_install_script_targets_user_hot_load_directory_and_uses_staged_cutover(self) -> None:
        install_path = PLUGIN_ROOT / "install.ps1"
        self.assertTrue(install_path.is_file())
        source = install_path.read_text(encoding="utf-8")
        self.assertIn("folderbridge-mcp\\extensions", source)
        self.assertIn("folderbridge-extension.json", source)
        self.assertIn("comfyui_runtime.py", source)
        self.assertIn("Move-Item", source)
        self.assertIn("ReparsePoint", source)
        self.assertIn(
            'Write-Host "Open FolderBridge Extensions & Skills, click Rescan, then approve the new exact hash and enable it."',
            source,
        )
        self.assertEqual(source.count("exit 0"), 1)
        self.assertNotIn("$LASTEXITCODE", source)
        self.assertNotIn("Invoke-Expression", source)
        self.assertNotIn("iex ", source.lower())

    def test_external_plugin_depends_only_on_public_extension_error_api(self) -> None:
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("folderbridge_mcp.extension_api", source)
        self.assertNotIn("folderbridge_mcp.comfyui", source)
        self.assertNotIn("folderbridge_mcp.security", source)
        spec = importlib.util.spec_from_file_location("folderbridge_external_comfyui_test", PLUGIN_ROOT / "plugin.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        old_path = list(sys.path)
        try:
            sys.path.insert(0, str(PLUGIN_ROOT))
            spec.loader.exec_module(module)
        finally:
            sys.path[:] = old_path
        self.assertTrue(callable(module.handle))
        self.assertTrue(callable(module.run_workflow))


if __name__ == "__main__":
    unittest.main()
