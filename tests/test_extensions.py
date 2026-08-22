from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.config import load_config
from folderbridge_mcp.extensions import ExtensionRegistry, ExtensionTrustStore, load_extension
from folderbridge_mcp.security import Workspace
from folderbridge_mcp.tools import ToolRuntime


class ExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.user_root = self.base / "extensions"
        self.bundled_root = self.base / "bundled"
        self.user_root.mkdir()
        self.bundled_root.mkdir()
        self.trust = ExtensionTrustStore(self.base / "trust.json")
        self.registry = ExtensionRegistry(
            user_root=self.user_root,
            bundled_root=self.bundled_root,
            trust_store=self.trust,
        )
        self.workspace_root = self.base / "workspace"
        self.workspace_root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_extension(
        self,
        extension_id: str = "example",
        *,
        bundled: bool = False,
        authorization: str = "global",
        adapter: dict[str, object] | None = None,
        permissions: list[str] | None = None,
        plugin_source: str | None = None,
    ) -> Path:
        root = (self.bundled_root if bundled else self.user_root) / extension_id
        root.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "id": extension_id,
            "name": f"Extension {extension_id}",
            "version": "1.0.0",
            "description": "test extension",
            "entrypoint": "plugin.py",
            "permissions": permissions or ["workspace.read", "extension.state"],
            "execution": {"mode": "isolated-process", "timeout_seconds": 30},
            "workspace_adapter": adapter or {"mode": "none", "state": "profile"},
            "actions": {
                "echo": {
                    "read_only": True,
                    "requires_workspace": True,
                    "authorization": authorization,
                    "input_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "string", "maxLength": 100}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }
            },
        }
        (root / "folderbridge-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / "plugin.py").write_text(
            plugin_source
            or "def handle(action, params, context):\n"
               "    return {'action': action, 'value': params['value'], 'workspace_root': context['workspace_root'], 'state_dir': context['state_dir']}\n",
            encoding="utf-8",
        )
        return root

    def test_unknown_or_overbroad_permission_is_rejected(self) -> None:
        path = self.make_extension(permissions=["network.*"])
        with self.assertRaisesRegex(ValueError, "unknown or overbroad"):
            load_extension(path, bundled=False)

    def test_external_extension_cannot_opt_out_of_authorization(self) -> None:
        path = self.make_extension(authorization="none")
        with self.assertRaisesRegex(ValueError, "external extensions may not declare authorization=none"):
            load_extension(path, bundled=False)

    def test_exact_hash_approval_becomes_stale_after_code_change(self) -> None:
        path = self.make_extension()
        first = self.registry.get("example")
        self.trust.approve(first, enabled=True)
        self.assertTrue(self.trust.status(first)["enabled"])

        with (path / "plugin.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# changed\n")
        second = self.registry.get("example")
        status = self.trust.status(second)

        self.assertNotEqual(first.sha256, second.sha256)
        self.assertFalse(status["trusted"])
        self.assertFalse(status["enabled"])
        self.assertTrue(status["approval_stale"])

    def test_dynamic_workspace_adapter_becomes_applicable_later(self) -> None:
        self.make_extension(
            adapter={
                "mode": "dynamic",
                "state": "profile",
                "detect": {"any_of": ["special.marker"], "all_of": []},
            },
            permissions=["workspace.read", "workspace.adapter", "extension.state"],
        )
        before = self.registry.describe(self.workspace_root)["extensions"][0]
        self.assertFalse(before["applicable"])

        (self.workspace_root / "special.marker").write_text("ready", encoding="utf-8")
        after = self.registry.describe(self.workspace_root)["extensions"][0]
        self.assertTrue(after["applicable"])

    def test_external_extension_requires_approval_and_runs_out_of_process(self) -> None:
        self.make_extension()
        denied = None
        try:
            self.registry.run(
                "example",
                "echo",
                {"value": "hello"},
                workspace=Workspace(self.workspace_root),
                read_only=True,
            )
        except Exception as exc:  # assert ToolError code without importing internals here
            denied = exc
        self.assertIsNotNone(denied)
        self.assertEqual(getattr(denied, "code", None), "EXTENSION_NOT_TRUSTED")

        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)
        result = self.registry.run(
            "example",
            "echo",
            {"value": "hello"},
            workspace=Workspace(self.workspace_root),
            read_only=True,
        )
        self.assertEqual(result["value"], "hello")
        self.assertEqual(Path(result["workspace_root"]), self.workspace_root.resolve())
        self.assertTrue(result["state_dir"])
        self.assertEqual(result["extension_id"], "example")
        self.assertEqual(result["extension_action"], "echo")

    def test_action_schema_rejects_unknown_fields_before_worker_runs(self) -> None:
        self.make_extension()
        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)
        with self.assertRaises(Exception) as caught:
            self.registry.run(
                "example",
                "echo",
                {"value": "ok", "command": "rm -rf /"},
                workspace=Workspace(self.workspace_root),
                read_only=True,
            )
        self.assertEqual(getattr(caught.exception, "code", None), "INVALID_ARGUMENT")

    def test_bundled_read_only_action_can_opt_out_of_global_authorization(self) -> None:
        path = self.make_extension("bundled-status", bundled=True, authorization="none")
        manifest_path = path / "folderbridge-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["actions"]["echo"]["requires_workspace"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = self.registry.run(
            "bundled-status",
            "echo",
            {"value": "status"},
            workspace=Workspace(self.workspace_root),
            read_only=True,
        )
        self.assertEqual(result["value"], "status")

    def test_mcp_tool_catalog_stays_fixed_when_extensions_change(self) -> None:
        runtime = ToolRuntime(self.workspace_root, load_config(self.workspace_root))
        runtime.extensions = self.registry
        before = [tool["name"] for tool in runtime.list_tools()]
        self.make_extension()
        after = [tool["name"] for tool in runtime.list_tools()]

        self.assertEqual(before, after)
        self.assertIn("extension", after)
        listed = runtime.call("extension", {"action": "list"})["structuredContent"]
        self.assertEqual(listed["extensions"][0]["id"], "example")

    def test_bundled_comfyui_manifest_is_discoverable_from_repository(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        record = load_extension(project_root / "extensions" / "comfyui", bundled=True)
        self.assertEqual(record.manifest.extension_id, "comfyui")
        self.assertIn("status", record.manifest.actions)
        self.assertIn("run", record.manifest.actions)
        self.assertIn("network.loopback:127.0.0.1:8188", record.manifest.permissions)


if __name__ == "__main__":
    unittest.main()
