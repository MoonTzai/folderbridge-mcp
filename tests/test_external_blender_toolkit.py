from __future__ import annotations

import ast
import importlib.util
import json
import unittest
from unittest import mock
from pathlib import Path

from folderbridge_mcp.extensions import load_extension

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "blender-toolkit"


class BlenderToolkitRepositoryTests(unittest.TestCase):
    def test_current_host_accepts_manifest(self) -> None:
        record = load_extension(PLUGIN_ROOT, bundled=False)
        self.assertEqual(record.manifest.extension_id, "blender-toolkit")
        self.assertEqual(record.manifest.schema_version, 2)
        self.assertEqual(record.manifest.runtime_abi, 1)
        self.assertEqual(record.manifest.version, "0.1.2")
        self.assertEqual(len(record.manifest.actions), 41)
        self.assertEqual(
            set(record.manifest.permissions),
            {
                "network.loopback:127.0.0.1:8766",
                "workspace.read",
                "workspace.write",
                "process.execute:blender.exe",
            },
        )
        self.assertTrue(record.manifest.actions["screenshot"].read_only)
        self.assertEqual(record.manifest.actions["render-still"].run_mode, "job")
        self.assertEqual(record.manifest.actions["render-animation"].run_mode, "job")
        self.assertEqual(record.manifest.actions["headless-render"].run_mode, "job")
        self.assertFalse(record.manifest.actions["launch-ui"].read_only)
        self.assertEqual(record.manifest.actions["launch-ui"].run_mode, "foreground")

    def test_source_contains_no_arbitrary_code_execution_action(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        actions = set(manifest["actions"])
        self.assertFalse(actions.intersection({"run-script", "exec", "eval", "run-command", "shell"}))
        plugin_text = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertNotIn("shell=True", plugin_text)
        self.assertNotIn("os.system(", plugin_text)
        self.assertNotIn("eval(", plugin_text)
        self.assertNotIn("exec(", plugin_text)

    def test_plugin_module_imports_without_blender(self) -> None:
        spec = importlib.util.spec_from_file_location("blender_toolkit_repo_test", PLUGIN_ROOT / "plugin.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.BRIDGE_URL, "http://127.0.0.1:8766")

    def test_launch_ui_reuses_online_bridge_and_never_duplicates(self) -> None:
        spec = importlib.util.spec_from_file_location("blender_toolkit_launch_online_test", PLUGIN_ROOT / "plugin.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module, "_request", return_value={"bridge_version": "test"}), mock.patch.object(
            module.subprocess, "Popen"
        ) as popen:
            result = module._launch_ui({}, {"workspace_root": str(PLUGIN_ROOT)})
        self.assertTrue(result["bridge_online"])
        self.assertFalse(result["launched"])
        self.assertEqual(result["state"], "already_online")
        popen.assert_not_called()

    def test_launch_ui_refuses_existing_offline_blender_and_uses_fixed_bootstrap_for_new_ui(self) -> None:
        spec = importlib.util.spec_from_file_location("blender_toolkit_launch_test", PLUGIN_ROOT / "plugin.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        offline = module.ExtensionError("BLENDER_BRIDGE_OFFLINE", "offline", retryable=True)
        with mock.patch.object(module, "_request", side_effect=offline), mock.patch.object(
            module, "_running_blender_pids", return_value=[1234]
        ), mock.patch.object(module.subprocess, "Popen") as popen:
            result = module._launch_ui({}, {"workspace_root": str(PLUGIN_ROOT)})
        self.assertFalse(result["launched"])
        self.assertEqual(result["state"], "existing_blender_bridge_offline")
        popen.assert_not_called()

        fake_blender = PLUGIN_ROOT / "fake-blender.exe"
        fake_proc = mock.Mock()
        fake_proc.pid = 4321
        fake_proc.poll.return_value = None
        with mock.patch.object(module, "_request", side_effect=offline), mock.patch.object(
            module, "_running_blender_pids", return_value=[]
        ), mock.patch.object(module, "_find_blender", return_value=fake_blender), mock.patch.object(
            module.subprocess, "Popen", return_value=fake_proc
        ) as popen:
            result = module._launch_ui({"wait_seconds": 0}, {"workspace_root": str(PLUGIN_ROOT)})
        self.assertTrue(result["launched"])
        self.assertEqual(result["state"], "launched_bridge_pending")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], str(fake_blender))
        self.assertIn("--python-expr", argv)
        bootstrap = argv[argv.index("--python-expr") + 1]
        self.assertIn("folderbridge_blender_bridge", bootstrap)
        self.assertNotIn("shell", bootstrap.lower())

    def test_blender_addon_source_compiles_without_importing_bpy(self) -> None:
        addon = PLUGIN_ROOT / "blender_addon" / "folderbridge_blender_bridge" / "__init__.py"
        compile(addon.read_text(encoding="utf-8"), str(addon), "exec")

    def test_blender_5_compositor_migration_compatibility_is_present(self) -> None:
        addon = PLUGIN_ROOT / "blender_addon" / "folderbridge_blender_bridge" / "__init__.py"
        text = addon.read_text(encoding="utf-8")
        self.assertIn("scene.compositing_node_group", text)
        self.assertIn("bpy.data.node_groups.new(f\"{scene.name} Compositor\", \"CompositorNodeTree\")", text)
        self.assertIn("requested_type == \"CompositorNodeComposite\"", text)
        self.assertIn("return \"NodeGroupOutput\"", text)
        self.assertIn("requested_type == \"CompositorNodeMixRGB\"", text)
        self.assertIn("return \"ShaderNodeMixRGB\"", text)
        self.assertIn("interface.new_socket(name=\"Image\", in_out=\"OUTPUT\", socket_type=\"NodeSocketColor\")", text)
        self.assertIn("bpy.app.version < (5, 0, 0)", text)

    def test_blender_addon_does_not_dereference_bpy_data_at_module_import(self) -> None:
        addon = PLUGIN_ROOT / "blender_addon" / "folderbridge_blender_bridge" / "__init__.py"
        tree = ast.parse(addon.read_text(encoding="utf-8"), filename=str(addon))

        def is_bpy_data_chain(node: ast.AST) -> bool:
            current = node
            while isinstance(current, ast.Attribute):
                if (
                    current.attr == "data"
                    and isinstance(current.value, ast.Name)
                    and current.value.id == "bpy"
                ):
                    return True
                current = current.value
            return False

        offenders = []
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                if value is not None and any(is_bpy_data_chain(node) for node in ast.walk(value)):
                    offenders.append(getattr(statement, "lineno", 0))
        self.assertEqual(offenders, [], f"module-level bpy.data dereference at lines {offenders}")


if __name__ == "__main__":
    unittest.main()
