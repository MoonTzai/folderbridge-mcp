from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("remotion_toolkit_plugin_for_test", BASE / "plugin.py")
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class RemotionToolkitStaticTests(unittest.TestCase):
    def test_manifest(self):
        data = json.loads((BASE / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["id"], "remotion-toolkit")
        self.assertEqual(data["entrypoint"], "plugin.py")
        self.assertEqual(
            set(data["permissions"]),
            {"workspace.read", "workspace.write", "extension.state", "process.execute:node.exe"},
        )
        actions = set(data["actions"])
        self.assertEqual(actions, {
            "status", "verification-plan", "project-inspect", "preflight", "compositions",
            "bundle", "render-still", "render-stills", "render-frames", "render-video",
            "media-probe", "silence-scan", "studio-guide",
        })
        for name, action in data["actions"].items():
            self.assertTrue(action["requires_workspace"], name)
            self.assertEqual(action["authorization"], "global")
            self.assertFalse("run-all" in name)
            if not action["read_only"]:
                self.assertEqual(action["run_mode"], "job")
                self.assertIn("workspace.write", data["permissions"])
        self.assertLess((BASE / "folderbridge-extension.json").stat().st_size, 256 * 1024)

    def test_links_and_privacy(self):
        for path in ("../abc", "C:/other", "/outside", "node_modules/lib", "things/../key",
                     "secret.pem", ".folderbridge.json", "nested\\file"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                plugin._clean(path)
        self.assertEqual(plugin._clean("Vibe DUG Trans Video/projects/Pro-1-English").as_posix(),
                         "Vibe DUG Trans Video/projects/Pro-1-English")

    def test_no_arbitrary_command_or_untrusted_output(self):
        with self.assertRaisesRegex(RuntimeError, "Unknown Remotion options"):
            plugin._options("render-video", {"options": {"shell": "powershell"}})
        with self.assertRaisesRegex(RuntimeError, "Unknown Remotion options"):
            plugin._options("render-video", {"options": {"url": "https://example.invalid"}})
        with self.assertRaisesRegex(RuntimeError, "Explicit valid codec"):
            plugin._options("render-video", {"options": {"composition_id": "Example"}})
        with self.assertRaisesRegex(RuntimeError, "non-overlapping"):
            plugin._options("render-video", {"options": {"codec": "h264",
                "composition_id": "Comp", "frame_ranges": [[0, 5], [4, 8]]}})
        o = plugin._options("render-video", {"options": {
            "composition_id": "Pro1", "codec": "prores", "frame_ranges": [[0, 9], [25, 39]],
            "hardware_acceleration": "disable", "input_props": {"subtitle": "Hello"},
        }})
        self.assertEqual(o["codec"], "prores")

    def test_marker_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "My-Vibe-Film"
            (project / "src").mkdir(parents=True)
            entry = project / "src" / "index.tsx"
            entry.write_text("const safe = true;\n", encoding="utf-8")
            pkg = project / "package.json"
            pkg.write_text('{"name":"test-project","private":true}\n', encoding="utf-8")
            marker = {
                "schema": 1,
                "contract": "folderbridge-remotion-toolkit-v1",
                "entry": "src/index.tsx",
                "entry_sha256": digest(entry.read_bytes()),
                "package_sha256": digest(pkg.read_bytes()),
                "remotion_version": "4.0.539",
                "allowed_operations": ["preflight", "compositions", "render-video"],
                "required_modules": [],
            }
            (project / "remotion-toolkit.project.json").write_text(json.dumps(marker), encoding="utf-8")
            context = {"workspace_root": str(root)}
            value = plugin.handle("project-inspect", {"project_path": "My-Vibe-Film"}, context)
            self.assertTrue(value["source_integrity_pass"])
            entry.write_text("const changed = true;", encoding="utf-8")
            value2 = plugin.handle("project-inspect", {"project_path": "My-Vibe-Film"}, context)
            self.assertFalse(value2["source_integrity_pass"])
            self.assertNotEqual(value["entry_sha256"], value2["entry_sha256"])

    def test_shared_remotion_engine_sha_and_version_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "My-Vibe-Film"
            provider = root / "Vibe-Shared-Engine" / "production"
            (project / "src").mkdir(parents=True)
            provider.mkdir(parents=True)
            entry = project / "src" / "index.tsx"
            entry.write_text("export const Demo = () => null;", encoding="utf-8")
            core = {"remotion": "4.0.539", "react": "19.2.0",
                    "react-dom": "19.2.0", "@remotion/cli": "4.0.539"}
            project_pkg = project / "package.json"
            project_pkg.write_text(json.dumps({"name": "my-vibe", "dependencies": core}), encoding="utf-8")
            provider_pkg = provider / "package.json"
            provider_pkg.write_text(json.dumps({"name": "vibe-engine", "dependencies": core}), encoding="utf-8")
            lock = provider / "package-lock.json"
            lock.write_text('{"lockfileVersion":3}', encoding="utf-8")
            source = project / "content.json"
            source.write_text('{"lesson":"vibe"}', encoding="utf-8")
            marker = {
                "schema": 1, "contract": "folderbridge-remotion-toolkit-v1",
                "entry": "src/index.tsx",
                "entry_sha256": digest(entry.read_bytes()),
                "package_sha256": digest(project_pkg.read_bytes()),
                "remotion_version": "4.0.539",
                "allowed_operations": ["preflight", "compositions", "render-video"],
                "required_modules": ["@remotion/cli"],
                "source_sha256_map": {"content.json": digest(source.read_bytes())},
                "shared_dependency_provider": {
                    "project_path": "Vibe-Shared-Engine/production",
                    "package_sha256": digest(provider_pkg.read_bytes()),
                    "package_lock_sha256": digest(lock.read_bytes()),
                },
            }
            marker_path = project / "remotion-toolkit.project.json"
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            context = {"workspace_root": str(root)}
            params = {"project_path": "My-Vibe-Film"}
            result = plugin.handle("project-inspect", params, context)
            self.assertTrue(result["source_integrity_pass"])
            self.assertEqual(result["dependency_provider"]["mode"], "shared_read_only_project")
            self.assertEqual(result["dependency_provider"]["project_path"], "Vibe-Shared-Engine/production")
            self.assertEqual(result["source_input_coverage"], "PINNED_INPUT_MAP")
            lock.write_text('{"lockfileVersion":3,"changed":true}', encoding="utf-8")
            changed = plugin.handle("project-inspect", params, context)
            self.assertFalse(changed["source_integrity_pass"])
            self.assertFalse(changed["dependency_provider"]["hashes_match"])
            lock.write_text('{"lockfileVersion":3}', encoding="utf-8")
            source.write_text('{"lesson":"modified"}', encoding="utf-8")
            changed_source = plugin.handle("project-inspect", params, context)
            self.assertFalse(changed_source["source_integrity_pass"])
            source.write_text('{"lesson":"vibe"}', encoding="utf-8")
            marker["shared_dependency_provider"]["project_path"] = "../Outside"
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                plugin.handle("project-inspect", params, context)

    def test_central_shared_provider_exact_path_and_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "My-Vibe-Film"
            src = proj / "src"
            src.mkdir(parents=True)
            entry = src / "index.tsx"
            entry.write_text("export default null;", encoding="utf-8")
            core = {"remotion": "4.0.539", "react": "19.2.0",
                    "react-dom": "19.2.0", "@remotion/cli": "4.0.539"}
            project_pkg = proj / "package.json"
            project_pkg.write_text(json.dumps({"dependencies": core}), encoding="utf-8")
            provider = root / "Remotion-Shared" / "remotion-4.0.539"
            provider.mkdir(parents=True)
            engine_pkg = provider / "package.json"
            engine_pkg.write_text(json.dumps({"dependencies": core}), encoding="utf-8")
            engine_lock = provider / "package-lock.json"
            engine_lock.write_text('{"lockfileVersion":3}', encoding="utf-8")
            receipt = provider / "INSTALL-RECEIPT.json"
            receipt.write_text(json.dumps(
                {"status": "INDEPENDENT_ENGINE_INSTALLED__NO_CONSUMERS_SWITCHED"}),
                encoding="utf-8")
            marker = {
                "schema": 1, "contract": "folderbridge-remotion-toolkit-v1",
                "entry": "src/index.tsx", "entry_sha256": digest(entry.read_bytes()),
                "package_sha256": digest(project_pkg.read_bytes()),
                "remotion_version": "4.0.539",
                "allowed_operations": ["preflight", "compositions"],
                "required_modules": [],
                "shared_dependency_provider": {
                    "project_path": "Remotion-Shared/remotion-4.0.539",
                    "package_sha256": digest(engine_pkg.read_bytes()),
                    "package_lock_sha256": digest(engine_lock.read_bytes()),
                },
            }
            target = proj / "remotion-toolkit.project.json"
            target.write_text(json.dumps(marker), encoding="utf-8")
            ctx = {"workspace_root": str(root)}
            params = {"project_path": "My-Vibe-Film"}
            result = plugin.handle("project-inspect", params, ctx)
            self.assertTrue(result["source_integrity_pass"])
            self.assertEqual(
                result["dependency_provider"]["project_path"],
                "Remotion-Shared/remotion-4.0.539"
            )
            marker["shared_dependency_provider"]["project_path"] = "Remotion-Shared/remotion-4.0.540"
            target.write_text(json.dumps(marker), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not found"):
                plugin.handle("project-inspect", params, ctx)
            marker["shared_dependency_provider"]["project_path"] = "Remotion-Shared/remotion-4.0.539"
            target.write_text(json.dumps(marker), encoding="utf-8")
            receipt.write_text('{"status":"NOT_APPROVED"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "successful installation receipt"):
                plugin.handle("project-inspect", params, ctx)

    def test_legacy_project_inherits_engine_and_sha_pins_shared_public_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "Vibe-Knowledge" / "production" / "standard-core-20261029"
            entry = proj / "src" / "index.tsx"
            entry.parent.mkdir(parents=True)
            entry.write_text("export default null;", encoding="utf-8")
            legacy_package = proj / "package.json"
            legacy_package.write_text('{"name":"legacy-core","private":true}', encoding="utf-8")
            media = root / "Vibe-Knowledge" / "production" / "pilot-v1.1-20261028" / "public" / "fonts"
            media.mkdir(parents=True)
            font = media / "TinyFont.otf"
            font.write_bytes(b"illustrative fixed bytes")
            eng = root / "Remotion-Shared" / "remotion-4.0.539"
            eng.mkdir(parents=True)
            core = {
                "remotion": "4.0.539", "@remotion/cli": "4.0.539",
                "react": "19.2.0", "react-dom": "19.2.0",
            }
            p = eng / "package.json"
            p.write_text(json.dumps({"dependencies": core}), encoding="utf-8")
            lock = eng / "package-lock.json"
            lock.write_text('{"lockfileVersion":3}', encoding="utf-8")
            (eng / "INSTALL-RECEIPT.json").write_text(
                json.dumps({"status": "INDEPENDENT_ENGINE_INSTALLED__NO_CONSUMERS_SWITCHED"}),
                encoding="utf-8"
            )
            marker = {
                "schema": 1, "contract": "folderbridge-remotion-toolkit-v1",
                "entry": "src/index.tsx", "entry_sha256": digest(entry.read_bytes()),
                "package_sha256": digest(legacy_package.read_bytes()),
                "remotion_version": "4.0.539", "expected_react_version": "19.2.0",
                "allow_engine_inherited_dependencies": True,
                "allowed_operations": ["preflight", "compositions"],
                "required_modules": [],
                "shared_dependency_provider": {
                    "project_path": "Remotion-Shared/remotion-4.0.539",
                    "package_sha256": digest(p.read_bytes()),
                    "package_lock_sha256": digest(lock.read_bytes()),
                },
                "shared_public_dir": "Vibe-Knowledge/production/pilot-v1.1-20261028/public",
                "shared_public_sha256_map": {"fonts/TinyFont.otf": digest(font.read_bytes())},
            }
            target = proj / "remotion-toolkit.project.json"
            target.write_text(json.dumps(marker), encoding="utf-8")
            ctx = {"workspace_root": str(root)}
            args = {"project_path": "Vibe-Knowledge/production/standard-core-20261029"}
            result = plugin.handle("project-inspect", args, ctx)
            self.assertTrue(result["source_integrity_pass"])
            self.assertEqual(result["source_input_coverage"], "PINNED_INPUT_MAP")
            self.assertEqual(len(result["source_input_checks"]), 1)
            self.assertTrue(all(item["matches"] for item in result["source_input_checks"]))
            self.assertTrue(all(item["matches"] for item in
                                result["dependency_provider"]["core_package_versions"]))
            font.write_bytes(b"changed")
            result_changed = plugin.handle("project-inspect", args, ctx)
            self.assertFalse(result_changed["source_integrity_pass"])
            font.write_bytes(b"illustrative fixed bytes")
            marker["expected_react_version"] = "18.0.0"
            target.write_text(json.dumps(marker), encoding="utf-8")
            result_bad_version = plugin.handle("project-inspect", args, ctx)
            self.assertFalse(result_bad_version["source_integrity_pass"])

    def test_symlink_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "actual.txt").write_text("a")
            try:
                (root / "link.txt").symlink_to(root / "actual.txt")
            except OSError:
                self.skipTest("Symlinks not permitted for this user")
            with self.assertRaises(RuntimeError):
                plugin._existing(root, "link.txt")

    def test_node_bridge_selftest_if_available(self):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("Node is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "package.json"
            pkg.write_text('{"name":"mock","private":true}')
            payload = {"action": "selftest", "package": str(pkg),
                       "browser": "__MISSING__", "version": "4.0.539",
                       "required_modules": [], "options": {}}
            proc = subprocess.run([node, str(BASE / "bridge.cjs")], input=json.dumps(payload),
                                  text=True, capture_output=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [line[len("FBR_RESULT_JSON="):] for line in proc.stdout.splitlines()
                     if line.startswith("FBR_RESULT_JSON=")]
            self.assertEqual(len(lines), 1, proc.stdout)
            result = json.loads(lines[0])
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["result"]["status"], "SELFTEST_PASS")


if __name__ == "__main__":
    unittest.main()
