from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_public_docs_match_workspace_limit_and_external_comfyui_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        security = (ROOT / "docs" / "security-model.md").read_text(encoding="utf-8")

        self.assertIn("lists beyond sixteen entries", readme)
        self.assertIn("超过 16 项", readme_zh)
        self.assertIn("最多接受 16 个", security)
        self.assertIn("ComfyUI is a reference **external hot-load Extension**", readme)
        self.assertIn("ComfyUI 现在是标准的**外源热加载 Extension**", readme_zh)
        self.assertNotIn("ComfyUI is the first bundled extension", readme)
        self.assertNotIn("ComfyUI 是第一个 bundled extension", readme_zh)

    def test_public_private_repository_boundaries_are_explicit(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        extension_readme = (ROOT / "Plugins" / "extensions" / "README.md").read_text(encoding="utf-8")
        skill_readme = (ROOT / "Plugins" / "skill-packs" / "README.md").read_text(encoding="utf-8")

        self.assertIn("/local-private/", gitignore)
        self.assertIn("/Plugins/extensions/debate-judge-adapter/", gitignore)
        self.assertIn("/Plugins/skill-packs/folderbridge-discipline/", gitignore)
        self.assertIn("local-only/private", contributing)
        self.assertIn("local-private/", contributing)
        self.assertIn("optional **public** external extensions", extension_readme)
        self.assertIn("local-private/", extension_readme)
        self.assertIn("public, optional, non-bundled Skill Pack source", skill_readme)
        self.assertIn("local-private/skill-packs/", skill_readme)

    def test_public_external_plugins_use_the_public_folderbridge_python_helpers(self) -> None:
        public_plugins = (
            "comfyui",
            "ffmpeg-toolkit",
            "ftp-toolkit",
            "godot-ai",
            "gpt-sovits-local",
        )
        legacy_process_fallback = {
            "ffmpeg-toolkit",
            "ftp-toolkit",
            "gpt-sovits-local",
        }
        for plugin_id in public_plugins:
            with self.subTest(plugin_id=plugin_id):
                plugin_root = ROOT / "Plugins" / "extensions" / plugin_id
                combined = "\n".join(
                    source_path.read_text(encoding="utf-8")
                    for source_path in plugin_root.glob("*.py")
                )
                self.assertNotIn("from folderbridge_mcp.security import", combined)
                if plugin_id in legacy_process_fallback:
                    self.assertIn("from folderbridge_mcp.extension_api import owned_process_group_kwargs, terminate_owned_process_tree", combined)
                    self.assertEqual(combined.count("from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree"), 1)
                    self.assertIn("FolderBridge 0.8.21 compatibility before the public process-helper re-export", combined)
                else:
                    self.assertNotIn("from folderbridge_mcp.process_control import", combined)

    def test_published_external_extension_table_matches_manifest_versions(self) -> None:
        published = {
            "comfyui": ("1.3.0", "test_external_comfyui.py"),
            "ffmpeg-toolkit": ("0.1.2", "test_external_ffmpeg_toolkit.py"),
            "ftp-toolkit": ("0.2.1", "test_external_ftp_toolkit.py"),
            "godot-ai": ("0.1.0", "test_external_godot_ai.py"),
            "gpt-sovits-local": ("0.1.2", "test_external_gpt_sovits.py"),
        }
        readme = (ROOT / "Plugins" / "extensions" / "README.md").read_text(encoding="utf-8")
        for plugin_id, (expected_version, test_name) in published.items():
            with self.subTest(plugin_id=plugin_id):
                manifest = json.loads(
                    (ROOT / "Plugins" / "extensions" / plugin_id / "folderbridge-extension.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["version"], expected_version)
                self.assertIn(f"| `{plugin_id}` | {expected_version} |", readme)
                self.assertTrue((ROOT / "tests" / test_name).is_file())

    def test_extension_authoring_guide_uses_only_public_python_abi(self) -> None:
        spec = (ROOT / "folderbridge_mcp" / "extension_spec.py").read_text(encoding="utf-8")
        self.assertIn("folderbridge_mcp.extension_api", spec)
        self.assertIn("不要 import 其它 FolderBridge 产品私有模块", spec)
        self.assertIn("不要直接 import `folderbridge_mcp` 的私有实现模块", spec)

    def test_git_publisher_docs_match_tracked_deletion_contract(self) -> None:
        publisher_readme = (ROOT / "extensions" / "git-publisher" / "README.md").read_text(encoding="utf-8")
        extension_docs = (ROOT / "docs" / "extensions.md").read_text(encoding="utf-8")
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        root_readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

        for text in (publisher_readme, extension_docs):
            self.assertIn("tracked deletions", text)
            self.assertIn("--no-renames", text)
        self.assertIn("Git Publisher 1.3.4", root_readme)
        self.assertIn("Git Publisher 1.3.4", root_readme_zh)


if __name__ == "__main__":
    unittest.main()
