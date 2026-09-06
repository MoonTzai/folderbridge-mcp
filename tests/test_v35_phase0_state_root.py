from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class V35CanonicalStateRootAcceptanceTests(unittest.TestCase):
    def test_top_level_untrusted_config_root_override_is_ignored(self) -> None:
        from folderbridge_mcp.user_paths import INTERNAL_CONFIG_ROOT_ENV, user_config_root

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            local = base / "local"
            malicious = base / "split-authority"
            resolved = user_config_root(
                environ={
                    INTERNAL_CONFIG_ROOT_ENV: str(malicious),
                    "LOCALAPPDATA": str(local),
                    "USERPROFILE": str(base / "home"),
                },
                platform="win32",
            )
            self.assertEqual(resolved, (local / "folderbridge-mcp").resolve(strict=False))
            self.assertNotEqual(resolved, malicious.resolve(strict=False))

    def test_boot_bound_internal_marker_accepts_exact_parent_declared_root(self) -> None:
        from folderbridge_mcp.user_paths import (
            internal_child_config_root_environment,
            user_config_root,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            expected = base / "canonical-profile"
            inherited = internal_child_config_root_environment(
                expected,
                boot_id="a" * 32,
            )
            resolved = user_config_root(
                environ={
                    **inherited,
                    "USERPROFILE": str(base / "other-home"),
                },
                platform="win32",
            )
            self.assertEqual(resolved, expected.resolve(strict=False))

    def test_marker_is_bound_to_exact_canonical_root_and_cannot_authorize_another_path(self) -> None:
        from folderbridge_mcp.user_paths import (
            INTERNAL_CONFIG_ROOT_ENV,
            internal_child_config_root_environment,
            user_config_root,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            declared = base / "canonical-profile"
            forged = base / "different-profile"
            fallback = base / "local"
            inherited = internal_child_config_root_environment(
                declared,
                boot_id="b" * 32,
            )
            inherited[INTERNAL_CONFIG_ROOT_ENV] = str(forged)
            inherited["LOCALAPPDATA"] = str(fallback)
            resolved = user_config_root(environ=inherited, platform="win32")
            self.assertEqual(resolved, (fallback / "folderbridge-mcp").resolve(strict=False))

    def test_explicit_test_root_is_a_constructor_seam_not_an_environment_override(self) -> None:
        from folderbridge_mcp.user_paths import INTERNAL_CONFIG_ROOT_ENV, user_config_root

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            explicit = base / "test-authority"
            external = base / "external-env"
            resolved = user_config_root(
                environ={INTERNAL_CONFIG_ROOT_ENV: str(external)},
                platform="win32",
                test_root=explicit,
            )
            self.assertEqual(resolved, explicit.resolve(strict=False))

    def test_extension_worker_environment_carries_verified_internal_root_marker(self) -> None:
        import folderbridge_mcp.extensions as extensions_module
        from folderbridge_mcp.extensions import load_extension
        from folderbridge_mcp.user_paths import (
            INTERNAL_CONFIG_ROOT_ENV,
            INTERNAL_CONFIG_ROOT_MARKER_ENV,
            user_config_root,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            extension = base / "fixture"
            extension.mkdir()
            (extension / "plugin.py").write_text(
                "def handle(action, params, context):\n    return {'ok': True}\n",
                encoding="utf-8",
            )
            (extension / "folderbridge-extension.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "state-root-fixture",
                        "name": "State root fixture",
                        "version": "1.0.0",
                        "permissions": [],
                        "workspace_adapter": {"mode": "none", "state": "none"},
                        "actions": {
                            "probe": {
                                "read_only": True,
                                "requires_workspace": False,
                                "authorization": "none",
                                "input_schema": {
                                    "type": "object",
                                    "properties": {},
                                    "additionalProperties": False,
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            record = load_extension(extension, bundled=True)
            local = base / "local"
            hostile = base / "hostile"
            with mock.patch.dict(
                "os.environ",
                {
                    "LOCALAPPDATA": str(local),
                    "USERPROFILE": str(base / "home"),
                    INTERNAL_CONFIG_ROOT_ENV: str(hostile),
                },
                clear=True,
            ):
                _context, env = extensions_module._worker_context_and_environment(
                    record,
                    workspace=None,
                    read_only=True,
                )

            self.assertIn(INTERNAL_CONFIG_ROOT_ENV, env)
            self.assertIn(INTERNAL_CONFIG_ROOT_MARKER_ENV, env)
            expected = (local / "folderbridge-mcp").resolve(strict=False)
            self.assertEqual(Path(env[INTERNAL_CONFIG_ROOT_ENV]), expected)
            self.assertNotEqual(Path(env[INTERNAL_CONFIG_ROOT_ENV]), hostile.resolve(strict=False))
            self.assertEqual(user_config_root(environ=env, platform="win32"), expected)


if __name__ == "__main__":
    unittest.main()
