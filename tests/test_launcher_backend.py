from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from folderbridge_mcp.launcher_backend import (
    MAX_COMMAND_OUTPUT,
    LauncherError,
    LauncherSettings,
    LauncherSettingsStore,
    TunnelSupervisor,
    build_doctor_argv,
    build_init_argv,
    build_run_argv,
    control_plane_environment,
    find_tunnel_client,
    mcp_argv,
    mcp_command,
    redact_text,
    render_client_config,
    run_short_command,
)


class LauncherBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace with spaces"
        self.root.mkdir()
        self.client = Path(self.temp.name) / ("tunnel-client.exe" if os.name == "nt" else "tunnel-client")
        self.client.write_bytes(b"not executed")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def settings(self) -> LauncherSettings:
        return LauncherSettings(
            workspaces=[str(self.root)],
            access_mode="read_only",
            profile="folderbridge_1",
            tunnel_id="tunnel_0123456789abcdef0123456789abcdef",
            tunnel_client_path=str(self.client),
        )

    def test_settings_store_never_has_an_api_key_field(self) -> None:
        path = Path(self.temp.name) / "settings" / "launcher.json"
        store = LauncherSettingsStore(path)
        settings = self.settings()
        secret = "runtime-test-never-save-this-value"
        environment = control_plane_environment(secret)

        store.save(settings)

        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn("api_key", parsed)
        self.assertEqual(environment["CONTROL_PLANE_API_KEY"], secret)
        self.assertEqual(store.load(), settings)

    def test_settings_store_rejects_unknown_or_wrong_typed_fields(self) -> None:
        path = Path(self.temp.name) / "launcher.json"
        path.write_text('{"workspace": ".", "unexpected": "value"}', encoding="utf-8")
        self.assertEqual(LauncherSettingsStore(path).load(), LauncherSettings())

        path.write_text('{"version": 2, "workspaces": [7]}', encoding="utf-8")
        self.assertEqual(LauncherSettingsStore(path).load(), LauncherSettings())

    def test_settings_store_migrates_the_single_workspace_format(self) -> None:
        path = Path(self.temp.name) / "launcher.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "workspace": str(self.root),
                    "access_mode": "read_only",
                    "profile": "folderbridge",
                    "tunnel_id": "",
                    "tunnel_client_path": "",
                    "allow_tasks": False,
                    "configured_fingerprint": "legacy",
                }
            ),
            encoding="utf-8",
        )

        migrated = LauncherSettingsStore(path).load()

        self.assertEqual(migrated.version, 3)
        self.assertEqual(migrated.capabilities, [])
        self.assertEqual(migrated.workspaces, [str(self.root)])
        self.assertEqual(migrated.configured_fingerprint, "legacy")

    def test_settings_validate_profile_and_tunnel_id_before_command_building(self) -> None:
        settings = self.settings()
        settings.profile = "name; run-something"
        with self.assertRaises(LauncherError):
            settings.validate(require_tunnel_id=True)

        settings = self.settings()
        settings.tunnel_id = "tunnel_bad value"
        with self.assertRaises(LauncherError):
            settings.validate(require_tunnel_id=True)

    def test_tunnel_commands_are_argument_arrays(self) -> None:
        settings = self.settings()
        workspaces = settings.validate(require_tunnel_id=True)
        init = build_init_argv(self.client, settings, workspaces)

        self.assertEqual(init[:5], [str(self.client), "init", "--sample", "sample_mcp_stdio_local", "--profile"])
        self.assertEqual(init[5], settings.profile)
        self.assertEqual(init[6:8], ["--tunnel-id", settings.tunnel_id])
        self.assertEqual(init[8], "--mcp-command")
        expected_workspace = str(self.root).replace("\\", "/") if os.name == "nt" else str(self.root)
        self.assertIn(expected_workspace, init[9])
        self.assertEqual(build_doctor_argv(self.client, settings.profile), [str(self.client), "doctor", "--profile", settings.profile, "--explain"])
        self.assertEqual(build_run_argv(self.client, settings.profile), [str(self.client), "run", "--profile", settings.profile])

    def test_init_replaces_the_launcher_managed_profile(self) -> None:
        settings = self.settings()
        workspaces = settings.validate(require_tunnel_id=True)

        init = build_init_argv(self.client, settings, workspaces)

        self.assertIn("--force", init)

    def test_frozen_build_uses_the_executable_as_the_stdio_server(self) -> None:
        settings = self.settings()
        workspaces = settings.validate(require_tunnel_id=True)
        with mock.patch.object(sys, "frozen", True, create=True):
            argv = mcp_argv(workspaces, "read_only", False)
        self.assertEqual(argv[0], str(Path(sys.executable).resolve()))
        self.assertEqual(argv[1:4], ["serve", "--workspace", str(workspaces[0])])
        self.assertEqual(argv[-1], "--read-only")

    def test_multi_workspace_command_keeps_each_path_as_its_own_argument(self) -> None:
        second = Path(self.temp.name) / "second workspace"
        second.mkdir()
        settings = self.settings()
        settings.workspaces.append(str(second))
        workspaces = settings.validate(require_tunnel_id=True)

        argv = mcp_argv(workspaces, "read_only", False)

        self.assertEqual(argv.count("--workspace"), 2)
        self.assertEqual(argv[argv.index("--workspace") + 1], str(self.root.resolve()))
        self.assertIn(str(second.resolve()), argv)

    def test_client_configs_preserve_command_and_argument_boundaries(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True):
            rendered_json = render_client_config((self.root,), "read_only", False, "json")
            rendered_toml = render_client_config((self.root,), "read_only", False, "toml")
            rendered_command = render_client_config((self.root,), "read_only", False, "tunnel")

        parsed = json.loads(rendered_json)
        server = parsed["mcpServers"]["folderbridge"]
        self.assertEqual(server["command"], str(Path(sys.executable).resolve()))
        self.assertEqual(server["args"][:3], ["serve", "--workspace", str(self.root)])
        self.assertEqual(server["args"][-1], "--read-only")
        self.assertIn("[mcp_servers.folderbridge]", rendered_toml)
        self.assertIn(str(self.root).replace("\\", "\\\\"), rendered_toml)
        self.assertIn("--read-only", rendered_command)

    def test_client_config_rejects_unknown_output_format(self) -> None:
        with self.assertRaises(LauncherError):
            render_client_config((self.root,), "read_only", False, "yaml")

    @unittest.skipUnless(os.name == "nt", "Windows tunnel command quoting regression")
    def test_tunnel_command_preserves_windows_paths_for_posix_style_parser(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True):
            command = mcp_command((self.root,), "read_only", False)

        executable = str(Path(sys.executable).resolve()).replace("\\", "/")
        workspace = str(self.root).replace("\\", "/")
        self.assertIn(executable, command)
        self.assertIn(workspace, command)
        self.assertNotIn("\\", command)

    def test_runtime_component_is_not_accepted_as_tunnel_client(self) -> None:
        suffix = ".exe" if os.name == "nt" else ""
        runtime = Path(self.temp.name) / f"tunnel-client-runtime-cloudflared{suffix}"
        runtime.write_bytes(b"not a tunnel client")

        self.assertEqual(find_tunnel_client(str(self.client)), self.client.resolve())
        self.assertIsNone(find_tunnel_client(str(runtime)))

    def test_fingerprint_changes_when_workspace_access_changes(self) -> None:
        settings = self.settings()
        first = settings.fingerprint()
        settings.access_mode = "read_write"
        second = settings.fingerprint()
        self.assertNotEqual(first, second)

    def test_global_capabilities_persist_and_flow_into_server_command(self) -> None:
        path = Path(self.temp.name) / "launcher-capabilities.json"
        store = LauncherSettingsStore(path)
        settings = self.settings()
        settings.capabilities = ["package-windows", "git-push"]
        store.save(settings)

        loaded = store.load()
        self.assertEqual(loaded.capabilities, settings.capabilities)
        argv = mcp_argv((self.root,), "read_only", False, loaded.capabilities)
        self.assertEqual(argv.count("--capability"), 2)
        for name in loaded.capabilities:
            self.assertIn(name, argv)

        first = settings.fingerprint()
        settings.capabilities.append("test")
        self.assertNotEqual(first, settings.fingerprint())

    def test_retired_comfyui_capability_is_migrated_without_resetting_settings(self) -> None:
        path = Path(self.temp.name) / "launcher-v3-comfyui.json"
        settings = self.settings()
        payload = {
            "version": 3,
            "workspaces": settings.workspaces,
            "access_mode": settings.access_mode,
            "profile": settings.profile,
            "tunnel_id": settings.tunnel_id,
            "tunnel_client_path": settings.tunnel_client_path,
            "allow_tasks": False,
            "capabilities": ["test", "comfyui", "git-push"],
            "configured_fingerprint": "kept",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = LauncherSettingsStore(path).load()

        self.assertEqual(loaded.workspaces, settings.workspaces)
        self.assertEqual(loaded.capabilities, ["test", "git-push"])
        self.assertEqual(loaded.configured_fingerprint, "kept")

    def test_unknown_global_capability_is_rejected(self) -> None:
        settings = self.settings()
        settings.capabilities = ["arbitrary-shell"]
        with self.assertRaises(LauncherError):
            settings.validate(require_tunnel_id=True)

    def test_tunnel_stop_terminates_owned_process_tree_before_returning(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.pid = 4321
                self.returncode = None
                self.stdout = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if self.returncode is None:
                    raise AssertionError("wait called before process tree termination")
                return self.returncode

            def kill(self):
                self.returncode = -9

        supervisor = TunnelSupervisor(lambda _text: None)
        process = FakeProcess()
        supervisor._process = process  # type: ignore[assignment]

        def terminate(fake):
            fake.returncode = -9

        with mock.patch("folderbridge_mcp.launcher_backend._terminate_process_tree", side_effect=terminate) as terminate_tree:
            code = supervisor.stop()

        terminate_tree.assert_called_once_with(process)
        self.assertEqual(code, -9)
        self.assertFalse(supervisor.running())

    def test_control_plane_environment_is_a_copy(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            env = control_plane_environment("sk-memory-only")
            self.assertEqual(env["CONTROL_PLANE_API_KEY"], "sk-memory-only")
            self.assertNotIn("CONTROL_PLANE_API_KEY", os.environ)
            with self.assertRaises(LauncherError):
                control_plane_environment("")

    def test_frozen_launcher_resets_pyinstaller_environment_for_nested_server(self) -> None:
        inherited = {
            "_PYI_ARCHIVE_FILE": r"C:\FolderBridge.exe",
            "_PYI_PARENT_PROCESS_LEVEL": "1",
        }
        with mock.patch.dict(os.environ, inherited, clear=True), mock.patch.object(
            sys, "frozen", True, create=True
        ):
            env = control_plane_environment("runtime-key")

        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(env["_PYI_ARCHIVE_FILE"], inherited["_PYI_ARCHIVE_FILE"])
        self.assertNotIn("PYINSTALLER_RESET_ENVIRONMENT", os.environ)

    def test_redaction_covers_explicit_and_bearer_secrets(self) -> None:
        secret = "runtime-secret-value"
        prefixed_key = "sk-" + "example12345"
        text = f"value={secret} Authorization: Bearer abcdef {prefixed_key}"
        redacted = redact_text(text, (secret,))
        self.assertNotIn(secret, redacted)
        self.assertNotIn("abcdef", redacted)
        self.assertNotIn(prefixed_key, redacted)

    def test_short_command_captures_output_without_a_shell(self) -> None:
        result = run_short_command(
            [sys.executable, "-c", "print('launcher-ok')"],
            env=dict(os.environ),
            timeout_seconds=10,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("launcher-ok", result.output)
        self.assertFalse(result.timed_out)

    def test_short_command_output_is_memory_bounded(self) -> None:
        result = run_short_command(
            [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {MAX_COMMAND_OUTPUT + 65536})"],
            env=dict(os.environ),
            timeout_seconds=10,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.output), MAX_COMMAND_OUTPUT + 64)


if __name__ == "__main__":
    unittest.main()
